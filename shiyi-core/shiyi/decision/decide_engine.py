"""
DecideEngine - 决策引擎

v260530 架构对齐后：
1. 预审 → PreReviewEngine（区分+拆分+情感）
2. 检索 → MemoryEngine.recall(query) + VectorSearch
3. 排序 → 四壳权重 × 扩散（Phase 2-4）
4. 组装 → PromptAssembler（纯本地）
5. 主 LLM → 回复 + 记忆提取 + Skill 路由

LLM 调用次数：2 次
- 1 次 flash（预审：区分+拆分+情感）
- 1 次 pro（主回复+记忆提取+Skill路由）

降级策略：无。LLM 不可用时明确报错。
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple, Generator

from shiyi.common.interfaces import LLMProvider, EmbeddingProvider
from shiyi.common.types import PreReviewResult, Fragment, EmotionShell, SceneShell, TimeShell, LifeShell
from shiyi.common.errors import LLMUnavailableError
from shiyi.memory.engine import MemoryEngine
from shiyi.perception.pre_review_engine import PreReviewEngine
from shiyi.perception.conversation import ConversationManager

from shiyi.decision.prompt_assembler import PromptAssembler
from shiyi.decision.fragment_extractor import FragmentExtractor
from shiyi.decision.vector_search import VectorSearch
from shiyi.common.constants import DEFAULT_MAIN_LLM_MODEL


logger = logging.getLogger(__name__)


class DecideResult:
    """决策结果"""

    def __init__(
        self,
        reply: str,
        fragments: List[Dict[str, Any]],
        emotion_tag: str,
        retrieval_count: int,
        llm_used: bool = True,
        tool_call: Optional[Dict[str, Any]] = None,
    ):
        self.reply = reply
        self.fragments = fragments
        self.emotion_tag = emotion_tag
        self.retrieval_count = retrieval_count
        self.llm_used = llm_used
        self.tool_call = tool_call


class DecideEngine:
    """决策引擎"""

    def __init__(
        self,
        memory_engine: MemoryEngine,
        pre_review_engine: PreReviewEngine,
        conversation_manager: ConversationManager,
        llm_provider: Optional[LLMProvider] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        vector_index: Optional[Any] = None,
        top_k: int = 10,
        main_model: Optional[str] = None,
        max_tokens: int = 4096,
    ):
        self._memory = memory_engine
        self._pre_review = pre_review_engine
        self._conversation = conversation_manager
        self._llm = llm_provider
        self._embedding = embedding_provider
        self._vector_index = vector_index
        self._top_k = top_k
        self._max_tokens = max_tokens

        if main_model is None:
            main_model = os.environ.get("SHIYI_MAIN_LLM_MODEL", DEFAULT_MAIN_LLM_MODEL)
        self._main_model = main_model

        self._prompt_assembler = PromptAssembler()
        self._fragment_extractor = FragmentExtractor()
        self._vector_search = VectorSearch(
            embedding_provider=embedding_provider,
            vector_index=vector_index,
        )

    @property
    def embedding_available(self) -> bool:
        return self._vector_search.is_available

    def _prepare_context(
        self,
        query: str,
        conversation_id: str,
        normalized_text: Optional[str] = None,
        pre_result=None,
        skip_retrieval: bool = False,
        cached_fragments: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, PreReviewResult, List[Dict[str, Any]], List[Dict[str, str]]]:
        normalized = normalized_text or query

        # 1. 预审（flash LLM）
        history = self._conversation.get_history_for_llm(conversation_id, max_turns=5)
        if pre_result is None:
            pre_result = self._pre_review.analyze(
                query=normalized,
                history=history,
            )

        # 2. 记忆检索 + 向量搜索 + 融合去重
        if skip_retrieval and cached_fragments is not None:
            fragments = cached_fragments
        else:
            fragments = self._recall(pre_result)
            if self._vector_search.is_available:
                vector_results = self._vector_search.search(query, top_k=self._top_k)
                fragments = self._merge_results(fragments, vector_results)
            fragments = fragments[:self._top_k]

        return normalized, pre_result, fragments, history

    def _check_llm_available(self) -> None:
        if not (self._llm is not None and self._llm.is_available()):
            raise LLMUnavailableError(
                "LLM服务不可用，对话无法进行。"
                "请检查网络连接和API配置（DEEPSEEK_API_KEY或SILICONFLOW_API_KEY）。"
            )

    def decide(
        self,
        query: str,
        conversation_id: str,
        normalized_text: Optional[str] = None,
        pre_result=None,
        tools: Optional[List[Dict[str, Any]]] = None,
        messages_extra: Optional[List[Dict[str, Any]]] = None,
        platform_context: str = "",
        skip_retrieval: bool = False,
        cached_fragments: Optional[List[Dict[str, Any]]] = None,
    ) -> DecideResult:
        normalized, pre_result, fragments, history = self._prepare_context(
            query, conversation_id, normalized_text, pre_result,
            skip_retrieval=skip_retrieval, cached_fragments=cached_fragments,
        )

        self._check_llm_available()
        return self._decide_with_llm(
            query=query,
            normalized=normalized,
            pre_result=pre_result,
            fragments=fragments,
            history=history,
            conversation_id=conversation_id,
            tools=tools,
            messages_extra=messages_extra,
            platform_context=platform_context,
        )

    def decide_stream(
        self,
        query: str,
        conversation_id: str,
        normalized_text: Optional[str] = None,
        pre_result=None,
        platform_context: str = "",
    ) -> Generator[Tuple[str, Any], None, None]:
        normalized, pre_result, fragments, history = self._prepare_context(
            query, conversation_id, normalized_text, pre_result
        )

        self._check_llm_available()

        messages = self._prompt_assembler.assemble(
            pre_result=pre_result,
            fragments=fragments,
            conversation_history=history,
            query=query,
            platform_context=platform_context,
        )

        buffer = ""
        try:
            for token in self._llm.stream_chat(
                messages=messages,
                model=self._main_model,
                temperature=0.7,
                max_tokens=self._max_tokens,
            ):
                buffer += token
                yield ("token", token)
        except Exception as e:
            logger.error(f"流式LLM调用失败: {e}")
            raise LLMUnavailableError(
                f"流式LLM调用失败: {e}。请检查网络连接和API配置后重试。"
            )

        reply = self._fragment_extractor.extract_reply_only(buffer)
        extracted_fragments = self._fragment_extractor.extract(buffer)

        stored_count = 0
        for f in extracted_fragments:
            try:
                fragment_id = self._store_fragment(
                    fact_kernel=f.get("fact_kernel", ""),
                    emotion_shell=f.get("emotion_shell", {}),
                    scene_shell=f.get("scene_shell", {}),
                    linked_to=f.get("linked_to", ""),
                    conversation_id=conversation_id,
                )
                if fragment_id:
                    stored_count += 1
            except Exception as e:
                logger.warning(f"Failed to store fragment: {e}")

        yield ("done", DecideResult(
            reply=reply,
            fragments=extracted_fragments,
            emotion_tag=str(pre_result.emotion_dominant) if pre_result and pre_result.emotion_dominant else "",
            retrieval_count=len(fragments),
            llm_used=True,
        ))

    def _recall(
        self,
        pre_result: PreReviewResult,
    ) -> List[Dict[str, Any]]:
        results = []

        if not pre_result.need_retrieval:
            return results

        queries = pre_result.search_terms if pre_result.search_terms else [""]
        queries = [q for q in queries if q.strip()]

        # 有检索需求且有关键词时启用深度检索（冷层向量搜索）
        deep_search = bool(queries)

        for q in queries:
            try:
                query_vector = None
                if self._vector_search.is_available:
                    try:
                        query_vector = self._embedding.embed(q)
                    except Exception:
                        pass

                recall_results = self._memory.recall(
                    q,
                    top_k=self._top_k,
                    query_vector=query_vector,
                    deep=deep_search,
                )
                results.extend(recall_results)

                for r in recall_results:
                    if r.get("fragment"):
                        self._memory.refresh(r["fragment"].id)
            except Exception as e:
                logger.warning(f"Memory recall error: {e}")

        return results

    def _merge_results(
        self,
        recall_results: List[Dict[str, Any]],
        vector_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        seen_ids = set()
        merged = []

        for r in recall_results:
            fid = r.get("fragment", {}).id if isinstance(r.get("fragment"), Fragment) else r.get("id", "")
            if fid and fid not in seen_ids:
                seen_ids.add(fid)
                merged.append({
                    "id": fid,
                    "fact_kernel": r.get("fragment", {}).fact_kernel if isinstance(r.get("fragment"), Fragment) else r.get("fact_kernel", ""),
                    "score": r.get("score", 0),
                    "source": r.get("source", "recall"),
                })

        for r in vector_results:
            fid = r.get("fragment_id", "")
            if fid and fid not in seen_ids:
                seen_ids.add(fid)
                merged.append({
                    "id": fid,
                    "fact_kernel": r.get("fact_kernel", ""),
                    "score": r.get("score", 0),
                    "source": r.get("source", "vector"),
                })

        return merged

    def _decide_with_llm(
        self,
        query: str,
        normalized: str,
        pre_result: PreReviewResult,
        fragments: List[Dict[str, Any]],
        history: List[Dict[str, str]],
        conversation_id: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        messages_extra: Optional[List[Dict[str, Any]]] = None,
        platform_context: str = "",
    ) -> DecideResult:
        # 1. Prompt 装配（传递 pre_result 而非 intent_result）
        messages = self._prompt_assembler.assemble(
            pre_result=pre_result,
            fragments=fragments,
            conversation_history=history,
            query=query,
            platform_context=platform_context,
        )

        if messages_extra:
            messages = list(messages) + list(messages_extra)

        # 2. 主 LLM 调用
        try:
            response = self._llm.chat(
                messages=messages,
                model=self._main_model,
                temperature=0.7,
                max_tokens=self._max_tokens,
                tools=tools,
            )
        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            raise LLMUnavailableError(
                f"LLM调用失败: {e}。请检查网络连接和API配置后重试。"
            )

        # 3. Tool call 检测
        if isinstance(response, dict) and response.get("type") == "tool_call":
            return DecideResult(
                reply="",
                fragments=[],
                emotion_tag="",
                retrieval_count=len(fragments),
                llm_used=True,
                tool_call=response,
            )

        # 4. 提取回复和 Fragment
        reply = self._fragment_extractor.extract_reply_only(response)
        extracted_fragments = self._fragment_extractor.extract(response)

        # 5. 存储 Fragment
        stored_count = 0
        for f in extracted_fragments:
            try:
                fragment_id = self._store_fragment(
                    fact_kernel=f.get("fact_kernel", ""),
                    emotion_shell=f.get("emotion_shell", {}),
                    scene_shell=f.get("scene_shell", {}),
                    linked_to=f.get("linked_to", ""),
                    conversation_id=conversation_id,
                )
                if fragment_id:
                    stored_count += 1
            except Exception as e:
                logger.warning(f"Failed to store fragment: {e}")

        return DecideResult(
            reply=reply,
            fragments=extracted_fragments,
            emotion_tag=str(pre_result.emotion_dominant),  # 用情感标签替代旧意图
            retrieval_count=len(fragments),
            llm_used=True,
        )

    def _store_fragment(
        self,
        fact_kernel: str,
        emotion_shell: Dict[str, Any],
        scene_shell: Dict[str, Any],
        linked_to: str,
        conversation_id: str,
    ) -> Optional[str]:
        now = datetime.now(timezone.utc)

        fragment = Fragment(
            id=str(uuid.uuid4()),
            fact_kernel=fact_kernel,
            emotion_shell=EmotionShell(
                valence=emotion_shell.get("valence", 0.0),
                arousal=emotion_shell.get("arousal", 0.0),
                primary=emotion_shell.get("primary", "中性"),
            ),
            scene_shell=SceneShell(
                activation_level=scene_shell.get("activation_level", 0.5),
                source=scene_shell.get("source", "对话"),
            ),
            time_shell=TimeShell(created_at=now.isoformat()),
            life_shell=LifeShell(),
            linked_to=linked_to,
            source_conversation_id=conversation_id,
        )

        vector = None
        if self._vector_search.is_available and self._embedding:
            try:
                vector = self._embedding.embed(fact_kernel)
            except Exception as e:
                logger.warning(f"Failed to compute embedding: {e}")

        self._memory.remember_fragment(fragment, embedding=vector)
        return fragment.id


def create_decide_engine(
    memory_engine: MemoryEngine,
    pre_review_engine: PreReviewEngine,
    conversation_manager: ConversationManager,
    llm_provider: Optional[LLMProvider] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    vector_index: Optional[Any] = None,
) -> DecideEngine:
    return DecideEngine(
        memory_engine=memory_engine,
        pre_review_engine=pre_review_engine,
        conversation_manager=conversation_manager,
        llm_provider=llm_provider,
        embedding_provider=embedding_provider,
        vector_index=vector_index,
    )
