"""shiyi-shell - 史佚外壳模块

职责：
- 提供LLM调用的具体实现（兼容层）
- 提供Embedding调用的具体实现（兼容层）
- 提供CLI命令行入口

核心组件已迁移至 shiyi-providers 包。
此处保留重新导出以向后兼容。
"""

__version__ = "260528.0"

from shiyi.shell.llm_caller import (
    DeepSeekLLMCaller,
    MockLLMCaller,
    create_llm_caller,
    create_light_caller,
    create_fallback_caller,
)

from shiyi.shell.embedding_caller import (
    DeepSeekEmbeddingCaller,
    BGEEmbeddingCaller,
    SiliconFlowEmbeddingCaller,
    create_embedding_caller,
)


__all__ = [
    # LLM
    "DeepSeekLLMCaller",
    "MockLLMCaller",
    "create_llm_caller",
    "create_light_caller",
    "create_fallback_caller",
    # Embedding
    "DeepSeekEmbeddingCaller",
    "BGEEmbeddingCaller",
    "SiliconFlowEmbeddingCaller",
    "create_embedding_caller",
]
