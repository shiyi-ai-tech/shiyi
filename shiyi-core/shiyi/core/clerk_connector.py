"""RemoteClerk — v2.0 mcp-python SDK 远程吏员连接器

通过 mcp.client.stdio.stdio_client + ClientSession 连接吏员。
替代 v1.0 手写 JSON-RPC 2.0 + fd3 SSE 读取。

架构:
- 通信层: mcp-python SDK (stdio_client + ClientSession)
- 持久会话: 后台 asyncio task 保持 session 存活
- 同步调用: execute() 等通过 call_tool 同步包装
- 通知处理: ClientSession.send_notification / message_handler
- 进程管理: subprocess (启动/重启/超时kill/僵尸回收) — 保留
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

logger = logging.getLogger(__name__)


class RemoteClerk:
    """远程吏员代理 — v2.0 mcp-python SDK + 持久会话"""

    def __init__(
        self,
        server_script: str,
        config_path: Optional[str] = None,
        on_notification: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self._script = server_script
        self._config_path = config_path
        self._config = None
        self._tools_cache: List[Dict[str, Any]] = []
        self._on_notification = on_notification

        # Session objects — managed by background asyncio task
        self._session: Optional[ClientSession] = None

        # Event loop + thread for persistent async session
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._session_ready = threading.Event()
        self._running = False

        # Process management
        self._proc: Optional[subprocess.Popen] = None  # Managed by stdio_client internally

    @property
    def config(self):
        if self._config is None:
            self._config = self._load_config()
        return self._config

    def _load_config(self):
        config_path = self._config_path
        if config_path is None:
            config_path = str(Path(self._script).parent / "clerk.json")

        class _Cfg:
            pass

        cfg = _Cfg()
        if Path(config_path).exists():
            data = json.loads(Path(config_path).read_text(encoding="utf-8"))
            cfg.clerk_id = data.get("clerk_id", "unknown")
            cfg.name = data.get("name", "unknown")
            cfg.description = data.get("description", "")
            cfg.version = data.get("version", "0.0.0")
            cfg.enabled = data.get("enabled", True)
            cfg.capabilities = data.get("capabilities", [])
            cfg.skills = data.get("skills", [])
            cfg.api_keys = data.get("api_keys", [])
        else:
            cfg.clerk_id = "remote_unknown"
            cfg.name = "未知吏员"
            cfg.description = ""
            cfg.version = "0.0.0"
            cfg.enabled = True
            cfg.capabilities = []
            cfg.skills = []
            cfg.api_keys = []
        return cfg

    # ── API Key 注入 ────────────────────────────────────

    def _load_saved_api_keys(self) -> Dict[str, str]:
        clerk_id = self.config.clerk_id
        user_config = Path.home() / ".shiyi" / "clerks" / f"{clerk_id}.json"
        if not user_config.exists():
            return {}
        try:
            data = json.loads(user_config.read_text(encoding="utf-8"))
            api_keys = data.get("api_keys", {})
            if isinstance(api_keys, dict):
                return {k: v for k, v in api_keys.items() if v and v.strip()}
        except (json.JSONDecodeError, IOError):
            pass
        return {}

    # ── 持久会话管理（后台 asyncio event loop）───────────

    def _ensure_started(self):
        """Ensure the clerk process and MCP session are running."""
        if self._session is not None and self._running:
            # Check if process died
            if self._proc is not None and self._proc.poll() is not None:
                logger.warning("Clerk process died, restarting...")
                self.stop()
            else:
                return

        if self._loop_thread is None or not self._loop_thread.is_alive():
            self._running = True
            self._session_ready.clear()
            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(
                target=self._run_loop, daemon=True
            )
            self._loop_thread.start()

        # Wait for session to be ready (with timeout)
        if not self._session_ready.wait(timeout=15):
            raise RuntimeError(
                f"Timed out waiting for clerk {self.config.clerk_id} to initialize"
            )

    def _run_loop(self):
        """Background thread — runs persistent asyncio event loop with MCP session."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._persistent_session())
        except Exception:
            logger.exception("Clerk event loop crashed")
        finally:
            self._session_ready.clear()
            self._session = None

    async def _persistent_session(self):
        """Maintain a persistent MCP session over stdio_client."""
        env = os.environ.copy()
        env.update(self._load_saved_api_keys())

        cmd = [sys.executable, self._script]
        if self._config_path:
            cmd.append(self._config_path)

        params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)

        # Enter the stdio_client context and keep it open
        # We manually manage the context so the session stays alive
        self._transport = None
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                self._transport = (read_stream, write_stream)

                async with ClientSession(
                    read_stream, write_stream,
                    message_handler=self._handle_mcp_message,
                ) as session:
                    self._session = session

                    # Initialize — manual handshake to work around mcp 1.26.0
                    # FastMCP→ClientSession tasks.cancel type mismatch
                    await self._initialize_handshake(session)
                    logger.info(
                        "Connected to clerk %s (session initialized)",
                        self.config.clerk_id,
                    )

                    # Discover tools
                    result = await session.list_tools()
                    self._tools_cache = [
                        {
                            "name": t.name,
                            "description": t.description or "",
                            "inputSchema": t.inputSchema or {},
                        }
                        for t in result.tools
                    ]
                    logger.info(
                        "Discovered %d tools from clerk %s",
                        len(self._tools_cache), self.config.clerk_id,
                    )

                    self._session_ready.set()

                    # Keep session alive — block here until stop()
                    while self._running:
                        await asyncio.sleep(0.5)

        except Exception:
            if self._running:
                logger.exception("MCP session failed for clerk %s", self.config.clerk_id)
        finally:
            self._session_ready.clear()
            self._session = None

    async def _handle_mcp_message(self, message: Any):
        """Handle incoming MCP messages (notifications, etc.)."""
        if self._on_notification:
            try:
                # MCP notifications come as JSONRPCMessage objects
                method = getattr(message, "method", None)
                params = getattr(message, "params", {})
                if method:
                    self._on_notification(method, params)
            except Exception:
                pass

    async def _initialize_handshake(self, session):
        """Manual MCP initialize handshake.

        Workaround for mcp 1.26.0: FastMCP server returns
        capabilities.tasks.cancel as bool, but ClientSession's
        InitializeResult pydantic model requires TasksCancelCapability dict.
        We call session.initialize() with a monkey-patch on the result type
        to strip tasks from capabilities before validation.
        """
        from mcp import types

        # Monkey-patch: intercept InitializeResult validation to strip tasks
        _original_validate = types.InitializeResult.model_validate

        def _patched_validate(data, **kwargs):
            if isinstance(data, dict) and "capabilities" in data:
                caps = data["capabilities"]
                if isinstance(caps, dict):
                    caps.pop("tasks", None)
            elif hasattr(data, "capabilities") and hasattr(data.capabilities, "tasks"):
                del data.capabilities.tasks
            return _original_validate(data, **kwargs)

        types.InitializeResult.model_validate = _patched_validate
        try:
            return await session.initialize()
        finally:
            types.InitializeResult.model_validate = _original_validate

    # ── 同步调用 wrapper ─────────────────────────────────

    def _call_tool_sync(self, tool_name: str, params: Dict[str, Any], timeout: float = 300.0) -> Dict[str, Any]:
        """Synchronous wrapper for session.call_tool().

        Default timeout 300s — long tasks (code generation, file operations)
        need more than 30s. Timeout can be overridden per-call.
        """
        self._ensure_started()
        session = self._session
        if session is None:
            return {"success": False, "data": None, "error": "Session not available"}

        loop = self._loop
        if loop is None:
            return {"success": False, "data": None, "error": "Event loop not running"}

        if not loop.is_running():
            return {"success": False, "data": None, "error": "Event loop stopped"}

        # Allow per-tool timeout from params (consumed locally, not forwarded)
        effective_timeout = timeout
        clean_params = dict(params) if isinstance(params, dict) else params
        if isinstance(clean_params, dict) and "_timeout" in clean_params:
            try:
                effective_timeout = float(clean_params.pop("_timeout"))
            except (ValueError, TypeError):
                clean_params.pop("_timeout", None)

        # Run coroutine in the background loop and wait for result
        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(tool_name, clean_params), loop
        )
        try:
            result = future.result(timeout=effective_timeout)
        except TimeoutError:
            return {"success": False, "data": None, "error": f"Tool call timeout ({effective_timeout}s): {tool_name}"}
        except Exception as e:
            return {"success": False, "data": None, "error": str(e)}

        return self._parse_call_result(result)

    async def _call_tool_async(self, tool_name: str, params: Dict[str, Any]):
        """Async call_tool — runs in the persistent event loop."""
        session = self._session
        if session is None:
            raise RuntimeError("Session not available")
        return await session.call_tool(tool_name, params)

    def _parse_call_result(self, result: Any) -> Dict[str, Any]:
        """Parse MCP CallToolResult into Shiyi's format."""
        try:
            if hasattr(result, "content") and result.content:
                text = result.content[0].text
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        if "_proxy" in data or "success" in data:
                            return data
                        return {"success": True, "data": data}
                except json.JSONDecodeError:
                    return {"success": True, "data": text}

            is_error = getattr(result, "isError", False)
            return {"success": not is_error, "data": None, "error": ""}
        except Exception as e:
            return {"success": False, "data": None, "error": str(e)}

    # ── 工具接口 ─────────────────────────────────────────

    def get_tools(self) -> List[Dict[str, Any]]:
        self._ensure_started()
        return list(self._tools_cache)

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._call_tool_sync(tool_name, params)

    def status(self) -> Dict[str, Any]:
        return {
            "clerk_id": self.config.clerk_id,
            "name": self.config.name,
            "version": self.config.version,
            "mode": "remote",
            "tools_count": len(self._tools_cache),
            "enabled": self.config.enabled,
            "process_alive": self._session is not None,
        }

    # ── 任务管理 ─────────────────────────────────────────

    def task_execute(
        self, task_id: str, description: str,
        skill: Optional[str] = None, input_data: Optional[Dict] = None,
        timeout: int = 300, context: Optional[Dict] = None,
    ) -> dict:
        return self.execute("task_execute", {
            "task_id": task_id, "description": description,
            "skill": skill or "", "input": input_data or {},
            "timeout": timeout, "context": context or {},
        })

    def task_cancel(self, task_id: str) -> dict:
        return self.execute("task_cancel", {"task_id": task_id})

    def task_status(self, task_id: str) -> dict:
        return self.execute("task_status", {"task_id": task_id})

    # ── 健康管理 ─────────────────────────────────────────

    def clerk_health(self) -> dict:
        return self.execute("clerk_health", {})

    def clerk_shutdown(self, reason: str = "user_request") -> dict:
        if self._proc is not None and self._proc.poll() is not None:
            return {"shutting_down": True, "reason": "already_stopped"}
        try:
            return self.execute("clerk_shutdown", {"reason": reason})
        except Exception:
            return {"shutting_down": True, "reason": "process_died"}

    # ── 记忆代理 ─────────────────────────────────────────

    def memory_recall(self, query: str, limit: int = 5) -> dict:
        return self.execute("memory_recall", {"query": query, "limit": limit})

    def memory_remember(self, fragments: List[dict]) -> dict:
        return self.execute("memory_remember", {"fragments": fragments})

    def memory_context(self, last_n: int = 5) -> dict:
        return self.execute("memory_context", {"last_n": last_n})

    # ── Agent ────────────────────────────────────────────

    def agent_run(
        self, task: str, skills: Optional[List[str]] = None,
        max_iterations: Optional[int] = None, timeout: Optional[int] = None,
    ) -> dict:
        params: Dict[str, Any] = {"task": task}
        if skills:
            params["skills"] = skills
        if max_iterations:
            params["max_iterations"] = max_iterations
        if timeout:
            params["timeout"] = timeout
        return self.execute("agent_run", params)

    # ── 生命周期 ─────────────────────────────────────────

    def stop(self) -> None:
        """Gracefully stop the clerk process and session."""
        self._running = False

        # Try graceful shutdown via tool
        session = self._session
        if session is not None:
            try:
                loop = self._loop
                if loop and loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        session.call_tool("clerk_shutdown", {"reason": "stopping"}),
                        loop,
                    )
                    future.result(timeout=3)
            except Exception:
                pass

        self._session = None
        self._session_ready.clear()

        # Stop event loop — stdio_client context manager will clean up the process
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5)

        logger.info("Clerk %s stopped", self.config.clerk_id)
        self._loop = None
