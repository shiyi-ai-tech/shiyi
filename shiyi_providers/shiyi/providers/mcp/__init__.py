"""shiyi.providers.mcp - MCP工具调用接口

MCP (Model Context Protocol) Provider

提供：
- ToolDefinition: 工具定义数据结构
- ToolResult: 工具执行结果
- MCPProvider: 抽象接口类
- SimpleToolRegistry: 内存工具注册器实现

使用示例：
    >>> from shiyi.providers.mcp import SimpleToolRegistry, ToolDefinition
    >>> registry = SimpleToolRegistry()
    >>> registry.register_tool(ToolDefinition(name="echo", description="回显"))
"""

from shiyi.providers.mcp.base import (
    ToolDefinition,
    ToolResult,
    MCPProvider,
    SimpleToolRegistry,
)

__all__ = [
    "ToolDefinition",
    "ToolResult",
    "MCPProvider",
    "SimpleToolRegistry",
]
