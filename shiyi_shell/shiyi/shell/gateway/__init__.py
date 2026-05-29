"""Gateway module — platform adapters for Shiyi agent.

Gateway != Clerk:
- Gateway is the message TRANSPORT layer (Feishu, WeChat, etc.)
- Clerk is the tool EXECUTION layer (file, web, etc.)
- Gateway calls Shiyi.chat() directly, not through clerking.
"""

from .base import BaseAdapter, AdapterConfig, MessageEvent
from .config import load_feishu_config
from .adapters import ADAPTERS
from .run import run
