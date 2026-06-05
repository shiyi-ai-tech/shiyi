"""shiyi-common 全局常量定义

所有模型名、URL、provider 等硬编码默认值集中管理于此。
其他模块一律从此处导入，不再各自硬编码。
"""

# ── LLM 模型默认值 ──
DEFAULT_MAIN_LLM_MODEL = "deepseek-v4-pro"      # 主 LLM（回复生成）
DEFAULT_LIGHT_LLM_MODEL = "deepseek-v4-flash"   # 轻量 LLM（意图识别等感知层）
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com/v1"

# ── Embedding 默认值 ──
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_DEEPSEEK_EMBED_MODEL = "deepseek-embed"

# ── Provider 默认值 ──
DEFAULT_LLM_PROVIDER = "deepseek"
DEFAULT_EMBEDDING_PROVIDER = "auto"
