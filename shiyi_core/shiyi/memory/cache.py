"""CacheLayer - 三层缓存系统

层级：
- hot: 内存 LRU 缓存（默认容量 50）
- warm: SQLite FragmentStore
- cold: 向量索引（存但日常不扫，只有 recall(deep=True) 才扫）

统一接口：
- get: 获取 Fragment
- put: 存入缓存
- evict: 驱逐缓存
"""

from collections import OrderedDict
from typing import Dict, List, Optional, Any

from shiyi.common.types import Fragment
from shiyi.store.fragment_store import FragmentStore
from shiyi.store.vector_index import VectorIndex


class HotCache:
    """热层缓存 - 内存 LRU"""
    
    def __init__(self, capacity: int = 50):
        """初始化热层缓存
        
        Args:
            capacity: LRU 缓存容量
        """
        self.capacity = capacity
        self._cache: OrderedDict[str, Fragment] = OrderedDict()
    
    def get(self, fragment_id: str) -> Optional[Fragment]:
        """获取 Fragment
        
        Args:
            fragment_id: Fragment ID
            
        Returns:
            Fragment 或 None
        """
        if fragment_id in self._cache:
            # LRU: 移到末尾
            self._cache.move_to_end(fragment_id)
            return self._cache[fragment_id]
        return None
    
    def put(self, fragment: Fragment) -> None:
        """存入缓存
        
        Args:
            fragment: Fragment 实例
        """
        if not fragment or not fragment.id:
            return
        
        if fragment.id in self._cache:
            self._cache.move_to_end(fragment.id)
        else:
            self._cache[fragment.id] = fragment
        
        # LRU 驱逐
        while len(self._cache) > self.capacity:
            oldest = next(iter(self._cache))
            self._cache.pop(oldest)
    
    def batch_put(self, fragments: List[Fragment]) -> None:
        """批量存入缓存
        
        Args:
            fragments: Fragment 列表
        """
        for f in fragments:
            self.put(f)
    
    def evict(self, fragment_id: str) -> bool:
        """驱逐缓存
        
        Args:
            fragment_id: Fragment ID
            
        Returns:
            是否驱逐成功
        """
        if fragment_id in self._cache:
            self._cache.pop(fragment_id)
            return True
        return False
    
    def clear(self) -> None:
        """清空缓存"""
        self._cache.clear()
    
    def get_all(self) -> List[Fragment]:
        """获取所有缓存的 Fragment"""
        return list(self._cache.values())
    
    def count(self) -> int:
        """缓存数量"""
        return len(self._cache)
    
    def contains(self, fragment_id: str) -> bool:
        """检查是否在缓存中"""
        return fragment_id in self._cache


class CacheLayer:
    """三层缓存管理器
    
    统一接口封装 hot / warm / cold 三层缓存
    """
    
    def __init__(
        self,
        store: FragmentStore,
        vector_index: Optional[VectorIndex] = None,
        hot_capacity: int = 50,
    ):
        """初始化缓存层
        
        Args:
            store: FragmentStore 实例（warm 层）
            vector_index: VectorIndex 实例（cold 层）
            hot_capacity: 热层容量
        """
        self.store = store
        self.vector_index = vector_index
        self.hot_cache = HotCache(capacity=hot_capacity)
    
    def get(self, fragment_id: str, layer_hint: Optional[str] = None) -> Optional[Fragment]:
        """获取 Fragment（自动查找各层）
        
        Args:
            fragment_id: Fragment ID
            layer_hint: 层提示，None 则依次查找 hot -> warm -> cold
            
        Returns:
            Fragment 或 None
        """
        # 1. 热层
        if layer_hint is None or layer_hint == "hot":
            f = self.hot_cache.get(fragment_id)
            if f:
                return f
        
        # 2. 温层
        if layer_hint is None or layer_hint == "warm":
            f = self.store.get(fragment_id)
            if f:
                # 提升到热层
                self.hot_cache.put(f)
                return f
        
        # 3. 冷层（需要从向量索引获取 ID，这里只能返回 None）
        # 冷层存储在向量索引中，不存储完整对象
        return None
    
    def put(self, fragment: Fragment, layer: str = "hot") -> None:
        """存入缓存
        
        Args:
            fragment: Fragment 实例
            layer: 目标层，默认 "hot"
        """
        if layer == "hot":
            self.hot_cache.put(fragment)
        elif layer == "warm":
            # 写入 SQLite（upsert：有则更新，无则插入）
            existing = self.store.get(fragment.id)
            if existing:
                self.store.update(fragment)
            else:
                self.store.insert(fragment)
            # 不驱逐热层——热层是缓存加速层
        elif layer == "cold":
            # 冷层不存储完整对象，只存储向量
            # 由上层调用 vector_index.add_vector
            pass
    
    def evict(self, fragment_id: str) -> bool:
        """驱逐 Fragment
        
        Args:
            fragment_id: Fragment ID
            
        Returns:
            是否驱逐成功
        """
        # 从热层驱逐
        self.hot_cache.evict(fragment_id)
        # 从温层删除
        self.store.delete(fragment_id)
        return True
    
    def cold_search(
        self,
        query_vector: List[float],
        top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        """冷层搜索（向量检索）
        
        Args:
            query_vector: 查询向量
            top_k: 返回数量
            
        Returns:
            [{"id": str, "distance": float, "similarity": float}, ...]
        """
        if not self.vector_index:
            return []
        
        return self.vector_index.search(query_vector, top_k)
    
    def cold_add_vector(
        self,
        fragment_id: str,
        vector: List[float],
    ) -> None:
        """冷层添加向量
        
        Args:
            fragment_id: Fragment ID
            vector: 向量
        """
        if self.vector_index:
            self.vector_index.add_vector(fragment_id, vector)
    
    def stats(self) -> Dict[str, Any]:
        """缓存统计
        
        Returns:
            统计信息
        """
        store_stats = self.store.count_by_layer()
        
        return {
            "hot": {
                "count": self.hot_cache.count(),
                "capacity": self.hot_cache.capacity,
            },
            "warm": {
                "count": store_stats.get("warm", 0),
                "layer_counts": store_stats,
            },
            "cold": {
                "count": self.vector_index.count() if self.vector_index else 0,
            },
            "total": self.store.count(),
        }
    
    def clear_hot(self) -> None:
        """清空热层缓存"""
        self.hot_cache.clear()
    
    def sync_hot_from_warm(self, limit: int = 50) -> int:
        """从温层同步到热层
        
        Args:
            limit: 同步数量
            
        Returns:
            同步数量
        """
        fragments = self.store.get_by_layer("hot", limit=limit)
        count = 0
        for f in fragments:
            if not self.hot_cache.contains(f.id):
                self.hot_cache.put(f)
                count += 1
        return count
