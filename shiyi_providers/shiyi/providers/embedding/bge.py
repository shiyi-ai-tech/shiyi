"""shiyi.providers.embedding.bge - BGE Embedding调用实现

职责：
- 封装本地BGE-M3服务的HTTP调用
- 封装SiliconFlow BGE-M3 API调用
- 支持 embed/embed_batch 接口

BGE-M3: BAAI通用多语言多粒度嵌入模型
维度: 1024 (可扩展)
"""

import os
import time
import logging
from typing import List, Optional

import requests

from shiyi.common.interfaces import EmbeddingProvider


logger = logging.getLogger(__name__)


class BGEEmbeddingCaller(EmbeddingProvider):
    """BGE Embedding调用器 - 本地部署
    
    使用本地部署的 BGE-M3 模型服务
    默认地址: http://localhost:8000
    """
    
    API_URL = "http://localhost:8000/embed"
    DIMENSION = 1024
    MAX_RETRIES = 3
    INITIAL_DELAY = 1.0
    
    def __init__(self, api_url: Optional[str] = None, dimension: int = 1024):
        """初始化BGE Embedding调用器
        
        Args:
            api_url: BGE API地址，默认 http://localhost:8000/embed
            dimension: embedding向量维度
        """
        self._api_url = api_url or os.environ.get("BGE_API_URL", self.API_URL)
        self.DIMENSION = dimension
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
        })
    
    def embed(self, text: str) -> List[float]:
        """生成单个文本的embedding向量
        
        Args:
            text: 输入文本
            
        Returns:
            embedding向量，List[float]
        """
        results = self.embed_batch([text])
        return results[0] if results else []
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量生成embedding向量
        
        Args:
            texts: 输入文本列表
            
        Returns:
            embedding向量列表
        """
        payload = {"inputs": texts}
        
        last_error = None
        delay = self.INITIAL_DELAY
        
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self._session.post(
                    self._api_url,
                    json=payload,
                    timeout=30,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return data.get("embeddings", [])
                
                last_error = f"API error ({response.status_code}): {response.text}"
                
            except requests.exceptions.RequestException as e:
                last_error = f"Request error: {e}"
            
            if attempt < self.MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2
        
        logger.error(f"BGE Embedding API failed: {last_error}")
        return []
    
    def is_available(self) -> bool:
        """检查BGE API是否可用
        
        Returns:
            True if 本地服务可访问
        """
        try:
            result = self.embed("test")
            return len(result) > 0
        except Exception:
            return False
    
    @property
    def dimension(self) -> int:
        return self.DIMENSION


class SiliconFlowEmbeddingCaller(EmbeddingProvider):
    """SiliconFlow Embedding调用器
    
    使用 SiliconFlow 的 BGE-M3 embedding API
    模型: BAAI/bge-m3
    维数: 1024
    """
    
    DEFAULT_MODEL = "BAAI/bge-m3"
    DIMENSION = 1024
    MAX_RETRIES = 3
    INITIAL_DELAY = 1.0
    
    def __init__(self, api_key: Optional[str] = None, api_base: Optional[str] = None):
        """初始化 SiliconFlow Embedding 调用器
        
        Args:
            api_key: API key，默认从 EMBEDDING_API_KEY 读取
            api_base: API 地址，默认从 EMBEDDING_API_BASE 读取
        """
        self._api_key = api_key or os.environ.get("EMBEDDING_API_KEY")
        if not self._api_key:
            raise ValueError(
                "SiliconFlow API key not found. "
                "Please set EMBEDDING_API_KEY environment variable."
            )
        self._api_base = api_base or os.environ.get("EMBEDDING_API_BASE", "https://api.siliconflow.cn/v1")
        self._api_url = f"{self._api_base.rstrip('/')}/embeddings"
        self._model = os.environ.get("EMBEDDING_MODEL", self.DEFAULT_MODEL)
        
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        })
    
    def embed(self, text: str) -> List[float]:
        """生成单个文本的embedding向量"""
        results = self.embed_batch([text])
        return results[0] if results else []
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量生成embedding向量"""
        payload = {
            "model": self._model,
            "input": texts,
        }
        
        last_error = None
        delay = self.INITIAL_DELAY
        
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self._session.post(
                    self._api_url,
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
                    logger.warning(f"SiliconFlow API rate limited (429), retry {attempt + 1}/{self.MAX_RETRIES}")
                    last_error = "Rate limited (429)"
                
                elif 500 <= response.status_code < 600:
                    logger.warning(f"SiliconFlow API server error ({response.status_code}), retry {attempt + 1}/{self.MAX_RETRIES}")
                    last_error = f"Server error ({response.status_code})"
                
                else:
                    error_msg = f"API error: {response.status_code} - {response.text[:200]}"
                    logger.error(error_msg)
                    raise Exception(error_msg)
                    
            except requests.exceptions.Timeout:
                logger.warning(f"SiliconFlow API timeout, retry {attempt + 1}/{self.MAX_RETRIES}")
                last_error = "Timeout"
            
            except requests.exceptions.RequestException as e:
                logger.warning(f"SiliconFlow API request failed: {e}, retry {attempt + 1}/{self.MAX_RETRIES}")
                last_error = str(e)
            
            if attempt < self.MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2
        
        logger.error(f"SiliconFlow API failed: {last_error}")
        return []
    
    def is_available(self) -> bool:
        try:
            result = self.embed("test")
            return len(result) > 0
        except Exception:
            return False
    
    @property
    def dimension(self) -> int:
        return self.DIMENSION
