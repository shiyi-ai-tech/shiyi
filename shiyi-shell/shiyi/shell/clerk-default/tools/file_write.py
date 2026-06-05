"""
EnhancedFileWriteTool - 增强版文件写入

增强版工具，特性：
- 支持追加模式 (append)
- 自动创建目录
- 原子写入保护
- 返回写入详情
"""

import logging
import tempfile
import shutil
from pathlib import Path
from typing import Dict, Any

from ._utils import safe_path, ensure_workspace

logger = logging.getLogger(__name__)


class EnhancedFileWriteTool:
    """增强版文件写入工具"""
    
    name = "enhanced_file_write"
    description = "增强版文件写入，支持覆盖/追加模式，自动创建目录"
    
    schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（沙箱内）"
            },
            "content": {
                "type": "string",
                "description": "要写入的内容"
            },
            "mode": {
                "type": "string",
                "description": "写入模式: overwrite(默认) 或 append",
                "enum": ["overwrite", "append"],
                "default": "overwrite"
            },
            "create_dirs": {
                "type": "boolean",
                "description": "是否自动创建父目录（默认 True）",
                "default": True
            }
        },
        "required": ["path", "content"]
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        执行文件写入
        
        Args:
            params: {
                "path": str,           # 文件路径
                "content": str,        # 要写入的内容
                "mode": str,           # 写入模式: overwrite/append
                "create_dirs": bool    # 自动创建目录
            }
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "data": {"bytes_written": int, "path": str}, "error": str}
        """
        filepath = params.get("path", "")
        content = params.get("content", "")
        mode = params.get("mode", "overwrite")
        create_dirs = params.get("create_dirs", True)
        
        if not filepath.strip():
            return {"success": False, "data": None, "error": "Empty path"}
        
        try:
            safe = safe_path(filepath, workspace)
            
            # 不允许覆盖目录
            if safe.is_dir():
                return {"success": False, "data": None, "error": "Cannot overwrite directory"}
            
            # 确保父目录存在
            if create_dirs:
                safe.parent.mkdir(parents=True, exist_ok=True)
            elif not safe.parent.exists():
                return {"success": False, "data": None, "error": f"父目录不存在: {safe.parent}"}
            
            # 原子写入：先写临时文件再重命名
            with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                dir=safe.parent,
                delete=False
            ) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)
            
            try:
                # 根据模式决定如何写入
                if mode == "append" and safe.exists():
                    # 追加模式：先读取现有内容
                    with open(safe, "r", encoding="utf-8") as f:
                        existing = f.read()
                    # 合并后写入临时文件
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        f.write(existing + content)
                
                # 重命名到目标位置（原子操作）
                shutil.move(str(tmp_path), str(safe))
                
            except Exception:
                # 清理临时文件
                if tmp_path.exists():
                    tmp_path.unlink()
                raise
            
            # 获取写入结果
            size = safe.stat().st_size
            bytes_written = len(content.encode('utf-8'))
            
            # 记录最后写入的文件路径，供网关自动发送
            _LAST_WRITTEN = Path.home() / ".shiyi" / ".last_written_file"
            try:
                _LAST_WRITTEN.parent.mkdir(parents=True, exist_ok=True)
                _LAST_WRITTEN.write_text(str(safe))
            except Exception:
                pass
            
            return {
                "success": True,
                "data": {
                    "path": str(safe),
                    "bytes_written": bytes_written,
                    "total_size": size,
                    "mode": mode
                },
                "error": ""
            }
            
        except PermissionError as e:
            logger.warning(f"Path traversal blocked: {e}")
            return {"success": False, "data": None, "error": str(e)}
        except Exception as e:
            logger.warning(f"File write failed: {e}")
            return {"success": False, "data": None, "error": str(e)}
