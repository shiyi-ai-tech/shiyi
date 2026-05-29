"""
吏员工作器模板 (ClerkWorker Template)
══════════════════════════════════════════

接入步骤：
  1. 继承或重写 Tool 类，实现 execute(params) → {"success", "result", "error"}
  2. 将你的 Tool 注册到 TOOL_REGISTRY 字典
  3. 不需要改 ClerkWorker 类本身 — 它自动从 TOOL_REGISTRY + clerk.json 加载工具
  4. mcp_server.py 是固定模板，直接复制 clerk-default 的，无需修改

接口约定（与 ClerkRegistry 对接）：
  - get_tools() → list[dict]  返回工具列表（name + description + inputSchema）
  - execute(tool_name, params) → dict  返回 {"success": bool, "result": str, "error": str}
  - status() → dict  返回吏员状态信息

如果你需要 LLM 能力：
  在 clerk.json 中设 requires_llm=true，在 api_keys 中声明需要哪些 key，
  用户通过 WebUI 填写后，key 会以环境变量注入吏员进程。

如果你需要本地知识库：
  在 clerk.json 的 knowledge_base 中指定目录，吏员启动时读取文件，
  史佚可以通过 MCP 工具调用你的 search_kb 工具来检索。

安全注意：
  - 文件操作必须限制在沙箱内（参考 clerk-default 的 _safe_path 实现）
  - 不要信任用户输入的文件路径
  - API key 从环境变量读取，不要硬编码
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 吏员配置（不需要修改 — 自动从 clerk.json 加载）
# ═══════════════════════════════════════════════════════

class ClerkConfig:
    """吏员配置 — 从 clerk.json 自动加载"""

    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            config_file = Path(config_path)
        else:
            config_file = Path(__file__).parent / "clerk.json"

        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 基本身份
        self.clerk_id = data.get("clerk_id", "unknown")
        self.name = data.get("name", "未命名吏员")
        self.version = data.get("version", "0.1.0")
        self.description = data.get("description", "")
        self.created_at = data.get("created_at", "")

        # 能力声明
        self.tools_def = data.get("tools", [])
        self.capabilities = data.get("capabilities", [])
        self.enabled = data.get("enabled", True)

        # 可选能力
        self.api_keys = data.get("api_keys", [])
        self.requires_llm = data.get("requires_llm", False)
        self.knowledge_base = data.get("knowledge_base", "")
        self.skills = data.get("skills", [])


# ═══════════════════════════════════════════════════════
# 工具实现 — 在这里添加你的工具
# ═══════════════════════════════════════════════════════

class MyTool:
    """
    自定义工具模板

    每个 Tool 类需要三个属性：
      - name: str       工具名（唯一，与 clerk.json 的 tools[].name 对应）
      - description: str 工具描述（LLM 据此判断何时调用）
      - schema: dict    参数定义（JSON Schema，与 clerk.json 的 inputSchema 对应）

    一个静态方法：
      - execute(params) → {"success": bool, "result": str, "error": str}
    """

    name = "my_tool"
    description = "我的自定义工具 — 替换为你的工具描述"

    schema = {
        "type": "object",
        "properties": {
            "param1": {
                "type": "string",
                "description": "参数1的描述",
            },
        },
        "required": ["param1"],
    }

    @staticmethod
    def execute(params: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行工具逻辑

        Args:
            params: LLM 传递的参数（与 schema 对应）

        Returns:
            {"success": bool, "result": str, "error": str}
            - success=True  → result 中包含返回给 LLM 的内容
            - success=False → error 中包含错误信息
        """
        param1 = params.get("param1", "")
        if not param1:
            return {"success": False, "result": "", "error": "param1 不能为空"}

        # TODO: 替换为你的实际逻辑
        result_text = f"处理了: {param1}"

        return {"success": True, "result": result_text, "error": ""}


# ═══════════════════════════════════════════════════════
# 工具注册表 — 添加你的 Tool 类
# ═══════════════════════════════════════════════════════

TOOL_REGISTRY: Dict[str, Any] = {
    "my_tool": MyTool,
    # "your_second_tool": YourSecondTool,  ← 新增工具在这里注册
}


# ═══════════════════════════════════════════════════════
# ClerkWorker — 不需要修改！
# ═══════════════════════════════════════════════════════

class ClerkWorker:
    """吏员工作器 — 框架代码，无需修改"""

    def __init__(self, config_path: Optional[str] = None):
        self.config = ClerkConfig(config_path)
        self._handlers: Dict[str, callable] = {}

        # 注册工具 handler
        for tool_name, tool_class in TOOL_REGISTRY.items():
            if tool_name in self.config.capabilities:
                self._handlers[tool_name] = lambda p, tc=tool_class: tc.execute(p)

    def get_tools(self) -> List[Dict[str, Any]]:
        """返回工具列表（MCP tools/list 格式）"""
        tools = []
        for tool_name, tool_class in TOOL_REGISTRY.items():
            if tool_name in self.config.capabilities:
                tools.append({
                    "name": tool_class.name,
                    "description": tool_class.description,
                    "inputSchema": tool_class.schema,
                })
        return tools

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具调用"""
        if not self.config.enabled:
            return {"success": False, "result": "", "error": "吏员已禁用"}

        handler = self._handlers.get(tool_name)
        if handler is None:
            available = list(self._handlers.keys())
            return {
                "success": False,
                "result": "",
                "error": f"未知工具: {tool_name}。可用: {available}",
            }

        try:
            return handler(params)
        except Exception as e:
            logger.error(f"工具执行失败: {tool_name}, {e}")
            return {"success": False, "result": "", "error": str(e)}

    def status(self) -> Dict[str, Any]:
        """返回吏员状态"""
        return {
            "clerk_id": self.config.clerk_id,
            "name": self.config.name,
            "version": self.config.version,
            "mode": "local",
            "tools_count": len(self.get_tools()),
            "enabled": self.config.enabled,
            "capabilities": self.config.capabilities,
        }


# ═══════════════════════════════════════════════════════
# 调试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    clerk = ClerkWorker()
    print("=== 吏员状态 ===")
    print(json.dumps(clerk.status(), indent=2, ensure_ascii=False))
    print("\n=== 可用工具 ===")
    for t in clerk.get_tools():
        print(f"  - {t['name']}: {t['description']}")
