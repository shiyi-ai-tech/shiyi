"""shiyi-core 配置加载模块

负责加载和校验 config.json 配置文件
"""

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from shiyi.common.errors import ConfigError
from shiyi.common.constants import (
    DEFAULT_MAIN_LLM_MODEL,
    DEFAULT_LIGHT_LLM_MODEL,
    DEFAULT_LLM_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_DEEPSEEK_EMBED_MODEL,
    DEFAULT_LLM_PROVIDER,
    DEFAULT_EMBEDDING_PROVIDER,
)


# 默认配置
DEFAULT_CONFIG: Dict[str, Any] = {
    "version": "0.11.1",
    "features": {
        "memory": True,
        "actions": False,
        "knowledge_base": False,
        "web_chat": False,
    },
    "memory": {
        "halflife_days": 60,
        "emotion_multiplier": 1.5,
        "access_multiplier": 2.0,
        "max_hops": 2,
        "decay_per_hop": 0.5,
        "hot_capacity": 50,
        "warm_capacity": 500,
        "embedding_model": "BAAI/bge-m3",
        "embedding_base": "https://api.siliconflow.cn/v1",
    },
    "llm": {
        "main_model": DEFAULT_MAIN_LLM_MODEL,
        "light_model": DEFAULT_LIGHT_LLM_MODEL,
        "main_base": DEFAULT_LLM_BASE_URL,
        "light_base": DEFAULT_LLM_BASE_URL,
    },
    "embedding": {
        "provider": DEFAULT_EMBEDDING_PROVIDER,
        "deepseek_model": DEFAULT_DEEPSEEK_EMBED_MODEL,
        "siliconflow_model": DEFAULT_EMBEDDING_MODEL,
        "siliconflow_base": DEFAULT_EMBEDDING_BASE_URL,
    },
}


class Config:
    """配置管理类"""
    
    def __init__(self, config_path: Optional[str] = None):
        """初始化配置
        
        Args:
            config_path: 配置文件路径，如果为 None 则使用内置默认配置
        """
        self._config = DEFAULT_CONFIG.copy()
        self._config["features"] = DEFAULT_CONFIG["features"].copy()
        self._config["memory"] = DEFAULT_CONFIG["memory"].copy()
        self._config["llm"] = DEFAULT_CONFIG["llm"].copy()
        self._config["embedding"] = DEFAULT_CONFIG["embedding"].copy()
        
        if config_path:
            self._load_from_file(config_path)
        
        # 环境变量覆盖（优先级：环境变量 > config.json > 默认值）
        self._apply_env_overrides()
    
    def _apply_env_overrides(self) -> None:
        """用环境变量覆盖配置值
        
        支持的环境变量：
        - SHIYI_MAIN_LLM_MODEL → llm.main_model
        - SHIYI_LIGHT_LLM_MODEL → llm.light_model
        - SHIYI_MAIN_API_BASE → llm.main_base
        - SHIYI_EMBEDDING_PROVIDER → embedding.provider
        - SHIYI_EMBEDDING_MODEL → embedding.siliconflow_model
        """
        import os
        env_map = {
            "SHIYI_MAIN_LLM_MODEL": ("llm", "main_model"),
            "SHIYI_LIGHT_LLM_MODEL": ("llm", "light_model"),
            "SHIYI_MAIN_API_BASE": ("llm", "main_base"),
            "SHIYI_EMBEDDING_PROVIDER": ("embedding", "provider"),
            "SHIYI_EMBEDDING_MODEL": ("embedding", "siliconflow_model"),
        }
        for env_key, (section, key) in env_map.items():
            val = os.environ.get(env_key)
            if val:
                self._config[section][key] = val
    
    def _load_from_file(self, config_path: str) -> None:
        """从文件加载配置
        
        Args:
            config_path: 配置文件路径
            
        Raises:
            ConfigError: 配置加载失败
        """
        path = Path(config_path)
        if not path.exists():
            raise ConfigError(f"配置文件不存在: {config_path}")
        
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            
            # 深度合并配置
            self._deep_merge(self._config, user_config)
        except json.JSONDecodeError as e:
            raise ConfigError(f"配置文件格式错误: {e}")
        except Exception as e:
            raise ConfigError(f"加载配置文件失败: {e}")
    
    def _deep_merge(self, base: Dict, update: Dict) -> None:
        """深度合并字典
        
        Args:
            base: 基础字典
            update: 更新字典
        """
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值
        
        Args:
            key: 配置键，支持点号分隔的路径，如 "memory.halflife_days"
            default: 默认值
            
        Returns:
            配置值
        """
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value
    
    @property
    def version(self) -> str:
        """版本号 — 优先从 VERSION.txt 读取"""
        try:
            vfile = Path(__file__).parent.parent.parent / "VERSION.txt"
            if vfile.exists():
                return vfile.read_text().strip()
        except Exception:
            pass
        return self._config.get("version", "0.0.0")
    
    @property
    def features(self) -> Dict[str, bool]:
        """功能开关"""
        return copy.deepcopy(self._config.get("features", DEFAULT_CONFIG["features"]))

    @property
    def memory(self) -> Dict[str, Any]:
        """记忆配置"""
        return copy.deepcopy(self._config.get("memory", DEFAULT_CONFIG["memory"]))

    @property
    def llm(self) -> Dict[str, Any]:
        """LLM配置"""
        return copy.deepcopy(self._config.get("llm", DEFAULT_CONFIG["llm"]))

    @property
    def embedding(self) -> Dict[str, Any]:
        """Embedding配置"""
        return copy.deepcopy(self._config.get("embedding", DEFAULT_CONFIG["embedding"]))
    
    def validate(self) -> None:
        """校验配置
        
        Raises:
            ConfigError: 配置校验失败
        """
        # 校验版本
        if not self.version:
            raise ConfigError("版本号不能为空")
        
        # 校验 memory 配置
        memory = self.memory
        if memory.get("halflife_days", 0) <= 0:
            raise ConfigError("halflife_days 必须大于 0")
        if memory.get("emotion_multiplier", 0) < 0:
            raise ConfigError("emotion_multiplier 不能为负数")
        if memory.get("access_multiplier", 0) < 0:
            raise ConfigError("access_multiplier 不能为负数")
        if memory.get("max_hops", 0) < 0:
            raise ConfigError("max_hops 不能为负数")
        if memory.get("decay_per_hop", 0) < 0 or memory.get("decay_per_hop", 0) > 1:
            raise ConfigError("decay_per_hop 必须在 0-1 之间")
        if memory.get("hot_capacity", 0) <= 0:
            raise ConfigError("hot_capacity 必须大于 0")
        if memory.get("warm_capacity", 0) <= 0:
            raise ConfigError("warm_capacity 必须大于 0")
        if not memory.get("embedding_model"):
            raise ConfigError("embedding_model 不能为空")


def load_config(config_path: Optional[str] = None) -> Config:
    """加载配置
    
    Args:
        config_path: 配置文件路径
        
    Returns:
        Config 实例
        
    Raises:
        ConfigError: 配置加载或校验失败
    """
    config = Config(config_path)
    config.validate()
    return config
