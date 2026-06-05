"""MCP server for clerk-default — v1.0

通过 stdin/stdout JSON-RPC 2.0 暴露吏员工具、记忆、任务管理。
通过 fd 3 发送 SSE 事件（心跳、进度、完成通知）。

MCP 方法:
- initialize: 握手，声明吏员能力和身份
- tools/list, tools/call: 工具发现和执行
- resources/list, resources/read: 资源和知识库
- prompts/list, prompts/get: 提示模板
- agent/run: 吏员自主执行循环
- memory/recall: 读取史佚主记忆
- memory/remember: 写入记忆碎片
- memory/context: 获取当前对话上下文
- task/execute, task/cancel, task/status: 任务生命周期
- clerk/health, clerk/shutdown: 吏员健康管理和优雅退出

SSE 事件:
- notifications/heartbeat: 30s 心跳
- notifications/progress: 任务进度
- notifications/complete: 任务完成 + 结果 + 新记忆
- notifications/error: 任务异常
"""

import sys
import os
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, List

try:
    from .worker import ClerkWorker, ClerkConfig
except ImportError:
    from worker import ClerkWorker, ClerkConfig

logger = logging.getLogger("clerk_mcp")

# SSE 输出文件描述符（stdio 模式用 fd 3/CLERK_SSE_FD，HTTP 模式走 /events 端点）
_SSE_FD: Optional[int] = None
_SSE_FILE: Any = None  # 保持引用防止 GC 关闭 fd


def _init_sse():
    """初始化 SSE 输出通道 — 使用 CLERK_SSE_FD 环境变量或 fd 3"""
    global _SSE_FD, _SSE_FILE
    sse_fd_str = os.environ.get("CLERK_SSE_FD", "3")
    try:
        sse_fd = int(sse_fd_str)
        _SSE_FILE = os.fdopen(sse_fd, "w", buffering=1)
        _SSE_FD = _SSE_FILE.fileno()
        return True
    except (OSError, ValueError):
        return False


def _sse_event(event: str, data: Dict[str, Any]) -> None:
    """发送 SSE 事件"""
    global _SSE_FD
    if _SSE_FD is None:
        return
    try:
        payload = json.dumps(data, ensure_ascii=False)
        msg = f"event: {event}\ndata: {payload}\n\n"
        os.write(_SSE_FD, msg.encode("utf-8"))
    except Exception:
        pass


class ClerkMCP:
    """MCP server wrapper for ClerkWorker — v1.0"""

    def __init__(self, config_path: str = None):
        self.worker = ClerkWorker(config_path)
        self._server_info = {
            "name": f"clerk-{self.worker.config.clerk_id}",
            "version": self.worker.config.version,
        }
        self._session_id: Optional[str] = None
        self._current_task_id: Optional[str] = None
        self._task_results: Dict[str, Dict[str, Any]] = {}  # task_id → {status, result, error}
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._shutting_down = False

    # ── 主路由器 ────────────────────────────────────────────

    def handle_request(self, request: dict) -> Optional[dict]:
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        try:
            handler = self._METHODS.get(method)
            if handler is None:
                result = None
                error = {"code": -32601, "message": f"Unknown method: {method}"}
                return {"jsonrpc": "2.0", "id": req_id, "error": error}
            result = handler(self, params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        except Exception as e:
            logger.error(f"Error handling {method}: {e}")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            }

    # ── 方法注册表 ───────────────────────────────────────────

    # ── initialize ──
    def _handle_initialize(self, params: dict) -> dict:
        clerk_id = params.get("clerk_id", self.worker.config.clerk_id)
        self._session_id = params.get("session_id", f"sess_{uuid.uuid4().hex[:12]}")

        # 启动心跳
        if self._heartbeat_thread is None:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True
            )
            self._heartbeat_thread.start()

        _sse_event("notifications/initialized", {
            "clerk_id": clerk_id,
            "session_id": self._session_id,
            "status": "ready",
        })

        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": self._server_info,
            "session_id": self._session_id,
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {},
                "memory": {"read": True, "write": True},
                "tasks": {"execute": True, "cancel": True, "status": True},
            },
        }

    # ── tools ──
    def _handle_tools_list(self, params: dict) -> dict:
        tools = self.worker.get_tools()
        return {"tools": tools}

    def _handle_tools_call(self, params: dict) -> dict:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = self.worker.execute(tool_name, arguments)
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": not result.get("success", False),
        }

    # ── resources ──
    def _handle_resources_list(self, params: dict) -> dict:
        resources = []
        skills_dir = Path(__file__).parent / "skills"
        if skills_dir.is_dir():
            for sf in sorted(skills_dir.glob("*.md")):
                resources.append({
                    "uri": f"skill://{sf.stem}",
                    "name": sf.stem,
                    "description": f"Skill: {sf.stem}",
                    "mimeType": "text/markdown",
                })
        knowledge_dir = Path(__file__).parent / "knowledge"
        if knowledge_dir.is_dir():
            for kf in sorted(knowledge_dir.glob("*.md")):
                resources.append({
                    "uri": f"knowledge://{kf.stem}",
                    "name": kf.stem,
                    "description": f"Knowledge: {kf.stem}",
                    "mimeType": "text/markdown",
                })
        return {"resources": resources}

    def _handle_resources_read(self, params: dict) -> dict:
        uri = params.get("uri", "")
        if uri.startswith("skill://"):
            name = uri.replace("skill://", "")
            content = self.worker._load_skill(name)
            if content:
                return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": content}]}
        elif uri.startswith("knowledge://"):
            name = uri.replace("knowledge://", "")
            kf = Path(__file__).parent / "knowledge" / f"{name}.md"
            if kf.exists():
                with open(kf, "r", encoding="utf-8") as f:
                    return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": f.read()}]}
        return {"contents": [], "error": f"Resource not found: {uri}"}

    # ── prompts ──
    _PROMPT_TEMPLATES = {
        "execute_task": "请完成以下任务：{task}",
        "search_and_summarize": "请搜索并总结：{query}",
        "file_operation": "请对文件执行 {operation}，路径：{path}",
    }

    def _handle_prompts_list(self, params: dict) -> dict:
        prompts = [
            {"name": "execute_task", "description": "让吏员自主执行任务", "arguments": [{"name": "task", "description": "任务描述", "required": True}]},
            {"name": "search_and_summarize", "description": "搜索并总结信息", "arguments": [{"name": "query", "description": "搜索查询", "required": True}]},
            {"name": "file_operation", "description": "文件操作", "arguments": [{"name": "operation", "description": "操作类型", "required": True}, {"name": "path", "description": "文件路径", "required": True}]},
        ]
        return {"prompts": prompts}

    def _handle_prompts_get(self, params: dict) -> dict:
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        if name not in self._PROMPT_TEMPLATES:
            return {"error": f"Prompt not found: {name}"}
        text = self._PROMPT_TEMPLATES[name].format(**arguments)
        return {"description": f"Prompt: {name}", "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}

    # ── agent ──
    def _handle_agent_run(self, params: dict) -> dict:
        task = params.get("task", "")
        if not task.strip():
            return {"error": "task is required"}

        task_id = params.get("task_id", f"task_{uuid.uuid4().hex[:10]}")
        self._current_task_id = task_id
        skills = params.get("skills")
        max_iterations = params.get("max_iterations")
        timeout_val = params.get("timeout")

        def _run():
            try:
                _sse_event("notifications/progress", {
                    "task_id": task_id,
                    "percent": 0,
                    "message": f"{self.worker.config.name} 开始执行...",
                })
                result = self.worker.run_agent_loop(
                    task=task,
                    skills=skills,
                    max_iterations=max_iterations,
                    timeout=timeout_val,
                )
                if result.get("success"):
                    _sse_event("notifications/complete", {
                        "task_id": task_id,
                        "result": result.get("result", ""),
                        "iterations": result.get("iterations", 0),
                    })
                    self._task_results[task_id] = {"status": "done", "result": result.get("result", "")}
                else:
                    _sse_event("notifications/error", {
                        "task_id": task_id,
                        "code": "TASK_FAILED",
                        "message": result.get("error", "Unknown error"),
                    })
                    self._task_results[task_id] = {"status": "failed", "error": result.get("error", "执行失败")}
            except Exception as e:
                _sse_event("notifications/error", {
                    "task_id": task_id,
                    "code": "CRASH",
                    "message": str(e),
                })
                self._task_results[task_id] = {"status": "failed", "error": str(e)}
            finally:
                if self._current_task_id == task_id:
                    self._current_task_id = None

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return {"task_id": task_id, "status": "started"}

    # ── memory (v1.0 新增) ──
    def _handle_memory_recall(self, params: dict) -> dict:
        """读取史佚主记忆 — 通过 _proxy 标记由管家在主进程执行"""
        return {
            "_proxy": True,
            "tool": "memory/recall",
            "params": params,
        }

    def _handle_memory_remember(self, params: dict) -> dict:
        """写入记忆碎片 — 通过 _proxy 标记由管家在主进程写入"""
        return {
            "_proxy": True,
            "tool": "memory/remember",
            "params": params,
        }

    def _handle_memory_context(self, params: dict) -> dict:
        """获取对话上下文 — 通过 _proxy 标记由管家在主进程提供"""
        return {
            "_proxy": True,
            "tool": "memory/context",
            "params": params,
        }

    # ── tasks (v1.0 新增) ──
    def _handle_task_execute(self, params: dict) -> dict:
        task_id = params.get("task_id", f"task_{uuid.uuid4().hex[:12]}")
        description = params.get("description", "")
        skill = params.get("skill")
        input_data = params.get("input", {})
        timeout_val = params.get("timeout", 300)

        self._current_task_id = task_id

        # 如果有 context 中的记忆引用，先加载
        context = params.get("context", {})
        relevant_memories = context.get("relevant_memories", [])

        # 在后台线程执行任务
        def _run():
            try:
                _sse_event("notifications/progress", {
                    "task_id": task_id,
                    "percent": 0,
                    "message": f"任务已接收: {description[:100]}",
                })

                result = self.worker.run_agent_loop(
                    task=f"{description}\n\n输入数据：{json.dumps(input_data, ensure_ascii=False)[:2000]}",
                    skills=[skill] if skill else None,
                    timeout=timeout_val,
                )

                if result.get("success"):
                    new_fragments = []
                    if result.get("result"):
                        new_fragments.append({
                            "content": result["result"][:500],
                            "importance": 0.7,
                            "source": self.worker.config.clerk_id,
                        })

                    _sse_event("notifications/complete", {
                        "task_id": task_id,
                        "result": result.get("result", ""),
                        "iterations": result.get("iterations", 0),
                        "new_fragments": new_fragments,
                    })
                    self._task_results[task_id] = {"status": "done", "result": result.get("result", "")}
                else:
                    _sse_event("notifications/error", {
                        "task_id": task_id,
                        "code": "EXECUTION_FAILED",
                        "message": result.get("error", "Unknown error"),
                    })
                    self._task_results[task_id] = {"status": "failed", "error": result.get("error", "执行失败")}
            except Exception as e:
                _sse_event("notifications/error", {
                    "task_id": task_id,
                    "code": "CRASH",
                    "message": str(e),
                })
                self._task_results[task_id] = {"status": "failed", "error": str(e)}
            finally:
                if self._current_task_id == task_id:
                    self._current_task_id = None

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        return {"accepted": True, "task_id": task_id, "estimated_duration": timeout_val // 6}

    def _handle_task_cancel(self, params: dict) -> dict:
        task_id = params.get("task_id", "")
        if task_id == self._current_task_id:
            self._current_task_id = None
            self._task_results[task_id] = {"status": "cancelled", "error": "已取消"}
            return {"cancelled": True, "task_id": task_id}
        return {"cancelled": False, "error": "Task not found or not current task"}

    def _handle_task_status(self, params: dict) -> dict:
        task_id = params.get("task_id", "")
        # 先查已完成的
        if task_id in self._task_results:
            return {"task_id": task_id, **self._task_results[task_id]}
        # 正在运行的
        if task_id == self._current_task_id:
            return {
                "task_id": task_id,
                "status": "running",
                "clerk_id": self.worker.config.clerk_id,
            }
        return {"task_id": task_id, "status": "unknown"}

    # ── clerk health (v1.0 新增) ──
    def _handle_clerk_health(self, params: dict) -> dict:
        return {
            "status": "healthy" if not self._shutting_down else "shutting_down",
            "clerk_id": self.worker.config.clerk_id,
            "current_task": self._current_task_id,
            "session_id": self._session_id,
        }

    def _handle_clerk_shutdown(self, params: dict) -> dict:
        self._shutting_down = True
        reason = params.get("reason", "user_request")
        _sse_event("notifications/error", {
            "task_id": self._current_task_id,
            "code": "CLERK_SHUTDOWN",
            "message": f"吏员正在关闭: {reason}",
        })
        return {"shutting_down": True, "reason": reason}

    # ── 方法表 ──
    _METHODS = {
        "initialize": _handle_initialize,
        "tools/list": _handle_tools_list,
        "tools/call": _handle_tools_call,
        "resources/list": _handle_resources_list,
        "resources/read": _handle_resources_read,
        "prompts/list": _handle_prompts_list,
        "prompts/get": _handle_prompts_get,
        "agent/run": _handle_agent_run,
        "memory/recall": _handle_memory_recall,
        "memory/remember": _handle_memory_remember,
        "memory/context": _handle_memory_context,
        "task/execute": _handle_task_execute,
        "task/cancel": _handle_task_cancel,
        "task/status": _handle_task_status,
        "clerk/health": _handle_clerk_health,
        "clerk/shutdown": _handle_clerk_shutdown,
        "ping": lambda self, p: {"pong": True},
    }

    # ── 心跳循环 ──
    def _heartbeat_loop(self):
        while not self._shutting_down:
            time.sleep(30)
            if not self._shutting_down:
                _sse_event("notifications/heartbeat", {
                    "clerk_id": self.worker.config.clerk_id,
                    "status": "running",
                    "current_task": self._current_task_id,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })

    # ── 主循环 ──
    def run_stdio(self) -> None:
        logger.info("Clerk MCP server starting (stdio mode, JSON-RPC 2.0 + SSE)")

        for line in sys.stdin:
            if self._shutting_down:
                break

            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON: {line[:100]}")
                continue

            response = self.handle_request(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()


# ── 入口 ──
def main():
    config_path = None
    if len(sys.argv) > 1:
        if sys.argv[1] in ("--config", "-c") and len(sys.argv) > 2:
            config_path = sys.argv[2]
        elif not sys.argv[1].startswith("-"):
            config_path = sys.argv[1]
    if config_path is None:
        config_path = str(Path(__file__).parent / "clerk.json")

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    _init_sse()

    server = ClerkMCP(config_path)
    server.run_stdio()


if __name__ == "__main__":
    main()
