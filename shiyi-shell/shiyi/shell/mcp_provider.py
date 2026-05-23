"""MCPProvider — MCP 工具调用接口槽位

Phase 4: core 零网络依赖，MCP 接口在 shell 预留。
吏员系统（Phase 4+）将实现此接口。

接口设计：
- ToolDefinition: 单个工具的定义（名字+参数schema+描述）
- MCPProvider: 工具注册/调用/列表的抽象接口
- 吏员 Agent 通过 MCPProvider 访问外部工具
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable


@dataclass
class ToolDefinition:
    """工具定义

    Attributes:
        name: 工具名称（唯一标识）
        description: 工具描述（供 LLM 选择工具时参考）
        parameters: JSON Schema 参数定义
        handler: 执行函数 (params) -> ToolResult
    """
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    handler: Optional[Callable] = None


@dataclass
class ToolResult:
    """工具执行结果"""
    tool_name: str
    success: bool = False
    result: Optional[Any] = None
    error: str = ""


class MCPProvider(ABC):
    """MCP 工具提供者抽象接口

    shell 层定义，吏员系统实现。
    吏员通过此接口注册工具、列出可用工具、调用工具。
    """

    @abstractmethod
    def register_tool(self, tool: ToolDefinition) -> bool:
        """注册一个工具

        Args:
            tool: 工具定义

        Returns:
            是否注册成功
        """
        ...

    @abstractmethod
    def unregister_tool(self, tool_name: str) -> bool:
        """注销一个工具

        Args:
            tool_name: 工具名称

        Returns:
            是否注销成功
        """
        ...

    @abstractmethod
    def list_tools(self) -> List[ToolDefinition]:
        """列出所有已注册工具

        Returns:
            工具定义列表
        """
        ...

    @abstractmethod
    def call_tool(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        """调用工具

        Args:
            tool_name: 工具名称
            params: 工具参数

        Returns:
            工具执行结果
        """
        ...

    @abstractmethod
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """获取工具 schema 列表（供 LLM function calling）

        Returns:
            [{name, description, parameters}, ...]
        """
        ...


class SimpleToolRegistry(MCPProvider):
    """简易工具注册器 — 内存实现

    吏员系统就绪前的基本实现。
    吏员系统可用后，吏员 Agent 通过 MCPProvider 接口访问。
    """

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}

    def register_tool(self, tool: ToolDefinition) -> bool:
        """注册工具"""
        if not tool.name:
            return False
        self._tools[tool.name] = tool
        return True

    def unregister_tool(self, tool_name: str) -> bool:
        """注销工具"""
        if tool_name in self._tools:
            del self._tools[tool_name]
            return True
        return False

    def list_tools(self) -> List[ToolDefinition]:
        """列出所有工具"""
        return list(self._tools.values())

    def call_tool(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        """调用工具"""
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(tool_name=tool_name, error=f"工具 '{tool_name}' 未注册")

        if not tool.handler:
            return ToolResult(tool_name=tool_name, error=f"工具 '{tool_name}' 无处理器")

        try:
            result = tool.handler(params)
            return ToolResult(tool_name=tool_name, success=True, result=result)
        except Exception as e:
            return ToolResult(tool_name=tool_name, error=str(e))

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """获取工具 schema"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]
