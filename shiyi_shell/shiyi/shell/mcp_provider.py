"""shell层 - MCP Provider（兼容层）

⚠️ 已迁移至 shiyi-providers 包

本文件仅保留用于向后兼容，实际实现已移至：
    shiyi.providers.mcp

请使用新的导入方式：
    from shiyi.providers.mcp import MCPProvider, ToolDefinition, ToolResult
"""

# 重新导出所有符号以保持兼容性
from shiyi.providers.mcp import (
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
