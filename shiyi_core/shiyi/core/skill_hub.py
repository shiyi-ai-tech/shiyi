"""
SkillHub — 技能市场搜索与安装

内置多源 Skill 搜索引擎：
  - Hermes Catalog（GitHub raw，解析 markdown 表格）
  - agentskills.io（标准目录）
  - skillsmp.com（120 万+ 市场）
  - agentskill.sh / officialskills.sh（社区索引）

设计原则：
  - 搜索：多源并发 → 合并去重 → 返回候选列表
  - 安装：下载 SKILL.md → 保存到 ~/.shiyi/skills/ → 触发刷新
  - 不自动安装——由用户选择确认后执行
"""

import io
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════


@dataclass
class SkillHubEntry:
    """技能市场条目"""
    skill_id: str          # 唯一标识，如 "software-development/python-debugpy"
    name: str              # 技能名称
    description: str       # 描述
    category: str          # 分类
    source: str            # 来源: hermes | agentskills.io | skillsmp.com | ...
    install_url: str       # SKILL.md 下载地址
    homepage: str = ""     # 技能主页（可选）


@dataclass
class SearchResult:
    """搜索结果"""
    query: str
    entries: List[SkillHubEntry] = field(default_factory=list)
    sources_searched: List[str] = field(default_factory=list)
    total_hits: int = 0
    elapsed_ms: float = 0

    def to_list(self, max_items: int = 20) -> List[Dict[str, Any]]:
        """转为 API 友好的列表"""
        items = []
        for e in self.entries[:max_items]:
            items.append({
                "skill_id": e.skill_id,
                "name": e.name,
                "description": e.description,
                "category": e.category,
                "source": e.source,
                "install_url": e.install_url,
                "homepage": e.homepage,
            })
        return items


# ═══════════════════════════════════════════
# SkillHub
# ═══════════════════════════════════════════


class SkillHub:
    """技能市场引擎

    使用方式:
        hub = SkillHub(skills_dir=Path.home() / ".shiyi" / "skills")
        results = hub.search("视频制作")
        hub.install("creative/ascii-video", source="hermes")
    """

    # ── 内置源 ──
    SOURCES = {
        "hermes": {
            "name": "Hermes Catalog",
            "catalog_url": "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/reference/skills-catalog.md",
            "raw_base": "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/skills",
            "type": "catalog",
        },
        "agentskills.io": {
            "name": "agentskills.io",
            "url": "https://agentskills.io",
            "type": "search_site",
        },
        "skillsmp.com": {
            "name": "skillsmp.com",
            "url": "https://skillsmp.com",
            "type": "market",
        },
        "agentskill.sh": {
            "name": "agentskill.sh",
            "url": "https://agentskill.sh",
            "type": "community",
        },
        "officialskills.sh": {
            "name": "officialskills.sh",
            "url": "https://officialskills.sh",
            "type": "community",
        },
    }

    def __init__(self, skills_dir: Path, cache_ttl_seconds: int = 3600):
        """初始化 SkillHub

        Args:
            skills_dir: 本地 Skills 目录 (~/.shiyi/skills/)
            cache_ttl_seconds: 目录缓存有效期（默认 1 小时）
        """
        self._skills_dir = Path(skills_dir)
        self._cache_ttl = cache_ttl_seconds
        self._catalog_cache: Optional[List[SkillHubEntry]] = None
        self._catalog_cache_time: float = 0

    # ═══════════════════════════════════════════
    # 搜索
    # ═══════════════════════════════════════════

    def search(self, query: str, max_results: int = 20) -> SearchResult:
        """搜索技能

        优先搜索 Hermes Catalog（本地缓存），
        如果命中不够，再尝试其他源。

        Args:
            query: 搜索关键词（中英文均可）
            max_results: 最大结果数

        Returns:
            SearchResult
        """
        t0 = time.time()
        result = SearchResult(query=query)

        # 1. 搜索 Hermes Catalog（主源，有缓存）
        try:
            catalog = self._get_hermes_catalog()
            result.sources_searched.append("hermes")
            for entry in catalog:
                if self._match(entry, query):
                    result.entries.append(entry)
        except Exception as e:
            logger.warning("Hermes catalog search failed: %s", e)

        # 2. 尝试其他源（HTTP 搜索——当前仅标记已尝试，
        #    具体 API 需要按实际情况适配）
        for source_id in ["agentskills.io", "skillsmp.com"]:
            try:
                extra = self._search_external(query, source_id)
                if extra:
                    result.sources_searched.append(source_id)
                    result.entries.extend(extra)
            except Exception as e:
                logger.debug("Source %s search skipped: %s", source_id, e)

        # 去重
        result.entries = self._deduplicate(result.entries)
        result.total_hits = len(result.entries)
        result.elapsed_ms = (time.time() - t0) * 1000

        # 截断
        result.entries = result.entries[:max_results]

        logger.info(
            "SkillHub search '%s': %d hits in %.0fms (sources: %s)",
            query, result.total_hits, result.elapsed_ms,
            ", ".join(result.sources_searched),
        )
        return result

    # ═══════════════════════════════════════════
    # 安装
    # ═══════════════════════════════════════════

    def install(self, skill_id: str, source: str = "hermes") -> Tuple[bool, str]:
        """安装一个 Skill

        Args:
            skill_id: Skill 标识，如 "creative/ascii-video"
            source: 来源标识

        Returns:
            (success, message)
        """
        # 确定下载 URL
        if source == "hermes":
            raw_base = self.SOURCES["hermes"]["raw_base"]
            url = f"{raw_base}/{skill_id}/SKILL.md"
        else:
            return False, f"Source '{source}' install not yet supported"

        # 下载
        try:
            content = self._http_get(url, timeout=30)
        except Exception as e:
            return False, f"Download failed: {e}"

        if not content or len(content.strip()) < 50:
            return False, f"Downloaded content too short ({len(content)} bytes)"

        # 保存到 ~/.shiyi/skills/<skill_id>/SKILL.md
        target_dir = self._skills_dir / skill_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / "SKILL.md"

        target_file.write_text(content, encoding="utf-8")
        logger.info("Skill installed: %s → %s", skill_id, target_file)

        # 清除缓存以便下次扫描
        self._catalog_cache = None

        return True, str(target_file)

    def get_installed_ids(self) -> set:
        """获取已安装的 skill_id 集合（避免重复安装）"""
        installed = set()
        if not self._skills_dir.exists():
            return installed
        for md_path in self._skills_dir.rglob("SKILL.md"):
            rel = md_path.parent.relative_to(self._skills_dir)
            installed.add(str(rel).replace(os.sep, "/"))
        return installed

    # ═══════════════════════════════════════════
    # 内部：Hermes Catalog
    # ═══════════════════════════════════════════

    def _get_hermes_catalog(self) -> List[SkillHubEntry]:
        """获取并缓存 Hermes Catalog"""
        if self._catalog_cache and (time.time() - self._catalog_cache_time) < self._cache_ttl:
            return self._catalog_cache

        catalog_url = self.SOURCES["hermes"]["catalog_url"]
        try:
            md = self._http_get(catalog_url, timeout=30)
            entries = self._parse_catalog_markdown(md)
            self._catalog_cache = entries
            self._catalog_cache_time = time.time()
            logger.info("Hermes catalog loaded: %d skills", len(entries))
            return entries
        except Exception as e:
            logger.warning("Failed to load Hermes catalog: %s", e)
            # 如果有旧缓存，降级使用
            if self._catalog_cache:
                logger.info("Using stale Hermes catalog cache")
                return self._catalog_cache
            return []

    @staticmethod
    def _parse_catalog_markdown(md: str) -> List[SkillHubEntry]:
        """解析 Hermes skills-catalog.md 中的表格

        格式:
        ## category_name
        | [`skill-name`](link) | Description text. | `category/skill-name` |
        """
        entries: List[SkillHubEntry] = []
        current_category = ""
        raw_base = SkillHub.SOURCES["hermes"]["raw_base"]

        in_table = False
        header_seen = False

        for line in md.split("\n"):
            stripped = line.strip()

            # 分类标题: ## category-name
            if stripped.startswith("## ") and not stripped.startswith("### "):
                current_category = stripped[3:].strip()

            # 表格头: | Skill | Description | Path |
            if stripped.startswith("| Skill") or stripped.startswith("|-------"):
                in_table = True
                header_seen = True
                continue

            if in_table and stripped.startswith("|"):
                cols = [c.strip() for c in stripped.split("|")[1:-1]]
                if len(cols) >= 3:
                    name_link = cols[0]
                    description = cols[1]
                    skill_path = cols[2]

                    # 提取 skill_id: "category/skill-name"
                    skill_id = skill_path.strip("`")

                    # 提取 name: [`skill-name`](url)
                    name_match = re.search(r'\[`?([^`\]\[]+)`?\]', name_link)
                    name = name_match.group(1) if name_match else skill_id.split("/")[-1]

                    entries.append(SkillHubEntry(
                        skill_id=skill_id,
                        name=name,
                        description=description,
                        category=current_category or skill_id.split("/")[0],
                        source="hermes",
                        install_url=f"{raw_base}/{skill_id}/SKILL.md",
                        homepage=(
                            f"https://hermes-agent.nousresearch.com/"
                            f"user-guide/skills/bundled/{skill_id.replace('/', '/skills-')}"
                        ),
                    ))

        logger.debug("Parsed %d entries from Hermes catalog", len(entries))
        return entries

    # ═══════════════════════════════════════════
    # 内部：匹配
    # ═══════════════════════════════════════════

    @staticmethod
    def _match(entry: SkillHubEntry, query: str) -> bool:
        """关键词匹配（大小写不敏感，支持中英文）

        中文查询会自动映射到英文关键词域。
        """
        q = query.lower().strip()
        if not q:
            return False

        # 中文→英文关键词映射
        cn_to_en = {
            "视频": "video", "制作": "generate create render comfyui manim",
            "图片": "image pixel art diagram screenshot photo",
            "代码": "code program develop script",
            "调试": "debug debugpy inspect traceback",
            "音乐": "music audio song spotify audiocraft heartmula",
            "游戏": "game minecraft pokemon",
            "搜索": "search web arxiv research lookup",
            "笔记": "note obsidian memory",
            "邮件": "email himalaya gmail",
            "文档": "document pdf ocr markdown",
            "设计": "design diagram sketch architecture draw",
            "部署": "deploy docker server devops",
            "测试": "test testing pytest tdd",
            "安全": "security red-team jailbreak",
            "数据": "data database sql analytics",
            "智能": "agent ai llm model",
            "语音": "voice tts stt speech audio",
            "画图": "diagram draw excalidraw architecture sketch",
            "写作": "write writing plan research-paper",
            "家居": "home smart-home hue",
            "绘画": "draw paint pixel art sketch",
        }

        # 如果查询含中文，替换为英文关键词（组内 OR，组间 AND）
        word_groups = []  # [[word1, word2, ...], ...]
        for word in q.split():
            if any('\u4e00' <= c <= '\u9fff' for c in word):
                # 中文词 → 英文映射（OR 组）
                group = []
                for cn, en in cn_to_en.items():
                    if cn in word:
                        group.extend(en.split())
                if group:
                    word_groups.append(group)
                # 未映射的中文词跳过
            else:
                word_groups.append([word])

        if not word_groups:
            return False

        search_text = f"{entry.name} {entry.description} {entry.category} {entry.skill_id}".lower()

        # 每组至少匹配一个词
        for group in word_groups:
            if not any(w in search_text for w in group):
                return False
        return True

    # ═══════════════════════════════════════════
    # 内部：去重
    # ═══════════════════════════════════════════

    @staticmethod
    def _deduplicate(entries: List[SkillHubEntry]) -> List[SkillHubEntry]:
        """按 skill_id 去重，保留第一个来源"""
        seen: Dict[str, SkillHubEntry] = {}
        for e in entries:
            if e.skill_id not in seen:
                seen[e.skill_id] = e
        return list(seen.values())

    # ═══════════════════════════════════════════
    # 内部：外部源搜索（占位）
    # ═══════════════════════════════════════════

    def _search_external(self, query: str, source_id: str) -> List[SkillHubEntry]:
        """搜索外部源（当前为占位实现）

        实际接入需要根据各站 API 适配。
        此处返回空列表，不影响主流程。
        """
        # TODO: 接入 agentskills.io / skillsmp.com 搜索 API
        # 各站 API 格式待确认
        return []

    # ═══════════════════════════════════════════
    # 内部：HTTP
    # ═══════════════════════════════════════════

    @staticmethod
    def _http_get(url: str, timeout: float = 15) -> str:
        """HTTP GET，返回文本"""
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "ShiYi-SkillHub/1.0", "Accept": "text/plain,text/markdown,*/*"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")


# ═══════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════


def create_skill_hub(skills_dir: Optional[str] = None) -> SkillHub:
    """创建 SkillHub 实例

    Args:
        skills_dir: Skills 目录，默认 ~/.shiyi/skills/

    Returns:
        SkillHub 实例
    """
    if skills_dir is None:
        skills_dir = os.path.join(Path.home(), ".shiyi", "skills")
    return SkillHub(skills_dir=Path(skills_dir))
