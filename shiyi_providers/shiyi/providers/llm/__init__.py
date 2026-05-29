"""shiyi.providers.llm - LLM调用实现

提供多种LLM提供商的调用实现：
- DeepSeekLLMCaller: DeepSeek API调用
- MockLLMCaller: 测试用的Mock实现

使用示例：
    >>> from shiyi.providers.llm import DeepSeekLLMCaller
    >>> caller = DeepSeekLLMCaller(api_key="your-key")
    >>> response = caller.chat([{"role": "user", "content": "你好"}])
"""

from shiyi.providers.llm.deepseek import (
    DeepSeekLLMCaller,
    MockLLMCaller,
    create_llm_caller,
    create_light_caller,
    create_fallback_caller,
)

__all__ = [
    "DeepSeekLLMCaller",
    "MockLLMCaller",
    "create_llm_caller",
    "create_light_caller",
    "create_fallback_caller",
]
