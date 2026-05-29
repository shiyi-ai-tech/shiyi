"""ClerkSelector — 吏员选择器

当前阶段（v0.12.0）工具名→吏员映射唯一，选择器退化为简单查表。
v0.14.0+ 多吏员同名工具冲突时，加入 LLM-based 选择逻辑。
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class ClerkSelector:
    """吏员选择器

    当前策略：
    - 工具名唯一映射，直接查表
    - 保留 LLM 选择接口（v0.14.0+ 启用）
    """

    def __init__(self, registry):
        """初始化选择器

        Args:
            registry: ClerkRegistry 实例
        """
        self._registry = registry

    def select(
        self,
        tool_name: str,
        intent: Optional[str] = None,
        context: Optional[str] = None,
    ) -> Optional[str]:
        """为工具调用选择吏员

        当前实现：直接查 clerck 的 tool_clerk 映射表。
        v0.14.0+ 同名工具冲突时通过 LLM 判断。

        Args:
            tool_name: 工具名
            intent: 意图类型（预留）
            context: 上下文（预留）

        Returns:
            clerk_id 或 None
        """
        clerk_id = self._registry.tool_owner(tool_name)

        if clerk_id is None:
            logger.warning("No clerk found for tool: %s", tool_name)

        return clerk_id

    def select_for_task(
        self,
        task_description: str,
        required_capabilities: Optional[list] = None,
    ) -> Optional[str]:
        """为任务描述选择最合适的吏员

        v0.14.0+ LLM-based 选择，当前退化为能力匹配。

        Args:
            task_description: 任务描述
            required_capabilities: 需具备的能力列表

        Returns:
            最佳 clerk_id
        """
        # 简单能力匹配
        best_match = None
        best_count = 0

        for clerk_info in self._registry.list_clerks():
            if not clerk_info["enabled"]:
                continue
            if required_capabilities:
                match_count = sum(
                    1 for cap in required_capabilities
                    if cap in clerk_info["tools"]
                )
                if match_count > best_count:
                    best_count = match_count
                    best_match = clerk_info["clerk_id"]
            else:
                # 无要求，选第一个启用的
                if best_match is None:
                    best_match = clerk_info["clerk_id"]

        return best_match
