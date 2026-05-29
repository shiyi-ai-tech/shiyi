"""FuziExperiment — 夫子的参数管理与实验引擎

功能：
- 参数读写（统一路径命名）
- 实验管理（参数快照 + 评分DB）
- A/B 对比
- 敏感性分析（单变量扫描）
- 基准测试执行
- 安全报告生成
"""

import json
import sqlite3
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple


# ═══════════════════════════════════════════
# 参数路径 → 引擎属性映射
# ═══════════════════════════════════════════

class ParamBridge:
    """参数读写桥 — 将路径名（decay.halflife_days）映射到引擎实际属性

    对特殊路径（如 cache.hot_capacity → hot_cache.capacity）进行
    自动重定向，保证 list_params() 中全部参数都可读写。
    """

    # 路径简名 → 实际属性链条重定向
    # 例如 "cache.hot_capacity" → ("cache", "hot_cache", "capacity")
    _REDIRECTS: Dict[str, tuple] = {
        "cache.hot_capacity": ("cache", "hot_cache", "capacity"),
    }

    def __init__(self, engine):
        """绑定记忆引擎

        Args:
            engine: MemoryEngine 实例
        """
        self._engine = engine

    def _resolve_path(self, path: str):
        """将参数路径解析为 (obj, attr, redirect)

        如果是重定向路径，返回重定向后的部件列表。

        Returns:
            (target_obj, attr_name)
        """
        redirect = self._REDIRECTS.get(path)
        if redirect:
            parts = list(redirect)
        else:
            parts = path.split(".")

        obj = self._engine
        for part in parts[:-1]:
            if part == "decay":
                obj = obj.decay_engine
            elif part == "trigger":
                obj = obj.trigger_engine
            elif part == "relation":
                obj = obj.relation_engine
            elif part == "cache":
                obj = obj.cache
            else:
                obj = getattr(obj, part)

        return obj, parts[-1]

    def get_param(self, path: str):
        """读取参数值

        Args:
            path: 参数路径，如 'decay.halflife_days', 'trigger.max_hops'

        Returns:
            参数值
        """
        obj, attr = self._resolve_path(path)
        if not hasattr(obj, attr):
            raise AttributeError(f"参数路径 {path} 不存在: {type(obj).__name__} 无属性 {attr}")
        return getattr(obj, attr)

    def set_param(self, path: str, value):
        """写入参数值

        Args:
            path: 参数路径
            value: 新值
        """
        obj, attr = self._resolve_path(path)
        if not hasattr(obj, attr):
            raise AttributeError(f"引擎无属性 {attr} (路径: {path})")
        setattr(obj, attr, value)

    def list_params(self) -> List[str]:
        """列出所有可调参数路径（7个引擎参数）"""
        return [
            "decay.halflife_days",
            "decay.emotion_multiplier",
            "decay.access_multiplier",
            "trigger.max_hops",
            "trigger.decay_per_hop",
            "trigger.activation_threshold",
            "cache.hot_capacity",
        ]


# ═══════════════════════════════════════════
# FuziExperiment 主类
# ═══════════════════════════════════════════

class FuziExperiment:
    """夫子实验引擎 — 参数管理与优化"""

    def __init__(self, bridge: ParamBridge, db_path: str = ""):
        """初始化实验引擎
        
        Args:
            bridge: 参数读写桥
            db_path: 实验数据库路径，默认 ~/.shiyi/data/fuzi.db
        """
        self.bridge = bridge
        self.db_path = db_path or str(
            Path.home() / ".shiyi" / "data" / "fuzi.db"
        )
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化实验数据库"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fuzi_experiments (
                    id TEXT PRIMARY KEY,
                    params_json TEXT NOT NULL,
                    score REAL,
                    notes TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fuzi_adjustments (
                    id TEXT PRIMARY KEY,
                    from_experiment_id TEXT,
                    param_path TEXT NOT NULL,
                    old_value TEXT NOT NULL,
                    new_value TEXT NOT NULL,
                    reason TEXT,
                    applied_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fuzi_metrics (
                    id TEXT PRIMARY KEY,
                    metric_name TEXT NOT NULL,
                    value REAL NOT NULL,
                    recorded_at TEXT NOT NULL
                )
            """)
            conn.commit()

    # ═══════════════════════════════════════════
    # 参数快照
    # ═══════════════════════════════════════════

    def snapshot(self) -> Dict[str, Any]:
        """获取当前参数快照
        
        Returns:
            {param_path: value, ...}
        """
        params = {}
        for path in self.bridge.list_params():
            try:
                params[path] = self.bridge.get_param(path)
            except AttributeError:
                pass
        return params

    def restore(self, params: Dict[str, Any]):
        """从快照恢复参数
        
        Args:
            params: {param_path: value, ...}
        """
        for path, value in params.items():
            try:
                self.bridge.set_param(path, value)
            except AttributeError:
                pass

    # ═══════════════════════════════════════════
    # 实验管理
    # ═══════════════════════════════════════════

    def record_experiment(
        self,
        score: float = 0.0,
        notes: str = "",
    ) -> str:
        """记录当前参数到实验快照
        
        Args:
            score: 基准测试综合评分
            notes: 实验备注
            
        Returns:
            实验 ID
        """
        exp_id = str(uuid.uuid4())
        params = self.snapshot()
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO fuzi_experiments (id, params_json, score, notes, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (exp_id, json.dumps(params, ensure_ascii=False), score, notes, now),
            )
            conn.commit()

        return exp_id

    def list_experiments(self, limit: int = 20) -> List[Dict[str, Any]]:
        """列出历史实验
        
        Args:
            limit: 返回数量
            
        Returns:
            [{id, params_json, score, notes, created_at}, ...]
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM fuzi_experiments ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [dict(row) for row in rows]

    def get_experiment(self, exp_id: str) -> Optional[Dict[str, Any]]:
        """获取单个实验
        
        Args:
            exp_id: 实验 ID
            
        Returns:
            实验数据 或 None
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM fuzi_experiments WHERE id=?",
                (exp_id,),
            ).fetchone()

        return dict(row) if row else None

    def compare_experiments(
        self,
        exp_a: str,
        exp_b: str,
    ) -> Dict[str, Any]:
        """对比两个实验
        
        Args:
            exp_a: 实验 A 的 ID
            exp_b: 实验 B 的 ID
            
        Returns:
            {params_a, params_b, param_diffs, score_a, score_b, score_delta}
        """
        a = self.get_experiment(exp_a)
        b = self.get_experiment(exp_b)

        if not a or not b:
            return {"error": "实验不存在"}

        params_a = json.loads(a["params_json"])
        params_b = json.loads(b["params_json"])

        all_params = set(params_a.keys()) | set(params_b.keys())
        param_diffs = {}
        for p in all_params:
            va = params_a.get(p)
            vb = params_b.get(p)
            if va != vb:
                param_diffs[p] = {"from": va, "to": vb}

        return {
            "params_a": params_a,
            "params_b": params_b,
            "param_diffs": param_diffs,
            "score_a": a["score"],
            "score_b": b["score"],
            "score_delta": (b["score"] or 0) - (a["score"] or 0),
        }

    # ═══════════════════════════════════════════
    # 敏感性分析
    # ═══════════════════════════════════════════

    def sensitivity_analysis(
        self,
        param_path: str,
        values: List[float],
        test_func,
    ) -> List[Dict[str, Any]]:
        """单变量参数敏感性分析
        
        对指定参数逐个测试不同取值，测量对 score 的影响。
        
        Args:
            param_path: 参数路径
            values: 待测试的参数值列表
            test_func: 评分函数 (params) -> score
            
        Returns:
            [{value, score}, ...] 按 value 排序
        """
        original_value = self.bridge.get_param(param_path)
        results = []

        for v in values:
            self.bridge.set_param(param_path, v)
            score = test_func(self.snapshot())
            results.append({"value": v, "score": score})

        # 恢复原始值
        self.bridge.set_param(param_path, original_value)

        return sorted(results, key=lambda x: x["value"])

    # ═══════════════════════════════════════════
    # 基准测试
    # ═══════════════════════════════════════════

    def run_benchmark(
        self,
        benchmark_cases: List[Dict[str, Any]],
        recall_func,
    ) -> Dict[str, Any]:
        """执行基准测试
        
        Args:
            benchmark_cases: 测试用例列表
            recall_func: recall(query) -> List[Dict] 函数
            
        Returns:
            {total_cases, passed, failed, score, case_results: [...]}
        """
        total = len(benchmark_cases)
        passed = 0
        case_results = []
        total_score = 0.0

        for case in benchmark_cases:
            query = case.get("query", "")
            intent_hint = case.get("intent_hint", "fact")
            expected_keywords = case.get("expected", [])
            layer_weights = case.get("layer_weights", {})

            try:
                results = recall_func(query)
            except Exception as e:
                results = []
                case_results.append({
                    "query": query,
                    "intent": intent_hint,
                    "expected": expected_keywords,
                    "score": 0.0,
                    "passed": False,
                    "error": str(e),
                    "hit_count": 0,
                    "expected_count": len(expected_keywords),
                    "recalled": [],
                })
                continue

            recalled_texts = [
                r.get("fact_kernel", "") or r.get("content", "")
                for r in results
            ]

            # 评分：关键词命中
            keyword_hits = 0
            for kw in expected_keywords:
                for text in recalled_texts:
                    if kw in text:
                        keyword_hits += 1
                        break

            expected_count = len(expected_keywords)
            score = keyword_hits / max(expected_count, 1)

            ok = score >= 0.5  # 至少命中一半关键词算通过
            if ok:
                passed += 1
            total_score += score

            case_results.append({
                "query": query,
                "intent": intent_hint,
                "expected": expected_keywords,
                "score": round(score, 3),
                "passed": ok,
                "hit_count": keyword_hits,
                "expected_count": expected_count,
                "recalled": recalled_texts[:3],
            })

        return {
            "total_cases": total,
            "passed": passed,
            "failed": total - passed,
            "score": round(total_score / max(total, 1), 3),
            "case_results": case_results,
        }

    # ═══════════════════════════════════════════
    # 安全报告
    # ═══════════════════════════════════════════

    def safe_report(
        self,
        include_raw: bool = False,
        engine_stats: Optional[Dict[str, Any]] = None,
        learner_signals: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """生成安全报告
        
        生产模式（include_raw=False）：匿名统计，不含敏感数据
        开发模式（include_raw=True）：完整数据，用于内部调试
        
        Args:
            include_raw: 是否包含原始数据
            engine_stats: 记忆引擎统计
            learner_signals: 学习信号统计
            
        Returns:
            报告字典
        """
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "full" if include_raw else "safe",
            "metrics": {
                "current_params": self.snapshot(),
            },
        }

        if engine_stats:
            report["engine"] = {
                "total_fragments": engine_stats.get("fragments", {}).get("total", 0),
                "total_relations": engine_stats.get("relations", {}).get("total_relations", 0),
                "avg_degree": engine_stats.get("relations", {}).get("avg_degree", 0),
            }

        if learner_signals:
            report["learning"] = {
                "ask_more_rate": learner_signals.get("ask_more_rate", 0),
                "correction_rate": learner_signals.get("correction_rate", 0),
                "satisfaction_ratio": learner_signals.get("satisfaction_ratio", 0),
            }

        # 退化检测
        recent_experiments = self.list_experiments(limit=10)
        if len(recent_experiments) >= 4:
            recent_scores = [e["score"] for e in recent_experiments[:4] if e["score"]]
            old_scores = [e["score"] for e in recent_experiments[4:8] if e["score"]]
            if recent_scores and old_scores:
                recent_avg = sum(recent_scores) / len(recent_scores)
                old_avg = sum(old_scores) / len(old_scores)
                report["degradation"] = {
                    "recent_avg_score": round(recent_avg, 3),
                    "previous_avg_score": round(old_avg, 3),
                    "delta": round(recent_avg - old_avg, 3),
                    "degrading": recent_avg < old_avg,
                }

        # 调整建议（基于学习信号）
        if learner_signals:
            suggestions = []
            if learner_signals.get("ask_more_rate", 0) > 0.3:
                suggestions.append({
                    "param": "trigger.max_hops",
                    "suggested": "increase by 1",
                    "reason": f"追问率 {learner_signals['ask_more_rate']:.0%} > 30%，扩散可能不足",
                })
            if learner_signals.get("correction_rate", 0) > 0.15:
                suggestions.append({
                    "param": "decay.halflife_days",
                    "suggested": "decrease",
                    "reason": f"纠正率 {learner_signals['correction_rate']:.0%} > 15%，记忆可能不够灵活",
                })
            report["suggestions"] = suggestions

        if include_raw:
            report["raw_params"] = self.snapshot()
            report["raw_experiments"] = self.list_experiments(limit=50)

        return report

    def save_report_to_file(
        self,
        report: Dict[str, Any],
        output_path: Optional[str] = None,
    ) -> str:
        """保存报告到文件
        
        Args:
            report: 报告字典
            output_path: 输出路径，默认 ~/.shiyi/data/fuzi_report_{timestamp}.json
            
        Returns:
            文件路径
        """
        if not output_path:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(
                Path.home() / ".shiyi" / "data" / f"fuzi_report_{ts}.json"
            )

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        return output_path

    def close(self):
        """关闭实验引擎"""
        pass
