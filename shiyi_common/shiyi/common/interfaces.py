"""shiyi-common 抽象接口

core层零网络依赖，所有网络调用必须由shell层实现。
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Generator


class KnowledgeBaseAdapter(ABC):
    """知识库适配器抽象类"""

    @abstractmethod
    def query(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """查询知识库

        Returns:
            [{"content": "...", "metadata": {...}}, ...]
        """
        return []


class LLMProvider(ABC):
    """LLM调用抽象接口
    
    core层零网络依赖，LLM调用必须在shell层实现。
    这里只定义抽象接口，具体HTTP实现在shell/shiyi/llm_caller.py中。
    """

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """调用LLM对话

        Args:
            messages: [{"role": "system/user/assistant", "content": "..."}]
            model: 模型名称
            temperature: 温度
            max_tokens: 最大token数
            tools: 可选的工具定义列表，用于Function Calling

        Returns:
            LLM回复文本 或 Function Calling结果(dict)
        """
        ...

    @abstractmethod
    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2000,
    ) -> Generator[str, None, None]:
        """流式调用LLM对话

        返回生成器，逐token yield文本。
        与 chat() 相同的参数，但输出为流式生成器。

        Returns:
            Generator yielding token strings
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """检查LLM是否可用"""
        ...


class EmbeddingProvider(ABC):
    """Embedding生成抽象接口
    
    core层零网络依赖，embedding调用必须在shell层实现。
    这里只定义抽象接口，具体HTTP实现在shell/shiyi/embedding_caller.py中。
    
    方法：
    - embed(text) -> List[float]: 生成单个文本的embedding向量
    - embed_batch(texts) -> List[List[float]]: 批量生成embedding向量
    """

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        """生成单个文本的embedding向量"""
        ...

    @abstractmethod
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量生成embedding向量"""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """检查embedding服务是否可用"""
        ...
