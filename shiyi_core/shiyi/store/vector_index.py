"""VectorIndex - HNSW 向量索引层

功能：
- 使用 hnswlib 实现 HNSW 近似最近邻搜索
- 方法：add_vector, batch_add, search, save, load
- 索引持久化到文件
"""

import os
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

try:
    import hnswlib
    HNSWLIB_AVAILABLE = True
except ImportError:
    HNSWLIB_AVAILABLE = False

from shiyi.common.errors import StorageError


class VectorIndex:
    """HNSW 向量索引
    
    属性：
        space: 向量空间类型，默认 'cosine'
        dim: 向量维度
    """
    
    def __init__(
        self,
        index_path: str = "",
        space: str = "cosine",
        dim: int = 1024,
        max_elements: int = 10000,
        ef_construction: int = 200,
        M: int = 16,
    ):
        """初始化向量索引
        
        Args:
            index_path: 索引文件路径，默认 ~/.shiyi/data/vector_index.bin
            space: 向量空间，'cosine' 或 'l2'
            dim: 向量维度
            max_elements: 最大元素数量
            ef_construction: 构建时的 ef 参数
            M: HNSW 的 M 参数
        """
        if not HNSWLIB_AVAILABLE:
            raise StorageError("hnswlib 未安装，请运行: pip install hnswlib")
        
        self.space = space
        self.dim = dim
        self.max_elements = max_elements
        self.ef_construction = ef_construction
        self.M = M
        
        if not index_path:
            index_path = str(Path.home() / ".shiyi" / "data" / "vector_index.bin")
        self.index_path = index_path
        Path(self.index_path).parent.mkdir(parents=True, exist_ok=True)
        
        self._index: Optional[hnswlib.Index] = None
        self._id_to_external: Dict[int, str] = {}  # 内部ID -> 外部ID
        self._external_to_id: Dict[str, int] = {}  # 外部ID -> 内部ID
        self._deleted_ids: set = set()  # 已标记删除的内部ID
        self._current_id: int = 0
        
        self._init_index()
    
    def _init_index(self) -> None:
        """初始化 HNSW 索引"""
        self._index = hnswlib.Index(space=self.space, dim=self.dim)
        self._index.set_num_threads(4)
        
        # 尝试加载已有索引
        if os.path.exists(self.index_path):
            try:
                self._load_index()
                return
            except Exception as e:
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to load vector index, rebuilding: {e}")
        
        # 创建新索引
        self._index.init_index(
            max_elements=self.max_elements,
            ef_construction=self.ef_construction,
            M=self.M,
        )
        self._id_to_external = {}
        self._external_to_id = {}
        self._current_id = 0
    
    def add_vector(
        self,
        fragment_id: str,
        vector: List[float],
    ) -> None:
        """添加单个向量
        
        Args:
            fragment_id: Fragment ID
            vector: 向量列表
        """
        if not self._index:
            raise StorageError("索引未初始化")
        
        if len(vector) != self.dim:
            raise StorageError(f"向量维度不匹配: 期望 {self.dim}, 实际 {len(vector)}")
        
        # 如果已存在，更新
        if fragment_id in self._external_to_id:
            internal_id = self._external_to_id[fragment_id]
            self._index.update_vector(vector, internal_id)
            return
        
        # 添加新向量
        self._index.add_items(
            [vector],
            [self._current_id],
        )
        
        self._id_to_external[self._current_id] = fragment_id
        self._external_to_id[fragment_id] = self._current_id
        self._current_id += 1
    
    def batch_add(
        self,
        items: List[Dict[str, Any]],
    ) -> None:
        """批量添加向量
        
        Args:
            items: [{"id": str, "vector": List[float]}, ...]
        """
        if not items:
            return
        
        ids = []
        vectors = []
        
        for item in items:
            fragment_id = item["id"]
            vector = item["vector"]
            
            if len(vector) != self.dim:
                continue
            
            if fragment_id in self._external_to_id:
                # 更新已有
                internal_id = self._external_to_id[fragment_id]
                self._index.update_items([vector], [internal_id])
                continue
            
            ids.append(self._current_id)
            vectors.append(vector)
            
            self._id_to_external[self._current_id] = fragment_id
            self._external_to_id[fragment_id] = self._current_id
            self._current_id += 1
        
        if vectors:
            self._index.add_items(vectors, ids)
    
    def search(
        self,
        query_vector: List[float],
        top_k: int = 10,
        ef: int = 100,
    ) -> List[Dict[str, Any]]:
        """向量搜索
        
        Args:
            query_vector: 查询向量
            top_k: 返回数量
            ef: 搜索时的 ef 参数，越大越精确但越慢
            
        Returns:
            [{"id": str, "distance": float}, ...]
        """
        if not self._index:
            raise StorageError("索引未初始化")
        
        if len(query_vector) != self.dim:
            raise StorageError(f"向量维度不匹配: 期望 {self.dim}, 实际 {len(query_vector)}")
        
        # 设置搜索参数
        self._index.set_ef(ef)
        
        # 搜索
        labels, distances = self._index.knn_query(
            [query_vector],
            k=min(top_k, self._index.get_current_count()),
        )
        
        results = []
        for label, distance in zip(labels[0], distances[0]):
            label_int = int(label)
            if label_int in self._deleted_ids:
                continue
            external_id = self._id_to_external.get(label_int)
            if external_id:
                # 余弦距离转相似度
                similarity = 1.0 - distance if self.space == "cosine" else 0.0
                results.append({
                    "id": external_id,
                    "distance": float(distance),
                    "similarity": float(similarity),
                })
        
        return results
    
    def delete(self, fragment_id: str) -> bool:
        """删除向量（标记为已删除）
        
        Args:
            fragment_id: Fragment ID
            
        Returns:
            是否删除成功
        """
        if fragment_id not in self._external_to_id:
            return False
        
        # HNSW 不支持真正删除，在外部维护 deleted set
        internal_id = self._external_to_id[fragment_id]
        self._deleted_ids.add(internal_id)
        return True
    
    def save(self) -> None:
        """保存索引到文件"""
        if not self._index:
            return
        
        self._index.save_index(self.index_path)
        
        # 保存映射表
        meta_path = self.index_path + ".meta"
        with open(meta_path, "w") as f:
            json.dump({
                "id_to_external": self._id_to_external,
                "external_to_id": self._external_to_id,
                "current_id": self._current_id,
                "deleted_ids": list(self._deleted_ids),
                "space": self.space,
                "dim": self.dim,
            }, f)
    
    def _load_index(self) -> None:
        """加载索引"""
        self._index.load_index(self.index_path)
        
        # 加载映射表
        meta_path = self.index_path + ".meta"
        if os.path.exists(meta_path):
            with open(meta_path, "r") as f:
                meta = json.load(f)
                self._id_to_external = meta["id_to_external"]
                self._external_to_id = meta["external_to_id"]
                self._current_id = meta["current_id"]
                self._deleted_ids = set(meta.get("deleted_ids", []))
                self.space = meta.get("space", "cosine")
                self.dim = meta.get("dim", self.dim)
    
    def count(self) -> int:
        """向量数量
        
        Returns:
            数量
        """
        if not self._index:
            return 0
        return self._index.get_current_count()
    
    def clear(self) -> None:
        """清空索引"""
        self._init_index()
        if os.path.exists(self.index_path):
            os.remove(self.index_path)
        meta_path = self.index_path + ".meta"
        if os.path.exists(meta_path):
            os.remove(meta_path)
