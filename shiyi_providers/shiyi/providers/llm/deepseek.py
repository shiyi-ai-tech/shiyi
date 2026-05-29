"""shiyi.providers.llm.deepseek - DeepSeek LLM调用实现

职责：
- 封装HTTP调用DeepSeek API的具体实现
- 支持 chat/stream_chat/is_available 接口
- 自动重试机制（超时/429/5xx → 重试3次，间隔递增）

API Key从环境变量DEEPSEEK_API_KEY读取
"""

import os
import sys
import time
import json
import logging
from typing import List, Dict, Any, Optional, Union, Generator

import requests

from shiyi.common.interfaces import LLMProvider


logger = logging.getLogger(__name__)


class DeepSeekLLMCaller(LLMProvider):
    """DeepSeek LLM调用器

    支持模型：
    - deepseek-v4-flash (默认): 轻量级快速模型
    - deepseek-v4: 完整版模型
    - deepseek-chat: 对话模型

    特性：
    - Function Calling 支持
    - 流式输出支持
    - 自动重试机制
    """

    # 硬编码默认值，可通过 api_url 参数覆盖
    DEFAULT_API_URL = "https://api.deepseek.com/v1"
    DEFAULT_MODEL = "deepseek-v4-flash"
    MAX_RETRIES = 3
    INITIAL_DELAY = 1.0  # 秒

    def __init__(self, api_key: Optional[str] = None, api_url: Optional[str] = None):
        """初始化DeepSeek调用器

        Args:
            api_key: DeepSeek API密钥，默认从环境变量DEEPSEEK_API_KEY读取
            api_url: API地址，默认 https://api.deepseek.com/v1
        """
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not self._api_key:
            raise ValueError(
                "DeepSeek API key not found. "
                "Please set DEEPSEEK_API_KEY environment variable."
            )
        self._api_url = (api_url or self.DEFAULT_API_URL).rstrip("/") + "/chat/completions"
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        })
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "deepseek-v4-flash",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """调用DeepSeek LLM聊天接口
        
        返回:
            str — 普通文本回复
            dict — Function Calling 工具调用
        
        Args:
            messages: 消息列表
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大token数
            
        Returns:
            LLM回复文本或工具调用结构
        """
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        
        last_error = None
        delay = self.INITIAL_DELAY
        
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self._session.post(
                    self._api_url,
                    json=payload,
                    timeout=60,
                )
                
                # 处理不同HTTP状态码
                if response.status_code == 200:
                    data = response.json()
                    choice = data.get("choices", [{}])[0]
                    message = choice.get("message", {})
                    
                    # 检测 Function Calling 返回
                    if message.get("tool_calls"):
                        return {
                            "type": "tool_call",
                            "tool_calls": message["tool_calls"],
                            "message": message,
                            "content": message.get("content"),
                        }
                    
                    return message.get("content", "")
                
                elif response.status_code == 429:
                    # Rate limit，重试
                    logger.warning(f"DeepSeek API rate limited (429), retry {attempt + 1}/{self.MAX_RETRIES}")
                    last_error = f"Rate limited (429)"
                
                elif 500 <= response.status_code < 600:
                    # 服务器错误，重试
                    logger.warning(f"DeepSeek API server error ({response.status_code}), retry {attempt + 1}/{self.MAX_RETRIES}")
                    last_error = f"Server error ({response.status_code})"
                
                else:
                    # 其他错误，不重试
                    error_msg = f"API error: {response.status_code} - {response.text[:200]}"
                    logger.error(error_msg)
                    raise Exception(error_msg)
                    
            except requests.exceptions.Timeout:
                logger.warning(f"DeepSeek API timeout, retry {attempt + 1}/{self.MAX_RETRIES}")
                last_error = "Timeout"
            
            except requests.exceptions.RequestException as e:
                logger.warning(f"DeepSeek API request failed: {e}, retry {attempt + 1}/{self.MAX_RETRIES}")
                last_error = str(e)
            
            # 递增延迟重试
            if attempt < self.MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2  # 递增延迟
        
        # 所有重试都失败
        raise Exception(f"DeepSeek API failed after {self.MAX_RETRIES} retries: {last_error}")
    
    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "deepseek-v4-flash",
        temperature: float = 0.7,
        max_tokens: int = 2000,
    ) -> Generator[str, None, None]:
        """流式调用DeepSeek LLM，逐个token yield
        
        DeepSeek API 支持 stream=True，返回 SSE 格式：
        data: {"choices":[{"delta":{"content":"Hello"}}]}
        data: [DONE]
        
        Args:
            messages: 消息列表
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大token数
            
        Yields:
            每个token字符串
        """
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        
        response = self._session.post(
            self._api_url,
            json=payload,
            timeout=120,
            stream=True,
        )
        response.raise_for_status()
        
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]  # Remove "data: " prefix
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
                choices = data.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
    
    def is_available(self) -> bool:
        """检查LLM是否可用
        
        Returns:
            True if API可访问且key有效
        """
        if not self._api_key:
            return False
        try:
            # 发送一个简单的测试请求
            response = self._session.post(
                self._api_url,
                json={
                    "model": self.DEFAULT_MODEL,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 5,
                },
                timeout=10,
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"DeepSeek API availability check failed: {e}")
            return False


class MockLLMCaller(LLMProvider):
    """Mock LLM调用器，用于测试
    
    特性：
    - 返回固定或根据查询生成的mock响应
    - 支持意图识别请求的模拟响应
    - 支持流式输出模拟
    """
    
    def __init__(self):
        self._available = True
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "deepseek-v4-flash",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """返回mock响应"""
        last_message = messages[-1]["content"] if messages else ""
        
        # 检测是否是意图识别请求（包含特殊标记）
        if "[INTENT_ANALYSIS]" in last_message:
            # 提取查询（处理带历史上下文的情况）
            query = last_message
            if "当前输入：" in query:
                query = query.split("当前输入：")[-1]
            query = query.replace("[INTENT_ANALYSIS]", "").strip()
            return self._mock_intent_response(query)
        
        return "这是mock回复"
    
    def _mock_intent_response(self, query: str) -> str:
        """生成模拟意图识别响应"""
        # 默认闲聊意图
        return """{
    "intent": "chat",
    "sub_queries": [],
    "entities": [],
    "needs_retrieval": false,
    "is_followup": false,
    "confidence": 0.8
}"""
    
    def is_available(self) -> bool:
        return self._available
    
    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "deepseek-v4-flash",
        temperature: float = 0.7,
        max_tokens: int = 2000,
    ) -> Generator[str, None, None]:
        """流式mock响应"""
        reply = self.chat(messages, model=model, temperature=temperature, max_tokens=max_tokens)
        for char in reply:
            yield char
            time.sleep(0.01)


def create_llm_caller() -> Optional[LLMProvider]:
    """创建LLM调用器实例

    从环境变量DEEPSEEK_API_KEY读取API key
    如果key不存在或API不可用，返回None

    Returns:
        LLMProvider实例，如果API key不存在或不可用则返回None
    """
    # 检查是否有 Key
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.warning("DEEPSEEK_API_KEY not found")
        return None

    try:
        caller = DeepSeekLLMCaller(api_key=api_key)
        if caller.is_available():
            return caller
        else:
            logger.warning("DeepSeek API key exists but API unreachable or key invalid")
            return None
    except ValueError:
        logger.warning("DEEPSEEK_API_KEY not found, LLM unavailable")
        return None
    except Exception as e:
        logger.warning(f"Failed to create LLM caller: {e}")
        return None


def create_light_caller() -> Optional[LLMProvider]:
    """创建加速模型调用器 — 读取 SHIYI_LIGHT_* 环境变量

    优先级：
    - SHIYI_LIGHT_API_KEY > DEEPSEEK_API_KEY (fallback)
    - SHIYI_LIGHT_API_BASE > SHIYI_MAIN_API_BASE > 默认 DeepSeek URL

    Returns:
        LLMProvider 实例，或 None（无可用 Key）
    """
    api_key = os.environ.get("SHIYI_LIGHT_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.warning("No key available for light model (neither SHIYI_LIGHT_API_KEY nor DEEPSEEK_API_KEY)")
        return None
    api_url = os.environ.get("SHIYI_LIGHT_API_BASE") or os.environ.get("SHIYI_MAIN_API_BASE") or ""
    try:
        return DeepSeekLLMCaller(api_key=api_key, api_url=api_url)
    except Exception as e:
        logger.warning(f"Failed to create light caller: {e}")
        return None


def create_fallback_caller() -> Optional[LLMProvider]:
    """创建备用模型调用器 — 读取 SHIYI_FALLBACK_* 环境变量

    优先级：
    - SHIYI_FALLBACK_API_KEY > DEEPSEEK_API_KEY (fallback)
    - SHIYI_FALLBACK_API_BASE > SHIYI_MAIN_API_BASE > 默认 DeepSeek URL

    Returns:
        LLMProvider 实例，或 None（未配置或 Key 缺失）
    """
    api_key = os.environ.get("SHIYI_FALLBACK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.warning("No key available for fallback model (neither SHIYI_FALLBACK_API_KEY nor DEEPSEEK_API_KEY)")
        return None
    api_url = os.environ.get("SHIYI_FALLBACK_API_BASE") or os.environ.get("SHIYI_MAIN_API_BASE") or ""
    try:
        return DeepSeekLLMCaller(api_key=api_key, api_url=api_url)
    except Exception as e:
        logger.warning(f"Failed to create fallback caller: {e}")
        return None
