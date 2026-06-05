"""
工具辅助函数

提供路径安全检查等通用功能
"""

import re
from pathlib import Path
from typing import Union

# 路径穿越正则
PATH_TRAVERSAL_PATTERN = re.compile(r'(\.\.\/|\.\.\\|%2e%2e%2f|%2e%2e\/)', re.IGNORECASE)


def safe_path(filepath: Union[str, Path], workspace: Path) -> Path:
    """
    将路径限制在安全工作目录内

    Args:
        filepath: 用户提供的文件路径
        workspace: 工作目录
        
    Returns:
        归一化后的安全路径
        
    Raises:
        PermissionError: 路径穿越尝试
    """
    safe_root = Path(workspace).resolve()
    
    # 防止 URL 编码的路径穿越
    if isinstance(filepath, str):
        decoded = filepath.replace('%2e', '.').replace('%2E', '.')
        if PATH_TRAVERSAL_PATTERN.search(decoded):
            raise PermissionError(f"路径穿越被拒绝: {filepath}")
    
    # 处理相对路径
    p = Path(filepath)
    if not p.is_absolute():
        p = safe_root / p
    
    # 归一化路径
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as e:
        raise PermissionError(f"路径解析失败: {filepath} - {e}")
    
    # 防止路径穿越
    if not str(resolved).startswith(str(safe_root)):
        raise PermissionError(f"路径穿越被拒绝: {filepath} -> {resolved}")
    
    return resolved


def ensure_workspace(workspace: Path) -> None:
    """确保工作目录存在"""
    workspace.mkdir(parents=True, exist_ok=True)


def truncate_output(text: str, max_size: int = 100 * 1024) -> str:
    """截断输出"""
    if len(text.encode('utf-8')) > max_size:
        return text[:max_size] + f"\n\n[输出过长，已截断至 {max_size} 字节]"
    return text
