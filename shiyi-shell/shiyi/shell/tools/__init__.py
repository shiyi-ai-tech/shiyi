"""
Shell 工具层 — 第一批工具实现

工具协议（与 MCP 兼容）：
- 每个工具是一个可调用对象，接受 dict 参数
- 返回 {"success": bool, "result": str, "error": str}
"""
from shiyi.shell.tools.registry import ToolRegistry
from shiyi.shell.tools.web_search import web_search_tool
from shiyi.shell.tools.file_ops import file_read_tool, file_write_tool

__all__ = ["ToolRegistry", "web_search_tool", "file_read_tool", "file_write_tool"]
