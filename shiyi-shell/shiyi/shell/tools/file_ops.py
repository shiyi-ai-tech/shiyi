"""
文件操作工具

安全约束：
- 读文件：限制在 ~/.shiyi/workspace/ 目录内
- 写文件：同样限制在 workspace 目录
- 路径必须归一化，防止路径穿越
"""
import os
import logging
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

# 安全工作目录
SAFE_ROOT = Path.home() / ".shiyi" / "workspace"

# 最大读取行数
MAX_READ_LINES = 500


def _safe_path(filepath: str) -> Path:
    """将路径限制在安全工作目录内"""
    safe_root = SAFE_ROOT.resolve()

    # 处理相对路径
    p = Path(filepath)
    if not p.is_absolute():
        p = safe_root / p

    resolved = p.resolve()

    # 防止路径穿越
    if not str(resolved).startswith(str(safe_root)):
        raise PermissionError(f"路径穿越被拒绝: {filepath} -> {resolved}")

    return resolved


def _ensure_workspace() -> None:
    """确保工作目录存在"""
    SAFE_ROOT.mkdir(parents=True, exist_ok=True)


# ─── 读文件工具 ───

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "文件路径（相对于 ~/.shiyi/workspace/ 或绝对路径）",
        },
        "limit": {
            "type": "integer",
            "description": f"最大读取行数（默认 {MAX_READ_LINES}）",
            "default": MAX_READ_LINES,
        },
    },
    "required": ["path"],
}


def file_read_handler(args: Dict[str, Any]) -> Dict[str, Any]:
    """读取文件内容"""
    filepath = args.get("path", "")
    limit = int(args.get("limit", MAX_READ_LINES))

    if not filepath.strip():
        return {"success": False, "result": "", "error": "Empty path"}

    try:
        safe = _safe_path(filepath)

        if not safe.exists():
            return {"success": False, "result": "", "error": f"文件不存在: {safe}"}

        if safe.is_dir():
            # 列出目录内容
            entries = sorted(safe.iterdir())[:limit * 2]
            lines = [f"{'📁' if e.is_dir() else '📄'} {e.name}" for e in entries]
            return {"success": True, "result": "\n".join(lines[:limit]), "error": ""}

        # 读文本文件
        with open(safe, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[:limit]

        text = "".join(lines)
        if len(lines) == limit:
            text += f"\n\n[文件过长，仅显示前 {limit} 行]"

        return {"success": True, "result": text, "error": ""}

    except PermissionError as e:
        logger.warning(f"Path traversal blocked: {e}")
        return {"success": False, "result": "", "error": str(e)}
    except Exception as e:
        logger.warning(f"File read failed: {e}")
        return {"success": False, "result": "", "error": str(e)}


# ─── 写文件工具 ───

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "文件路径（相对于 ~/.shiyi/workspace/）",
        },
        "content": {
            "type": "string",
            "description": "要写入的内容",
        },
    },
    "required": ["path", "content"],
}


def file_write_handler(args: Dict[str, Any]) -> Dict[str, Any]:
    """写入文件内容"""
    filepath = args.get("path", "")
    content = args.get("content", "")

    if not filepath.strip():
        return {"success": False, "result": "", "error": "Empty path"}

    try:
        _ensure_workspace()
        safe = _safe_path(filepath)

        # 不允许写入目录
        if safe.is_dir():
            return {"success": False, "result": "", "error": "Cannot overwrite directory"}

        # 创建父目录
        safe.parent.mkdir(parents=True, exist_ok=True)

        with open(safe, "w", encoding="utf-8") as f:
            f.write(content)

        size = safe.stat().st_size
        return {
            "success": True,
            "result": f"已写入: {safe} ({size} 字节)",
            "error": "",
        }

    except PermissionError as e:
        logger.warning(f"Path traversal blocked: {e}")
        return {"success": False, "result": "", "error": str(e)}
    except Exception as e:
        logger.warning(f"File write failed: {e}")
        return {"success": False, "result": "", "error": str(e)}


# 工具注册用
file_read_tool = {
    "name": "file_read",
    "handler": file_read_handler,
    "description": "读取文件内容或列出目录。安全限制在 ~/.shiyi/workspace/ 目录内。",
    "parameters": READ_SCHEMA,
}

file_write_tool = {
    "name": "file_write",
    "handler": file_write_handler,
    "description": "写入内容到文件。安全限制在 ~/.shiyi/workspace/ 目录内。",
    "parameters": WRITE_SCHEMA,
}
