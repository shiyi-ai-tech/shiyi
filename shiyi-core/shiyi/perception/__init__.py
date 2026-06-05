"""感知层 - Perception 模块

职责：
- 原始输入标准化（Perception）
- 预审（PreReviewEngine）— 区分+拆分+情感判断
- 意图识别（IntentEngine）— 旧接口，保留兼容
- 对话历史管理（ConversationManager）

子模块：
- normalizer: 输入清洗
- pre_review_engine: 基于 flash LLM 的预审（v260530 新架构）
- intent_engine: 旧意图识别引擎（保留兼容）
- conversation: 对话历史持久化+滑动窗口
"""

from shiyi.perception.normalizer import (
    NormalizedInput,
    normalize,
)

from shiyi.common.types import PreReviewResult

from shiyi.perception.pre_review_engine import (
    PreReviewEngine,
)

from shiyi.perception.conversation import (
    Message,
    ConversationManager,
)


__all__ = [
    # normalizer
    "NormalizedInput",
    "normalize",
    # pre_review_engine (v260530)
    "PreReviewEngine",
    "PreReviewResult",
    # conversation
    "Message",
    "ConversationManager",
]
