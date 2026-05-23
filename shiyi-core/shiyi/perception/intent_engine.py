"""意图识别引擎 - 基于轻量LLM的意图识别+子查询拆分

职责：
- 替代死词表，基于轻量LLM做意图识别
- 复杂query拆成独立子查询
- 纯呼应词("好""嗯""行")→CHAT意图，不触发检索

注意：
- 轻量LLM写死deepseek-v4-flash
- 通过LLMProvider抽象接口调用（依赖注入）
- LLM调用失败时降级为关键词规则
"""

import json
import logging
import re
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

from shiyi.common.types import SubQuery, IntentType as CommonIntentType


logger = logging.getLogger(__name__)


# ═══ 前向引用LLMProvider ═══

class LLMProvider:
    """LLM调用抽象接口 - 前向引用避免循环导入"""
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "deepseek-v4-flash",
        temperature: float = 0.7,
        max_tokens: int = 2000,
    ) -> str:
        raise NotImplementedError()
    
    def is_available(self) -> bool:
        raise NotImplementedError()


class IntentType(str):
    """意图类型枚举"""
    QUERY = "query"          # 查询类：问事实、问信息
    CHAT = "chat"            # 闲聊类：打招呼、情感交流、纯呼应
    ACTION = "action"        # 行动类：执行操作、工具调用
    CONFIRM = "confirm"      # 确认类：询问是否、确认信息
    CLARIFY = "clarify"      # 澄清类：追问细节
    EMOTION = "emotion"      # 情感类：表达情绪
    RECALL = "recall"        # 回忆类：询问记忆、过去的事
    TIME = "time"            # 时间类：询问时间、日期
    MIXED = "mixed"          # 混合类：多意图混合


@dataclass
class IntentResult:
    """意图解析结果"""
    intent: IntentType = IntentType.MIXED      # 主意图
    sub_queries: List[SubQuery] = field(default_factory=list)  # 子查询列表
    entities: List[str] = field(default_factory=list)          # 识别的实体
    needs_retrieval: bool = True                                 # 是否需要检索
    is_followup: bool = False                                   # 是否是追问
    confidence: float = 1.0                                      # 置信度
    raw_output: str = ""                                        # 原始LLM输出


# ═══ 降级规则匹配 ═══

# 闲聊/呼应词
CHAT_PATTERNS = [
    r'^(好|嗯|行|是|啊|哦|哈|哎|噢|哇|嗯嗯|好好|哈哈|对对|是是|行行|ok|OK|好嘞|好哒|好的)$',
    r'^(你好|您好|嗨|hi|hello|hi there)',
    r'^(谢谢|感谢|多谢|谢啦)',
    r'^(再见|拜拜|下次见|回见)',
]

# 查询类关键词
QUERY_PATTERNS = [
    r'(什么|哪|谁|怎么|如何|为什么|多少|几)',
    r'(是什么|在哪里|是谁|怎么用|怎么做|为什么是)',
    r'(帮我找|帮我查|帮我看看)',
]

# 回忆类关键词
RECALL_PATTERNS = [
    r'(上次|上次去|上次说|上次做的)',
    r'(之前|以前|曾经|记得|回忆)',
    r'(我之前|我上次|我曾经|我记得)',
]

# 时间类关键词
TIME_PATTERNS = [
    r'(什么时候|几点|几点钟|日期|几号|星期)',
    r'(今天|明天|昨天|后天|前天)',
    r'(今年|去年|明年|这周|下周)',
]

# 行动类关键词
ACTION_PATTERNS = [
    r'(帮我|请帮我|能不能帮我)',
    r'(生成|创建|打开|关闭|发送|运行|执行)',
    r'(写|编|做|搞|弄)',
]


class IntentEngine:
    """意图识别引擎"""
    
    def __init__(self, llm_provider: Optional[LLMProvider] = None, light_model: Optional[str] = None):
        """初始化意图引擎
        
        Args:
            llm_provider: LLM调用提供者，通过依赖注入传入
            light_model: 轻量LLM模型名（默认: 环境变量SHIYI_LIGHT_LLM_MODEL 或 deepseek-v4-flash）
        """
        import os
        self._llm = llm_provider
        self._use_llm = llm_provider is not None and llm_provider.is_available()
        
        # 轻量LLM模型选择：显式参数 > 环境变量 > 配置默认
        if light_model is None:
            light_model = os.environ.get("SHIYI_LIGHT_LLM_MODEL", "deepseek-v4-flash")
        self._light_model = light_model
    
    def analyze(
        self,
        query: str,
        history: Optional[List[Dict[str, str]]] = None,
        recent_intents: Optional[List[IntentResult]] = None,
    ) -> IntentResult:
        """分析用户输入的意图
        
        Args:
            query: 用户输入文本
            history: 对话历史 [[role, content], ...]
            recent_intents: 最近几次的意图结果
            
        Returns:
            IntentResult 意图分析结果
        """
        # 1. 检查纯呼应词
        if self._is_echo_word(query):
            return IntentResult(
                intent=IntentType.CHAT,
                needs_retrieval=False,
                is_followup=False,
                confidence=1.0,
            )
        
        # 2. 尝试使用LLM
        if self._use_llm:
            try:
                result = self._analyze_with_llm(query, history, recent_intents)
                if result:
                    return result
            except Exception as e:
                logger.warning(f"LLM意图识别失败，降级为规则匹配: {e}")
        
        # 3. 降级为规则匹配
        return self._analyze_with_rules(query, history, recent_intents)
    
    @staticmethod
    def _is_echo_word(text: str) -> bool:
        """判断是否为纯呼应词"""
        ECHO_WORDS = {
            "好", "嗯", "行", "是", "啊", "哦", "哈", "哎", "噢", "哇",
            "嗯嗯", "好好", "哈哈", "对对", "是是", "行行", "ok", "OK",
            "好嘞", "好哒", "好的", "收到", "了解", "知道了", "明白",
        }
        cleaned = text.strip()
        return cleaned in ECHO_WORDS
    
    def _analyze_with_llm(
        self,
        query: str,
        history: Optional[List[Dict[str, str]]],
        recent_intents: Optional[List[IntentResult]],
    ) -> Optional[IntentResult]:
        """使用LLM进行意图识别"""
        if not self._llm:
            return None
        
        # 构建prompt
        system_prompt = """你是一个中文对话意图识别助手。请分析用户输入，返回JSON格式的意图分析结果。

意图类型说明：
- query: 查询类，问事实、问信息、问知识
- chat: 闲聊类，打招呼、情感交流、纯呼应词
- action: 行动类，执行操作、工具调用
- confirm: 确认类，询问是否、确认信息
- clarify: 澄清类，追问细节
- emotion: 情感类，表达情绪
- recall: 回忆类，询问记忆、过去的事
- time: 时间类，询问时间、日期
- mixed: 混合类，多意图混合

子查询拆分规则：
- 如果用户提到多个事物（如"我上次去的餐厅和住的酒店"），拆成独立子查询
- 每个子查询包含意图和重写后的查询

返回格式（严格JSON）：
{
    "intent": "意图类型",
    "sub_queries": [
        {
            "intent": "子意图类型",
            "query_rewrite": "重写后的查询",
            "entities": ["实体1", "实体2"],
            "temporal_hint": "时间提示词",
            "source": "来自用户输入的哪部分"
        }
    ],
    "entities": ["识别的实体列表"],
    "needs_retrieval": true或false,
    "is_followup": true或false,
    "confidence": 0.0到1.0之间的置信度
}

注意：
- 纯呼应词("好""嗯""行")返回chat意图，needs_retrieval=false
- 如果是回忆类查询，intent用recall
- 如果需要检索记忆，needs_retrieval=true"""

        # 构建用户消息
        user_message = f"[INTENT_ANALYSIS] {query}"
        
        # 添加历史上下文（如果有）
        if history and len(history) > 0:
            history_text = "\n".join([f"{h.get('role', 'user')}: {h.get('content', '')}" for h in history[-3:]])
            user_message = f"对话历史：\n{history_text}\n\n当前输入：[INTENT_ANALYSIS] {query}"
        
        # 调用LLM
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        
        response = self._llm.chat(messages, model=self._light_model, temperature=0.3, max_tokens=1000)
        
        # 解析JSON
        try:
            data = json.loads(response)
            
            # 转换子查询
            sub_queries = []
            for sq in data.get("sub_queries", []):
                sub_queries.append(SubQuery(
                    intent=sq.get("intent", ""),
                    query_rewrite=sq.get("query_rewrite", ""),
                    entities=sq.get("entities", []),
                    temporal_hint=sq.get("temporal_hint", ""),
                    source=sq.get("source", ""),
                ))
            
            # 判断是否是追问
            is_followup = data.get("is_followup", False)
            if recent_intents and len(recent_intents) > 0:
                last_intent = recent_intents[-1]
                if last_intent.intent == IntentType.QUERY or last_intent.intent == IntentType.RECALL:
                    is_followup = True
            
            return IntentResult(
                intent=IntentType(data.get("intent", "mixed")),
                sub_queries=sub_queries,
                entities=data.get("entities", []),
                needs_retrieval=data.get("needs_retrieval", True),
                is_followup=is_followup,
                confidence=data.get("confidence", 0.8),
                raw_output=response,
            )
        except json.JSONDecodeError as e:
            logger.warning(f"LLM返回非JSON格式，降级为规则: {e}, response: {response[:200]}")
            return None
    
    def _analyze_with_rules(
        self,
        query: str,
        history: Optional[List[Dict[str, str]]],
        recent_intents: Optional[List[IntentResult]],
    ) -> IntentResult:
        """使用规则进行意图识别（降级方案）"""
        # 1. 检查闲聊模式
        for pattern in CHAT_PATTERNS:
            if re.search(pattern, query.strip(), re.IGNORECASE):
                return IntentResult(
                    intent=IntentType.CHAT,
                    needs_retrieval=False,
                    is_followup=False,
                    confidence=0.9,
                )
        
        # 2. 检查回忆模式
        for pattern in RECALL_PATTERNS:
            if re.search(pattern, query):
                sub_queries = self._split_sub_queries(query)
                return IntentResult(
                    intent=IntentType.RECALL,
                    sub_queries=sub_queries,
                    needs_retrieval=True,
                    is_followup=self._check_followup(recent_intents),
                    confidence=0.85,
                )
        
        # 3. 检查时间模式
        for pattern in TIME_PATTERNS:
            if re.search(pattern, query):
                return IntentResult(
                    intent=IntentType.TIME,
                    needs_retrieval=True,
                    is_followup=self._check_followup(recent_intents),
                    confidence=0.8,
                )
        
        # 4. 检查行动模式
        for pattern in ACTION_PATTERNS:
            if re.search(pattern, query):
                return IntentResult(
                    intent=IntentType.ACTION,
                    needs_retrieval=True,
                    is_followup=False,
                    confidence=0.85,
                )
        
        # 5. 检查查询模式
        for pattern in QUERY_PATTERNS:
            if re.search(pattern, query):
                sub_queries = self._split_sub_queries(query)
                return IntentResult(
                    intent=IntentType.QUERY,
                    sub_queries=sub_queries,
                    needs_retrieval=True,
                    is_followup=self._check_followup(recent_intents),
                    confidence=0.8,
                )
        
        # 6. 默认作为闲聊
        return IntentResult(
            intent=IntentType.CHAT,
            needs_retrieval=False,
            is_followup=False,
            confidence=0.6,
        )
    
    def _split_sub_queries(self, query: str) -> List[SubQuery]:
        """拆分复杂query为子查询
        
        例如："我上次去的餐厅和住的酒店"
        → ["我上次去的餐厅", "住的酒店"]
        """
        # 连接词模式
        connectors = ['和', '与', '以及', '还有', '、', '，', ',']
        
        # 检查是否含连接词
        has_connector = False
        for conn in connectors:
            if conn in query:
                has_connector = True
                break
        
        if not has_connector:
            # 无连接词，返回单个子查询
            return [SubQuery(
                intent="query",
                query_rewrite=query,
                entities=[],
                source=query,
            )]
        
        # 按连接词拆分
        parts = query
        for conn in connectors:
            parts = parts.replace(conn, '|||SPLIT|||')
        
        sub_parts = [p.strip() for p in parts.split('|||SPLIT|||') if p.strip()]
        
        sub_queries = []
        for part in sub_parts:
            sub_queries.append(SubQuery(
                intent="query",
                query_rewrite=part,
                entities=[],
                source=part,
            ))
        
        return sub_queries if sub_queries else [SubQuery(
            intent="query",
            query_rewrite=query,
            entities=[],
            source=query,
        )]
    
    def _check_followup(self, recent_intents: Optional[List[IntentResult]]) -> bool:
        """检查是否是追问"""
        if not recent_intents or len(recent_intents) == 0:
            return False
        
        last_intent = recent_intents[-1]
        # 如果上一轮是查询类，很可能是追问
        if last_intent.intent in [IntentType.QUERY, IntentType.RECALL, IntentType.TIME]:
            return True
        
        return False
