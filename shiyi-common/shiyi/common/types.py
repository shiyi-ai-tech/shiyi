"""shiyi-common 类型定义

核心数据结构：
- Fragment: 记忆碎片
- EmotionShell/SceneShell/TimeShell/LifeShell: 四壳
- IntentResult/SubQuery: 意图解析
- ToolCall/ToolResult: 工具调用
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum


class EmotionLabel(str, Enum):
    """情感标签 - 不预设枚举，LLM自动命名，这里只是常用参考"""
    pass


@dataclass
class EmotionShell:
    """情感壳"""
    valence: float = 0.0       # -1.0(负) ~ +1.0(正)
    arousal: float = 0.0       # 0.0(平静) ~ 1.0(激动)
    primary: str = "中性"       # 主情感


@dataclass
class SceneShell:
    """场景壳"""
    domain: str = ""             # 场景领域
    participants: List[str] = field(default_factory=list)
    location: str = ""


@dataclass
class TimeShell:
    """时间壳"""
    created_at: str = ""              # 创建时间
    last_accessed_at: str = ""        # 最后访问时间
    referenced_time: str = ""         # 引用的时间点


@dataclass
class LifeShell:
    """生命壳"""
    energy: float = 1.0           # 能量 (1.0~0, 0=冷层)
    confidence: float = 1.0       # 置信度
    access_count: int = 0         # 访问次数
    forgotten: bool = False       # 是否被遗忘


@dataclass
class Fragment:
    """记忆碎片 - 核心数据结构"""
    id: str = ""
    fact_kernel: str = ""                          # 核心事实
    emotion_shell: EmotionShell = field(default_factory=EmotionShell)
    scene_shell: SceneShell = field(default_factory=SceneShell)
    time_shell: TimeShell = field(default_factory=TimeShell)
    life_shell: LifeShell = field(default_factory=LifeShell)
    reply_context: str = ""                        # AI回复中与此事实对应的部分
    linked_to: str = ""                            # 关联Fragment ID
    source_conversation_id: str = ""               # 来源对话ID
    embedding_model: str = ""                      # embedding模型名
    embedding_version: str = ""                    # embedding版本


class IntentType(str, Enum):
    """意图类型"""
    entity = "entity"
    fact = "fact"
    emotion = "emotion"
    recall = "recall"
    time = "time"
    mixed = "mixed"
    greeting = "greeting"
    action = "action"


@dataclass
class SubQuery:
    """子查询"""
    intent: str = ""
    query_rewrite: str = ""                     # 重写后的查询
    entities: List[str] = field(default_factory=list)
    temporal_hint: str = ""                     # 时间提示词
    temporal_value: str = ""                    # 时间值
    source: str = ""                            # 来源文本
    search_terms: List[str] = field(default_factory=list)


@dataclass
class IntentResult:
    """意图解析结果"""
    intent: IntentType = IntentType.mixed   # 主意图
    sub_queries: List[SubQuery] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    needs_retrieval: bool = True
    needs_kb: bool = False                  # 是否需要知识库
    needs_action: bool = False              # 是否需要工具调用
    is_followup: bool = False
    confidence: float = 1.0
    raw_output: str = ""                    # 原始LLM输出（v0.11.6新增）


@dataclass
class ToolCall:
    """工具调用"""
    tool_name: str = ""
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """工具执行结果"""
    tool_name: str = ""
    success: bool = False
    result: Optional[Any] = None
    error: str = ""


@dataclass
class ProviderConfig:
    """API提供商配置"""
    provider: str = ""                # 提供商名称
    model: str = ""                   # 模型名
    base_url: str = ""                # API地址
    api_key: str = ""                 # API Key
    capabilities: Dict[str, bool] = field(default_factory=dict)
