"""FuziLearner — 夫子的反馈学习机制

5种反馈信号 → 周期聚合 → 参数进化建议 → 回滚安全

核心原则：
- Fuzi 只做建议，决策权归人类
- 参数调整需先记录实验快照，再应用
- 回滚安全：任何调整前自动快照，可一键恢复
"""

import uuid
import sqlite3
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional


class FeedbackType(str, Enum):
    """反馈信号类型"""
    CORRECTED = "corrected"       # 用户纠正 —— "不对/不是这样"
    ASK_MORE = "ask_more"         # 追问 —— "还有呢/继续/然后呢"
    SILENCE = "silence"           # 无反馈 —— 用户未继续追问
    EXPLICIT_LIKE = "like"        # 用户满意（显式）
    EXPLICIT_DISLIKE = "dislike"  # 用户不满意（显式）


class FeedbackSignal:
    """单条反馈信号"""

    def __init__(
        self,
        feedback_type: FeedbackType,
        conversation_id: str = "",
        query: str = "",
        details: Optional[Dict[str, Any]] = None,
    ):
        self.id = str(uuid.uuid4())
        self.feedback_type = feedback_type if isinstance(feedback_type, FeedbackType) else FeedbackType(feedback_type)
        self.conversation_id = conversation_id
        self.query = query
        self.details = details or {}
        self.created_at = datetime.now(timezone.utc).isoformat()


class FuziLearner:
    """夫子学习引擎"""

    def __init__(self, db_path: str = "", parent_fuzi=None):
        """初始化学习引擎
        
        Args:
            db_path: 信号数据库路径
            parent_fuzi: FuziExperiment 实例（用于实验快照和回滚）
        """
        self.db_path = db_path or str(
            Path.home() / ".shiyi" / "data" / "fuzi.db"
        )
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._parent_fuzi = parent_fuzi
        self._init_db()

    def _init_db(self):
        """初始化信号数据库"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback_signals (
                    id TEXT PRIMARY KEY,
                    feedback_type TEXT NOT NULL,
                    conversation_id TEXT DEFAULT '',
                    query TEXT DEFAULT '',
                    details_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_aggregates (
                    id TEXT PRIMARY KEY,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    total_signals INTEGER NOT NULL DEFAULT 0,
                    ask_more_count INTEGER DEFAULT 0,
                    corrected_count INTEGER DEFAULT 0,
                    silence_count INTEGER DEFAULT 0,
                    like_count INTEGER DEFAULT 0,
                    dislike_count INTEGER DEFAULT 0,
                    aggregated_at TEXT NOT NULL
                )
            """)
            conn.commit()

    # ═══════════════════════════════════════════
    # 反馈记录
    # ═══════════════════════════════════════════

    def record_feedback(
        self,
        feedback_type,
        conversation_id: str = "",
        query: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """记录一条反馈信号
        
        Args:
            feedback_type: 反馈类型（字符串或 FeedbackType）
            conversation_id: 对话 ID
            query: 触发此反馈的查询
            details: 附加细节
            
        Returns:
            信号 ID
        """
        if isinstance(feedback_type, str):
            try:
                feedback_type = FeedbackType(feedback_type)
            except ValueError:
                feedback_type = FeedbackType.SILENCE

        signal = FeedbackSignal(
            feedback_type=feedback_type,
            conversation_id=conversation_id,
            query=query,
            details=details,
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO feedback_signals
                   (id, feedback_type, conversation_id, query, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    signal.id,
                    signal.feedback_type.value,
                    signal.conversation_id,
                    signal.query,
                    sqlite3.adapt(signal.details),
                    signal.created_at,
                ),
            )
            conn.commit()

        return signal.id

    def get_recent_signals(
        self,
        days: int = 7,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """获取近期反馈信号
        
        Args:
            days: 回溯天数
            limit: 返回上限
            
        Returns:
            [{id, feedback_type, conversation_id, query, created_at}, ...]
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, feedback_type, conversation_id, query, created_at
                   FROM feedback_signals
                   WHERE created_at >= ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (cutoff, limit),
            ).fetchall()

        return [dict(row) for row in rows]

    # ═══════════════════════════════════════════
    # 信号聚合
    # ═══════════════════════════════════════════

    def aggregate_period(self, days: int = 7) -> Dict[str, Any]:
        """聚合最近 N 天的反馈信号
        
        Args:
            days: 聚合天数
            
        Returns:
            {total, by_type: {ask_more, corrected, silence, like, dislike},
             ask_more_rate, correction_rate, satisfaction_ratio}
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT feedback_type, COUNT(*) as cnt
                   FROM feedback_signals
                   WHERE created_at >= ?
                   GROUP BY feedback_type""",
                (cutoff,),
            ).fetchall()

        by_type = {row[0]: row[1] for row in rows}
        total = sum(by_type.values())

        ask_more = by_type.get(FeedbackType.ASK_MORE.value, 0)
        corrected = by_type.get(FeedbackType.CORRECTED.value, 0)
        like = by_type.get(FeedbackType.EXPLICIT_LIKE.value, 0)
        dislike = by_type.get(FeedbackType.EXPLICIT_DISLIKE.value, 0)
        silence = by_type.get(FeedbackType.SILENCE.value, 0)

        ask_more_rate = ask_more / max(total, 1)
        correction_rate = corrected / max(total, 1)
        explicit = like + dislike
        satisfaction_ratio = like / max(explicit, 1) if explicit > 0 else 0.5

        # 存储聚合记录
        agg_id = str(uuid.uuid4())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO signal_aggregates
                   (id, period_start, period_end, total_signals,
                    ask_more_count, corrected_count, silence_count,
                    like_count, dislike_count, aggregated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    agg_id, cutoff, now, total,
                    ask_more, corrected, silence, like, dislike, now,
                ),
            )
            conn.commit()

        return {
            "total": total,
            "by_type": by_type,
            "ask_more_rate": round(ask_more_rate, 3),
            "correction_rate": round(correction_rate, 3),
            "satisfaction_ratio": round(satisfaction_ratio, 3),
            "period_days": days,
        }

    # ═══════════════════════════════════════════
    # 参数进化建议
    # ═══════════════════════════════════════════

    def propose_evolution(
        self,
        signals: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """基于反馈信号提出参数进化建议
        
        Args:
            signals: 聚合信号（None 则自动聚合最近 7 天）
            
        Returns:
            [{param, current, suggested, reason, confidence}, ...]
        """
        if signals is None:
            signals = self.aggregate_period(days=7)

        suggestions = []

        # 1. 追问率过高 → 扩散不足
        ask_more_rate = signals.get("ask_more_rate", 0)
        if ask_more_rate > 0.3:
            suggestions.append({
                "param": "trigger.max_hops",
                "suggested_action": "increase",
                "suggested_delta": +1 if ask_more_rate > 0.5 else +0,
                "reason": f"追问率 {ask_more_rate:.0%} > 30%，扩散范围可能不足",
                "confidence": round(min(ask_more_rate, 1.0), 2),
            })

        # 2. 纠正率高 → 记忆不够灵活（半衰期太长）
        correction_rate = signals.get("correction_rate", 0)
        if correction_rate > 0.15:
            suggestions.append({
                "param": "decay.halflife_days",
                "suggested_action": "decrease",
                "suggested_delta": -20 if correction_rate > 0.3 else -10,
                "reason": f"纠正率 {correction_rate:.0%} > 15%，记忆可能过于固化",
                "confidence": round(min(correction_rate * 2, 1.0), 2),
            })

        # 3. 满意度低 → 建议回滚
        satisfaction = signals.get("satisfaction_ratio", 0.5)
        if satisfaction < 0.3 and self._parent_fuzi:
            recent = self._parent_fuzi.list_experiments(limit=3)
            if recent:
                suggestions.append({
                    "param": "rollback",
                    "suggested_action": "rollback",
                    "suggested_delta": None,
                    "target_experiment_id": recent[0]["id"],
                    "reason": f"满意度比 {satisfaction:.0%} < 30%，建议回滚到上个实验",
                    "confidence": round(1.0 - satisfaction, 2),
                })

        return suggestions

    # ═══════════════════════════════════════════
    # 回滚
    # ═══════════════════════════════════════════

    def rollback_to_experiment(self, experiment_id: str) -> bool:
        """回滚参数到指定实验快照
        
        Args:
            experiment_id: 目标实验 ID
            
        Returns:
            是否成功
        """
        if not self._parent_fuzi:
            return False

        exp = self._parent_fuzi.get_experiment(experiment_id)
        if not exp:
            return False

        params = json.loads(exp["params_json"])
        self._parent_fuzi.bridge.restore(params)

        # 记录回滚
        rollback_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO fuzi_adjustments
                   (id, from_experiment_id, param_path, old_value, new_value, reason, applied_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (rollback_id, experiment_id, "rollback", "", "", "回滚到实验快照", now),
            )
            conn.commit()

        return True

    def get_adjustment_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """获取参数调整历史
        
        Args:
            limit: 返回上限
            
        Returns:
            [{id, from_experiment_id, param_path, old_value, new_value, reason, applied_at}, ...]
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM fuzi_adjustments ORDER BY applied_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self):
        """关闭学习引擎"""
        pass


# JSON 适配：sqlite3 不直接支持 JSON，通过 adapter 序列化
try:
    import json as _json
    sqlite3.register_adapter(dict, lambda d: _json.dumps(d, ensure_ascii=False))
except Exception:
    pass
