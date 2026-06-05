"""感知层 - 原始输入标准化

职责：
- 输入清洗：去特殊字符、截断超长、安全检测
"""

import re
from dataclasses import dataclass, field
from typing import Optional, Dict, Any


@dataclass
class NormalizedInput:
    """标准化后的输入"""
    original_text: str                    # 原始输入
    normalized_text: str                  # 清洗后的文本
    session_context: Dict[str, Any] = field(default_factory=dict)  # 会话上下文
    metadata: Dict[str, Any] = field(default_factory=dict)         # 其他元数据


# ═══ 特殊字符清洗规则 ═══

# 需移除的控制字符
CONTROL_CHARS = re.compile(
    r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'
)

# 需规范化的空白字符
WHITESPACE = re.compile(r'\s+')


def normalize(
    raw_message: str,
    conversation_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> NormalizedInput:
    """标准化原始输入
    
    Args:
        raw_message: 原始消息
        conversation_id: 会话ID
        metadata: 附加元数据
        
    Returns:
        NormalizedInput 标准化后的输入
    """
    # 1. 安全检测 - 空消息处理
    if not raw_message or not raw_message.strip():
        return NormalizedInput(
            original_text=raw_message or "",
            normalized_text="",
            session_context={"conversation_id": conversation_id},
            metadata=metadata or {},
        )
    
    original = raw_message
    
    # 2. 清洗特殊字符
    cleaned = _clean_text(original)
    
    # 3. 截断超长文本
    if len(cleaned) > 2000:
        cleaned = cleaned[:2000]
    
    # 4. 构建上下文
    context = {
        "conversation_id": conversation_id,
        "original_length": len(original),
        "cleaned_length": len(cleaned),
    }
    
    return NormalizedInput(
        original_text=original,
        normalized_text=cleaned,
        session_context=context,
        metadata=metadata or {},
    )


def _clean_text(text: str) -> str:
    """清洗文本：去除控制字符、规范空白"""
    # 移除控制字符
    text = CONTROL_CHARS.sub('', text)
    # 规范化空白字符为空格
    text = WHITESPACE.sub(' ', text)
    # 去除首尾空白
    text = text.strip()
    return text
