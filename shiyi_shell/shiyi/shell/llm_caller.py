"""shell层 - LLM调用实现（兼容层）

⚠️ 已迁移至 shiyi-providers 包

本文件仅保留用于向后兼容，实际实现已移至：
    shiyi.providers.llm

请使用新的导入方式：
    from shiyi.providers.llm import DeepSeekLLMCaller, create_llm_caller
"""

# 重新导出所有符号以保持兼容性
from shiyi.providers.llm import (
    DeepSeekLLMCaller,
    MockLLMCaller,
    create_llm_caller,
    create_light_caller,
    create_fallback_caller,
)

__all__ = [
    "DeepSeekLLMCaller",
    "MockLLMCaller",
    "create_llm_caller",
    "create_light_caller",
    "create_fallback_caller",
]
