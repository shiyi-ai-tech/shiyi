"""ClerkConnector — v0.13.0 MCP 远程吏员连接器

通过 subprocess + stdin/stdout JSON-RPC 连接吏员 MCP server。
对 ClerkRegistry 暴露与 ClerkWorker 完全相同的接口:
- get_tools()
- execute(tool_name, params)
- status()

同接口意味着 ClerkRegistry 不区分本地/远程吏员。
"""

import os
import sys
import json
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class RemoteClerk:
    """远程吏员代理 — 与 ClerkWorker 等接口"""

    def __init__(self, server_script: str, config_path: Optional[str] = None):
        """
        Args:
            server_script: mcp_server.py 路径
            config_path: clerk.json 路径
        """
        self._script = server_script
        self._config_path = config_path
        self._proc: Optional[subprocess.Popen] = None
        self._req_counter = 0
        self._config = None
        self._tools_cache: List[Dict[str, Any]] = []

    @property
    def config(self):
        """吏员配置（延迟从 MCP 获取）"""
        if self._config is None:
            self._config = self._load_config()
        return self._config

    def _load_config(self) -> Any:
        """从本地 clerk.json 读取配置"""
        import json as _json
        config_path = self._config_path
        if config_path is None:
            config_path = str(Path(self._script).parent / "clerk.json")

        class _Cfg:
            pass

        if Path(config_path).exists():
            data = _json.loads(Path(config_path).read_text())
            cfg = _Cfg()
            cfg.clerk_id = data.get("clerk_id", "unknown")
            cfg.name = data.get("name", "unknown")
            cfg.version = data.get("version", "0.0.0")
            cfg.enabled = data.get("enabled", True)
            cfg.capabilities = data.get("capabilities", [])
            return cfg

        cfg = _Cfg()
        cfg.clerk_id = "remote_unknown"
        cfg.name = "未知吏员"
        cfg.version = "0.0.0"
        cfg.enabled = True
        cfg.capabilities = []
        return cfg

    def _ensure_started(self) -> None:
        """确保 MCP server 进程已启动"""
        if self._proc is not None and self._proc.poll() is None:
            return

        cmd = [sys.executable, self._script]
        if self._config_path:
            cmd.extend(["-c", self._config_path])

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        # 发送 initialize
        resp = self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
        })
        logger.info(
            "Connected to clerk %s (v%s)",
            resp.get("result", {}).get("serverInfo", {}).get("name", "?"),
            resp.get("result", {}).get("serverInfo", {}).get("version", "?"),
        )

    def _send_request(self, method: str, params: dict) -> dict:
        """发送 JSON-RPC 请求，返回完整响应"""
        self._req_counter += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._req_counter,
            "method": method,
            "params": params,
        }

        self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError(f"Clerk process died: {method}")

        return json.loads(line)

    def get_tools(self) -> List[Dict[str, Any]]:
        """获取工具列表"""
        self._ensure_started()
        resp = self._send_request("tools/list", {})
        tools = resp.get("result", {}).get("tools", [])
        self._tools_cache = tools
        return tools

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具调用"""
        self._ensure_started()

        result = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": params,
        })

        content = result.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            import json as _json
            return _json.loads(content[0]["text"])

        return {
            "success": result.get("result", {}).get("isError", True),
            "result": "",
            "error": str(result.get("error", "Unknown")),
        }

    def status(self) -> Dict[str, Any]:
        """获取吏员状态"""
        return {
            "clerk_id": self.config.clerk_id,
            "name": self.config.name,
            "version": self.config.version,
            "mode": "remote",
            "tools_count": len(self._tools_cache),
            "enabled": self.config.enabled,
            "process_alive": self._proc is not None and self._proc.poll() is None,
        }

    def stop(self) -> None:
        """停止吏员进程"""
        if self._proc and self._proc.poll() is None:
            self._proc.stdin.close()
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            logger.info("Clerk %s stopped", self.config.clerk_id)
