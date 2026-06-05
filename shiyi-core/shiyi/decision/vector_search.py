"""VectorSearch - 语义搜索

职责：
- 把embedding生成+向量搜索串起来
- core零网络依赖！embedding生成通过抽象接口(EmbeddingProvider)
- shell层实现具体HTTP调用

输入：query_text -> 调用EmbeddingProvider -> 得到vector -> VectorIndex.search
方法：search(query_text, top_k) -> List[SearchResult]
降级：无EmbeddingProvider时跳过向量搜索
"""

import logging
from typing import List, Dict, Any, Optional

from shiyi.common.interfaces import EmbeddingProvider


logger = logging.getLogger(__name__)


# ═══ 搜索结果结构 ═══

class SearchResult:
    """搜索结果"""
    
    def __init__(
        self,
        fragment_id: str,
        fact_kernel: str,
        score: float,
        source: str = "vector",
    ):
        self.fragment_id = fragment_id
        self.fact_kernel = fact_kernel
        self.score = score
        self.source = source
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "fragment_id": self.fragment_id,
            "fact_kernel": self.fact_kernel,
            "score": self.score,
            "source": self.source,
        }


class VectorSearch:
    """向量语义搜索
    
    整合 EmbeddingProvider + VectorIndex
    core层零网络依赖，embedding调用通过EmbeddingProvider接口
    """
    
    def __init__(
        self,
        embedding_provider: Optional[EmbeddingProvider] = None,
        vector_index: Optional[Any] = None,  # VectorIndex实例
    ):
        """初始化向量搜索
        
        Args:
            embedding_provider: Embedding生成服务（shell层注入）
            vector_index: 向量索引实例（VectorIndex）
        """
        self._embedding_provider = embedding_provider
        self._vector_index = vector_index
    
    @property
    def is_available(self) -> bool:
        """检查向量搜索是否可用"""
        return (
            self._embedding_provider is not None 
            and self._embedding_provider.is_available()
            and self._vector_index is not None
        )
    
    def search(
        self,
        query_text: str,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """语义搜索
        
        流程：
        1. 调用EmbeddingProvider生成query的embedding向量
        2. 使用VectorIndex搜索相似的记忆
        3. 返回搜索结果
        
        Args:
            query_text: 查询文本
            top_k: 返回数量
            
        Returns:
            搜索结果列表 [{"fragment_id": str, "fact_kernel": str, "score": float, "source": str}, ...]
        """
        # 降级：无embedding服务时返回空
        if not self.is_available:
            logger.debug("Vector search unavailable, skipping")
            return []
        
        try:
            # 1. 生成embedding
            vector = self._embedding_provider.embed(query_text)
            if not vector:
                logger.warning("Failed to generate embedding")
                return []
            
            # 2. 向量搜索
            raw_results = self._vector_index.search(vector, top_k=top_k)
            
            # 3. 转换为标准格式
            results = []
            for r in raw_results:
                results.append({
                    "fragment_id": r.get("id", ""),
                    "fact_kernel": r.get("fact_kernel", ""),
                    "score": r.get("similarity", 0.0),
                    "source": "vector",
                })
            
            return results
            
        except Exception as e:
            logger.error(f"Vector search error: {e}")
            return []
    
    def search_with_threshold(
        self,
        query_text: str,
        top_k: int = 10,
        min_score: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """带阈值过滤的语义搜索
        
        Args:
            query_text: 查询文本
            top_k: 返回数量
            min_score: 最低相似度阈值
            
        Returns:
            过滤后的搜索结果
        """
        results = self.search(query_text, top_k=top_k * 2)  # 多取一些，过滤后可能不够
        return [r for r in results if r.get("score", 0) >= min_score][:top_k]


def create_vector_search(
    embedding_provider: Optional[EmbeddingProvider] = None,
    vector_index: Optional[Any] = None,
) -> VectorSearch:
    """创建向量搜索实例
    
    Args:
        embedding_provider: Embedding服务
        vector_index: 向量索引
        
    Returns:
        VectorSearch实例
    """
    return VectorSearch(
        embedding_provider=embedding_provider,
        vector_index=vector_index,
    )
