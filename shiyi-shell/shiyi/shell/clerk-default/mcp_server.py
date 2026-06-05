"""MCP server for clerk-default — v2.0 (mcp-python SDK)

FastMCP-based server replacing hand-rolled JSON-RPC 2.0.
Uses mcp-python SDK for communication — no manual stdin/stdout parsing.
All custom methods (memory/*, task/*, clerk/*) registered as @mcp.tool().
Notifications via MCP session.send_notification().

MCP tools auto-discovered by any MCP client:
  agent_run, task_execute, task_cancel, task_status,
  memory_recall, memory_remember, memory_context,
  clerk_health, clerk_shutdown
"""

import json
import logging
import sys
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any

# ── Ensure shiyi packages are importable ──
# When started as subprocess via stdio_client, the venv may not
# have shiyi packages installed. Add the repo root to sys.path.
_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _THIS_FILE.parent.parent.parent.parent.parent  # clerk-default → shell → shiyi → shiyi-shell → root
for _pkg in ("shiyi-common", "shiyi-providers", "shiyi-core", "shiyi-shell"):
    _pkg_path = str(_PROJECT_ROOT / _pkg)
    if _pkg_path not in sys.path and Path(_pkg_path).is_dir():
        sys.path.insert(0, _pkg_path)

from mcp.server.fastmcp import FastMCP

try:
    from .worker import ClerkWorker
except ImportError:
    import sys
    from pathlib import Path
    # 将 clerk-default 目录加入 sys.path，使 tools/ 可直接导入
    _clerk_dir = str(Path(__file__).parent)
    if _clerk_dir not in sys.path:
        sys.path.insert(0, _clerk_dir)
    from worker import ClerkWorker

logger = logging.getLogger("clerk_mcp")


class ClerkMCPServer:
    """FastMCP-based clerk server — wraps ClerkWorker as MCP tools."""

    def __init__(self, config_path: Optional[str] = None):
        self.worker = ClerkWorker(config_path)
        self.mcp = FastMCP(
            name=f"clerk-{self.worker.config.clerk_id}",
        )
        self._session_id: str = ""
        self._current_task_id: str = ""
        self._task_results: Dict[str, Dict[str, Any]] = {}
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._shutting_down = False

        self._register_tools()

    def _register_tools(self):
        """Register all tools, resources, and prompts on FastMCP."""
        mcp = self.mcp
        worker = self.worker
        tasks = self._task_results
        self_ref = self  # captured by closures

        # ── 吏员自有工具（从 worker.get_tools() 动态注册） ──
        for tool_def in worker.get_tools():
            _register_worker_tool(mcp, worker, tool_def)

        # ── agent_run ──
        @mcp.tool()
        async def agent_run(
            task: str,
            skills: Optional[List[str]] = None,
            max_iterations: Optional[int] = None,
            timeout: Optional[int] = None,
            ctx=None,
        ) -> str:
            """吏员自主执行循环 — 后台线程执行，通过通知回报进度"""
            task_id = f"task_{uuid.uuid4().hex[:10]}"
            self_ref._current_task_id = task_id

            def _run():
                try:
                    _notify(ctx, "notifications/progress", {
                        "task_id": task_id, "percent": 0,
                        "message": f"{worker.config.name} 开始执行...",
                    })
                    result = worker.run_agent_loop(
                        task=task, skills=skills or None,
                        max_iterations=max_iterations, timeout=timeout,
                    )
                    if result.get("success"):
                        _notify(ctx, "notifications/complete", {
                            "task_id": task_id,
                            "result": result.get("result", ""),
                            "iterations": result.get("iterations", 0),
                        })
                        tasks[task_id] = {"status": "done", "result": result.get("result", "")}
                    else:
                        _notify(ctx, "notifications/error", {
                            "task_id": task_id, "code": "TASK_FAILED",
                            "message": result.get("error", "Unknown error"),
                        })
                        tasks[task_id] = {"status": "failed", "error": result.get("error", "执行失败")}
                except Exception as e:
                    _notify(ctx, "notifications/error", {
                        "task_id": task_id, "code": "CRASH", "message": str(e),
                    })
                    tasks[task_id] = {"status": "failed", "error": str(e)}
                finally:
                    if self_ref._current_task_id == task_id:
                        self_ref._current_task_id = ""

            threading.Thread(target=_run, daemon=True).start()
            return json.dumps({"task_id": task_id, "status": "started"}, ensure_ascii=False)

        # ── memory_recall ──
        @mcp.tool()
        async def memory_recall(query: str, limit: int = 5) -> str:
            """读取史佚主记忆 — _proxy 标记由管家在主进程执行"""
            return json.dumps({
                "_proxy": True, "tool": "memory/recall",
                "params": {"query": query, "limit": limit},
            }, ensure_ascii=False)

        # ── memory_remember ──
        @mcp.tool()
        async def memory_remember(fragments: List[Dict[str, Any]]) -> str:
            """写入记忆碎片 — _proxy 标记由管家在主进程写入"""
            return json.dumps({
                "_proxy": True, "tool": "memory/remember",
                "params": {"fragments": fragments},
            }, ensure_ascii=False)

        # ── memory_context ──
        @mcp.tool()
        async def memory_context(last_n: int = 5) -> str:
            """获取对话上下文 — _proxy 标记由管家在主进程提供"""
            return json.dumps({
                "_proxy": True, "tool": "memory/context",
                "params": {"last_n": last_n},
            }, ensure_ascii=False)

        # ── task_execute ──
        @mcp.tool()
        async def task_execute(
            task_id: str = "",
            description: str = "",
            skill: str = "",
            input: Optional[Dict[str, Any]] = None,
            timeout: int = 300,
            context: Optional[Dict[str, Any]] = None,
            ctx=None,
        ) -> str:
            """执行任务 — 后台线程运行，通过通知回报进度和结果"""
            if not task_id:
                task_id = f"task_{uuid.uuid4().hex[:12]}"
            self_ref._current_task_id = task_id
            input_data = input or {}

            def _run():
                try:
                    _notify(ctx, "notifications/progress", {
                        "task_id": task_id, "percent": 0,
                        "message": f"任务已接收: {description[:100]}",
                    })
                    task_prompt = (
                        f"{description}\n\n"
                        f"输入数据：{json.dumps(input_data, ensure_ascii=False)[:2000]}"
                    )
                    result = worker.run_agent_loop(
                        task=task_prompt,
                        skills=[skill] if skill else None,
                        timeout=timeout,
                    )
                    if result.get("success"):
                        new_fragments = []
                        if result.get("result"):
                            new_fragments.append({
                                "content": result["result"][:500],
                                "importance": 0.7,
                                "source": worker.config.clerk_id,
                            })
                        _notify(ctx, "notifications/complete", {
                            "task_id": task_id,
                            "result": result.get("result", ""),
                            "iterations": result.get("iterations", 0),
                            "new_fragments": new_fragments,
                        })
                        tasks[task_id] = {"status": "done", "result": result.get("result", "")}
                    else:
                        _notify(ctx, "notifications/error", {
                            "task_id": task_id,
                            "code": "EXECUTION_FAILED",
                            "message": result.get("error", "Unknown error"),
                        })
                        tasks[task_id] = {"status": "failed", "error": result.get("error", "执行失败")}
                except Exception as e:
                    _notify(ctx, "notifications/error", {
                        "task_id": task_id, "code": "CRASH", "message": str(e),
                    })
                    tasks[task_id] = {"status": "failed", "error": str(e)}
                finally:
                    if self_ref._current_task_id == task_id:
                        self_ref._current_task_id = ""

            threading.Thread(target=_run, daemon=True).start()
            return json.dumps({
                "accepted": True, "task_id": task_id,
                "estimated_duration": timeout // 6,
            }, ensure_ascii=False)

        # ── task_cancel ──
        @mcp.tool()
        async def task_cancel(task_id: str) -> str:
            """取消任务"""
            if task_id == self_ref._current_task_id:
                self_ref._current_task_id = ""
                tasks[task_id] = {"status": "cancelled", "error": "已取消"}
                return json.dumps({"cancelled": True, "task_id": task_id}, ensure_ascii=False)
            return json.dumps({"cancelled": False, "error": "Task not found"}, ensure_ascii=False)

        # ── task_status ──
        @mcp.tool()
        async def task_status(task_id: str) -> str:
            """查询任务状态"""
            if task_id in tasks:
                return json.dumps({"task_id": task_id, **tasks[task_id]}, ensure_ascii=False)
            if task_id == self_ref._current_task_id:
                return json.dumps({
                    "task_id": task_id, "status": "running",
                    "clerk_id": worker.config.clerk_id,
                }, ensure_ascii=False)
            return json.dumps({"task_id": task_id, "status": "unknown"}, ensure_ascii=False)

        # ── clerk_health ──
        @mcp.tool()
        async def clerk_health() -> str:
            """吏员健康检查"""
            return json.dumps({
                "status": "healthy" if not self_ref._shutting_down else "shutting_down",
                "clerk_id": worker.config.clerk_id,
                "current_task": self_ref._current_task_id,
                "session_id": self_ref._session_id,
            }, ensure_ascii=False)

        # ── clerk_shutdown ──
        @mcp.tool()
        async def clerk_shutdown(reason: str = "user_request", ctx=None) -> str:
            """优雅关闭吏员"""
            self_ref._shutting_down = True
            _notify(ctx, "notifications/error", {
                "task_id": self_ref._current_task_id,
                "code": "CLERK_SHUTDOWN",
                "message": f"吏员正在关闭: {reason}",
            })
            return json.dumps({"shutting_down": True, "reason": reason}, ensure_ascii=False)

        # ── resources ──
        @mcp.resource("skill://{name}")
        def skill_resource(name: str) -> str:
            skills_dir = Path(__file__).parent / "skills"
            sf = skills_dir / f"{name}.md"
            return sf.read_text(encoding="utf-8") if sf.exists() else ""

        @mcp.resource("knowledge://{name}")
        def knowledge_resource(name: str) -> str:
            knowledge_dir = Path(__file__).parent / "knowledge"
            kf = knowledge_dir / f"{name}.md"
            return kf.read_text(encoding="utf-8") if kf.exists() else ""

        # ── prompts ──
        @mcp.prompt()
        def execute_task(task: str) -> str:
            return f"请完成以下任务：{task}"

        @mcp.prompt()
        def search_and_summarize(query: str) -> str:
            return f"请搜索并总结：{query}"

        @mcp.prompt()
        def file_operation(operation: str, path: str) -> str:
            return f"请对文件执行 {operation}，路径：{path}"

    def run(self):
        """Start the MCP server via stdio transport."""
        logger.info("Clerk FastMCP server starting (stdio)")

        # Heartbeat — MCP protocol has built-in ping.
        # Clients use clerk_health() tool for explicit health checks.
        # No custom heartbeat needed.

        self.mcp.run(transport="stdio")


def _register_worker_tool(mcp: FastMCP, worker: ClerkWorker, tool_def: Dict[str, Any]):
    """Register a single worker tool on FastMCP with dynamic parameter schema."""
    tool_name = tool_def["name"]
    tool_desc = tool_def.get("description", "")
    params_schema = tool_def.get("inputSchema", {}).get("properties", {})
    required_params = tool_def.get("inputSchema", {}).get("required", [])

    # Build the handler dynamically
    def make_handler(tn, desc, schema, reqs):
        async def handler(**kwargs):
            result = worker.execute(tn, kwargs)
            return json.dumps(result, ensure_ascii=False)
        handler.__name__ = tn
        handler.__doc__ = desc
        # Annotate parameters
        for pname in reqs:
            handler.__annotations__[pname] = str
        for pname in schema:
            if pname not in handler.__annotations__:
                handler.__annotations__[pname] = Optional[str]
        return handler

    handler = make_handler(tool_name, tool_desc, params_schema, required_params)
    mcp.tool()(handler)


def _notify(ctx, method: str, data: Dict[str, Any]) -> None:
    """Send an MCP notification to the connected client."""
    if ctx is None:
        return
    try:
        session = ctx.session
        session.send_notification(method, data)
    except Exception:
        pass


# ── 入口 ──
def main():
    import sys

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

    server = ClerkMCPServer(config_path)
    server.run()


if __name__ == "__main__":
    main()
