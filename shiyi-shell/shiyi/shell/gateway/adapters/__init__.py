"""Adapters package.

Each platform has its own adapter module.
"""

from .feishu import FeishuAdapter
from .wechat import WeChatAdapter

ADAPTERS = {
    "feishu": FeishuAdapter,
    "wechat": WeChatAdapter,
}
