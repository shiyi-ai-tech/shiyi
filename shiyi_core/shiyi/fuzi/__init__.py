"""Fuzi (夫子) — 史佚内核学习与优化模块

Phase 4: 进化 — 参数自调、反馈学习、基准测试

核心原则：
- Fuzi 只做建议，不做自动调参。决策权永远归人类。
- 所有参数路径统一命名（decay.halflife_days, trigger.max_hops 等）
- core 零网络依赖
"""

from shiyi.fuzi.experiment import FuziExperiment
from shiyi.fuzi.learner import FuziLearner, FeedbackType, FeedbackSignal
from shiyi.fuzi.benchmarks import STANDARD_BENCHMARKS

__all__ = [
    "FuziExperiment",
    "FuziLearner", 
    "FeedbackType",
    "FeedbackSignal",
    "STANDARD_BENCHMARKS",
]
