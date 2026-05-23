"""shiyi-shell - 史佚外壳模块

职责：
- 提供LLM调用的具体实现
- 提供Embedding调用的具体实现
- 提供CLI命令行入口

核心组件：
- llm_caller: DeepSeek LLM调用实现
- embedding_caller: DeepSeek/BGE Embedding调用实现
"""

__version__ = "0.15.1"

from shiyi.shell.llm_caller import (
    LLMProvider,
    DeepSeekLLMCaller,
    MockLLMCaller,
    create_llm_caller,
)

from shiyi.shell.embedding_caller import (
    EmbeddingProvider,
    DeepSeekEmbeddingCaller,
    BGEEmbeddingCaller,
    create_embedding_caller,
)


__all__ = [
    # LLM
    "LLMProvider",
    "DeepSeekLLMCaller",
    "MockLLMCaller",
    "create_llm_caller",
    # Embedding
    "EmbeddingProvider",
    "DeepSeekEmbeddingCaller",
    "BGEEmbeddingCaller",
    "create_embedding_caller",
]
