"""shiyi.providers - LLM/Embedding/MCP Provider包

顶层导出：
- LLMProvider: LLM调用抽象接口
- EmbeddingProvider: Embedding调用抽象接口
- MCPProvider: MCP工具调用接口

创建器函数：
- create_llm_caller(): 创建默认LLM调用器
- create_light_caller(): 创建加速模型调用器
- create_fallback_caller(): 创建备用模型调用器
- create_embedding_caller(): 创建Embedding调用器

模型实现：
- DeepSeekLLMCaller: DeepSeek LLM调用
- DeepSeekEmbeddingCaller: DeepSeek Embedding调用
- BGEEmbeddingCaller: 本地BGE-M3 Embedding
- SiliconFlowEmbeddingCaller: SiliconFlow BGE-M3 Embedding

使用示例：
    >>> from shiyi.providers import create_llm_caller
    >>> llm = create_llm_caller()
    >>> response = llm.chat([{"role": "user", "content": "你好"}])
"""

from shiyi.common.interfaces import LLMProvider, EmbeddingProvider

# LLM 模块
from shiyi.providers.llm import (
    DeepSeekLLMCaller,
    MockLLMCaller,
    create_llm_caller,
    create_light_caller,
    create_fallback_caller,
)

# Embedding 模块
from shiyi.providers.embedding import (
    DeepSeekEmbeddingCaller,
    BGEEmbeddingCaller,
    SiliconFlowEmbeddingCaller,
    create_embedding_caller,
)

# MCP 模块
from shiyi.providers.mcp import (
    ToolDefinition,
    ToolResult,
    MCPProvider,
    SimpleToolRegistry,
)

__version__ = "0.1.0"

__all__ = [
    # 接口
    "LLMProvider",
    "EmbeddingProvider",
    "MCPProvider",
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
    # MCP
    "ToolDefinition",
    "ToolResult",
    "SimpleToolRegistry",
]
