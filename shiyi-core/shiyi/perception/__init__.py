"""感知层 - Perception 模块

职责：
- 原始输入标准化（Perception）
- 意图识别（IntentEngine）
- 对话历史管理（ConversationManager）

子模块：
- normalizer: 输入清洗、复杂度预判、工具调用检测
- intent_engine: 基于LLM的意图识别+子查询拆分
- conversation: 对话历史持久化+滑动窗口
"""

from shiyi.perception.normalizer import (
    Complexity,
    NormalizedInput,
    normalize,
    is_echo_word,
)

from shiyi.perception.intent_engine import (
    IntentType,
    IntentEngine,
    IntentResult,
)

from shiyi.perception.conversation import (
    Message,
    ConversationManager,
)


__all__ = [
    # normalizer
    "Complexity",
    "NormalizedInput",
    "normalize",
    "is_echo_word",
    # intent_engine
    "IntentType",
    "IntentEngine",
    "IntentResult",
    # conversation
    "Message",
    "ConversationManager",
]
