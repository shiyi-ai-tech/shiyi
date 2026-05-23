"""TriggerEngine - 触发引擎（BFS扩散）

触发条件（严格）：
- 主动回忆（recall < 3 条）→ 扩散补充
- 情感查询高唤醒 → 情绪相似网络

剪枝策略：
- max_hops: 最大扩散跳数（默认2）
- decay_per_hop: 每跳衰减（默认0.5）
- visited set 防循环
- 能量 < 0.1 的片段不扩散
- >1年的冷层记忆不参与扩散
"""

from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

from shiyi.common.types import Fragment
from shiyi.store.fragment_store import FragmentStore


class TriggerEngine:
    """触发引擎 - 条件 BFS 扩散"""
    
    def __init__(
        self,
        store: FragmentStore,
        max_hops: int = 2,
        decay_per_hop: float = 0.5,
        activation_threshold: float = 0.1,
    ):
        """初始化触发引擎
        
        Args:
            store: FragmentStore 实例
            max_hops: 最大扩散跳数
            decay_per_hop: 每跳衰减系数
            activation_threshold: 激活阈值
        """
        self.store = store
        self.max_hops = max_hops
        self.decay_per_hop = decay_per_hop
        self.activation_threshold = activation_threshold
        self._default_max_hops = max_hops  # 原始默认值，供 reset 使用
        self._current_recall_count = 0  # 跟踪连续 recall 轮次
    
    def increase_hops(self, n: int = 1) -> int:
        """逐步增加扩散跳数
        
        用于深度追问场景：用户连续追问时自动扩大搜索范围。
        
        Args:
            n: 增加的跳数（默认1）
            
        Returns:
            当前 max_hops
        """
        self.max_hops += n
        return self.max_hops
    
    def reset_hops(self) -> None:
        """重置跳数为默认值"""
        self.max_hops = self._default_max_hops
    
    def should_trigger(self, recall_count: int, intent_type: str = "") -> bool:
        """判断是否触发扩散
        
        Args:
            recall_count: recall 返回数量
            intent_type: 意图类型
            
        Returns:
            是否触发
        """
        if intent_type in ("recall", "entity"):
            return True
        if recall_count < 3:
            return True
        return False
    
    def diffuse(
        self,
        seed_ids: List[str],
        max_hops: Optional[int] = None,
        decay_per_hop: Optional[float] = None,
    ) -> List[Tuple[str, float]]:
        """BFS 扩散 - 从种子片段沿关系网络扩展
        
        Args:
            seed_ids: 种子 Fragment ID 列表
            max_hops: 最大跳数，None 使用默认值
            decay_per_hop: 每跳衰减，None 使用默认值
            
        Returns:
            [(fragment_id, activation_score), ...]
        """
        if not seed_ids:
            return []
        
        hops = max_hops if max_hops is not None else self.max_hops
        decay = decay_per_hop if decay_per_hop is not None else self.decay_per_hop
        
        visited: set = set()
        frontier: Dict[str, float] = {sid: 1.0 for sid in seed_ids}
        all_activated: Dict[str, float] = {}
        
        one_year_ago = datetime.now(timezone.utc).timestamp() - 365 * 86400
        
        for _ in range(hops):
            if not frontier:
                break
            
            next_frontier: Dict[str, float] = {}
            
            for frag_id, activation in frontier.items():
                if frag_id in visited:
                    continue
                visited.add(frag_id)
                
                # 剪枝：激活值太低不扩散
                if activation < self.activation_threshold:
                    continue
                
                # 记录激活值（取最高）
                if frag_id not in all_activated or activation > all_activated[frag_id]:
                    all_activated[frag_id] = activation
                
                # 查邻居
                neighbors = self._get_neighbors(frag_id)
                
                for neighbor_id, rel_weight in neighbors:
                    if neighbor_id in visited:
                        continue
                    
                    # 每跳衰减 × 关系权重
                    next_act = activation * decay * rel_weight
                    
                    if neighbor_id not in next_frontier or next_act > next_frontier[neighbor_id]:
                        next_frontier[neighbor_id] = next_act
            
            frontier = next_frontier
        
        # 过滤掉种子
        results = [
            (fid, act) for fid, act in all_activated.items()
            if fid not in seed_ids
        ]
        
        # 过滤：排除能量低、太老的片段
        fragments = self.store.batch_get([fid for fid, _ in results])
        frag_map = {f.id: f for f in fragments}
        
        filtered: List[Tuple[str, float]] = []
        for fid, act in results:
            f = frag_map.get(fid)
            if not f:
                continue
            if f.life_shell and f.life_shell.energy < 0.1:
                continue
            # 过滤太老的冷层
            if f.time_shell and f.time_shell.created_at:
                try:
                    created_ts = datetime.fromisoformat(f.time_shell.created_at).timestamp()
                    if created_ts < one_year_ago:
                        continue
                except (ValueError, TypeError):
                    pass
            filtered.append((fid, round(act, 4)))
        
        # 按激活值排序
        filtered.sort(key=lambda x: x[1], reverse=True)
        return filtered
    
    def diffuse_from_fragments(
        self,
        seeds: List[Fragment],
        max_hops: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """从 Fragment 对象列表扩散
        
        Args:
            seeds: 种子 Fragment 列表
            max_hops: 最大跳数
            
        Returns:
            [{"fragment": Fragment, "score": float}, ...]
        """
        seed_ids = [f.id for f in seeds if f.id]
        diffused = self.diffuse(seed_ids, max_hops)
        
        if not diffused:
            return []
        
        fragments = self.store.batch_get([fid for fid, _ in diffused])
        frag_map = {f.id: f for f in fragments}
        
        results = []
        for fid, score in diffused:
            f = frag_map.get(fid)
            if f:
                results.append({
                    "fragment": f,
                    "score": score,
                })
        
        return results
    
    def get_neighbors(self, fragment_id: str) -> List[Tuple[str, float]]:
        """获取直连邻居（对外暴露）
        
        Args:
            fragment_id: Fragment ID
            
        Returns:
            [(neighbor_id, weight), ...]
        """
        return self._get_neighbors(fragment_id)
    
    def _get_neighbors(self, fragment_id: str) -> List[Tuple[str, float]]:
        """内部：查片段的所有关系邻居
        
        Args:
            fragment_id: Fragment ID
            
        Returns:
            [(neighbor_id, weight), ...]
        """
        import sqlite3
        
        try:
            with sqlite3.connect(self.store.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                # 获取作为 source 的关系
                rows1 = conn.execute(
                    "SELECT target_id, weight FROM relations WHERE source_id=?",
                    (fragment_id,)
                ).fetchall()
                
                # 获取作为 target 的关系
                rows2 = conn.execute(
                    "SELECT source_id, weight FROM relations WHERE target_id=?",
                    (fragment_id,)
                ).fetchall()
                
                neighbors = {}
                for row in rows1:
                    nid = row["target_id"]
                    w = row["weight"] if row["weight"] else 1.0
                    if nid not in neighbors or w > neighbors[nid]:
                        neighbors[nid] = w
                
                for row in rows2:
                    nid = row["source_id"]
                    w = row["weight"] if row["weight"] else 1.0
                    if nid not in neighbors or w > neighbors[nid]:
                        neighbors[nid] = w
                
                return [(nid, w) for nid, w in neighbors.items()]
        
        except sqlite3.Error:
            return []
