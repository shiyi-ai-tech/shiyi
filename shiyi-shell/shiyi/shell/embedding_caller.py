"""shell层 - Embedding调用实现

职责：
- 封装HTTP调用embedding服务的具体实现
- core层零网络依赖，embedding调用必须在shell层
- 支持 DeepSeek 或 bge-m3 的 embedding API

使用 requests 库调用 API
API key 从环境变量读取
错误处理：超时/429/5xx → 自动重试3次，间隔递增
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
            embedding向量，List[float]
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
        """检查DeepSeek Embedding API是否可用"""
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


class BGEEmbeddingCaller(EmbeddingProvider):
    """BGE Embedding调用器
    
    使用本地部署的 BGE-M3 模型
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
        """生成单个文本的embedding向量"""
        results = self.embed_batch([text])
        return results[0] if results else []
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量生成embedding向量"""
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
        """检查BGE API是否可用"""
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
        results = self.embed_batch([text])
        return results[0] if results else []
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
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


def create_embedding_caller() -> Optional[EmbeddingProvider]:
    """创建Embedding调用器
    
    尝试按顺序创建：
    1. SiliconFlow Embedding（如果 EMBEDDING_API_KEY 存在）
    2. DeepSeek Embedding（如果 DEEPSEEK_API_KEY 存在）
    3. BGE Embedding（如果本地服务可用）
    4. 返回 None（完全降级）
    
    Returns:
        EmbeddingProvider实例，或None
    """
    # 1. 尝试 SiliconFlow
    siliconflow_key = os.environ.get("EMBEDDING_API_KEY")
    if siliconflow_key:
        try:
            logger.info("Using SiliconFlow Embedding (BGE-M3)")
            return SiliconFlowEmbeddingCaller(api_key=siliconflow_key)
        except Exception as e:
            logger.warning(f"Failed to create SiliconFlow Embedding caller: {e}")
    
    # 2. 尝试 DeepSeek
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    if deepseek_key:
        try:
            logger.info("Using DeepSeek Embedding")
            return DeepSeekEmbeddingCaller(api_key=deepseek_key)
        except Exception as e:
            logger.warning(f"Failed to create DeepSeek Embedding caller: {e}")
    
    # 3. 尝试 BGE
    try:
        bge = BGEEmbeddingCaller()
        if bge.is_available():
            return bge
    except Exception as e:
        logger.warning(f"Failed to create BGE Embedding caller: {e}")
    
    # 4. 完全降级
    logger.info("No embedding provider available, vector search disabled")
    return None
