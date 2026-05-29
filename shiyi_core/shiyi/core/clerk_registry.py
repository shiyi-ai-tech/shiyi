"""ClerkRegistry — 统一工具 + 吏员注册中心

合并原 ToolRegistry 与 ClerkRegistry：
- 吏员：注册 ClerkWorker 实例，工具 schema 自动合并
- 直注册：register(name, handler) 用于吏员系统外的简单工具
- 执行：自动路由到正确的吏员或直注册处理器
- 工具同名：多吏员可共享同名工具，_tool_clerk 存列表

接口对上层透明 — engine.chat() 调用 get_schemas() + execute() 即可。
"""

import json
import logging
from typing import Dict, Any, List, Callable, Optional

logger = logging.getLogger(__name__)


class ClerkRegistry:
    """统一工具 + 吏员注册中心"""

    def __init__(self):
        # clerk_id → (worker_instance, metadata)
        self._clerks: Dict[str, Dict[str, Any]] = {}
        # tool_name → [clerk_id, ...]（工具→吏员列表，支持多吏员同名工具）
        self._tool_clerk: Dict[str, List[str]] = {}
        # 直注册工具（不归属于任何吏员）
        self._direct_tools: Dict[str, Dict[str, Any]] = {}

    # ═══════════════════════════════════════════
    # 吏员注册
    # ═══════════════════════════════════════════

    def register_clerk(self, worker) -> str:
        """注册一个吏员

        从 ClerkWorker 实例提取 clerk_id、工具列表，
        将其所有工具注册到统一 schema 中。

        Args:
            worker: ClerkWorker 实例（需提供 get_tools() / execute() / status()）

        Returns:
            clerk_id
        """
        clerk_id = worker.config.clerk_id

        tools = worker.get_tools()
        if not tools:
            logger.warning("Clerk %s has no tools", clerk_id)

        self._clerks[clerk_id] = {
            "worker": worker,
            "tools": tools,
        }

        # 注册每个工具到映射表（支持多吏员同名工具）
        for tool in tools:
            tname = tool["name"]
            if tname not in self._tool_clerk:
                self._tool_clerk[tname] = []
            if clerk_id not in self._tool_clerk[tname]:
                self._tool_clerk[tname].append(clerk_id)
            else:
                logger.debug("Tool %s: clerk %s already in mapping", tname, clerk_id)

        logger.info("Registered clerk: %s (%d tools)", clerk_id, len(tools))
        return clerk_id

    def unregister_clerk(self, clerk_id: str) -> bool:
        """移除吏员及其工具"""
        if clerk_id not in self._clerks:
            return False
        clerk = self._clerks.pop(clerk_id)
        for tool in clerk["tools"]:
            tname = tool["name"]
            if tname in self._tool_clerk:
                try:
                    self._tool_clerk[tname].remove(clerk_id)
                    if not self._tool_clerk[tname]:
                        del self._tool_clerk[tname]
                except ValueError:
                    pass
        return True

    # ═══════════════════════════════════════════
    # 直注册（向后兼容原 ToolRegistry）
    # ═══════════════════════════════════════════

    def register(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> None:
        """直注册工具（不归属于吏员）"""
        self._direct_tools[name] = {
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
            "handler": handler,
        }

    # ═══════════════════════════════════════════
    # 工具 schema（供 LLM Function Calling）
    # ═══════════════════════════════════════════

    def get_schemas(self) -> List[Dict[str, Any]]:
        """获取全部工具的 Function Calling schema

        合并所有吏员的工具 + 直注册工具，按 tool name 去重（多吏员同名工具只取一个）。
        """
        seen: set = set()
        schemas = []

        # 吏员工具
        for clerk_data in self._clerks.values():
            for tool in clerk_data["tools"]:
                tname = tool["name"]
                if tname in seen:
                    continue
                seen.add(tname)
                schemas.append({
                    "type": "function",
                    "function": {
                        "name": tname,
                        "description": tool.get("description", ""),
                        "parameters": tool.get("inputSchema", {}),
                    },
                })

        # 直注册工具
        for t in self._direct_tools.values():
            schemas.append(t["schema"])

        return schemas

    # ═══════════════════════════════════════════
    # 执行
    # ═══════════════════════════════════════════

    def execute(
        self,
        name: str,
        arguments: Dict[str, Any],
        clerk_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行工具调用

        自动路由：
        1. 吏员工具 → 委托给对应 ClerkWorker.execute()
        2. 直注册工具 → 调用 handler
        
        特殊处理：
        - recall_memory / search_conversations → 由主进程执行（_proxy 标记）

        Returns:
            {"success": bool, "result": str, "error": str}
        """
        # 吏员路由
        clerk_ids = self._tool_clerk.get(name)
        if clerk_ids is not None:
            # 多吏员同名工具：优先匹配传入 clerk_id，否则取第一个
            target_id = clerk_id if clerk_id in clerk_ids else clerk_ids[0]
            if clerk_id and clerk_id not in clerk_ids:
                logger.debug(
                    "Tool %s: clerk %s not in owners %s, using %s",
                    name, clerk_id, clerk_ids, target_id,
                )
            clerk = self._clerks.get(target_id)
            if clerk is None:
                return {"success": False, "result": "", "error": f"Clerk {target_id} not found"}
            try:
                result = clerk["worker"].execute(name, arguments)
                
                # 检查 _proxy 标记（需要主进程执行）
                if isinstance(result, dict) and result.get("_proxy"):
                    tool_name = result.get("tool", "")
                    params = result.get("params", {})
                    return self._execute_proxy_tool(tool_name, params)
                
                return result
            except Exception as e:
                return {"success": False, "result": "", "error": str(e)}

        # 直注册工具
        tool = self._direct_tools.get(name)
        if tool is None:
            available = list(self._direct_tools.keys()) + list(self._tool_clerk.keys())
            return {"success": False, "result": "", "error": f"Tool not found: {name}. Available: {available}"}

        try:
            return tool["handler"](arguments)
        except Exception as e:
            return {"success": False, "result": "", "error": str(e)}
    
    def _execute_proxy_tool(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行需要主进程处理的工具
        
        Args:
            tool_name: 工具名
            params: 工具参数
            
        Returns:
            执行结果
        """
        if tool_name == "recall_memory":
            return self._execute_recall_memory(params)
        elif tool_name == "search_conversations":
            return self._execute_search_conversations(params)
        else:
            return {"success": False, "result": "", "error": f"Unknown proxy tool: {tool_name}"}
    
    def _execute_recall_memory(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行 recall_memory 工具
        
        调用 MemoryEngine.recall() 查询记忆库
        
        Args:
            params: {"query": str, "top_k": int, "deep": bool}
            
        Returns:
            查询结果
        """
        memory_engine = getattr(self, "_memory_engine", None)
        if memory_engine is None:
            return {"success": False, "result": "", "error": "MemoryEngine not available"}
        
        query = params.get("query", "")
        top_k = int(params.get("top_k", 5))
        deep = bool(params.get("deep", False))
        
        if not query:
            return {"success": False, "result": "", "error": "Empty query"}
        
        try:
            results = memory_engine.recall(query, deep=deep)
            # 截断到 top_k
            results = results[:top_k]
            
            # 格式化结果
            formatted = []
            for r in results:
                formatted.append({
                    "fact_kernel": r.get("fact_kernel", ""),
                    "score": r.get("score", 0.0),
                    "emotion": r.get("emotion_shell", {}).get("primary", ""),
                    "fragment_id": r.get("fragment_id", ""),
                })
            
            return {
                "success": True,
                "result": json.dumps(formatted, ensure_ascii=False),
                "count": len(formatted),
                "error": "",
            }
        except Exception as e:
            logger.error(f"recall_memory failed: {e}")
            return {"success": False, "result": "", "error": str(e)}
    
    def _execute_search_conversations(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行 search_conversations 工具
        
        调用 ConversationManager 搜索对话历史
        
        Args:
            params: {"query": str, "limit": int, "session_id": str}
            
        Returns:
            搜索结果
        """
        conversation_manager = getattr(self, "_conversation_manager", None)
        if conversation_manager is None:
            return {"success": False, "result": "", "error": "ConversationManager not available"}
        
        query = params.get("query", "")
        limit = int(params.get("limit", 10))
        session_id = params.get("session_id", None)
        
        if not query:
            return {"success": False, "result": "", "error": "Empty query"}
        
        try:
            # 获取对话历史并搜索
            # 搜索所有会话或指定会话
            if session_id:
                sessions = [session_id]
            else:
                # 获取所有会话
                from shiyi.perception.conversation import ConversationManager
                sessions = conversation_manager.list_conversations() or []
                if isinstance(sessions, list) and sessions:
                    if isinstance(sessions[0], dict):
                        sessions = [s.get("conversation_id", "") for s in sessions]
            
            results = []
            for conv_id in sessions:
                try:
                    history = conversation_manager.get_history(conv_id, max_turns=50)
                    if history:
                        for msg in history:
                            content = msg.get("content", "") or ""
                            if query.lower() in content.lower():
                                results.append({
                                    "conversation_id": conv_id,
                                    "role": msg.get("role", ""),
                                    "content": content[:200],  # 截断
                                    "timestamp": msg.get("timestamp", ""),
                                })
                                if len(results) >= limit:
                                    break
                except Exception:
                    continue
                
                if len(results) >= limit:
                    break
            
            return {
                "success": True,
                "result": json.dumps(results, ensure_ascii=False),
                "count": len(results),
                "error": "",
            }
        except Exception as e:
            logger.error(f"search_conversations failed: {e}")
            return {"success": False, "result": "", "error": str(e)}

    # ═══════════════════════════════════════════
    # 查询
    # ═══════════════════════════════════════════

    def set_task_tracker(self, tracker) -> None:
        """注入任务追踪器（供异步执行使用）"""
        self._task_tracker = tracker

    def execute_async(self, name: str, arguments: Dict[str, Any]) -> str:
        """异步执行工具调用

        在后台线程执行，立即返回 task_id。
        结果通过 TaskTracker 查询。

        Args:
            name: 工具名
            arguments: 工具参数

        Returns:
            task_id (str)
        """
        import threading

        clerk_ids = self._tool_clerk.get(name, [])
        target_clerk = clerk_ids[0] if clerk_ids else "unknown"
        tracker = getattr(self, "_task_tracker", None)

        if tracker is None:
            raise RuntimeError("TaskTracker not set on ClerkRegistry")

        task_id = tracker.start(clerk_id=target_clerk, tool_name=name, params=arguments)

        def _run():
            try:
                result = self.execute(name, arguments, clerk_id=target_clerk)
                tracker.complete(task_id, result)
            except Exception as e:
                tracker.fail(task_id, str(e))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        return task_id

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """查询异步任务状态"""
        tracker = getattr(self, "_task_tracker", None)
        if tracker is None:
            return None
        return tracker.get(task_id)

    def has_running_tasks(self, clerk_id: Optional[str] = None) -> bool:
        """检查是否有运行中的异步任务"""
        tracker = getattr(self, "_task_tracker", None)
        if tracker is None:
            return False
        for t in tracker.list_recent(limit=50):
            if t["status"] == "running":
                if clerk_id is None or t["clerk_id"] == clerk_id:
                    return True
        return False

    def list_clerks(self) -> List[Dict[str, Any]]:
        """列出所有已注册吏员"""
        return [
            {
                "clerk_id": cid,
                "name": cdata["worker"].config.name,
                "version": cdata["worker"].config.version,
                "description": getattr(cdata["worker"].config, "description", ""),
                "enabled": cdata["worker"].config.enabled,
                "tool_count": len(cdata["tools"]),
                "tools": [t["name"] for t in cdata["tools"]],
            }
            for cid, cdata in self._clerks.items()
        ]

    def get_clerk(self, clerk_id: str):
        """获取吏员 worker 实例"""
        clerk = self._clerks.get(clerk_id)
        return clerk["worker"] if clerk else None

    def list_tools(self) -> List[str]:
        """列出所有可用工具名"""
        return list(self._tool_clerk.keys()) + list(self._direct_tools.keys())

    def tool_owner(self, tool_name: str) -> Optional[str]:
        """查询工具归属吏员（多吏员时返回第一个注册的）"""
        clerk_ids = self._tool_clerk.get(tool_name)
        return clerk_ids[0] if clerk_ids else None

    def tool_owners(self, tool_name: str) -> List[str]:
        """查询工具归属的所有吏员列表"""
        return self._tool_clerk.get(tool_name, [])

    @property
    def clerk_count(self) -> int:
        return len(self._clerks)

    @property
    def tool_count(self) -> int:
        return len(self._tool_clerk) + len(self._direct_tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tool_clerk or name in self._direct_tools

    def __len__(self) -> int:
        return self.tool_count
