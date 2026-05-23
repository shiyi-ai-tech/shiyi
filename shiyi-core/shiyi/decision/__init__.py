"""决策层 - Decision 模块

职责：
- PromptAssembler: 根据意图+检索结果+对话历史动态组装prompt
- FragmentExtractor: 从主LLM回复中提取Fragment
- VectorSearch: 把embedding生成+向量搜索串起来
- DecideEngine: 决策引擎，整合所有决策链路

子模块：
- prompt_assembler: Prompt装配器
- fragment_extractor: Fragment提取器
- vector_search: 语义搜索
- decide_engine: 决策引擎
"""

from shiyi.decision.prompt_assembler import PromptAssembler
from shiyi.decision.fragment_extractor import FragmentExtractor
from shiyi.decision.vector_search import VectorSearch, SearchResult
from shiyi.decision.decide_engine import DecideEngine, DecideResult


__all__ = [
    "PromptAssembler",
    "FragmentExtractor",
    "VectorSearch",
    "SearchResult",
    "DecideEngine",
    "DecideResult",
]
