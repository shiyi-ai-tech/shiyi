"""ClerkRegistry — 统一工具 + 吏员注册中心

合并原 ToolRegistry 与 ClerkRegistry：
- 吏员：注册 ClerkWorker 实例，工具 schema 自动合并
- 直注册：register(name, handler) 用于吏员系统外的简单工具
- 执行：自动路由到正确的吏员或直注册处理器

接口对上层透明 — engine.chat() 调用 get_schemas() + execute() 即可。
"""

import logging
from typing import Dict, Any, List, Callable, Optional

logger = logging.getLogger(__name__)


class ClerkRegistry:
    """统一工具 + 吏员注册中心"""

    def __init__(self):
        # clerk_id → (worker_instance, metadata)
        self._clerks: Dict[str, Dict[str, Any]] = {}
        # tool_name → clerk_id（工具→吏员映射）
        self._tool_clerk: Dict[str, str] = {}
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

        # 注册每个工具到映射表
        for tool in tools:
            tname = tool["name"]
            if tname in self._tool_clerk and self._tool_clerk[tname] != clerk_id:
                logger.warning(
                    "Tool %s already registered by clerk %s, overwritten by %s",
                    tname, self._tool_clerk[tname], clerk_id,
                )
            self._tool_clerk[tname] = clerk_id

        logger.info("Registered clerk: %s (%d tools)", clerk_id, len(tools))
        return clerk_id

    def unregister_clerk(self, clerk_id: str) -> bool:
        """移除吏员及其工具"""
        if clerk_id not in self._clerks:
            return False
        clerk = self._clerks.pop(clerk_id)
        for tool in clerk["tools"]:
            self._tool_clerk.pop(tool["name"], None)
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

        合并所有吏员的工具 + 直注册工具。
        """
        schemas = []

        # 吏员工具
        for clerk_data in self._clerks.values():
            for tool in clerk_data["tools"]:
                schemas.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
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

    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具调用

        自动路由：
        1. 吏员工具 → 委托给对应 ClerkWorker.execute()
        2. 直注册工具 → 调用 handler

        Returns:
            {"success": bool, "result": str, "error": str}
        """
        # 吏员路由
        clerk_id = self._tool_clerk.get(name)
        if clerk_id is not None:
            clerk = self._clerks.get(clerk_id)
            if clerk is None:
                return {"success": False, "result": "", "error": f"Clerk {clerk_id} not found"}
            try:
                return clerk["worker"].execute(name, arguments)
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

        clerk_id = self._tool_clerk.get(name, "unknown")
        tracker = getattr(self, "_task_tracker", None)

        if tracker is None:
            raise RuntimeError("TaskTracker not set on ClerkRegistry")

        task_id = tracker.start(clerk_id=clerk_id, tool_name=name, params=arguments)

        def _run():
            try:
                result = self.execute(name, arguments)
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
        """查询工具归属吏员（无则为 None）"""
        return self._tool_clerk.get(tool_name)

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
