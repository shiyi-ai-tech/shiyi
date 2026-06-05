"""
SkillInstaller — 技能安装向导 (v0.17.4)
══════════════════════════════════════════

解析 SKILL.md → 分析能力 → 匹配已有吏员 → 建议连接/新建。

用法：
  from shiyi.core.skill_installer import SkillInstaller
  si = SkillInstaller(clerk_repo="/path/to/shiyi-shell/shiyi/shell")
  result = si.analyze("/path/to/some-skill/SKILL.md")
  print(result.summary())
"""

import json
import re
import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple


# ═══════════════════════════════════════════════════════
# 技能解析
# ═══════════════════════════════════════════════════════

# 从 SKILL.md 中提取的 structured info
class SkillProfile:
    """解析后的技能画像"""

    def __init__(self):
        self.name: str = ""
        self.description: str = ""
        self.triggers: List[str] = []
        self.tools: List[str] = []          # 显式工具/命令引用
        self.keywords: List[str] = []        # 正文关键词
        self.dependencies: List[str] = []    # npm/pip/apt 依赖
        self.domain_tags: List[str] = []     # 领域标签：web/messaging/file/media/...
        self.raw_body: str = ""              # YAML 之后的正文
        self.source_path: str = ""
        # Phase 5: requires 声明
        self.requires: Dict[str, Any] = {
            "tools": [],      # 需要的工具
            "clerks": [],     # 需要的吏员
            "apis": [],       # 需要的API类型
            "dependencies": [],  # pip/npm依赖
        }


def parse_skill_md(filepath: str) -> SkillProfile:
    """解析 SKILL.md 文件，提取结构化技能画像"""
    sp = SkillProfile()
    sp.source_path = filepath

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return sp

    # ── 解析 YAML frontmatter ──
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    body = content
    if fm_match:
        fm_text = fm_match.group(1)
        body = content[fm_match.end():]
        _parse_yaml_frontmatter(fm_text, sp)

    sp.raw_body = body

    # ── 从正文提取：工具命令 ──
    sp.tools = _extract_tool_commands(body)

    # ── 从正文提取：关键词（标题 + 题头词） ──
    sp.keywords = _extract_keywords(body)

    # ── 提取依赖 ──
    sp.dependencies = _extract_dependencies(body)

    # ── 推断领域标签 ──
    sp.domain_tags = _infer_domains(sp)

    # ── 从正文提取 requires 声明（Phase 5） ──
    _parse_requires_from_body(body, sp)

    return sp


def _parse_yaml_frontmatter(text: str, sp: SkillProfile) -> None:
    """极简 YAML 解析：name/description/triggers + requires"""
    import json
    
    lines = text.split("\n")
    
    # 找到requires块的位置
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
            current_indent = len(line) - len(line.lstrip())
            if stripped and current_indent <= base_indent:
                requires_end = i
                break
    
    if requires_end < 0 and requires_start >= 0:
        requires_end = len(lines)
    
    # 解析普通字段
    for i, line in enumerate(lines):
        if requires_start >= 0 and requires_start <= i < requires_end:
            continue  # 跳过 requires 块
        
        line = line.strip()
        if line.startswith("#"):
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip().strip("'\"")
            if key == "name":
                sp.name = val
            elif key == "description":
                sp.description = val
            elif key == "triggers":
                # 可能是内联列表 "[a, b]" 或下一行的 -
                if val.startswith("[") and val.endswith("]"):
                    try:
                        sp.triggers = json.loads(val)
                    except Exception:
                        sp.triggers = [s.strip() for s in val.strip("[]").split(",") if s.strip()]
                else:
                    sp.triggers.append(val)
        elif line.startswith("- ") and sp.triggers is not None:
            sp.triggers.append(line[2:].strip().strip("'\""))
    
    # 解析 requires 块
    if requires_start >= 0:
        for i in range(requires_start + 1, requires_end):
            line = lines[i].strip()
            if not line or line.startswith("#"):
                continue
            
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip().strip("'\"")
                
                if val.startswith("[") and val.endswith("]"):
                    try:
                        sp.requires[key] = json.loads(val)
                    except Exception:
                        sp.requires[key] = [s.strip() for s in val.strip("[]").split(",") if s.strip()]
                elif val:
                    sp.requires[key] = val
                else:
                    sp.requires[key] = []


def _extract_tool_commands(body: str) -> List[str]:
    """从 markdown 正文中提取 CLI 命令/工具引用"""
    tools = set()

    # 反引号中的 CLI 命令：`command subcommand`
    for match in re.finditer(r"`([a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*)+)`", body):
        cmd = match.group(1).strip()
        # 识别常见 CLI 工具
        known_prefixes = [
            "openclaw", "gh ", "git ", "npm ", "pip ", "docker ",
            "curl ", "hermes ", "web_search", "file_read", "file_write",
        ]
        for prefix in known_prefixes:
            if cmd.startswith(prefix):
                tools.add(prefix.strip())
                break

    # HTML/Markdown 链接中的外部工具引用
    for match in re.finditer(r"\[([^\]]+)\]\(https?://[^\)]+\)", body):
        link_text = match.group(1).lower()
        for kw in ["api", "tool", "cli", "sdk", "plugin"]:
            if kw in link_text:
                tools.add(link_text)

    return sorted(tools)


def _extract_keywords(body: str) -> List[str]:
    """从正文提取能力关键词"""
    kw = set()

    # 标题行（# 开头）
    for match in re.finditer(r"^#{1,4}\s+(.+)", body, re.MULTILINE):
        heading = match.group(1).strip().lower()
        # 去虚词后取关键词
        for word in re.findall(r"[a-z\u4e00-\u9fff]{2,}", heading):
            if word not in {"the", "and", "for", "with", "use", "when", "how", "you", "this", "that"}:
                kw.add(word)

    # 加粗文字 (**text**)
    for match in re.finditer(r"\*\*([^*]+)\*\*", body):
        kw.add(match.group(1).strip().lower())

    # 列表项
    for match in re.finditer(r"^-\s+(.+)", body, re.MULTILINE):
        item = match.group(1).strip()
        if len(item) > 3 and len(item) < 80:
            kw.add(item.lower().split(":")[0].split("(")[0].strip())

    return sorted(kw)


def _extract_dependencies(body: str) -> List[str]:
    """提取依赖声明"""
    deps = set()
    for match in re.finditer(r"(?:npm install|pip install|apt install|brew install)\s+(\S+)", body):
        deps.add(match.group(1))
    return sorted(deps)


def _infer_domains(sp: SkillProfile) -> List[str]:
    """从技能画像推断领域标签"""
    domains = set()
    all_text = f"{sp.name} {sp.description} {' '.join(sp.triggers)} {' '.join(sp.keywords)} {sp.raw_body[:2000]}".lower()

    domain_map = {
        "messaging": ["message", "chat", "send", "notify", "wechat", "telegram", "whatsapp", "discord", "短信", "消息", "发送"],
        "web":          ["web", "browser", "html", "http", "url", "爬虫", "网页", "搜索"],
        "file":         ["file", "read", "write", "save", "download", "upload", "文件", "读写"],
        "media":        ["image", "video", "audio", "media", "screenshot", "图片", "视频", "音频"],
        "code":         ["code", "git", "repo", "commit", "pr", "pull request", "代码", "编程"],
        "data":         ["database", "sql", "api", "json", "csv", "analytics", "数据", "分析"],
        "ai":           ["llm", "agent", "ai", "gpt", "claude", "model", "prompt", "模型", "智能"],
        "system":       ["server", "deploy", "docker", "process", "cron", "系统", "部署"],
    }

    for domain, keywords in domain_map.items():
        if any(kw in all_text for kw in keywords):
            domains.add(domain)

    return sorted(domains)


def _parse_requires_from_body(body: str, sp: SkillProfile) -> None:
    """从正文提取 requires 声明（Phase 5）
    
    支持格式:
    requires:
      tools: [web_search, file_write]
      clerks: [clerk-vision]
      apis: [vision_api]
      dependencies: [fpdf2]
    """
    import json
    
    # 查找 requires 块
    requires_match = re.search(
        r"^requires:\s*\n((?:\s+.+\n)*)",
        body,
        re.MULTILINE
    )
    
    if not requires_match:
        # 尝试单行格式
        inline_match = re.search(r"requires:\s*\{([^}]+)\}", body)
        if inline_match:
            # 简单处理：提取逗号分隔的项
            content = inline_match.group(1)
            for key_val in content.split(","):
                if ":" in key_val:
                    key, val = key_val.split(":", 1)
                    key = key.strip()
                    val = val.strip()
                    if val.startswith("[") and val.endswith("]"):
                        try:
                            sp.requires[key] = json.loads(val)
                        except Exception:
                            sp.requires[key] = [s.strip() for s in val.strip("[]").split(",") if s.strip()]
                    else:
                        sp.requires[key] = val.strip("'\"")
        return
    
    requires_text = requires_match.group(1)
    current_key = None
    
    for line in requires_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        
        # 检查是否是键名行
        if line.endswith(":") and not line.startswith("-"):
            current_key = line.rstrip(":").strip()
            continue
        
        # 检查是否是列表项
        if line.startswith("- "):
            item = line[2:].strip().strip("'\"")
            if current_key and item:
                if isinstance(sp.requires.get(current_key), list):
                    sp.requires[current_key].append(item)
                else:
                    sp.requires[current_key] = [item]
            continue
        
        # 检查是否是内联列表 - 使用split(":")限制分割次数
        if ":" in line:
            parts = line.split(":", 1)  # 只分割一次
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if val.startswith("[") and val.endswith("]"):
                    try:
                        sp.requires[key] = json.loads(val)
                    except Exception:
                        sp.requires[key] = [s.strip() for s in val.strip("[]").split(",") if s.strip()]
                elif val:
                    sp.requires[key] = val.strip("'\"")


# ═══════════════════════════════════════════════════════
# Phase 5: Skill 依赖检查
# ═══════════════════════════════════════════════════════

class DependencyCheckResult:
    """单个依赖项的检查结果"""
    
    def __init__(self, name: str, check_type: str, status: str, message: str = ""):
        self.name = name
        self.check_type = check_type  # tools/clerks/apis/dependencies
        self.status = status          # ok/missing/warning
        self.message = message


class InstallCheckReport:
    """Skill 安装依赖检查报告"""
    
    def __init__(self, skill_name: str):
        self.skill_name = skill_name
        self.checks: List[DependencyCheckResult] = []
        self.passed = 0
        self.warnings = 0
        self.failed = 0
    
    def add_check(self, name: str, check_type: str, status: str, message: str = ""):
        self.checks.append(DependencyCheckResult(name, check_type, status, message))
        if status == "ok":
            self.passed += 1
        elif status == "warning":
            self.warnings += 1
        else:
            self.failed += 1
    
    @property
    def all_passed(self) -> bool:
        return self.failed == 0
    
    def summary(self) -> str:
        """生成人类可读的检查报告"""
        lines = []
        lines.append(f"📋 Skill: {self.skill_name}")
        lines.append("─" * 40)
        
        for check in self.checks:
            if check.status == "ok":
                icon = "✅"
            elif check.status == "warning":
                icon = "⚠️"
            else:
                icon = "❌"
            
            type_map = {
                "tools": "工具",
                "clerks": "吏员",
                "apis": "API",
                "dependencies": "依赖",
            }
            type_name = type_map.get(check.check_type, check.check_type)
            lines.append(f"{icon} [{type_name}] {check.name}")
            if check.message:
                lines.append(f"   {check.message}")
        
        lines.append("─" * 40)
        lines.append(f"总结: ✅ {self.passed} | ⚠️ {self.warnings} | ❌ {self.failed}")
        
        if self.all_passed:
            lines.append("状态: 可以安装")
        else:
            lines.append("状态: 需要处理上述问题后才能安装")
        
        return "\n".join(lines)


def check_skill_dependencies(skill_path: str, registered_clerks: List[str] = None) -> InstallCheckReport:
    """检查 Skill 的所有依赖是否就绪
    
    Args:
        skill_path: SKILL.md 路径
        registered_clerks: 已注册的吏员ID列表
        
    Returns:
        InstallCheckReport 检查报告
    """
    import subprocess
    import sys
    
    report = InstallCheckReport(skill_path)
    registered_clerks = registered_clerks or []
    
    # 解析 SKILL.md
    skill = parse_skill_md(skill_path)
    report.skill_name = skill.name or skill_path
    
    # 检查 requires.clerks
    clerks = skill.requires.get("clerks", [])
    for clerk_id in clerks:
        if clerk_id in registered_clerks:
            report.add_check(clerk_id, "clerks", "ok", "已注册")
        else:
            report.add_check(clerk_id, "clerks", "missing", "未注册，需要创建")
    
    # 检查 requires.dependencies (pip/npm)
    deps = skill.requires.get("dependencies", [])
    deps.extend(skill.dependencies)  # 也检查从正文提取的依赖
    
    for dep in deps:
        # 尝试导入检查
        try:
            if " " in dep:
                # pip install xxx
                pkg = dep.split()[-1].split("==")[0].split(">=")[0]
            else:
                pkg = dep.split("==")[0].split(">=")[0]
            
            __import__(pkg)
            report.add_check(pkg, "dependencies", "ok")
        except ImportError:
            # 尝试 pip show
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "show", pkg],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    report.add_check(pkg, "dependencies", "ok")
                else:
                    report.add_check(pkg, "dependencies", "missing", f"需要安装: pip install {pkg}")
            except Exception:
                report.add_check(pkg, "dependencies", "missing", f"需要安装: pip install {pkg}")
    
    # 检查 requires.apis
    apis = skill.requires.get("apis", [])
    for api_name in apis:
        # 检查环境变量
        env_key = f"{api_name.upper()}_API_KEY"
        if env_key in os.environ:
            report.add_check(api_name, "apis", "ok", f"环境变量 {env_key} 已设置")
        else:
            # 检查 ~/.shiyi/.env
            env_file = Path.home() / ".shiyi" / ".env"
            if env_file.exists():
                with open(env_file, "r") as f:
                    if env_key in f.read():
                        report.add_check(api_name, "apis", "ok", f".env 中找到 {env_key}")
                        continue
            report.add_check(api_name, "apis", "warning", f"需要配置: {env_key}")
    
    # 检查 requires.tools（工具不一定需要注册，可能只是能力声明）
    tools = skill.requires.get("tools", [])
    for tool in tools:
        if tool in registered_clerks:
            report.add_check(tool, "tools", "ok", "已注册")
        else:
            # 工具声明只是一个提示，不强制要求
            report.add_check(tool, "tools", "ok", "工具声明（无需注册）")
    
    return report


# ═══════════════════════════════════════════════════════
# 吏员仓库
# ═══════════════════════════════════════════════════════

class ClerkProfile:
    """吏员画像 — 从 clerk.json 提取"""

    def __init__(self, clerk_id: str, name: str, desc: str,
                 caps: List[str], tools: List[Dict], dirname: str):
        self.clerk_id = clerk_id
        self.name = name
        self.description = desc
        self.capabilities = caps
        self.tools = tools
        self.dirname = dirname


def load_clerk_repo(base_dir: str) -> List[ClerkProfile]:
    """加载吏员仓库中所有吏员画像"""
    clerks = []
    base = Path(base_dir)
    if not base.exists():
        return clerks

    for clerk_dir in sorted(base.iterdir()):
        if not clerk_dir.is_dir() or not clerk_dir.name.startswith("clerk-"):
            continue
        cj = clerk_dir / "clerk.json"
        if not cj.exists():
            continue
        try:
            with open(cj, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        cp = ClerkProfile(
            clerk_id=data.get("clerk_id", clerk_dir.name),
            name=data.get("name", clerk_dir.name),
            desc=data.get("description", ""),
            caps=data.get("capabilities", []),
            tools=data.get("tools", []),
            dirname=clerk_dir.name,
        )
        clerks.append(cp)

    return clerks


# ═══════════════════════════════════════════════════════
# 匹配引擎
# ═══════════════════════════════════════════════════════

class MatchResult:
    """单个吏员匹配结果"""

    def __init__(self, clerk: ClerkProfile, score: float, reasons: List[str]):
        self.clerk = clerk
        self.score = score      # 0.0 ~ 1.0
        self.reasons = reasons  # 匹配原因


class AnalysisResult:
    """完整分析结果"""

    def __init__(self, skill: SkillProfile, matches: List[MatchResult]):
        self.skill = skill
        self.matches = matches          # 按 score 降序排列
        self.suggested_action: str = "" # "connect" / "new" / "ambiguous"
        self.suggested_clerk: Optional[MatchResult] = None
        self.clerk_template: Optional[Dict[str, Any]] = None  # 若建议新建，生成模板

    def summary(self) -> str:
        """人类可读的摘要报告"""
        lines = []
        lines.append(f"📋 技能: {self.skill.name or self.skill.source_path}")
        if self.skill.description:
            lines.append(f"   描述: {self.skill.description[:120]}")
        if self.skill.domain_tags:
            lines.append(f"   领域: {', '.join(self.skill.domain_tags)}")
        lines.append("")

        if self.matches:
            best = self.matches[0]
            if best.score >= 0.3:
                lines.append(f"🎯 建议: 接入已有吏员 `{best.clerk.name}` (匹配度 {best.score:.0%})")
                lines.append(f"   原因: {'; '.join(best.reasons[:3])}")
                if len(self.matches) > 1:
                    lines.append(f"   备选: {', '.join(m.clerk.name for m in self.matches[1:3])}")
            elif best.score >= 0.2:
                lines.append(f"🤔 部分匹配: {best.clerk.name} ({best.score:.0%})，建议新建吏员")
            else:
                lines.append("📦 建议: 新建吏员（无匹配现有吏员）")
        else:
            lines.append("📦 建议: 新建吏员（未找到已有吏员）")

        # 若生成了模板
        if self.clerk_template:
            lines.append("")
            lines.append("── 吏员模板预览 ──")
            lines.append(f"  clerk_id: {self.clerk_template.get('clerk_id', '')}")
            lines.append(f"  name: {self.clerk_template.get('name', '')}")
            caps = self.clerk_template.get("capabilities", [])
            lines.append(f"  capabilities: {caps}")
            lines.append(f"  输出路径: {self.clerk_template.get('_output_dir', '')}")

        return "\n".join(lines)


def compute_match_score(skill: SkillProfile, clerk: ClerkProfile) -> MatchResult:
    """计算技能与吏员的匹配度"""
    reasons = []
    total = 0.0

    # 1. 工具名/能力命中 (权重: 0.40)
    # 支持前缀匹配：技能工具名 "openclaw" 可匹配吏员能力 "openclaw_send"
    skill_tool_names = set(skill.tools)
    clerk_tool_names = set(clerk.capabilities)
    tool_overlap = set()
    for st in skill_tool_names:
        for ct in clerk_tool_names:
            if st == ct or st in ct or ct in st:
                tool_overlap.add(st)
    if tool_overlap:
        hit_rate = len(tool_overlap) / max(len(skill_tool_names), 1)
        total += 0.40 * hit_rate
        reasons.append(f"工具匹配: {', '.join(tool_overlap)}")

    # 2. 描述文本相似 (权重: 0.25)
    all_skill_text = f"{skill.name} {skill.description} {' '.join(skill.keywords)}".lower()
    all_clerk_text = f"{clerk.name} {clerk.description} {' '.join(clerk.capabilities)}".lower()
    common_words = set(all_skill_text.split()) & set(all_clerk_text.split())
    meaningful_words = {w for w in common_words if len(w) > 3 and w not in {
        "that", "this", "with", "from", "into", "when", "they", "have", "been", "will"
    }}
    if meaningful_words:
        # Use logarithmic scale: 5 meaningful words = good, 10 = great
        total += 0.25 * min(1.0, len(meaningful_words) / 5)
        sample = list(meaningful_words)[:5]
        reasons.append(f"关键词重叠: {', '.join(sample)}")

    # 3. 领域匹配 (权重: 0.20)
    domain_overlap = set(skill.domain_tags) & set(_infer_clerk_domains(clerk))
    if domain_overlap:
        total += 0.20 * (len(domain_overlap) / max(len(skill.domain_tags), 1))
        reasons.append(f"领域匹配: {', '.join(domain_overlap)}")

    # 4. 触发词 / capabilities 匹配 (权重: 0.15)
    trigger_overlap = set()
    for trig in skill.triggers:
        for cap in clerk.capabilities:
            if trig.lower() in cap.lower() or cap.lower() in trig.lower():
                trigger_overlap.add(trig)
    if trigger_overlap:
        total += 0.15 * min(1.0, len(trigger_overlap) / 2)
        reasons.append(f"触发词命中: {', '.join(trigger_overlap)}")

    total = min(1.0, total)
    return MatchResult(clerk, total, reasons)


def _infer_clerk_domains(clerk: ClerkProfile) -> List[str]:
    """推断吏员领域"""
    domains = set()
    text = f"{clerk.name} {clerk.description} {' '.join(clerk.capabilities)}".lower()
    dm = {
        "messaging": ["send", "channel", "notify", "message"],
        "web":      ["web", "search", "browser", "http", "url"],
        "file":     ["file", "read", "write", "save"],
        "system":   ["exec", "cmd", "server", "process"],
        "code":     ["git", "repo", "code", "commit"],
        "ai":       ["agent", "llm", "ai", "task"],
    }
    for d, kws in dm.items():
        if any(kw in text for kw in kws):
            domains.add(d)
    return sorted(domains)


# ═══════════════════════════════════════════════════════
# 吏员模板生成
# ═══════════════════════════════════════════════════════

def generate_clerk_template(skill: SkillProfile, output_dir: str = "") -> Dict[str, Any]:
    """根据技能画像生成 clerk.json + worker.py 模板内容"""
    slug = re.sub(r"[^a-z0-9_]", "_", skill.name.lower() or "custom")
    clerk_id = f"clerk_{slug[:20]}_001"

    # 推断工具
    inferred_tools = _infer_tools_from_skill(skill)

    template = {
        "clerk_id": clerk_id,
        "name": skill.name or "未命名吏员",
        "version": "0.1.0",
        "description": skill.description or "从技能自动生成的吏员",
        "capabilities": [t["name"] for t in inferred_tools],
        "tools": inferred_tools,
        "enabled": True,
        "created_at": "",  # 由生成脚本填充
        "config_path": f"clerk-{slug[:30]}/",
        "skills": [skill.name],
        "api_keys": [],
        "requires_llm": False,
        "data_policy": "shiyi_only",
        "_output_dir": output_dir or f"shiyi-shell/shiyi/shell/clerk-{slug[:30]}/",
    }
    return template


def _infer_tools_from_skill(skill: SkillProfile) -> List[Dict[str, Any]]:
    """从技能画像推断工具定义"""
    tools = []
    all_text = f"{skill.name} {skill.description} {' '.join(skill.triggers)} {' '.join(skill.keywords)} {skill.raw_body[:2000]}".lower()

    # 从反引号命令中提取 CLI 前缀作为工具名
    seen_prefixes = set()
    for tool in skill.tools:
        prefix = tool.split()[0]
        if prefix not in seen_prefixes:
            seen_prefixes.add(prefix)
            tools.append({
                "name": f"{prefix}_exec",
                "description": f"执行 {tool} 相关操作",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": f"{prefix} 命令"
                        }
                    },
                    "required": ["command"]
                }
            })

    # 推断通用工具
    if any(kw in all_text for kw in ["send", "message", "notify", "推送", "发送"]):
        if "messaging" in skill.domain_tags:
            tools.append({
                "name": "send_message",
                "description": "发送消息到指定平台/对话",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "消息内容"},
                        "target": {"type": "string", "description": "目标平台:渠道标识"}
                    },
                    "required": ["message"]
                }
            })

    if any(kw in all_text for kw in ["search", "query", "搜", "查"]):
        tools.append({
            "name": "search",
            "description": "搜索/查询信息",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "description": "最大结果数", "default": 5}
                },
                "required": ["query"]
            }
        })

    if any(kw in all_text for kw in ["file", "read", "write", "save", "文件", "读写"]):
        tools.append({
            "name": "file_write",
            "description": "写入文件到 $SHIYI_WORKSPACE",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（$SHIYI_WORKSPACE 内）"},
                    "content": {"type": "string", "description": "文件内容"}
                },
                "required": ["path", "content"]
            }
        })

    # 如果没有推断出任何工具，给一个通用占位
    if not tools:
        tools.append({
            "name": f"{skill.name.lower()[:20]}_execute",
            "description": f"执行 {skill.name} 技能操作",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "操作描述"}
                },
                "required": ["action"]
            }
        })

    return tools


def generate_worker_stub(skill: SkillProfile, clerk_id: str) -> str:
    """生成 worker.py 骨架"""
    tool_names = [t["name"] for t in _infer_tools_from_skill(skill)]
    tool_names_str = ", ".join(f'"{n}"' for n in tool_names)

    return '''"""worker.py — {skill_name} 吏员网络

从技能 "{skill_name}" 自动生成。请实现 _execute_* 方法。
"""

import subprocess
import json
import os
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path


# ═══ ClerkConfig ═══

@dataclass
class ClerkConfig:
    clerk_id: str = "{clerk_id}"
    name: str = "{skill_name}"
    version: str = "0.1.0"
    workspace: str = field(default_factory=lambda: os.path.expanduser("~/.shiyi/workspace"))
    enabled: bool = True


# ═══ 工具注册 ═══

TOOL_REGISTRY = {{
    {tool_registry}
}}


# ═══ ClerkWorker ═══

class ClerkWorker:
    """自动生成的吏员，请实现 _execute_* 方法"""

    config: ClerkConfig
    _config_path: Path
    _log: List[Dict[str, Any]]

    def __init__(self, config: Optional[ClerkConfig] = None, config_path: Optional[str] = None):
        self.config = config or ClerkConfig()
        self._config_path = Path(config_path or f"clerk-{{self.config.clerk_id}}")
        self._log = []

        # 确保 workspace 存在
        ws = Path(os.path.expanduser("~/.shiyi/workspace"))
        ws.mkdir(parents=True, exist_ok=True)

    def get_tools(self) -> List[Dict[str, Any]]:
        """返回工具列表（从 clerk.json 读取）"""
        cj = self._config_path / "clerk.json"
        if cj.exists():
            try:
                with open(cj, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("tools", [])
            except Exception:
                pass
        return []

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具调用"""
        handler = TOOL_REGISTRY.get(tool_name)
        if not handler:
            return {{"error": f"未知工具: {{tool_name}}", "success": False}}

        try:
            method = getattr(self, handler, None)
            if not method:
                return {{"error": f"处理器未实现: {{handler}}", "success": False}}

            result = method(params)
            result["success"] = True
            self._log.append({{
                "tool": tool_name,
                "params": params,
                "time": datetime.now().isoformat(),
            }})
            return result
        except Exception as e:
            return {{"error": str(e), "success": False}}

    def status(self) -> Dict[str, Any]:
        """返回吏员状态"""
        return {{
            "clerk_id": self.config.clerk_id,
            "name": self.config.name,
            "version": self.config.version,
            "enabled": self.config.enabled,
            "data_policy": "shiyi_only",
            "log_count": len(self._log),
        }}

    # ═══ 工具实现（请在此添加） ═══

{tool_methods}
'''.format(
        skill_name=skill.name or "custom",
        clerk_id=clerk_id,
        tool_registry=",\n    ".join(f'"{n}": "_execute_{n}"' for n in tool_names),
        tool_methods="".join(f'''
    def _execute_{n}(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """工具: {n} — TODO: 实现此方法"""
        raise NotImplementedError("工具 {n} 尚未实现 — 请在 worker.py 中补全 _execute_{n}() 方法")
''' for n in tool_names),
    )
