"""权限引擎 — Steward 调度环节的执行边界控制

v0.1.0 · shiyi+h 分支

设计原则：
  - 零配置降级：没有 config.json 时默认 default 级别，没有 clerk.json 时默认 allow
  - 不侵入记忆/决策层，只在 Steward.dispatch() 和 ClerkRegistry.execute() 入口拦截
  - 全局级别 → config.json 的 permission_level 字段
  - 工具标记 → clerk.json 每个 tool 的 permission 字段
  - 不加新文件
"""

import json
import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════

@dataclass
class PermissionResult:
    """权限检查结果"""
    allowed: bool = True
    need_confirm: bool = False
    reason: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed and not self.need_confirm


# 权限级别枚举
LEVEL_DEFAULT = "default"
LEVEL_PLAN = "plan"
LEVEL_FULLAUTO = "fullauto"
VALID_LEVELS = {LEVEL_DEFAULT, LEVEL_PLAN, LEVEL_FULLAUTO}

# 工具权限标记
PERM_ALLOW = "allow"
PERM_ASK = "ask"
PERM_DENY = "deny"
VALID_PERMS = {PERM_ALLOW, PERM_ASK, PERM_DENY}

# 默认命令黑名单
DEFAULT_COMMAND_BLACKLIST = [
    "rm -rf /", "rm -rf /*",
    "dd if=",
    "mkfs",
    "shutdown", "reboot",
    "format",
    ":(){ :|:& };:",  # fork bomb
    "> /dev/sd",
]

# 默认路径规则
DEFAULT_ALLOW_PATHS = ["$SHIYI_WORKSPACE"]
DEFAULT_DENY_PATHS = ["/etc", "/root/.ssh", "/etc/shadow", "/etc/passwd"]


# ═══════════════════════════════════════════════
# 权限引擎
# ═══════════════════════════════════════════════

class PermissionEngine:
    """权限引擎 — 读取配置，判定工具是否允许执行"""

    def __init__(
        self,
        config_path: Optional[str] = None,
        clerk_config_dir: Optional[str] = None,
    ):
        """初始化权限引擎

        Args:
            config_path: config.json 路径（shiyi-core/shiyi/config.json）
            clerk_config_dir: 吏员配置目录（包含 clerk.json）
        """
        self._level = LEVEL_DEFAULT
        self._tool_perms: Dict[str, str] = {}  # tool_name → permission
        self._command_blacklist: List[str] = list(DEFAULT_COMMAND_BLACKLIST)
        self._path_rules: Dict[str, List[str]] = {
            "allow_paths": list(DEFAULT_ALLOW_PATHS),
            "deny_paths": list(DEFAULT_DENY_PATHS),
        }
        self._loaded = False

        # 加载配置
        self._load_config(config_path)
        self._load_clerk_perms(clerk_config_dir)
        self._loaded = True

        logger.info(
            "PermissionEngine initialized: level=%s, tools=%d, blacklist=%d",
            self._level, len(self._tool_perms), len(self._command_blacklist),
        )

    # ──────────────────────────────
    # 配置加载
    # ──────────────────────────────

    def _load_config(self, config_path: Optional[str]) -> None:
        """加载全局配置 config.json"""
        if not config_path:
            # 尝试默认路径
            default_path = Path(__file__).parent.parent / "config.json"
            if default_path.exists():
                config_path = str(default_path)

        if not config_path or not Path(config_path).exists():
            logger.debug("No config.json found, using defaults")
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            # 权限级别
            level = config.get("permission_level", LEVEL_DEFAULT)
            if level in VALID_LEVELS:
                self._level = level
            else:
                logger.warning("Invalid permission_level '%s', using default", level)

            # 路径规则
            if "path_rules" in config:
                pr = config["path_rules"]
                if "allow_paths" in pr:
                    self._path_rules["allow_paths"] = pr["allow_paths"]
                if "deny_paths" in pr:
                    self._path_rules["deny_paths"] = pr["deny_paths"]

            # 命令黑名单
            if "command_blacklist" in config:
                self._command_blacklist = config["command_blacklist"]

        except Exception as e:
            logger.error("Failed to load config.json: %s", e)

    def _load_clerk_perms(self, clerk_config_dir: Optional[str]) -> None:
        """加载所有 clerk.json 中的工具权限标记"""
        if not clerk_config_dir:
            # 尝试默认路径
            default_dir = Path(__file__).parent.parent.parent / "shell" / "clerk-default"
            if default_dir.exists():
                clerk_config_dir = str(default_dir)

        if not clerk_config_dir:
            return

        clerk_dir = Path(clerk_config_dir)

        # 加载指定目录的 clerk.json
        clerk_json = clerk_dir / "clerk.json"
        if clerk_json.exists():
            self._parse_clerk_json(clerk_json)

        # 递归查找其他吏员目录
        if clerk_dir.parent.exists():
            for sub in clerk_dir.parent.iterdir():
                if sub.is_dir() and sub != clerk_dir:
                    cj = sub / "clerk.json"
                    if cj.exists():
                        self._parse_clerk_json(cj)

    def _parse_clerk_json(self, path: Path) -> None:
        """解析单个 clerk.json，提取工具权限"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for tool in data.get("tools", []):
                name = tool.get("name", "")
                perm = tool.get("permission", PERM_ALLOW)
                if name and perm in VALID_PERMS:
                    self._tool_perms[name] = perm

            logger.debug("Loaded %d tool perms from %s", len(data.get("tools", [])), path)

        except Exception as e:
            logger.error("Failed to parse %s: %s", path, e)

    # ──────────────────────────────
    # 权限检查
    # ──────────────────────────────

    def check(self, tool_name: str, clerk_id: Optional[str] = None) -> PermissionResult:
        """检查工具是否允许执行

        Args:
            tool_name: 工具名称
            clerk_id: 吏员 ID（可选，用于日志）

        Returns:
            PermissionResult
        """
        # 1. 查工具标记
        perm = self._tool_perms.get(tool_name, PERM_ALLOW)

        # 2. deny → 直接拒绝
        if perm == PERM_DENY:
            reason = f"工具 {tool_name} 被禁止执行 (permission=deny)"
            logger.warning("Permission denied: %s", reason)
            return PermissionResult(allowed=False, need_confirm=False, reason=reason)

        # 3. ask + default → 需确认
        if perm == PERM_ASK and self._level == LEVEL_DEFAULT:
            reason = f"工具 {tool_name} 需要用户确认 (当前权限级别: {self._level})"
            logger.info("Permission confirm required: %s", reason)
            return PermissionResult(allowed=True, need_confirm=True, reason=reason)

        # 4. allow 或 ask+plan/fullauto → 放行
        return PermissionResult(allowed=True, need_confirm=False)

    def ask_user(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """生成确认请求

        Args:
            tool_name: 工具名称
            params: 工具参数

        Returns:
            结构化的确认请求
        """
        return {
            "action": "confirm_required",
            "tool_name": tool_name,
            "params": params,
            "reason": f"工具 {tool_name} 需要用户确认（当前权限级别: {self._level}）",
            "options": ["确认执行", "跳过", "终止任务"],
        }

    # ──────────────────────────────
    # 工具方法
    # ──────────────────────────────

    def get_level(self) -> str:
        """返回当前全局权限级别"""
        return self._level

    def get_tool_perm(self, tool_name: str) -> str:
        """返回工具的权限标记"""
        return self._tool_perms.get(tool_name, PERM_ALLOW)

    def check_command(self, command: str) -> PermissionResult:
        """检查 shell 命令是否在黑名单中"""
        cmd_lower = command.lower().strip()
        for blocked in self._command_blacklist:
            if blocked.lower() in cmd_lower:
                reason = f"命令被黑名单拦截: {blocked}"
                logger.warning("Command blocked: %s", reason)
                return PermissionResult(allowed=False, need_confirm=False, reason=reason)
        return PermissionResult(allowed=True, need_confirm=False)

    def check_path(self, path: str) -> PermissionResult:
        """检查路径是否在允许范围内"""
        # 展开环境变量
        expanded = os.path.expandvars(path)
        abs_path = os.path.abspath(expanded)

        # 检查拒绝路径
        for deny in self._path_rules.get("deny_paths", []):
            deny_expanded = os.path.expandvars(deny)
            if abs_path.startswith(os.path.abspath(deny_expanded)):
                reason = f"路径被拒绝: {path} (匹配规则: {deny})"
                logger.warning("Path denied: %s", reason)
                return PermissionResult(allowed=False, need_confirm=False, reason=reason)

        # 检查允许路径
        allow_paths = self._path_rules.get("allow_paths", [])
        if allow_paths:
            in_allow = False
            for allow in allow_paths:
                allow_expanded = os.path.expandvars(allow)
                if abs_path.startswith(os.path.abspath(allow_expanded)):
                    in_allow = True
                    break
            if not in_allow:
                reason = f"路径不在允许范围内: {path}"
                logger.warning("Path not allowed: %s", reason)
                return PermissionResult(allowed=False, need_confirm=False, reason=reason)

        return PermissionResult(allowed=True, need_confirm=False)

    def reload(self) -> None:
        """重新加载配置（热更新）"""
        self._level = LEVEL_DEFAULT
        self._tool_perms.clear()
        self._command_blacklist = list(DEFAULT_COMMAND_BLACKLIST)
        self._path_rules = {
            "allow_paths": list(DEFAULT_ALLOW_PATHS),
            "deny_paths": list(DEFAULT_DENY_PATHS),
        }
        self._load_config(None)
        self._load_clerk_perms(None)
        logger.info("PermissionEngine reloaded: level=%s", self._level)
