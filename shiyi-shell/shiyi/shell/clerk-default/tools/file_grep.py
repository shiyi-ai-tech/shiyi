"""
FileGrepTool - 按内容搜索文件

增强版工具，功能：
- 正则表达式搜索
- 支持文件类型过滤
- 返回匹配行及上下文
"""

import re
import logging
from pathlib import Path
from typing import Dict, Any, List, Tuple

from ._utils import safe_path

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 50
DEFAULT_CONTEXT_LINES = 2


class FileGrepTool:
    """文件内容搜索工具"""
    
    name = "file_grep"
    description = "在文件内容中搜索匹配正则表达式的行，支持上下文显示"
    
    schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "正则表达式搜索模式"
            },
            "path": {
                "type": "string",
                "description": "搜索目录或文件路径（沙箱内，默认为 .）",
                "default": "."
            },
            "file_pattern": {
                "type": "string",
                "description": "文件类型过滤，如 *.py, *.md（可选）",
                "default": "*"
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "是否区分大小写（默认 False）",
                "default": False
            },
            "context_lines": {
                "type": "integer",
                "description": f"上下文行数（默认 {DEFAULT_CONTEXT_LINES}）",
                "default": DEFAULT_CONTEXT_LINES
            },
            "max_results": {
                "type": "integer",
                "description": f"最大返回匹配数（默认 {DEFAULT_MAX_RESULTS}）",
                "default": DEFAULT_MAX_RESULTS
            }
        },
        "required": ["pattern"]
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        执行内容搜索
        
        Args:
            params: {
                "pattern": str,        # 正则表达式
                "path": str,           # 搜索路径
                "file_pattern": str,   # 文件类型过滤
                "case_sensitive": bool,
                "context_lines": int,  # 上下文行数
                "max_results": int
            }
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "data": {"matches": [...], "total_matches": int}, "error": str}
        """
        pattern = params.get("pattern", "")
        search_path = params.get("path", ".")
        file_pattern = params.get("file_pattern", "*")
        case_sensitive = params.get("case_sensitive", False)
        context_lines = int(params.get("context_lines", DEFAULT_CONTEXT_LINES))
        max_results = int(params.get("max_results", DEFAULT_MAX_RESULTS))
        
        if not pattern.strip():
            return {"success": False, "data": None, "error": "Empty pattern"}
        
        try:
            base = safe_path(search_path, workspace)
            
            if not base.exists():
                return {"success": False, "data": None, "error": f"路径不存在: {base}"}
            
            # 编译正则
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                regex = re.compile(pattern, flags)
            except re.error as e:
                return {"success": False, "data": None, "error": f"正则表达式错误: {e}"}
            
            all_matches: List[Dict[str, Any]] = []
            total_matches = 0
            files_searched = 0
            
            def search_file(filepath: Path) -> List[Dict[str, Any]]:
                """搜索单个文件"""
                matches = []
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    
                    for i, line in enumerate(lines):
                        if regex.search(line):
                            # 获取上下文
                            start = max(0, i - context_lines)
                            end = min(len(lines), i + context_lines + 1)
                            
                            context = []
                            for j in range(start, end):
                                marker = ">>>" if j == i else "   "
                                context.append({
                                    "line_number": j + 1,
                                    "content": lines[j].rstrip("\n"),
                                    "marker": marker
                                })
                            
                            matches.append({
                                "line": i + 1,
                                "content": line.rstrip("\n"),
                                "context": context
                            })
                except (UnicodeDecodeError, IsADirectoryError):
                    pass
                return matches
            
            # 遍历文件
            if base.is_file():
                files_to_search = [base]
            else:
                files_to_search = [
                    f for f in base.glob(f"**/{file_pattern}")
                    if f.is_file()
                ]
            
            for filepath in files_to_search:
                if total_matches >= max_results:
                    break
                
                files_searched += 1
                file_matches = search_file(filepath)
                
                if file_matches:
                    # 获取相对路径
                    try:
                        rel_path = str(filepath.relative_to(workspace))
                    except ValueError:
                        rel_path = str(filepath)
                    
                    all_matches.append({
                        "file": rel_path,
                        "matches": file_matches,
                        "count": len(file_matches)
                    })
                    total_matches += len(file_matches)
            
            return {
                "success": True,
                "data": {
                    "matches": all_matches,
                    "total_matches": total_matches,
                    "files_searched": files_searched,
                    "pattern": pattern,
                    "case_sensitive": case_sensitive
                },
                "error": ""
            }
            
        except PermissionError as e:
            logger.warning(f"Path traversal blocked: {e}")
            return {"success": False, "data": None, "error": str(e)}
        except Exception as e:
            logger.warning(f"Grep search failed: {e}")
            return {"success": False, "data": None, "error": str(e)}
