"""DecayEngine - 衰减引擎

公式：
    effective_halflife = halflife × emotion_mult × access_mult
    energy = base × 2^(-days / effective_halflife)
    
衰减参数：
    halflife: 60 天（半衰期）
    emotion_multiplier: 1.5（情感加权）
    access_multiplier: 2.0（访问加分）
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
        """初始化衰减引擎
        
        Args:
            store: FragmentStore 实例
            halflife_days: 半衰期（天）
            emotion_multiplier: 情感强度乘数
            access_multiplier: 访问次数乘数
        """
        self.store = store
        self.halflife_days = halflife_days
        self.emotion_multiplier = emotion_multiplier
        self.access_multiplier = access_multiplier
    
    def compute_energy(self, fragment: Fragment) -> float:
        """计算当前能量
        
        公式：
            effective_halflife = base_halflife × emotion_mult × access_mult
            energy = base × 2^(-days / effective_halflife)
        
        Args:
            fragment: Fragment 实例
            
        Returns:
            能量值 [0, 1]
        """
        if not fragment.life_shell:
            return 1.0
        
        now = datetime.now(timezone.utc)
        last_access = self._get_last_access_time(fragment)
        
        if not last_access:
            return fragment.life_shell.energy
        
        # 计算天数
        days_since = (now - last_access).total_seconds() / 86400.0
        
        # 计算多维乘数
        em_mult = self._emotion_multiplier(fragment)
        ac_mult = self._access_multiplier_calc(fragment)
        
        # 有效半衰期
        effective_halflife = self.halflife_days * em_mult * ac_mult
        
        # 指数衰减
        decay_factor = math.pow(2, -days_since / effective_halflife)
        energy = fragment.life_shell.energy * decay_factor
        
        return max(0.0, min(1.0, energy))
    
    def refresh(self, fragment: Fragment) -> None:
        """刷新访问 - 被检索命中时调用
        
        Args:
            fragment: Fragment 实例
        """
        if not fragment.life_shell:
            fragment.life_shell = LifeShell()
        
        # 重置能量
        fragment.life_shell.energy = 0.9
        # 增加访问计数
        fragment.life_shell.access_count += 1
        # 更新访问时间
        if not fragment.time_shell:
            fragment.time_shell = TimeShell()
        fragment.time_shell.last_accessed_at = datetime.now(timezone.utc).isoformat()
    
    def decay_all(self) -> int:
        """批量衰减 warm 和 hot 层所有 Fragment
        
        Returns:
            处理的 Fragment 数量
        """
        count = 0
        
        for layer in ["warm", "hot"]:
            fragments = self.store.get_by_layer(layer, limit=1000)
            
            for fragment in fragments:
                new_energy = self.compute_energy(fragment)
                old_energy = fragment.life_shell.energy
                
                # 只更新变化明显的
                if abs(new_energy - old_energy) > 0.01:
                    fragment.life_shell.energy = new_energy
                    
                    self.store.update(fragment)
                    count += 1
        
        return count
    
    def get_energy_breakdown(self, fragment: Fragment) -> dict:
        """获取能量分解（调试用）
        
        Args:
            fragment: Fragment 实例
            
        Returns:
            能量计算详情
        """
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
            "current_energy": round(fragment.life_shell.energy, 4),
            "computed_energy": round(self.compute_energy(fragment), 4),
            "days_since_access": round(days_since, 2),
            "emotion_mult": round(em_mult, 2),
            "access_mult": round(ac_mult, 2),
            "effective_halflife": round(effective_halflife, 2),
            "access_count": fragment.life_shell.access_count,
            "valence": fragment.emotion_shell.valence,
            "arousal": fragment.emotion_shell.arousal,
        }
    
    def _get_last_access_time(self, fragment: Fragment) -> Optional[datetime]:
        """获取最后访问时间"""
        # 优先使用 last_accessed_at，回退到 created_at
        if fragment.time_shell:
            ts = fragment.time_shell.last_accessed_at or fragment.time_shell.created_at
            if ts:
                try:
                    return datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    pass
        return None
    
    def _emotion_multiplier(self, fragment: Fragment) -> float:
        """情感强度乘数
        
        强情感 → 慢衰减（乘数 > 1）
        公式：1.0 + intensity × emotion_multiplier
        """
        intensity = abs(fragment.emotion_shell.valence) * fragment.emotion_shell.arousal
        return 1.0 + intensity * self.emotion_multiplier
    
    def _access_multiplier_calc(self, fragment: Fragment) -> float:
        """访问频率乘数
        
        常访问 → 慢衰减
        阈值：3次 ×1.5, 5次 ×2.0, 10次 ×3.0
        """
        ac = fragment.life_shell.access_count
        if ac >= 10:
            return 3.0
        elif ac >= 5:
            return 2.0
        elif ac >= 3:
            return 1.5
        return 1.0
    
