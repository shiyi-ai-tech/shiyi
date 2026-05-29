"""ClerkConnector — v1.0 MCP 远程吏员连接器

通过 subprocess + stdin/stdout JSON-RPC 2.0 连接吏员 MCP server。
通过 SSE pipe (fd 3) 接收事件（心跳、进度、完成通知）。

协议分层:
- stdin  → JSON-RPC 请求（管家 → 吏员）
- stdout → JSON-RPC 响应（吏员 → 管家）
- fd 3   → SSE 事件流（吏员 → 管家，后台线程读取）

新增方法 (v1.0):
- task_execute / task_cancel / task_status: 任务生命周期
- clerk_health / clerk_shutdown: 健康管理
- memory_recall / memory_remember / memory_context: 记忆代理

对 ClerkRegistry 暴露与 ClerkWorker 完全相同的接口，不区分本地/远程吏员。
"""

import os
import sys
import json
import time
import logging
import subprocess
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger(__name__)


class SSEReader:
    """SSE 事件流读取器 — 从 fd 读取 SSE 格式的事件"""

    def __init__(self, fd):
        self._fd = fd
        self._buffer = ""
        self._stop = threading.Event()

    def read_event(self) -> Optional[Dict[str, Any]]:
        """读取一个完整的 SSE 事件，阻塞直到有数据或 stop"""
        while not self._stop.is_set():
            try:
                chunk = os.read(self._fd, 4096).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                return None
            if not chunk:
                return None  # EOF

            self._buffer += chunk
            # SSE 事件以 \n\n 分隔
            while "\n\n" in self._buffer:
                event_str, self._buffer = self._buffer.split("\n\n", 1)
                event = self._parse_event(event_str)
                if event:
                    return event
        return None

    def _parse_event(self, text: str) -> Optional[Dict[str, Any]]:
        """解析单个 SSE 事件"""
        event_type = ""
        data = ""
        for line in text.split("\n"):
            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if not event_type or not data:
            return None
        try:
            return {"event": event_type, "data": json.loads(data)}
        except json.JSONDecodeError:
            return None

    def close(self):
        self._stop.set()


class RemoteClerk:
    """远程吏员代理 — v1.0 JSON-RPC 2.0 + SSE"""

    def __init__(self, server_script: str, config_path: Optional[str] = None,
                 on_notification: Optional[Callable[[str, dict], None]] = None):
        self._script = server_script
        self._config_path = config_path
        self._proc: Optional[subprocess.Popen] = None
        self._req_counter = 0
        self._config = None
        self._tools_cache: List[Dict[str, Any]] = []
        self._on_notification = on_notification

        # SSE reader
        self._sse_pipe_read: Optional[int] = None
        self._sse_reader: Optional[SSEReader] = None
        self._sse_thread: Optional[threading.Thread] = None

        # Response handling
        self._resp_lock = threading.Lock()
        self._pending_responses: Dict[int, dict] = {}

    @property
    def config(self):
        if self._config is None:
            self._config = self._load_config()
        return self._config

    def _load_config(self) -> Any:
        config_path = self._config_path
        if config_path is None:
            config_path = str(Path(self._script).parent / "clerk.json")

        class _Cfg:
            pass

        if Path(config_path).exists():
            data = json.loads(Path(config_path).read_text(encoding="utf-8"))
            cfg = _Cfg()
            cfg.clerk_id = data.get("clerk_id", "unknown")
            cfg.name = data.get("name", "unknown")
            cfg.description = data.get("description", "")
            cfg.version = data.get("version", "0.0.0")
            cfg.enabled = data.get("enabled", True)
            cfg.capabilities = data.get("capabilities", [])
            cfg.skills = data.get("skills", [])
            cfg.api_keys = data.get("api_keys", [])
            return cfg

        cfg = _Cfg()
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

    # ── SSE 读取 ─────────────────────────────────────────

    def _start_sse_reader(self):
        """启动 SSE 事件读取线程"""
        if self._sse_thread and self._sse_thread.is_alive():
            return
        self._sse_thread = threading.Thread(target=self._sse_loop, daemon=True)
        self._sse_thread.start()

    def _sse_loop(self):
        """后台读取 SSE 事件并路由到回调"""
        import select
        while True:
            try:
                ready, _, _ = select.select([self._sse_pipe_read], [], [], 1.0)
                if not ready:
                    continue
            except (OSError, ValueError):
                break

            try:
                event = self._sse_reader.read_event()
            except Exception:
                break

            if event is None:
                break

            # 路由到通知回调
            if self._on_notification:
                event_type = event["event"]
                data = event["data"]
                try:
                    self._on_notification(event_type, data)
                except Exception:
                    logger.exception("SSE notification handler failed")
            else:
                logger.debug("SSE event: %s", event["event"])

    # ── Process 管理 ─────────────────────────────────────

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        # 创建 SSE pipe
        sse_r, sse_w = os.pipe()
        self._sse_pipe_read = sse_r
        self._sse_reader = SSEReader(sse_r)

        # API keys 注入
        saved_keys = self._load_saved_api_keys()
        env = os.environ.copy()
        env.update(saved_keys)

        cmd = [sys.executable, self._script]
        if self._config_path:
            cmd.append(self._config_path)

        # stdin=JSON-RPC 请求, stdout=JSON-RPC 响应, stderr=日志, fd 3/4+=SSE
        # CLERK_SSE_FD 告诉子进程正确的 SSE fd 号
        env["CLERK_SSE_FD"] = str(sse_w)
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(sse_w,),
            text=True,
            bufsize=1,
            env=env,
        )
        os.close(sse_w)  # 子进程已继承，关闭父进程端

        # 启动 SSE 事件读取
        self._start_sse_reader()

        # 握手 — 发送 initialize
        resp = self._send_request("initialize", {})
        if "result" in resp:
            logger.info(
                "Connected to clerk %s (v%s) session=%s",
                resp["result"].get("serverInfo", {}).get("name", "?"),
                resp["result"].get("serverInfo", {}).get("version", "?"),
                resp["result"].get("session_id", ""),
            )
        else:
            logger.warning("Clerk initialize failed: %s", resp.get("error", {}))

    def _send_request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        """发送 JSON-RPC 请求，等待匹配 id 的响应"""
        self._req_counter += 1
        req_id = self._req_counter
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

        # 读取 stdout 直到找到匹配的响应
        start = time.time()
        while time.time() - start < timeout:
            if self._proc.poll() is not None:
                raise RuntimeError(f"Clerk process died during {method}")

            # 检查 pending dict
            with self._resp_lock:
                if req_id in self._pending_responses:
                    resp = self._pending_responses.pop(req_id)
                    return resp

            # 从 stdout 读取
            try:
                line = self._proc.stdout.readline()
            except Exception:
                raise RuntimeError(f"Clerk stdout read error during {method}")

            if not line:
                time.sleep(0.05)
                continue

            try:
                msg = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            msg_id = msg.get("id")
            if msg_id is not None:
                if msg_id == req_id:
                    return msg
                # 存入等待字典（可能另一个线程在等）
                with self._resp_lock:
                    self._pending_responses[msg_id] = msg
            # 没有 id 的是 notification，忽略（SSE 通道处理）

        raise RuntimeError(f"Clerk request timeout: {method} (id={req_id})")

    # ── 工具接口 ─────────────────────────────────────────

    def get_tools(self) -> List[Dict[str, Any]]:
        self._ensure_started()
        resp = self._send_request("tools/list", {})
        tools = resp.get("result", {}).get("tools", [])
        self._tools_cache = tools
        return tools

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_started()
        result = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": params,
        })
        content = result.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except (json.JSONDecodeError, KeyError):
                return {"success": True, "result": content[0].get("text", "")}
        return {
            "success": not result.get("result", {}).get("isError", True),
            "result": "",
            "error": str(result.get("error", "Unknown")),
        }

    def status(self) -> Dict[str, Any]:
        return {
            "clerk_id": self.config.clerk_id,
            "name": self.config.name,
            "version": self.config.version,
            "mode": "remote",
            "tools_count": len(self._tools_cache),
            "enabled": self.config.enabled,
            "process_alive": self._proc is not None and self._proc.poll() is None,
        }

    # ── 任务管理 (v1.0 新增) ─────────────────────────────

    def task_execute(self, task_id: str, description: str,
                     skill: Optional[str] = None, input_data: dict = None,
                     timeout: int = 300, context: dict = None) -> dict:
        self._ensure_started()
        params = {
            "task_id": task_id,
            "description": description,
            "timeout": timeout,
        }
        if skill:
            params["skill"] = skill
        if input_data:
            params["input"] = input_data
        if context:
            params["context"] = context
        return self._send_request("task/execute", params).get("result", {})

    def task_cancel(self, task_id: str) -> dict:
        self._ensure_started()
        return self._send_request("task/cancel", {"task_id": task_id}).get("result", {})

    def task_status(self, task_id: str) -> dict:
        self._ensure_started()
        return self._send_request("task/status", {"task_id": task_id}).get("result", {})

    # ── 健康管理 (v1.0 新增) ─────────────────────────────

    def clerk_health(self) -> dict:
        self._ensure_started()
        return self._send_request("clerk/health", {}, timeout=5).get("result", {})

    def clerk_shutdown(self, reason: str = "user_request") -> dict:
        if self._proc is None or self._proc.poll() is not None:
            return {"shutting_down": True, "reason": "already_stopped"}
        try:
            return self._send_request("clerk/shutdown", {"reason": reason}).get("result", {})
        except RuntimeError:
            return {"shutting_down": True, "reason": "process_died"}

    # ── 记忆代理 (v1.0 新增) ─────────────────────────────

    def memory_recall(self, query: str, limit: int = 5) -> dict:
        self._ensure_started()
        return self._send_request("memory/recall", {"query": query, "limit": limit}).get("result", {})

    def memory_remember(self, fragments: List[dict]) -> dict:
        self._ensure_started()
        return self._send_request("memory/remember", {"fragments": fragments}).get("result", {})

    def memory_context(self, last_n: int = 5) -> dict:
        self._ensure_started()
        return self._send_request("memory/context", {"last_n": last_n}).get("result", {})

    # ── Agent ────────────────────────────────────────────

    def agent_run(self, task: str, skills: List[str] = None,
                  max_iterations: int = None, timeout: int = None) -> dict:
        self._ensure_started()
        params = {"task": task}
        if skills:
            params["skills"] = skills
        if max_iterations:
            params["max_iterations"] = max_iterations
        if timeout:
            params["timeout"] = timeout
        return self._send_request("agent/run", params).get("result", {})

    # ── 生命周期 ─────────────────────────────────────────

    def stop(self) -> None:
        if self._sse_reader:
            self._sse_reader.close()
        if self._proc and self._proc.poll() is None:
            try:
                self._send_request("clerk/shutdown", {"reason": "stopping"}, timeout=3)
            except Exception:
                pass
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            logger.info("Clerk %s stopped", self.config.clerk_id)
        if self._sse_pipe_read:
            try:
                os.close(self._sse_pipe_read)
            except OSError:
                pass
