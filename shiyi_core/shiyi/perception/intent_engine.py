"""意图识别引擎 - 基于轻量LLM的意图识别+子查询拆分

职责：
- 基于轻量LLM做意图识别
- 复杂query拆成独立子查询

注意：
- 轻量LLM写死deepseek-v4-flash
- 通过LLMProvider抽象接口调用（依赖注入）
- LLM不可用时抛出LLMUnavailableError，明确报错引导用户解决
"""

import json
import logging
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

from shiyi.common.types import SubQuery, IntentType, IntentResult
from shiyi.common.interfaces import LLMProvider
from shiyi.common.errors import LLMUnavailableError


logger = logging.getLogger(__name__)


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
            
        Raises:
            LLMUnavailableError: LLM服务不可用时抛出
        """
        # LLM不可用时直接报错，不降级
        if not self._use_llm:
            raise LLMUnavailableError(
                "意图识别需要LLM服务，但LLM当前不可用。"
                "请检查网络连接和API配置（DEEPSEEK_API_KEY或SILICONFLOW_API_KEY）。"
            )
        
        # 使用LLM进行意图识别
        try:
            result = self._analyze_with_llm(query, history, recent_intents)
            if result:
                return result
        except Exception as e:
            logger.warning(f"LLM意图识别失败: {e}")
            raise LLMUnavailableError(
                f"意图识别调用失败: {e}。"
                "请检查网络连接和API配置。"
            )
        
        # LLM返回空结果时也报错
        raise LLMUnavailableError(
            "意图识别返回空结果，请检查LLM服务配置。"
        )
    
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
                if last_intent.intent in (IntentType.query, IntentType.recall):
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
            logger.warning(f"LLM返回非JSON格式: {e}, response: {response[:200]}")
            return None
