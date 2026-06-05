"""
EnhancedFileReadTool - 增强版文件读取

增强版工具，特性：
- 支持 offset/limit 分页读取
- 支持行号显示
- 支持目录列表
- 大文件友好
"""

import logging
from pathlib import Path
from typing import Dict, Any

from ._utils import safe_path

logger = logging.getLogger(__name__)

# 默认参数
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000


class EnhancedFileReadTool:
    """增强版文件读取工具"""
    
    name = "enhanced_file_read"
    description = "增强版文件读取，支持 offset/limit 分页、行号显示、目录列表"
    
    schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（沙箱内）"
            },
            "limit": {
                "type": "integer",
                "description": f"最大读取行数（默认 {DEFAULT_LIMIT}，最大 {MAX_LIMIT}）",
                "default": DEFAULT_LIMIT
            },
            "offset": {
                "type": "integer",
                "description": "起始行偏移（从0开始，默认0）",
                "default": 0
            },
            "show_line_numbers": {
                "type": "boolean",
                "description": "是否显示行号（默认 False）",
                "default": False
            }
        },
        "required": ["path"]
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        执行文件读取
        
        Args:
            params: {
                "path": str,              # 文件路径
                "limit": int,             # 最大行数（默认500）
                "offset": int,            # 起始偏移（默认0）
                "show_line_numbers": bool # 显示行号（默认False）
            }
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "data": {"content": str, "total_lines": int, "truncated": bool}, "error": str}
        """
        filepath = params.get("path", "")
        limit = min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = int(params.get("offset", 0))
        show_line_numbers = params.get("show_line_numbers", False)
        
        if not filepath.strip():
            return {"success": False, "data": None, "error": "Empty path"}
        
        try:
            safe = safe_path(filepath, workspace)
            
            if not safe.exists():
                return {"success": False, "data": None, "error": f"文件不存在: {safe}"}
            
            if safe.is_dir():
                # 目录列表
                entries = sorted(safe.iterdir())
                total = len(entries)
                shown = entries[offset:offset + limit]
                
                lines = []
                for i, entry in enumerate(shown):
                    icon = "📁" if entry.is_dir() else "📄"
                    lines.append(f"{icon} {entry.name}")
                
                content = "\n".join(lines)
                truncated = (offset + limit) < total
                
                return {
                    "success": True,
                    "data": {
                        "content": content,
                        "total_items": total,
                        "offset": offset,
                        "limit": limit,
                        "truncated": truncated,
                        "type": "directory"
                    },
                    "error": ""
                }
            
            # 读取文本文件
            with open(safe, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            
            total_lines = len(all_lines)
            
            # 应用 offset 和 limit
            start = max(0, offset)
            end = min(len(all_lines), start + limit)
            shown_lines = all_lines[start:end]
            
            # 构建输出
            if show_line_numbers:
                content = "".join(
                    f"{start + i + 1:6d} | {line}" 
                    for i, line in enumerate(shown_lines)
                )
            else:
                content = "".join(shown_lines)
            
            truncated = end < total_lines
            
            return {
                "success": True,
                "data": {
                    "content": content,
                    "total_lines": total_lines,
                    "offset": start,
                    "limit": limit,
                    "truncated": truncated,
                    "type": "file"
                },
                "error": ""
            }
            
        except PermissionError as e:
            logger.warning(f"Path traversal blocked: {e}")
            return {"success": False, "data": None, "error": str(e)}
        except Exception as e:
            logger.warning(f"File read failed: {e}")
            return {"success": False, "data": None, "error": str(e)}
