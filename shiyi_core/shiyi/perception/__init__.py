"""感知层 - Perception 模块

职责：
- 原始输入标准化（Perception）
- 意图识别（IntentEngine）
- 对话历史管理（ConversationManager）

子模块：
- normalizer: 输入清洗
- intent_engine: 基于LLM的意图识别+子查询拆分
- conversation: 对话历史持久化+滑动窗口
"""

from shiyi.perception.normalizer import (
    NormalizedInput,
    normalize,
)

from shiyi.common.types import IntentType, IntentResult, SubQuery

from shiyi.perception.intent_engine import (
    IntentEngine,
)

from shiyi.perception.conversation import (
    Message,
    ConversationManager,
)


__all__ = [
    # normalizer
    "NormalizedInput",
    "normalize",
    # intent_engine
    "IntentType",
    "IntentEngine",
    "IntentResult",
    # conversation
    "Message",
    "ConversationManager",
]
