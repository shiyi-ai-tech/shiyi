"""MCP server for clerk-default — v0.13.0

通过 stdin/stdout JSON-RPC 暴露吏员工具。
史佚端通过 subprocess + stdio 连接。

MCP 方法:
- initialize: 握手
- tools/list: 返回工具列表
- tools/call: 执行工具调用
"""

import sys
import json
import logging
from pathlib import Path
from worker import ClerkWorker, ClerkConfig

logger = logging.getLogger("clerk_mcp")


class ClerkMCP:
    """MCP server wrapper for ClerkWorker"""

    def __init__(self, config_path: str = None):
        self.worker = ClerkWorker(config_path)
        self._server_info = {
            "name": f"clerk-{self.worker.config.clerk_id}",
            "version": self.worker.config.version,
        }

    def handle_request(self, request: dict) -> dict:
        """处理单个 JSON-RPC 请求"""
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "tools/list":
                result = self._handle_tools_list()
            elif method == "tools/call":
                result = self._handle_tools_call(params)
            elif method == "ping":
                result = {"pong": True}
            elif method == "notifications/initialized":
                return None  # 通知无需响应
            else:
                result = {"error": f"Unknown method: {method}"}

            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        except Exception as e:
            logger.error(f"Error handling {method}: {e}")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            }

    def _handle_initialize(self, params: dict) -> dict:
        """MCP 握手"""
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": self._server_info,
            "capabilities": {
                "tools": {},
            },
        }

    def _handle_tools_list(self) -> dict:
        """列出所有工具"""
        tools = self.worker.get_tools()
        return {"tools": tools}

    def _handle_tools_call(self, params: dict) -> dict:
        """执行工具调用"""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = self.worker.execute(tool_name, arguments)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False),
                }
            ],
            "isError": not result.get("success", False),
        }

    def run_stdio(self) -> None:
        """主循环：读 stdin → 处理 → 写 stdout"""
        logger.info("Clerk MCP server starting (stdio mode)")

        for line in sys.stdin:
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


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Clerk MCP Server")
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to clerk.json",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    server = ClerkMCP(args.config)
    server.run_stdio()


if __name__ == "__main__":
    main()
