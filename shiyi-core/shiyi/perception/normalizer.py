"""感知层 - 原始输入标准化

职责：
- 输入清洗：去特殊字符、截断超长、安全检测
- 工具调用检测
- 复杂度预判：TRIVIAL/SIMPLE/NORMAL/COMPLEX

复杂度规则：
- 空消息 → TRIVIAL
- 单词/呼应词 → SIMPLE
- 正常对话 → NORMAL
- 超长>1k字 → COMPLEX
"""

import re
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any


class Complexity(str, Enum):
    """消息复杂度级别"""
    TRIVIAL = "trivial"   # 空消息、纯标点
    SIMPLE = "simple"     # 单词/呼应词
    NORMAL = "normal"     # 正常对话
    COMPLEX = "complex"   # 超长/多问题/技术内容


@dataclass
class NormalizedInput:
    """标准化后的输入"""
    original_text: str                    # 原始输入
    normalized_text: str                  # 清洗后的文本
    complexity: Complexity                 # 复杂度级别
    has_tool_call: bool                   # 是否含工具调用信号
    session_context: Dict[str, Any] = field(default_factory=dict)  # 会话上下文
    metadata: Dict[str, Any] = field(default_factory=dict)         # 其他元数据


# ═══ 特殊字符清洗规则 ═══

# 需移除的控制字符
CONTROL_CHARS = re.compile(
    r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]'
)

# 需规范化的空白字符
WHITESPACE = re.compile(r'\s+')

# 工具调用信号词
TOOL_SIGNALS = [
    "帮我搜", "查一下", "搜索", "生成", "创建文件", "写代码",
    "运行", "执行", "发送", "下载", "帮我查", "找一下", "检索",
    "调用", "打开", "关闭", "计算", "分析", "翻译", "总结",
    "帮我", "请帮我", "能不能帮我",
]

# 纯呼应词列表
ECHO_WORDS = {
    "好", "嗯", "行", "是", "啊", "哦", "哈", "哎", "噢", "哇",
    "嗯嗯", "好好", "哈哈", "对对", "是是", "行行", "ok", "OK",
    "好嘞", "好哒", "好的", "收到", "了解", "知道了", "明白",
}

# 复杂度判断关键词
COMPLEX_SIGNALS = {
    "代码", "程序", "开发", "bug", "调试", "接口", "函数",
    "算法", "数据库", "python", "javascript", "java", "golang",
    "架构", "设计模式", "重构", "部署", "编译", "为什么", "怎么",
    "如何", "分析", "对比", "原因", "方案", "规划", "实现",
    "原理", "解释", "区别", "哪个", "什么",
}


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
            complexity=Complexity.TRIVIAL,
            has_tool_call=False,
            session_context={"conversation_id": conversation_id},
            metadata=metadata or {},
        )
    
    original = raw_message
    
    # 2. 清洗特殊字符
    cleaned = _clean_text(original)
    
    # 3. 截断超长文本
    if len(cleaned) > 2000:
        cleaned = cleaned[:2000]
    
    # 4. 复杂度预判
    complexity = _judge_complexity(cleaned, original)
    
    # 5. 工具调用检测
    has_tool = _has_tool_signal(cleaned)
    
    # 6. 构建上下文
    context = {
        "conversation_id": conversation_id,
        "original_length": len(original),
        "cleaned_length": len(cleaned),
    }
    
    return NormalizedInput(
        original_text=original,
        normalized_text=cleaned,
        complexity=complexity,
        has_tool_call=has_tool,
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


def _judge_complexity(cleaned: str, original: str) -> Complexity:
    """判断消息复杂度
    
    规则：
    1. 空消息 → TRIVIAL
    2. 单词/纯呼应词 → SIMPLE  
    3. 超长>1k字 → COMPLEX
    4. 含复杂信号词 → COMPLEX
    5. 多问句 → COMPLEX
    6. 其他 → NORMAL
    """
    # 空消息
    if not cleaned:
        return Complexity.TRIVIAL
    
    # 超长消息
    if len(cleaned) > 1000:
        return Complexity.COMPLEX
    
    # 单词或呼应词
    stripped = cleaned.strip()
    word_count = len(stripped)
    
    # 单字符或短呼应词
    if word_count <= 2:
        return Complexity.SIMPLE
    
    # 纯呼应词
    if stripped in ECHO_WORDS:
        return Complexity.SIMPLE
    
    # 纯标点/纯符号（含中英文标点、特殊符号、数学符号等，不含任何字母数字或汉字）
    if re.match(r'^[\s\W]+$', stripped) and not re.search(r'[\w]', stripped):
        return Complexity.TRIVIAL
    
    # 复杂信号词检测
    for signal in COMPLEX_SIGNALS:
        if signal in stripped:
            return Complexity.COMPLEX
    
    # 多问句检测 (>2个问号或问句)
    question_count = stripped.count('？') + stripped.count('?')
    if question_count >= 2:
        return Complexity.COMPLEX
    
    # 多句号检测
    sentence_count = stripped.count('。') + stripped.count('；') + stripped.count(';')
    if sentence_count >= 5:
        return Complexity.COMPLEX
    
    # 默认正常复杂度
    return Complexity.NORMAL


def _has_tool_signal(text: str) -> bool:
    """检测是否含工具调用信号"""
    for signal in TOOL_SIGNALS:
        if signal in text:
            return True
    return False


def is_echo_word(text: str) -> bool:
    """判断是否为纯呼应词"""
    cleaned = text.strip()
    return cleaned in ECHO_WORDS
