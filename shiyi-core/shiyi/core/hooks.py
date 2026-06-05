"""Hook 框架 — Steward 调度环节的生命周期钩子

v0.1.0 · shiyi+h 分支

设计原则：
  - PreToolUse: 执行前拦截/修改参数
  - PostToolUse: 执行后修改结果/记录日志
  - 支持精确匹配（指定工具名）和通配符（所有工具）
  - 内置 Hook: path_guard, command_guard, audit_log
  - 轻量级，不做复杂路由
"""

import os
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, Callable, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════

@dataclass
class HookResult:
    """PreHook 执行结果"""
    blocked: bool = False
    reason: str = ""
    modified_params: Optional[Dict[str, Any]] = None


# ═══════════════════════════════════════════════
# Hook 管理器
# ═══════════════════════════════════════════════

class HookManager:
    """Hook 管理器 — 注册和执行 Pre/Post ToolUse 钩子"""

    WILDCARD = "*"

    def __init__(self):
        # tool_name → [hook_fn, ...]
        self._pre_hooks: Dict[str, List[Callable]] = {}
        self._post_hooks: Dict[str, List[Callable]] = {}
        # 通配符 Hook
        self._pre_wildcards: List[Callable] = []
        self._post_wildcards: List[Callable] = []
        # 审计日志路径（默认不记录）
        self._audit_log_path: Optional[str] = None

    # ──────────────────────────────
    # 注册
    # ──────────────────────────────

    def register_pre(self, tool_name: str, hook_fn: Callable) -> None:
        """注册 PreToolUse Hook（精确匹配工具名）

        Args:
            tool_name: 工具名称
            hook_fn: (tool_name, params) → HookResult
        """
        if tool_name not in self._pre_hooks:
            self._pre_hooks[tool_name] = []
        self._pre_hooks[tool_name].append(hook_fn)
        logger.debug("Registered pre-hook for %s: %s", tool_name, hook_fn.__name__)

    def register_post(self, tool_name: str, hook_fn: Callable) -> None:
        """注册 PostToolUse Hook（精确匹配工具名）

        Args:
            tool_name: 工具名称
            hook_fn: (tool_name, params, result) → dict
        """
        if tool_name not in self._post_hooks:
            self._post_hooks[tool_name] = []
        self._post_hooks[tool_name].append(hook_fn)
        logger.debug("Registered post-hook for %s: %s", tool_name, hook_fn.__name__)

    def register_pre_wildcard(self, hook_fn: Callable) -> None:
        """注册 PreToolUse 通配符 Hook（匹配所有工具）"""
        self._pre_wildcards.append(hook_fn)
        logger.debug("Registered wildcard pre-hook: %s", hook_fn.__name__)

    def register_post_wildcard(self, hook_fn: Callable) -> None:
        """注册 PostToolUse 通配符 Hook（匹配所有工具）"""
        self._post_wildcards.append(hook_fn)
        logger.debug("Registered wildcard post-hook: %s", hook_fn.__name__)

    # ──────────────────────────────
    # 执行
    # ──────────────────────────────

    def run_pre(self, tool_name: str, params: Dict[str, Any]) -> HookResult:
        """执行所有匹配的 PreHook

        按顺序执行：通配符 Hook → 精确匹配 Hook
        任一 Hook 返回 blocked=True 则终止链

        Returns:
            HookResult
        """
        current_params = params

        # 通配符 Hook
        for hook_fn in self._pre_wildcards:
            try:
                result = hook_fn(tool_name, current_params)
                if isinstance(result, HookResult):
                    if result.blocked:
                        logger.info("PreHook %s blocked %s: %s", hook_fn.__name__, tool_name, result.reason)
                        return result
                    if result.modified_params:
                        current_params = result.modified_params
            except Exception as e:
                logger.error("PreHook %s failed: %s", hook_fn.__name__, e)

        # 精确匹配 Hook
        for hook_fn in self._pre_hooks.get(tool_name, []):
            try:
                result = hook_fn(tool_name, current_params)
                if isinstance(result, HookResult):
                    if result.blocked:
                        logger.info("PreHook %s blocked %s: %s", hook_fn.__name__, tool_name, result.reason)
                        return result
                    if result.modified_params:
                        current_params = result.modified_params
            except Exception as e:
                logger.error("PreHook %s failed: %s", hook_fn.__name__, e)

        return HookResult(
            blocked=False,
            modified_params=current_params if current_params != params else None,
        )

    def run_post(self, tool_name: str, params: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        """执行所有匹配的 PostHook

        按顺序执行：精确匹配 Hook → 通配符 Hook
        PostHook 不阻断，只可修改 result

        Returns:
            修改后的 result
        """
        current_result = result

        # 精确匹配 Hook
        for hook_fn in self._post_hooks.get(tool_name, []):
            try:
                modified = hook_fn(tool_name, params, current_result)
                if isinstance(modified, dict):
                    current_result = modified
            except Exception as e:
                logger.error("PostHook %s failed: %s", hook_fn.__name__, e)

        # 通配符 Hook
        for hook_fn in self._post_wildcards:
            try:
                modified = hook_fn(tool_name, params, current_result)
                if isinstance(modified, dict):
                    current_result = modified
            except Exception as e:
                logger.error("PostHook %s failed: %s", hook_fn.__name__, e)

        return current_result

    # ──────────────────────────────
    # 配置
    # ──────────────────────────────

    def set_audit_log_path(self, path: str) -> None:
        """设置审计日志文件路径"""
        self._audit_log_path = path

    @property
    def hook_count(self) -> Tuple[int, int]:
        """返回 (pre_hook_count, post_hook_count)"""
        pre = len(self._pre_wildcards) + sum(len(v) for v in self._pre_hooks.values())
        post = len(self._post_wildcards) + sum(len(v) for v in self._post_hooks.values())
        return pre, post


# ═══════════════════════════════════════════════
# 内置 Hook
# ═══════════════════════════════════════════════

def path_guard(tool_name: str, params: Dict[str, Any]) -> HookResult:
    """PreHook: 检查文件操作路径是否在 workspace 内

    对 file_read、file_write、file_edit 等工具，
    检查 path 参数是否在允许范围内。
    """
    # 需要路径检查的工具
    PATH_TOOLS = {"file_read", "file_write", "file_edit", "file_glob", "file_grep"}
    if tool_name not in PATH_TOOLS:
        return HookResult()

    path = params.get("path", "")
    if not path:
        return HookResult()

    # 展开环境变量
    expanded = os.path.expandvars(path)
    abs_path = os.path.abspath(expanded)

    # 获取 workspace
    workspace = os.environ.get("SHIYI_WORKSPACE", "")
    if not workspace:
        # 没有 workspace 限制，放行
        return HookResult()

    workspace_abs = os.path.abspath(workspace)

    # 检查路径是否在 workspace 内
    if not abs_path.startswith(workspace_abs):
        return HookResult(
            blocked=True,
            reason=f"路径越界: {path} 不在 workspace {workspace} 内",
        )

    return HookResult()


def command_guard(tool_name: str, params: Dict[str, Any]) -> HookResult:
    """PreHook: 检查 shell 命令是否在黑名单中

    对 run_command、bash 等工具，检查 command 参数。
    """
    CMD_TOOLS = {"run_command", "bash", "shell", "execute_code"}
    if tool_name not in CMD_TOOLS:
        return HookResult()

    command = params.get("command", "")
    if not command:
        return HookResult()

    # 黑名单检查
    blacklist = [
        "rm -rf /", "rm -rf /*",
        "dd if=",
        "mkfs",
        "shutdown", "reboot",
        "format",
        ":(){ :|:& };:",
        "> /dev/sd",
    ]

    cmd_lower = command.lower().strip()
    for blocked in blacklist:
        if blocked.lower() in cmd_lower:
            return HookResult(
                blocked=True,
                reason=f"命令被黑名单拦截: {blocked}",
            )

    return HookResult()


def audit_log(tool_name: str, params: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """PostHook: 记录工具调用审计日志

    写入 JSON Lines 格式的审计日志文件。
    不修改 result，只是记录。
    """
    log_entry = {
        "timestamp": time.time(),
        "time_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "tool": tool_name,
        "success": result.get("success", False),
        "params_summary": _summarize_params(params),
        "error": result.get("error", ""),
    }

    # 写入审计日志（如果配置了路径）
    # 这里只做 logger.info，实际文件写入由 Steward 配置 audit_log_path 触发
    if result.get("success"):
        logger.debug("Audit: %s OK", tool_name)
    else:
        logger.info("Audit: %s FAILED — %s", tool_name, result.get("error", ""))

    # 不修改 result
    return result


def _summarize_params(params: Dict[str, Any], max_len: int = 200) -> str:
    """参数摘要（截断过长参数）"""
    s = json.dumps(params, ensure_ascii=False)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


# ═══════════════════════════════════════════════
# 审计日志写入器
# ═══════════════════════════════════════════════

class AuditLogger:
    """审计日志写入器 — 持久化工具调用记录"""

    def __init__(self, log_path: str):
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, tool_name: str, params: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        """写入审计日志（JSON Lines 格式）"""
        entry = {
            "timestamp": time.time(),
            "time_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "tool": tool_name,
            "success": result.get("success", False),
            "params_summary": _summarize_params(params),
            "error": result.get("error", ""),
        }
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Failed to write audit log: %s", e)

        return result  # 不修改 result

    def read_recent(self, count: int = 50) -> List[Dict[str, Any]]:
        """读取最近的审计日志"""
        if not self._log_path.exists():
            return []
        try:
            lines = self._log_path.read_text(encoding="utf-8").strip().split("\n")
            entries = []
            for line in lines[-count:]:
                if line.strip():
                    entries.append(json.loads(line))
            return entries
        except Exception:
            return []
