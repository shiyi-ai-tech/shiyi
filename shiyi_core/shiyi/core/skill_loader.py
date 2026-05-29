"""SkillLoader - Skill 统一加载器

兼容 Hermes/OpenClaw 的 SKILL.md 格式（YAML frontmatter + Markdown 正文）

目录结构：
~/.shiyi/skills/<category>/<skill-name>/SKILL.md
"""

import os
import re
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SkillInfo:
    """Skill 元信息"""
    name: str                    # Skill 名称
    description: str             # Skill 描述
    category: str                 # 分类目录
    path: str                     # SKILL.md 完整路径
    triggers: List[str] = field(default_factory=list)   # 触发关键词
    keywords: List[str] = field(default_factory=list)   # 关键词
    requires: Dict[str, Any] = field(default_factory=dict)  # requires 声明
    raw_body: str = ""            # Markdown 正文（不含 YAML frontmatter）
    
    @property
    def skill_id(self) -> str:
        """唯一标识: category/skill-name"""
        rel_path = Path(self.path).parent.relative_to(self._base_dir)
        return str(rel_path).replace(os.sep, "/")
    
    @property
    def _base_dir(self) -> Path:
        """获取 skills 根目录"""
        return Path(self.path).parent.parent.parent


@dataclass
class SkillContent:
    """Skill 完整内容"""
    info: SkillInfo
    l1_content: str              # 完整 SKILL.md 内容
    l2_files: Dict[str, str] = field(default_factory=dict)  # 引用文件路径→内容


class SkillLoader:
    """Skill 统一加载器
    
    三级加载:
    - L0: 摘要列表（启动时加载，拼入 system prompt）
    - L1: 完整内容（按需加载单个 Skill）
    - L2: 引用文件（Skill 引用的附件内容）
    
    示例:
        loader = SkillLoader(registry)
        loader.scan()  # 扫描 ~/.shiyi/skills/
        
        # L0: 获取所有 Skill 摘要
        summary = loader.get_l0_summary()
        
        # L1: 加载单个 Skill 完整内容
        content = loader.get_l1_content("productivity/research")
        
        # L2: 加载 Skill 引用的文件
        file_content = loader.get_l2_file("productivity/research", "references/template.md")
    """
    
    def __init__(self, registry=None, skills_dir: str = ""):
        """初始化 SkillLoader
        
        Args:
            registry: ClerkRegistry 实例（用于注册 Skill 相关工具）
            skills_dir: Skills 根目录，默认 ~/.shiyi/skills/
        """
        self._registry = registry
        self._skills_dir = Path(skills_dir) if skills_dir else Path.home() / ".shiyi" / "skills"
        
        # 已扫描的 Skill 信息
        self._skills: Dict[str, SkillInfo] = {}  # skill_id -> SkillInfo
        
        # 已加载的 Skill 内容（L1）
        self._l1_cache: Dict[str, str] = {}
        
        # 已加载的 L2 文件内容
        self._l2_cache: Dict[str, Dict[str, str]] = {}
        
        # 确保目录存在
        self._skills_dir.mkdir(parents=True, exist_ok=True)
    
    @property
    def skills_dir(self) -> Path:
        """Skills 根目录"""
        return self._skills_dir
    
    def scan(self) -> int:
        """扫描 skills 目录，发现所有 SKILL.md
        
        Returns:
            发现的 Skill 数量
        """
        self._skills.clear()
        self._l1_cache.clear()
        self._l2_cache.clear()
        
        count = 0
        for root, dirs, files in os.walk(self._skills_dir, followlinks=True):
            if "SKILL.md" in files:
                skill_path = Path(root) / "SKILL.md"
                skill_info = self._parse_skill_md(skill_path)
                if skill_info.name or skill_info.description:
                    # 使用相对于 skills 目录的路径作为 skill_id
                    rel_path = skill_path.parent.relative_to(self._skills_dir)
                    skill_id = str(rel_path).replace(os.sep, "/")
                    self._skills[skill_id] = skill_info
                    count += 1
                    logger.info(f"发现 Skill: {skill_id} - {skill_info.name}")
        
        logger.info(f"Skill 扫描完成，共 {count} 个")
        return count
    
    def _parse_skill_md(self, skill_path: Path) -> SkillInfo:
        """解析 SKILL.md 文件
        
        Args:
            skill_path: SKILL.md 文件路径
            
        Returns:
            SkillInfo 元信息
        """
        try:
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"读取 SKILL.md 失败: {skill_path} - {e}")
            return SkillInfo(name="", description="", category="", path=str(skill_path))
        
        info = SkillInfo(
            name="",
            description="",
            category=skill_path.parent.name,
            path=str(skill_path),
        )
        
        # 解析 YAML frontmatter
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        body = content
        if fm_match:
            fm_text = fm_match.group(1)
            body = content[fm_match.end():]
            # 优先用 yaml.safe_load 完整解析（处理多行列表等复杂YAML）
            try:
                import yaml as _yaml
                fm_data = _yaml.safe_load(fm_text)
                if isinstance(fm_data, dict):
                    info.name = str(fm_data.get("name", ""))
                    info.description = str(fm_data.get("description", ""))
                    # triggers — 保持兼容，过滤空值
                    triggers = fm_data.get("triggers", [])
                    if isinstance(triggers, list):
                        info.triggers = [str(t).strip() for t in triggers if str(t).strip()]
                    elif isinstance(triggers, str) and triggers.strip():
                        info.triggers = [triggers.strip()]
            except Exception:
                # yaml 解析失败时回退到逐行解析
                self._parse_yaml_frontmatter(fm_text, info)
        
        info.raw_body = body
        
        # 从正文提取关键词 — 同时注入 name/description 做 fallback
        body_keywords = set(self._extract_keywords(body))
        meta_keywords = set()
        _noise = {"the", "and", "for", "with", "use", "when", "how", "you", "this", "that"}
        for field in [info.name, info.description]:
            # 拆 name 中的 hyphen/underscore（"hermes-agent-skill" → hermes, agent, skill）
            parts = re.split(r"[-_ ]", field.lower())
            for part in parts:
                if len(part) >= 2 and part not in _noise:
                    meta_keywords.add(part)
        info.keywords = sorted(body_keywords | meta_keywords)
        
        # 如果 frontmatter 中没有解析 requires，则从正文解析
        if not info.requires:
            info.requires = self._parse_requires(body)
        
        return info
    
    def _parse_yaml_frontmatter(self, text: str, info: SkillInfo) -> None:
        """解析 YAML frontmatter
        
        Args:
            text: YAML 文本
            info: SkillInfo 实例
        """
        import json
        
        # 将文本按行分割并记录缩进级别
        lines = text.split("\n")
        
        # 检测 requires 块的起始和结束
        requires_start = -1
        requires_end = -1
        base_indent = 0
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.lower() == "requires:":
                requires_start = i
                base_indent = len(line) - len(line.lstrip())
                continue
            if requires_start >= 0 and requires_end < 0:
                # 在 requires 块内
                current_indent = len(line) - len(line.lstrip())
                if stripped and current_indent <= base_indent:
                    # 离开 requires 块
                    requires_end = i
                    break
        
        if requires_end < 0 and requires_start >= 0:
            requires_end = len(lines)
        
        # 解析普通字段
        for i, line in enumerate(lines):
            if requires_start >= 0 and requires_start <= i < requires_end:
                continue  # 跳过 requires 块
            
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip().lower()
                val = val.strip().strip("'\"")
                
                if key == "name":
                    info.name = val
                elif key == "description":
                    info.description = val
                elif key == "triggers":
                    if isinstance(val, list):
                        # YAML 原生列表（yaml.safe_load 已解析）
                        info.triggers = [str(v).strip() for v in val if str(v).strip()]
                    elif val.startswith("[") and val.endswith("]"):
                        try:
                            info.triggers = json.loads(val)
                            info.triggers = [t.strip() for t in info.triggers if t.strip()]
                        except Exception:
                            info.triggers = [s.strip() for s in val.strip("[]").split(",") if s.strip()]
                    elif val.strip():
                        info.triggers.append(val.strip())
        
        # 解析 requires 块
        if requires_start >= 0:
            for i in range(requires_start + 1, requires_end):
                line = lines[i]
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                
                if ":" in stripped:
                    key, _, val = stripped.partition(":")
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    
                    if val.startswith("[") and val.endswith("]"):
                        try:
                            info.requires[key] = json.loads(val)
                        except Exception:
                            info.requires[key] = [s.strip() for s in val.strip("[]").split(",") if s.strip()]
                    elif val:
                        info.requires[key] = val
                    else:
                        info.requires[key] = []
    
    def _extract_keywords(self, body: str) -> List[str]:
        """从正文提取关键词
        
        Args:
            body: Markdown 正文
            
        Returns:
            关键词列表
        """
        keywords = set()

        # 扩展的噪音词表
        _noise = {
            "the", "and", "for", "with", "use", "when", "how", "you",
            "this", "that", "not", "are", "was", "but", "can", "all",
            "set", "see", "one", "get", "has", "its", "out", "too",
            "did", "may", "put", "run", "yet", "why", "who", "end",
            "its", "any", "off", "own", "new", "old", "our", "way",
            "too", "up", "us", "we", "be", "at", "by", "do", "go",
            "if", "in", "is", "it", "no", "of", "on", "or", "so",
            "to", "an", "as",
        }

        # 标题行
        for match in re.finditer(r"^#{1,4}\s+(.+)", body, re.MULTILINE):
            heading = match.group(1).strip().lower()
            for word in re.findall(r"[a-z\u4e00-\u9fff]{3,}", heading):
                if word not in _noise:
                    keywords.add(word)
        
        # 加粗文字
        for match in re.finditer(r"\*\*([^*]+)\*\*", body):
            keywords.add(match.group(1).strip().lower())
        
        return sorted(keywords)
    
    def _parse_requires(self, body: str) -> Dict[str, Any]:
        """解析 requires 声明
        
        格式:
        requires:
          tools: [web_search, file_write]
          clerks: [clerk-vision]
          apis: [vision_api]
          dependencies: [fpdf2]
        
        Args:
            body: Markdown 正文
            
        Returns:
            requires 字典
        """
        requires = {}
        
        # 查找 requires 块
        requires_match = re.search(r"^requires:\s*\n((?:\s+.+\n)*)", body, re.MULTILINE)
        if requires_match:
            requires_text = requires_match.group(1)
            for line in requires_text.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    
                    if val.startswith("[") and val.endswith("]"):
                        try:
                            import json
                            requires[key] = json.loads(val)
                        except Exception:
                            requires[key] = [s.strip() for s in val.strip("[]").split(",") if s.strip()]
                    else:
                        requires[key] = val
        
        return requires
    
    def list_skills(self) -> List[Dict[str, Any]]:
        """列出所有已扫描的 Skill
        
        Returns:
            Skill 列表（基本信息）
        """
        return [
            {
                "skill_id": skill_id,
                "name": info.name,
                "description": info.description,
                "category": info.category,
                "path": info.path,
                "triggers": info.triggers,
                "keywords": info.keywords[:5],  # 只返回前5个关键词
            }
            for skill_id, info in self._skills.items()
        ]
    
    def get_l0_summary(self) -> str:
        """获取 L0 摘要，拼入 system prompt
        
        Returns:
            所有 Skill 的摘要文本
        """
        if not self._skills:
            self.scan()
        
        if not self._skills:
            return ""
        
        lines = [
            "## 可用 Skills（按需加载）",
            "",
            "Skills 是一系列操作指南，告诉 Agent 如何使用工具完成任务。",
            "当需要使用 Skill 时，回复中包含 `{{{{use_skill:skill_id}}}}` 标记，系统会自动加载对应内容。",
            "",
        ]
        
        # 按分类组织
        categories: Dict[str, List[SkillInfo]] = {}
        for info in self._skills.values():
            cat = info.category or "general"
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(info)
        
        for cat in sorted(categories.keys()):
            lines.append(f"### {cat}")
            for info in categories[cat]:
                skill_id = str(Path(info.path).parent.relative_to(self._skills_dir)).replace(os.sep, "/")
                lines.append(f"- **{info.name}** (`{skill_id}`)")
                if info.description:
                    lines.append(f"  - {info.description[:100]}")
            lines.append("")
        
        return "\n".join(lines)
    
    def get_l1_content(self, skill_id: str) -> str:
        """获取 L1 完整内容
        
        Args:
            skill_id: Skill 标识（如 "productivity/research"）
            
        Returns:
            SKILL.md 完整内容
        """
        # 检查缓存
        if skill_id in self._l1_cache:
            return self._l1_cache[skill_id]
        
        info = self._skills.get(skill_id)
        if info is None:
            # 尝试重新扫描
            self.scan()
            info = self._skills.get(skill_id)
        
        if info is None:
            return f"Skill not found: {skill_id}"
        
        # 读取完整内容
        try:
            with open(info.path, "r", encoding="utf-8") as f:
                content = f.read()
            self._l1_cache[skill_id] = content
            return content
        except Exception as e:
            logger.error(f"读取 Skill 内容失败: {info.path} - {e}")
            return f"Error loading skill: {e}"
    
    def get_l2_file(self, skill_id: str, file_path: str) -> str:
        """获取 L2 引用文件
        
        Args:
            skill_id: Skill 标识
            file_path: 相对于 Skill 目录的文件路径
            
        Returns:
            文件内容
        """
        cache_key = f"{skill_id}:{file_path}"
        if cache_key in self._l2_cache.get(skill_id, {}):
            return self._l2_cache[skill_id][file_path]
        
        info = self._skills.get(skill_id)
        if info is None:
            return f"Skill not found: {skill_id}"
        
        # 构建完整路径
        skill_dir = Path(info.path).parent
        full_path = skill_dir / file_path
        
        if not full_path.exists():
            return f"File not found: {file_path}"
        
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            if skill_id not in self._l2_cache:
                self._l2_cache[skill_id] = {}
            self._l2_cache[skill_id][file_path] = content
            
            return content
        except Exception as e:
            logger.error(f"读取 Skill 引用文件失败: {full_path} - {e}")
            return f"Error loading file: {e}"
    
    def load(self, skill_path: str) -> str:
        """加载单个 Skill（别名，用于兼容 CLI）
        
        Args:
            skill_path: Skill 目录路径或 skill_id
            
        Returns:
            SKILL.md 完整内容
        """
        # 如果是路径，提取 skill_id
        if os.path.isabs(skill_path) or os.path.exists(skill_path):
            try:
                skill_dir = Path(skill_path).resolve()
                if skill_dir.is_file():
                    skill_dir = skill_dir.parent
                rel_path = skill_dir.relative_to(self._skills_dir)
                skill_id = str(rel_path).replace(os.sep, "/")
            except ValueError:
                # 不在默认目录下，创建符号链接或复制
                return self._install_local_skill(skill_path)
        else:
            skill_id = skill_path
        
        return self.get_l1_content(skill_id)
    
    def unload(self, skill_id: str) -> bool:
        """卸载 Skill
        
        Args:
            skill_id: Skill 标识
            
        Returns:
            是否成功卸载
        """
        if skill_id not in self._skills:
            return False
        
        del self._skills[skill_id]
        self._l1_cache.pop(skill_id, None)
        self._l2_cache.pop(skill_id, None)
        return True
    
    def _install_local_skill(self, source_path: str) -> str:
        """从本地路径安装 Skill
        
        Args:
            source_path: 源 SKILL.md 路径或目录
            
        Returns:
            安装后的 skill_id 或错误信息
        """
        source = Path(source_path)
        
        if source.is_file():
            # SKILL.md 文件
            skill_md = source
            target_dir = self._skills_dir / source.stem
        elif source.is_dir():
            # 目录，查找 SKILL.md
            skill_md = source / "SKILL.md"
            target_dir = source
        else:
            return f"Source not found: {source_path}"
        
        if not skill_md.exists():
            return f"SKILL.md not found in: {source_path}"
        
        # 解析 Skill 信息确定分类
        info = self._parse_skill_md(skill_md)
        category = info.category or "general"
        
        # 确定目标路径
        if source.is_file():
            skill_name = info.name.lower().replace(" ", "-") or source.stem
            target_dir = self._skills_dir / category / skill_name
        else:
            skill_name = source.name
            target_dir = self._skills_dir / category / skill_name
        
        # 复制文件
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            
            if source.is_file():
                import shutil
                shutil.copy(skill_md, target_dir / "SKILL.md")
            else:
                import shutil
                for item in source.iterdir():
                    if item.name != "SKILL.md":
                        dest = target_dir / item.name
                        if item.is_dir():
                            shutil.copytree(item, dest, dirs_exist_ok=True)
                        else:
                            shutil.copy(item, dest)
            
            # 重新扫描
            self.scan()
            
            # 查找新安装的 skill_id
            for skill_id, info in self._skills.items():
                if info.path == str(target_dir / "SKILL.md"):
                    return skill_id
            
            return f"Install failed: skill not found after install"
        except Exception as e:
            return f"Install failed: {e}"
    
    def install(self, source: str, category: str = "") -> Dict[str, Any]:
        """安装 Skill
        
        Args:
            source: 来源（本地路径、hermes:xxx、clawhub:xxx、github:xxx）
            category: 指定分类（可选）
            
        Returns:
            安装结果
        """
        result = {
            "success": False,
            "skill_id": "",
            "message": "",
        }
        
        # 本地路径
        if os.path.exists(source) or os.path.isabs(source):
            skill_id = self._install_local_skill(source)
            if skill_id.startswith("Install failed"):
                result["message"] = skill_id
            else:
                result["success"] = True
                result["skill_id"] = skill_id
                result["message"] = f"Skill installed: {skill_id}"
        else:
            # 远程来源（暂时不支持，后续扩展）
            result["message"] = f"Remote install not supported yet: {source}"
        
        return result
    
    def delete(self, skill_id: str) -> Dict[str, Any]:
        """删除 Skill
        
        Args:
            skill_id: Skill 标识
            
        Returns:
            删除结果
        """
        result = {
            "success": False,
            "message": "",
        }
        
        info = self._skills.get(skill_id)
        if info is None:
            result["message"] = f"Skill not found: {skill_id}"
            return result
        
        try:
            import shutil
            skill_dir = Path(info.path).parent
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
            
            self.unload(skill_id)
            
            result["success"] = True
            result["message"] = f"Skill deleted: {skill_id}"
        except Exception as e:
            result["message"] = f"Delete failed: {e}"
        
        return result
    
    def show(self, skill_id: str) -> Dict[str, Any]:
        """查看 Skill 详情
        
        Args:
            skill_id: Skill 标识
            
        Returns:
            Skill 详细信息
        """
        info = self._skills.get(skill_id)
        if info is None:
            return {"error": f"Skill not found: {skill_id}"}
        
        content = self.get_l1_content(skill_id)
        
        return {
            "skill_id": skill_id,
            "name": info.name,
            "description": info.description,
            "category": info.category,
            "path": info.path,
            "triggers": info.triggers,
            "keywords": info.keywords,
            "requires": info.requires,
            "content": content,
        }
    
    def check_dependencies(self, skill_id: str) -> Dict[str, Any]:
        """检查 Skill 依赖
        
        Args:
            skill_id: Skill 标识
            
        Returns:
            依赖检查结果
        """
        info = self._skills.get(skill_id)
        if info is None:
            return {"error": f"Skill not found: {skill_id}"}
        
        result = {
            "skill_id": skill_id,
            "dependencies": [],
            "system_deps": [],
            "missing": [],
        }
        
        requires = info.requires
        if not requires:
            return result
        
        # pip 依赖
        pip_deps = requires.get("dependencies", [])
        for dep in pip_deps:
            result["dependencies"].append(dep)
            try:
                __import__(dep)
            except ImportError:
                result["missing"].append(dep)
        
        # 系统依赖（提示但不检查）
        # 暂时为空
        
        return result
