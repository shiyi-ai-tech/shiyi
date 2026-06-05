"""预审引擎 — 基于 flash LLM 的区分+拆分+情感判断

v260530 架构对齐：替代旧 IntentEngine。
职责：
- 输入：最近3~5轮对话上下文 + 当前用户消息
- 输出：PreReviewResult { need_retrieval, search_terms, emotion }
- 不做：意图分类、short-circuit 直接回复、Skill 路由
- 不降级：LLM 不可用时抛出 LLMUnavailableError

LLM: 写死 flash 模型（通过 LLMProvider 注入，默认 deepseek-v4-flash）
"""

import json
import logging
import os
from typing import List, Optional, Dict, Any

from shiyi.common.types import PreReviewResult
from shiyi.common.interfaces import LLMProvider
from shiyi.common.errors import LLMUnavailableError


logger = logging.getLogger(__name__)


# ═══ Flash LLM Prompt ═══

PRE_REVIEW_SYSTEM_PROMPT = """你是一个预审助手。你的工作是分析用户消息：区分是否需要检索记忆、拆分检索关键词、判断情感倾向。

规则：
1. 区分（need_retrieval）：
   - 闲聊/打招呼/纯呼应词（"你好""谢谢""好的"）→ false
   - 需要记忆或知识才能回答的 → true
   - 需要执行操作的 → true（关键词给下游路由用）
   - 不确定的 → true（宁可多搜不少搜）

2. 拆分（search_terms）：
   - 把用户消息拆成独立的关键词组，每个是一串搜索词
   - 保留核心实体和关系词
   - 不要改写、不要补全、不要回答问题
   - 例："我上次去的餐厅和住的酒店" → ["上次 餐厅", "上次 酒店"]

3. 情感（emotion）：
   - valence: 正面 ~1.0, 负面 ~-1.0, 中性 0
   - arousal: 激动 ~1.0, 平静 0
   - dominant: 中文情感标签（开心/难过/愤怒/焦虑/期待/困惑/中性...）

返回格式（严格 JSON，不要多余内容）：
{
  "need_retrieval": true,
  "search_terms": ["关键词1 关键词2", "关键词3"],
  "emotion": {"valence": 0.5, "arousal": 0.3, "dominant": "期待"}
}"""


class PreReviewEngine:
    """预审引擎 — 区分+拆分+情感（不做意图分类、不短路、不路由）"""

    def __init__(
        self,
        llm_provider: Optional[LLMProvider] = None,
        light_model: Optional[str] = None,
    ):
        """初始化预审引擎

        Args:
            llm_provider: LLM 调用提供者，依赖注入
            light_model: flash 模型名（默认: 环境变量 SHIYI_LIGHT_LLM_MODEL 或 deepseek-v4-flash）
        """
        self._llm = llm_provider
        self._use_llm = llm_provider is not None and llm_provider.is_available()

        if light_model is None:
            light_model = os.environ.get("SHIYI_LIGHT_LLM_MODEL", "deepseek-v4-flash")
        self._light_model = light_model

    def analyze(
        self,
        query: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> PreReviewResult:
        """分析用户消息

        Args:
            query: 用户输入文本
            history: 对话历史 [{"role": ..., "content": ...}, ...]

        Returns:
            PreReviewResult

        Raises:
            LLMUnavailableError: LLM 不可用时不降级，直接报错
        """
        if not self._use_llm:
            raise LLMUnavailableError(
                "预审需要 LLM 服务，但 LLM 当前不可用。"
                "请检查网络连接和 API 配置。"
            )

        try:
            result = self._analyze_with_llm(query, history)
            if result:
                return result
        except Exception as e:
            logger.warning(f"预审 LLM 调用失败: {e}")
            raise LLMUnavailableError(
                f"预审调用失败: {e}。请检查网络连接和 API 配置。"
            )

        raise LLMUnavailableError("预审返回空结果，请检查 LLM 服务配置。")

    def _analyze_with_llm(
        self,
        query: str,
        history: Optional[List[Dict[str, str]]],
    ) -> Optional[PreReviewResult]:
        """使用 flash LLM 执行预审"""
        if not self._llm:
            return None

        # 构建用户消息：最近 3~5 轮上下文 + 当前查询
        user_message = query
        if history and len(history) > 0:
            recent = history[-5:]
            history_text = "\n".join(
                f"{h.get('role', 'user')}: {h.get('content', '')}"
                for h in recent
            )
            user_message = (
                f"对话上下文：\n{history_text}\n\n"
                f"当前用户消息（请分析这条消息）：{query}"
            )

        messages = [
            {"role": "system", "content": PRE_REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        response = self._llm.chat(
            messages,
            model=self._light_model,
            temperature=0.3,
            max_tokens=500,
        )

        try:
            data = json.loads(response)
            emotion = data.get("emotion", {})
            return PreReviewResult(
                need_retrieval=data.get("need_retrieval", True),
                search_terms=data.get("search_terms", []),
                emotion_valence=emotion.get("valence", 0.0),
                emotion_arousal=emotion.get("arousal", 0.0),
                emotion_dominant=emotion.get("dominant", "中性"),
                raw_output=response,
            )
        except json.JSONDecodeError as e:
            logger.warning(f"预审 LLM 返回非 JSON: {e}, raw: {response[:200]}")
            return None
