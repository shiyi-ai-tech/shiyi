"""DecayEngine - 衰减引擎

v260530: energy 不存库，检索时当场计算。

公式：
    effective_halflife = halflife × emotion_mult × access_mult
    energy = 1.0 × 2^(-days / effective_halflife)
"""

import math
from datetime import datetime, timezone
from typing import List, Optional

from shiyi.common.types import Fragment, LifeShell, TimeShell
from shiyi.store.fragment_store import FragmentStore


class DecayEngine:
    """记忆衰减引擎"""

    def __init__(
        self,
        store: FragmentStore,
        halflife_days: float = 60.0,
        emotion_multiplier: float = 1.5,
        access_multiplier: float = 2.0,
    ):
        self.store = store
        self.halflife_days = halflife_days
        self.emotion_multiplier = emotion_multiplier
        self.access_multiplier = access_multiplier

    def compute_energy(self, fragment: Fragment) -> float:
        """计算当前能量（v260530: 不再依赖 life_shell.energy 存值）

        公式：
            effective_halflife = base_halflife × emotion_mult × access_mult
            energy = 1.0 × 2^(-days / effective_halflife)
        """
        if not fragment.life_shell:
            return 1.0

        now = datetime.now(timezone.utc)
        last_access = self._get_last_access_time(fragment)

        if not last_access:
            return 1.0  # 新创建的 Fragment，满能量

        days_since = (now - last_access).total_seconds() / 86400.0

        em_mult = self._emotion_multiplier(fragment)
        ac_mult = self._access_multiplier_calc(fragment)

        effective_halflife = self.halflife_days * em_mult * ac_mult
        decay_factor = math.pow(2, -days_since / effective_halflife)
        energy = decay_factor  # 从 1.0 开始衰减

        return max(0.0, min(1.0, energy))

    def refresh(self, fragment: Fragment) -> None:
        """刷新访问 — 被检索命中时调用

        v260530: 不再重置 life_shell.energy，只更新 access_count + 时间。
        """
        if not fragment.life_shell:
            fragment.life_shell = LifeShell()

        fragment.life_shell.access_count += 1
        if not fragment.time_shell:
            fragment.time_shell = TimeShell()
        fragment.time_shell.last_accessed_at = datetime.now(timezone.utc).isoformat()

    def get_energy_breakdown(self, fragment: Fragment) -> dict:
        """获取能量分解（调试用）"""
        if not fragment.life_shell:
            return {"energy": 1.0, "error": "no life_shell"}

        now = datetime.now(timezone.utc)
        last_access = self._get_last_access_time(fragment)

        days_since = 0.0
        if last_access:
            days_since = (now - last_access).total_seconds() / 86400.0

        em_mult = self._emotion_multiplier(fragment)
        ac_mult = self._access_multiplier_calc(fragment)
        effective_halflife = self.halflife_days * em_mult * ac_mult

        return {
            "fragment_id": fragment.id,
            "computed_energy": round(self.compute_energy(fragment), 4),
            "days_since_access": round(days_since, 2),
            "emotion_mult": round(em_mult, 2),
            "access_mult": round(ac_mult, 2),
            "effective_halflife": round(effective_halflife, 2),
            "access_count": fragment.life_shell.access_count,
            "valence": fragment.emotion_shell.valence if fragment.emotion_shell else 0.0,
            "arousal": fragment.emotion_shell.arousal if fragment.emotion_shell else 0.0,
        }

    def _get_last_access_time(self, fragment: Fragment) -> Optional[datetime]:
        """获取最后访问时间。

        回退链：last_accessed_at → created_at → None
        - 有最后访问记录 → 使用 last_accessed_at
        - 新 Fragment（无访问记录） → 回退到 created_at
        - 两者都无 → None，调用方视为新创建（满能量 1.0）
        """
        if fragment.time_shell:
            # 显式回退：新 Fragment 以创建时间作为首次访问时间的近似
            ts = fragment.time_shell.last_accessed_at or fragment.time_shell.created_at
            if ts:
                try:
                    return datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    pass
        return None

    def _emotion_multiplier(self, fragment: Fragment) -> float:
        es = fragment.emotion_shell
        if es is None:
            return 1.0
        intensity = abs(es.valence) * es.arousal
        return 1.0 + intensity * self.emotion_multiplier

    def _access_multiplier_calc(self, fragment: Fragment) -> float:
        ac = fragment.life_shell.access_count
        if ac >= 10:
            return 3.0
        elif ac >= 5:
            return 2.0
        elif ac >= 3:
            return 1.5
        return 1.0
