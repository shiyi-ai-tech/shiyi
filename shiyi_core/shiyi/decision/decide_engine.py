"""DecideEngine - 决策引擎

职责：
- 整合所有决策链路
- 协调 PromptAssembler、FragmentExtractor、VectorSearch
- 统一记忆检索和Fragment存储

核心链路：
1. 感知层输出 → IntentEngine分析意图
2. 意图驱动检索 → MemoryEngine.recall(query)
3. 向量搜索 → VectorSearch.search(query)
4. 多路结果融合去重 → 截断top_k
5. Prompt装配 → PromptAssembler
6. 主LLM调用 → LLMProvider.chat()
7. Fragment提取 → FragmentExtractor
8. Fragment存储 → MemoryEngine.remember()
9. 对话历史更新 → ConversationManager
10. 返回回复

总共2次LLM调用：
- 1次轻量LLM（意图识别）→ IntentEngine
- 1次主LLM（回复+Fragment）→ LLMProvider

降级策略已删除：
- LLM不可用时明确报错，不降级
- Embedding不可用时在回复中提示（对话仍可继续）
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple, Generator

from shiyi.common.interfaces import LLMProvider, EmbeddingProvider
from shiyi.common.types import IntentResult, Fragment, EmotionShell, SceneShell, TimeShell, LifeShell
from shiyi.common.errors import LLMUnavailableError
from shiyi.memory.engine import MemoryEngine
from shiyi.perception.intent_engine import IntentEngine
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
        intent: str,
        retrieval_count: int,
        llm_used: bool = True,
        tool_call: Optional[Dict[str, Any]] = None,
    ):
        self.reply = reply
        self.fragments = fragments
        self.intent = intent
        self.retrieval_count = retrieval_count
        self.llm_used = llm_used
        self.tool_call = tool_call  # Function Calling 工具调用


class DecideEngine:
    """决策引擎"""
    
    def __init__(
        self,
        memory_engine: MemoryEngine,
        intent_engine: IntentEngine,
        conversation_manager: ConversationManager,
        llm_provider: Optional[LLMProvider] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        vector_index: Optional[Any] = None,
        top_k: int = 10,
        main_model: Optional[str] = None,
        max_tokens: int = 4096,
    ):
        """初始化决策引擎
        
        Args:
            memory_engine: 记忆引擎
            intent_engine: 意图识别引擎
            conversation_manager: 对话历史管理器
            llm_provider: 主LLM调用服务
            embedding_provider: Embedding服务
            vector_index: 向量索引
            top_k: 检索结果数量
            main_model: 主LLM模型名（默认: 环境变量SHIYI_MAIN_LLM_MODEL 或 deepseek-v4-pro）
            max_tokens: 主LLM最大输出token数（默认4096）
        """
        self._memory = memory_engine
        self._intent_engine = intent_engine
        self._conversation = conversation_manager
        self._llm = llm_provider
        self._embedding = embedding_provider
        self._vector_index = vector_index
        self._top_k = top_k
        self._max_tokens = max_tokens
        
        # 主LLM模型选择：显式参数 > 环境变量 > 配置默认
        if main_model is None:
            main_model = os.environ.get("SHIYI_MAIN_LLM_MODEL", DEFAULT_MAIN_LLM_MODEL)
        self._main_model = main_model
        
        # 初始化组件
        self._prompt_assembler = PromptAssembler()
        self._fragment_extractor = FragmentExtractor()
        self._vector_search = VectorSearch(
            embedding_provider=embedding_provider,
            vector_index=vector_index,
        )
    
    @property
    def embedding_available(self) -> bool:
        """检查Embedding服务是否可用"""
        return self._vector_search.is_available

    def _prepare_context(
        self,
        query: str,
        conversation_id: str,
        normalized_text: Optional[str] = None,
        intent_result = None,
    ) -> Tuple[str, IntentResult, List[Dict[str, Any]], List[Dict[str, str]]]:
        """准备决策上下文（提取decide和decide_stream的公共逻辑）
        
        Returns:
            (normalized, intent_result, fragments, history)
        """
        normalized = normalized_text or query
        
        # 1. 意图分析（轻量LLM，复用感知层产出）
        history = self._conversation.get_history_for_llm(conversation_id, max_turns=5)
        if intent_result is None:
            intent_result = self._intent_engine.analyze(
                query=normalized,
                history=history,
                recent_intents=None,
            )
        
        # 2-4. 记忆检索 + 向量搜索 + 融合去重
        fragments = self._recall(query, intent_result)
        if self._vector_search.is_available:
            vector_results = self._vector_search.search(query, top_k=self._top_k)
            fragments = self._merge_results(fragments, vector_results)
        fragments = fragments[:self._top_k]
        
        return normalized, intent_result, fragments, history
    
    def _check_llm_available(self) -> None:
        """检查LLM是否可用，不可用则抛出明确异常"""
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
        intent_result = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        messages_extra: Optional[List[Dict[str, Any]]] = None,
        platform_context: str = "",
    ) -> DecideResult:
        """执行决策
        
        完整链路：
        1. 意图分析（轻量LLM，优先复用感知层产出）
        2. 记忆检索（多路并行）
        3. 向量搜索（可选）
        4. 结果融合去重
        5. Prompt装配
        6. 主LLM调用（支持 Function Calling）
        7. Fragment提取
        8. Fragment存储
        9. 返回回复
        
        Args:
            query: 用户输入（原始）
            conversation_id: 会话ID
            normalized_text: 标准化后的文本（可选）
            intent_result: 感知层意图结果（可选，避免重复分析）
            tools: Function Calling 工具列表（可选）
            messages_extra: 额外追加的消息（工具结果等，可选）
            
        Returns:
            DecideResult 决策结果（含 tool_call 字段）
        """
        # 1-4. 准备上下文（意图分析、记忆检索、向量搜索、融合去重）
        normalized, intent_result, fragments, history = self._prepare_context(
            query, conversation_id, normalized_text, intent_result
        )
        
        # 5-9. 主LLM调用（LLM不可用时直接报错）
        self._check_llm_available()
        return self._decide_with_llm(
            query=query,
            normalized=normalized,
            intent_result=intent_result,
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
        intent_result = None,
        platform_context: str = "",
    ) -> Generator[Tuple[str, Any], None, None]:
        """流式决策 — 返回 (chunk_type, data) 生成器
        
        chunk_type:
            "token" — data 是文本片段，应立即推给客户端
            "done"  — data 是 DecideResult，流式结束后提供完整结果
        
        与 decide() 相同的预处理（意图→检索→融合），
        但主LLM调用改为流式，Fragment 提取在流完成后批量执行。
        不支持 Function Calling / Tool Calling。
        """
        # 1-4. 准备上下文（意图分析、记忆检索、向量搜索、融合去重）
        normalized, intent_result, fragments, history = self._prepare_context(
            query, conversation_id, normalized_text, intent_result
        )
        
        # 5-8. 流式 LLM 调用 + Fragment 提取存储（LLM不可用时直接报错）
        self._check_llm_available()
        
        # 流式 LLM
        messages = self._prompt_assembler.assemble(
            intent_result=intent_result,
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
                f"流式LLM调用失败: {e}。"
                "请检查网络连接和API配置后重试。"
            )
        
        # 流完成后提取 Fragment
        reply = self._fragment_extractor.extract_reply_only(buffer)
        extracted_fragments = self._fragment_extractor.extract(buffer)
        
        stored_count = 0
        for f in extracted_fragments:
            try:
                fragment_id = self._store_fragment(
                    fact_kernel=f.get("fact_kernel", ""),
                    emotion_shell=f.get("emotion_shell", {}),
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
            intent=str(intent_result.intent),
            retrieval_count=len(fragments),
            llm_used=True,
        ))
    
    def _recall(
        self,
        query: str,
        intent_result: IntentResult,
    ) -> List[Dict[str, Any]]:
        """记忆检索"""
        results = []
        
        # 使用子查询或原始查询
        queries = []
        if intent_result.sub_queries:
            queries = [sq.query_rewrite for sq in intent_result.sub_queries]
        else:
            queries = [query]
        
        for q in queries:
            try:
                # 生成 query embedding（如可用），用于 MemoryEngine 内向量搜索
                query_vector = None
                if self._vector_search.is_available:
                    try:
                        query_vector = self._embedding.embed(q)
                    except Exception:
                        pass  # 降级：无 embedding 时用纯关键词搜索
                
                recall_results = self._memory.recall(
                    q,
                    deep=intent_result.intent == "recall",
                    top_k=self._top_k,
                    query_vector=query_vector,
                )
                results.extend(recall_results)
                
                # 刷新命中的 Fragment（衰减+热层）
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
        """合并去重"""
        seen_ids = set()
        merged = []
        
        # 先加recall结果
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
        
        # 再加vector结果
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
        intent_result: IntentResult,
        fragments: List[Dict[str, Any]],
        history: List[Dict[str, str]],
        conversation_id: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        messages_extra: Optional[List[Dict[str, Any]]] = None,
        platform_context: str = "",
    ) -> DecideResult:
        """使用LLM进行决策（支持 Function Calling）"""
        # 1. Prompt装配
        messages = self._prompt_assembler.assemble(
            intent_result=intent_result,
            fragments=fragments,
            conversation_history=history,
            query=query,
            platform_context=platform_context,
        )
        
        # 追加额外消息（如工具执行结果）
        if messages_extra:
            messages = list(messages) + list(messages_extra)
        
        # 2. 调用主LLM（支持 Function Calling），捕获运行时异常
        try:
            response = self._llm.chat(
                messages=messages,
                model=self._main_model,  # 主LLM模型（可配置）
                temperature=0.7,
                max_tokens=self._max_tokens,
                tools=tools,
            )
        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            raise LLMUnavailableError(
                f"LLM调用失败: {e}。"
                "请检查网络连接和API配置后重试。"
            )
        
        # 3. 检测 Function Calling 返回
        if isinstance(response, dict) and response.get("type") == "tool_call":
            # LLM 要调工具，直接返回 tool_call，不做 Fragment 处理
            return DecideResult(
                reply="",
                fragments=[],
                intent=str(intent_result.intent),
                retrieval_count=len(fragments),
                llm_used=True,
                tool_call=response,
            )
        
        # 4. 提取回复和Fragment（普通文本回复）
        reply = self._fragment_extractor.extract_reply_only(response)
        extracted_fragments = self._fragment_extractor.extract(response)
        
        # 5. 存储Fragment
        stored_count = 0
        for f in extracted_fragments:
            try:
                fragment_id = self._store_fragment(
                    fact_kernel=f.get("fact_kernel", ""),
                    emotion_shell=f.get("emotion_shell", {}),
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
            intent=str(intent_result.intent),
            retrieval_count=len(fragments),
            llm_used=True,
        )
    
    def _store_fragment(
        self,
        fact_kernel: str,
        emotion_shell: Dict[str, Any],
        linked_to: str,
        conversation_id: str,
    ) -> Optional[str]:
        """存储Fragment到记忆引擎
        
        Args:
            fact_kernel: 核心事实
            emotion_shell: 情感壳
            linked_to: 关联ID
            conversation_id: 会话ID
            
        Returns:
            存储的Fragment ID
        """
        now = datetime.now(timezone.utc)
        
        # 构建Fragment对象
        fragment = Fragment(
            id=str(uuid.uuid4()),
            fact_kernel=fact_kernel,
            emotion_shell=EmotionShell(
                valence=emotion_shell.get("valence", 0.0),
                arousal=emotion_shell.get("arousal", 0.0),
                primary=emotion_shell.get("primary", "中性"),
            ),
            scene_shell=SceneShell(),
            time_shell=TimeShell(created_at=now.isoformat()),
            life_shell=LifeShell(),
            linked_to=linked_to,
            source_conversation_id=conversation_id,
        )
        
        # 向量化（可选）
        vector = None
        if self._vector_search.is_available and self._embedding:
            try:
                vector = self._embedding.embed(fact_kernel)
            except Exception as e:
                logger.warning(f"Failed to compute embedding: {e}")
        
        # 存储（包含衰减+缓存+关系+向量）
        self._memory.remember_fragment(fragment, embedding=vector)
        
        return fragment.id


def create_decide_engine(
    memory_engine: MemoryEngine,
    intent_engine: IntentEngine,
    conversation_manager: ConversationManager,
    llm_provider: Optional[LLMProvider] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    vector_index: Optional[Any] = None,
) -> DecideEngine:
    """创建决策引擎实例
    
    Args:
        memory_engine: 记忆引擎
        intent_engine: 意图识别引擎
        conversation_manager: 对话历史管理器
        llm_provider: LLM服务
        embedding_provider: Embedding服务
        vector_index: 向量索引
        
    Returns:
        DecideEngine实例
    """
    return DecideEngine(
        memory_engine=memory_engine,
        intent_engine=intent_engine,
        conversation_manager=conversation_manager,
        llm_provider=llm_provider,
        embedding_provider=embedding_provider,
        vector_index=vector_index,
    )
