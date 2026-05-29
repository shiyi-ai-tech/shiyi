"""shiyi.providers.embedding.deepseek - DeepSeek Embedding调用实现

职责：
- 封装HTTP调用DeepSeek Embedding API
- 支持 embed/embed_batch 接口
- 自动重试机制

API: https://api.deepseek.com/v1/embeddings
模型: deepseek-embed (1024维)
"""

import os
import time
import logging
from typing import List, Optional

import requests

from shiyi.common.interfaces import EmbeddingProvider


logger = logging.getLogger(__name__)


class DeepSeekEmbeddingCaller(EmbeddingProvider):
    """DeepSeek Embedding调用器
    
    使用 DeepSeek 的 embedding API
    模型: deepseek-embed 或 text-embedding-3-small
    维度: 1024
    """
    
    API_URL = "https://api.deepseek.com/v1/embeddings"
    DEFAULT_MODEL = "deepseek-embed"
    DIMENSION = 1024
    MAX_RETRIES = 3
    INITIAL_DELAY = 1.0  # 秒
    
    def __init__(self, api_key: Optional[str] = None):
        """初始化DeepSeek Embedding调用器
        
        Args:
            api_key: DeepSeek API密钥，默认从环境变量DEEPSEEK_API_KEY读取
        """
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not self._api_key:
            raise ValueError(
                "DeepSeek API key not found. "
                "Please set DEEPSEEK_API_KEY environment variable."
            )
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        })
    
    def embed(self, text: str) -> List[float]:
        """生成单个文本的embedding向量
        
        Args:
            text: 输入文本
            
        Returns:
            embedding向量，List[float]，维度1024
        """
        results = self.embed_batch([text])
        return results[0] if results else []
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量生成embedding向量
        
        Args:
            texts: 输入文本列表
            
        Returns:
            embedding向量列表，List[List[float]]
        """
        payload = {
            "model": self.DEFAULT_MODEL,
            "input": texts,
        }
        
        last_error = None
        delay = self.INITIAL_DELAY
        
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self._session.post(
                    self.API_URL,
                    json=payload,
                    timeout=30,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    embeddings = []
                    for item in data.get("data", []):
                        embedding = item.get("embedding", [])
                        embeddings.append(embedding)
                    return embeddings
                
                elif response.status_code == 429:
                    # 限流，重试
                    last_error = f"Rate limited (429): {response.text}"
                    logger.warning(f"DeepSeek API rate limited, retry {attempt + 1}/{self.MAX_RETRIES}")
                
                elif response.status_code >= 500:
                    # 服务端错误，重试
                    last_error = f"Server error ({response.status_code}): {response.text}"
                    logger.warning(f"DeepSeek API server error, retry {attempt + 1}/{self.MAX_RETRIES}")
                
                else:
                    # 其他错误，直接返回空
                    last_error = f"API error ({response.status_code}): {response.text}"
                    logger.error(f"DeepSeek API error: {last_error}")
                    return []
                
            except requests.exceptions.Timeout:
                last_error = "Request timeout"
                logger.warning(f"DeepSeek API timeout, retry {attempt + 1}/{self.MAX_RETRIES}")
            
            except requests.exceptions.RequestException as e:
                last_error = f"Request error: {e}"
                logger.warning(f"DeepSeek API request error, retry {attempt + 1}/{self.MAX_RETRIES}")
            
            # 等待后重试
            if attempt < self.MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2  # 指数退避
        
        logger.error(f"DeepSeek Embedding API failed after {self.MAX_RETRIES} retries: {last_error}")
        return []
    
    def is_available(self) -> bool:
        """检查DeepSeek Embedding API是否可用
        
        Returns:
            True if API可访问且key有效
        """
        try:
            # 发送一个简单请求测试
            result = self.embed("test")
            return len(result) > 0
        except Exception:
            return False
    
    @property
    def dimension(self) -> int:
        """返回embedding向量的维度"""
        return self.DIMENSION
