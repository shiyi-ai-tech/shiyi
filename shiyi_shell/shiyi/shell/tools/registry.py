"""
ToolRegistry — 工具注册中心

轻量 dict 映射：工具名 → (schema, callable)
不做复杂抽象，不做抽象基类。
"""
from typing import Dict, Any, List, Callable, Optional


class ToolRegistry:
    """工具注册中心"""

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> None:
        """注册工具

        Args:
            name: 工具名
            description: 工具描述
            parameters: JSON Schema 参数定义
            handler: 工具执行函数，接受 dict 参数
        """
        self._tools[name] = {
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
            "handler": handler,
        }

    def get_schemas(self) -> List[Dict[str, Any]]:
        """获取全部工具的 Function Calling schema"""
        return [t["schema"] for t in self._tools.values()]

    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具

        Args:
            name: 工具名
            arguments: 工具参数

        Returns:
            {"success": bool, "result": str, "error": str}
        """
        tool = self._tools.get(name)
        if tool is None:
            return {"success": False, "result": "", "error": f"Tool not found: {name}"}

        try:
            return tool["handler"](arguments)
        except Exception as e:
            return {"success": False, "result": "", "error": str(e)}

    def list_tools(self) -> List[str]:
        """列出所有已注册工具名"""
        return list(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
