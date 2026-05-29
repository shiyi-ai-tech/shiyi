"""shell层 - Embedding调用实现（兼容层）

⚠️ 已迁移至 shiyi-providers 包

本文件仅保留用于向后兼容，实际实现已移至：
    shiyi.providers.embedding

请使用新的导入方式：
    from shiyi.providers.embedding import create_embedding_caller
"""

# 重新导出所有符号以保持兼容性
from shiyi.providers.embedding import (
    DeepSeekEmbeddingCaller,
    BGEEmbeddingCaller,
    SiliconFlowEmbeddingCaller,
    create_embedding_caller,
)

__all__ = [
    "DeepSeekEmbeddingCaller",
    "BGEEmbeddingCaller",
    "SiliconFlowEmbeddingCaller",
    "create_embedding_caller",
]
