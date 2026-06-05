"""
FileEditTool - 精确文件编辑工具

增强版工具，功能：
- replace_one: 替换第一个匹配项
- replace_all: 替换所有匹配项
- append: 在文件末尾追加
- append_newline: 追加前确保以换行结尾
"""

import logging
from pathlib import Path
from typing import Dict, Any

from ._utils import safe_path

logger = logging.getLogger(__name__)


class FileEditTool:
    """精确文件编辑工具"""
    
    name = "file_edit"
    description = "精确编辑文件内容，支持替换单个/所有匹配项、追加内容"
    
    schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（沙箱内）"
            },
            "old_string": {
                "type": "string",
                "description": "要被替换的字符串（用于 replace_one/replace_all 模式）"
            },
            "new_string": {
                "type": "string",
                "description": "替换后的新字符串"
            },
            "mode": {
                "type": "string",
                "description": "编辑模式: replace_one(默认), replace_all, append, append_newline",
                "enum": ["replace_one", "replace_all", "append", "append_newline"],
                "default": "replace_one"
            }
        },
        "required": ["path", "mode"],
        "dependencies": {
            "old_string": ["new_string"]
        }
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        执行文件编辑
        
        Args:
            params: {
                "path": str,        # 文件路径
                "old_string": str,  # 被替换的字符串（replace_one/replace_all 模式）
                "new_string": str,  # 替换后的字符串
                "mode": str         # replace_one/replace_all/append/append_newline
            }
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "data": {"changes": int, "path": str}, "error": str}
        """
        filepath = params.get("path", "")
        old_string = params.get("old_string", "")
        new_string = params.get("new_string", "")
        mode = params.get("mode", "replace_one")
        
        if not filepath.strip():
            return {"success": False, "data": None, "error": "Empty path"}
        
        if mode in ("replace_one", "replace_all") and not old_string:
            return {"success": False, "data": None, "error": "replace_one/replace_all 模式需要 old_string"}
        
        try:
            safe = safe_path(filepath, workspace)
            
            if not safe.exists():
                return {"success": False, "data": None, "error": f"文件不存在: {safe}"}
            
            if safe.is_dir():
                return {"success": False, "data": None, "error": "Cannot edit directory"}
            
            # 读取文件内容
            with open(safe, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            
            original_content = content
            changes = 0
            
            if mode == "replace_one":
                if old_string in content:
                    content = content.replace(old_string, new_string, 1)
                    changes = 1
                else:
                    return {
                        "success": False,
                        "data": None,
                        "error": f"未找到匹配的字符串: {old_string[:50]}..."
                    }
            
            elif mode == "replace_all":
                changes = content.count(old_string)
                if changes == 0:
                    return {
                        "success": False,
                        "data": None,
                        "error": f"未找到匹配的字符串: {old_string[:50]}..."
                    }
                content = content.replace(old_string, new_string)
            
            elif mode == "append":
                content = content + new_string
            
            elif mode == "append_newline":
                if content and not content.endswith("\n"):
                    content = content + "\n" + new_string
                else:
                    content = content + new_string
            
            # 检查是否有变化
            if content == original_content and mode in ("replace_one", "replace_all"):
                return {
                    "success": False,
                    "data": None,
                    "error": "文件内容未变化（字符串已相同）"
                }
            
            # 写入文件
            with open(safe, "w", encoding="utf-8") as f:
                f.write(content)
            
            return {
                "success": True,
                "data": {
                    "path": str(safe),
                    "changes": changes,
                    "mode": mode
                },
                "error": ""
            }
            
        except PermissionError as e:
            logger.warning(f"Path traversal blocked: {e}")
            return {"success": False, "data": None, "error": str(e)}
        except Exception as e:
            logger.warning(f"File edit failed: {e}")
            return {"success": False, "data": None, "error": str(e)}
