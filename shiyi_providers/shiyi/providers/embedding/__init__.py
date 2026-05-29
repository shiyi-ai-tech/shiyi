"""shiyi.providers.embedding - Embedding调用实现

提供多种Embedding提供商的调用实现：
- DeepSeekEmbeddingCaller: DeepSeek Embedding API
- BGEEmbeddingCaller: 本地部署的BGE-M3服务
- SiliconFlowEmbeddingCaller: SiliconFlow BGE-M3 API

使用示例：
    >>> from shiyi.providers.embedding import create_embedding_caller
    >>> embedder = create_embedding_caller()
    >>> vector = embedder.embed("这是一段文本")
"""

from shiyi.providers.embedding.deepseek import DeepSeekEmbeddingCaller
from shiyi.providers.embedding.bge import BGEEmbeddingCaller, SiliconFlowEmbeddingCaller
from shiyi.providers.embedding.factory import create_embedding_caller

__all__ = [
    "DeepSeekEmbeddingCaller",
    "BGEEmbeddingCaller",
    "SiliconFlowEmbeddingCaller",
    "create_embedding_caller",
]
