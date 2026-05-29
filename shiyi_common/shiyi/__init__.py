"""shiyi-common - 公共类型定义"""
__version__ = "260526.0"

from shiyi.common.types import (
    Fragment, EmotionShell, SceneShell, TimeShell, LifeShell,
    IntentType, SubQuery, IntentResult,
    ToolCall, ToolResult, ProviderConfig,
    EmotionLabel,
)
from shiyi.common.interfaces import (
    LLMProvider, EmbeddingProvider, KnowledgeBaseAdapter,
)
from shiyi.common.errors import (
    ShiyiError, ConfigError, StorageError, LLMError,
)

__all__ = [
    "Fragment", "EmotionShell", "SceneShell", "TimeShell", "LifeShell",
    "IntentType", "SubQuery", "IntentResult",
    "ToolCall", "ToolResult", "ProviderConfig",
    "EmotionLabel",
    "LLMProvider", "EmbeddingProvider", "KnowledgeBaseAdapter",
    "ShiyiError", "ConfigError", "StorageError", "LLMError",
]
