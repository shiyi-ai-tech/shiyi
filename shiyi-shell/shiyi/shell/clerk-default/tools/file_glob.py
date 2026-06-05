"""
FileGlobTool - 按模式搜索文件名

增强版工具，功能：
- 使用 glob 模式匹配文件名
- 支持递归搜索
- 返回匹配文件列表
"""

import logging
from pathlib import Path
from typing import Dict, Any, List

from ._utils import safe_path

logger = logging.getLogger(__name__)

# 默认最大结果数
DEFAULT_MAX_RESULTS = 100


class FileGlobTool:
    """文件名模式匹配工具"""
    
    name = "file_glob"
    description = "按 glob 模式搜索文件名，如 *.py, **/*.md 等"
    
    schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "glob 模式，如 *.py, **/*.txt, src/**/*.js"
            },
            "base_dir": {
                "type": "string",
                "description": "搜索起始目录（沙箱内，默认为 workspace）",
                "default": "."
            },
            "recursive": {
                "type": "boolean",
                "description": "是否递归搜索子目录（默认 True）",
                "default": True
            },
            "max_results": {
                "type": "integer",
                "description": f"最大返回数量（默认 {DEFAULT_MAX_RESULTS}）",
                "default": DEFAULT_MAX_RESULTS
            }
        },
        "required": ["pattern"]
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        执行文件名搜索
        
        Args:
            params: {
                "pattern": str,       # glob 模式
                "base_dir": str,      # 起始目录
                "recursive": bool,   # 是否递归
                "max_results": int   # 最大结果数
            }
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "data": {"matches": [str], "count": int}, "error": str}
        """
        pattern = params.get("pattern", "")
        base_dir = params.get("base_dir", ".")
        recursive = params.get("recursive", True)
        max_results = int(params.get("max_results", DEFAULT_MAX_RESULTS))
        
        if not pattern.strip():
            return {"success": False, "data": None, "error": "Empty pattern"}
        
        try:
            base = safe_path(base_dir, workspace)
            
            if not base.exists():
                return {"success": False, "data": None, "error": f"目录不存在: {base}"}
            
            if not base.is_dir():
                return {"success": False, "data": None, "error": f"不是目录: {base}"}
            
            # 构建 glob 路径
            if recursive:
                glob_pattern = str(base / "**" / pattern)
            else:
                glob_pattern = str(base / pattern)
            
            matches: List[str] = []
            total_size = 0
            
            # 执行 glob 搜索
            for match in Path(base).glob(f"**/{pattern}" if recursive else pattern):
                if len(matches) >= max_results:
                    break
                
                # 获取相对路径（相对于 workspace）
                try:
                    rel_path = str(match.relative_to(workspace))
                    file_info = {
                        "path": rel_path,
                        "is_dir": match.is_dir(),
                        "size": match.stat().st_size if match.is_file() else 0
                    }
                    matches.append(file_info)
                    total_size += file_info["size"]
                except ValueError:
                    # 路径不在 workspace 内，跳过
                    continue
            
            return {
                "success": True,
                "data": {
                    "matches": matches,
                    "count": len(matches),
                    "total_size": total_size,
                    "pattern": pattern,
                    "base_dir": str(base.relative_to(workspace))
                },
                "error": ""
            }
            
        except PermissionError as e:
            logger.warning(f"Path traversal blocked: {e}")
            return {"success": False, "data": None, "error": str(e)}
        except Exception as e:
            logger.warning(f"Glob search failed: {e}")
            return {"success": False, "data": None, "error": str(e)}
