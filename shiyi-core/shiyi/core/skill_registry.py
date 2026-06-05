"""SkillRegistry — Skill 管理中心

在 SkillLoader 之上提供统一 Skill 查询和路由提示构建。
路由唯一职责：把用户请求分配给最合适的吏员执行。
"""

import logging
from typing import Dict, List, Any, Optional

from shiyi.core.skill_loader import SkillLoader, SkillInfo

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Skill 管理中心

    使用方式：
        loader = SkillLoader(registry)
        loader.scan()
        registry = SkillRegistry(loader)

        # 构建 LLM 路由提示（Skill + 吏员列表）
        prompt = registry.build_skill_clerk_prompt(clerks_info)
    """

    def __init__(self, loader: SkillLoader):
        """初始化

        Args:
            loader: 已调用 scan() 的 SkillLoader 实例
        """
        self._loader = loader
        self._skills: Dict[str, SkillInfo] = {}  # skill_id → SkillInfo

        # 从 loader 同步
        for skill_id, info in loader._skills.items():
            if info.name or info.description:
                self.register(skill_id, info)

    # ═══════════════════════════════════════════
    # 注册与管理
    # ═══════════════════════════════════════════

    def register(self, skill_id: str, info: SkillInfo) -> None:
        """注册一个 Skill"""
        self._skills[skill_id] = info
        logger.debug("Registered skill %s", skill_id)

    def unregister(self, skill_id: str) -> bool:
        """注销一个 Skill"""
        if skill_id not in self._skills:
            return False
        self._skills.pop(skill_id)
        return True

    def refresh(self) -> int:
        """重新扫描并同步 loader 的 skills"""
        self._skills.clear()
        self._loader.scan()
        count = 0
        for skill_id, info in self._loader._skills.items():
            if info.name or info.description:
                self.register(skill_id, info)
                count += 1
        return count

    # ═══════════════════════════════════════════
    # 查询
    # ═══════════════════════════════════════════

    def get(self, skill_id: str) -> Optional[SkillInfo]:
        """获取单个 Skill 信息"""
        return self._skills.get(skill_id)

    def list_all(self) -> List[Dict[str, Any]]:
        """列出所有已注册 Skill"""
        return [self._to_dict(sid) for sid in self._skills]

    @property
    def count(self) -> int:
        """Skill 总数"""
        return len(self._skills)

    # ═══════════════════════════════════════════
    # LLM 路由提示构建
    # ═══════════════════════════════════════════

    def build_skill_clerk_prompt(self, clerks_info: List[Dict[str, Any]]) -> str:
        """构建 Skill + 吏员路由提示，供 LLM 决策

        Skill 编号 → 吏员名下标注编号，LLM 一眼知道每个吏员能干什么。
        路由只有一条路：选 skill + 选 clerk → dispatch。
        """
        lines = []

        # ── 步骤1：收集所有 skill，统一编号 ──
        all_skill_ids = sorted(self._skills.keys())
        num_map: Dict[str, str] = {}  # skill_id → Snn
        lines.append("## Skill 编号表")
        lines.append("")
        if not all_skill_ids:
            lines.append("（暂无已安装的 Skill）")
        for i, sid in enumerate(all_skill_ids, 1):
            info = self._skills.get(sid)
            name = info.name if info else sid
            desc = info.description if info and info.description else ""
            num = f"S{i}"
            num_map[sid] = num
            lines.append(f"- **{num}**: `{sid}` — {name}")
            if desc:
                lines.append(f"  {desc}")

        # ── 步骤2：吏员列表，标注拥有哪些 skill ──
        lines.append("")
        lines.append("## 吏员与 Skill")
        lines.append("")
        if not clerks_info:
            lines.append("（无可用吏员）")
        for clerk in clerks_info:
            cid = clerk.get("clerk_id", "?")
            cname = clerk.get("name", cid)
            cdesc = clerk.get("description", "")
            cskills = clerk.get("skills", [])
            snums = [num_map[s] for s in cskills if s in num_map]
            snums_str = ", ".join(snums) if snums else "（无）"
            lines.append(f"- **{cid}** — {cname}: {snums_str}")
            if cdesc:
                lines.append(f"  {cdesc}")

        # ── 步骤3：路由决策指令 ──
        lines.append("")
        lines.append("## 路由决策")
        lines.append("你是 Skill 路由器。唯一职责：把用户请求分配给最合适的吏员执行。")
        lines.append("")
        lines.append("- 选择一个 skill（从编号表中）和一个 clerk（拥有该 skill 的吏员）")
        lines.append("- 如果请求不需要 skill，skill_id 填 null")
        lines.append("- clerk_id 必填 — 所有请求都由吏员执行，不存在'无需吏员'的情况")
        lines.append("")
        lines.append("**只返回纯 JSON，不要 markdown 代码块，不要额外文字：**")
        lines.append('{"skill_id": "S1对应的skill_id 或 null", "clerk_id": "clerk_xxx", "task_description": "任务简述"}')
        lines.append("")
        lines.append("例：")
        lines.append('- 用户:"帮我debug死锁" → {"skill_id":"software-development/python-debugpy","clerk_id":"clerk_001","task_description":"debug死锁"}')
        lines.append('- 用户:"今天天气怎么样" → {"skill_id":null,"clerk_id":"clerk_001","task_description":"查询天气"}')

        return "\n".join(lines)

    # ═══════════════════════════════════════════
    # Skill 内容
    # ═══════════════════════════════════════════

    def get_skill_content(self, skill_id: str) -> str:
        """获取 Skill 完整内容（SKILL.md 正文）

        Args:
            skill_id: Skill 标识

        Returns:
            SKILL.md 完整内容
        """
        return self._loader.get_l1_content(skill_id)

    # ═══════════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════════

    def _to_dict(self, skill_id: str) -> Dict[str, Any]:
        """将 SkillInfo 转为字典"""
        info = self._skills.get(skill_id)
        if info is None:
            return {"skill_id": skill_id, "error": "not found"}

        return {
            "skill_id": skill_id,
            "name": info.name,
            "description": info.description,
            "category": info.category,
            "triggers": info.triggers,
            "keywords": info.keywords[:5],
        }
