"""shiyi-common 错误定义"""


class ShiyiError(Exception):
    """基础错误"""
    pass


class ConfigError(ShiyiError):
    """配置错误"""
    pass


class StorageError(ShiyiError):
    """存储错误"""
    pass


class LLMError(ShiyiError):
    """LLM调用错误"""
    pass


class LLMUnavailableError(ShiyiError):
    """LLM服务不可用错误 - 服务不可用时应明确报错引导用户解决，不降级"""
    pass
