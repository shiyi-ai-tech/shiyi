"""shiyi.providers.embedding.factory - Embedding调用器工厂

职责：
- 根据环境变量自动选择并创建合适的Embedding调用器
- 优先级：SiliconFlow > DeepSeek > BGE
"""

import os
import logging
from typing import Optional

from shiyi.common.interfaces import EmbeddingProvider
from shiyi.providers.embedding.deepseek import DeepSeekEmbeddingCaller
from shiyi.providers.embedding.bge import BGEEmbeddingCaller, SiliconFlowEmbeddingCaller


logger = logging.getLogger(__name__)


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
