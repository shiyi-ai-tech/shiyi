"""Adapters package.

Each platform has its own adapter module.
"""

from .feishu import FeishuAdapter

ADAPTERS = {
    "feishu": FeishuAdapter,
}
