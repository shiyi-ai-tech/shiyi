"""
BashTool - 执行Shell命令（增强版）

BashTool - 执行Shell命令（增强版）
- 支持超时控制
- 输出截断保护
- 工作目录设置
- 标准输出/错误分离
- 危险命令检测
"""

import subprocess
import logging
from pathlib import Path
from typing import Dict, Any, Optional

try:
    from ._utils import safe_path
except ImportError:
    from ._utils import safe_path

logger = logging.getLogger(__name__)

# 危险命令黑名单
DANGEROUS_COMMANDS = [
    "rm -rf /", "rm -rf /*", "dd if=", "mkfs", "shutdown",
    "reboot", "init 0", "init 6", "> /dev/sda", "cat /dev/sda",
    # 数据保护：禁止破坏用户数据目录
    "rm .shiyi/", "> .shiyi/.env", "mv .shiyi/",
]

# 默认超时（秒）
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300

# 最大输出大小（字节）
MAX_OUTPUT_SIZE = 100 * 1024  # 100KB


class BashTool:
    """增强版 Bash 命令执行工具"""
    
    name = "bash"
    description = "在工作沙箱内执行安全外壳命令。支持 Python 脚本、系统命令等。自动超时保护，输出上限 100KB。禁止执行的命令：rm -rf /, dd, mkfs, shutdown 等"
    
    schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令"
            },
            "workdir": {
                "type": "string",
                "description": "命令工作目录（沙箱内的相对路径，可选）"
            },
            "timeout": {
                "type": "integer",
                "description": f"超时秒数（默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}）",
                "default": DEFAULT_TIMEOUT
            },
            "env": {
                "type": "object",
                "description": "额外的环境变量（可选）"
            }
        },
        "required": ["command"]
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        执行 Bash 命令
        
        Args:
            params: {
                "command": str,       # 要执行的命令
                "workdir": str,       # 工作目录（可选）
                "timeout": int,       # 超时秒数（默认60）
                "env": dict           # 额外环境变量（可选）
            }
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "data": {"stdout": str, "stderr": str, "exit_code": int}, "error": str}
        """
        command = params.get("command", "")
        workdir = params.get("workdir")
        timeout = min(int(params.get("timeout", DEFAULT_TIMEOUT)), MAX_TIMEOUT)
        extra_env = params.get("env", {})
        
        if not command.strip():
            return {"success": False, "data": None, "error": "Empty command"}
        
        # 危险命令检测
        cmd_lower = command.lower()
        for dangerous in DANGEROUS_COMMANDS:
            if dangerous.lower() in cmd_lower:
                logger.warning(f"Dangerous command blocked: {command[:100]}")
                return {
                    "success": False,
                    "data": None,
                    "error": f"危险命令被拒绝: {dangerous}"
                }
        
        try:
            # 设置工作目录
            cwd = workspace
            if workdir:
                cwd = safe_path(workdir, workspace)
            
            # 设置环境变量
            env = {}
            env.update(subprocess.os.environ)
            env.update(extra_env)
            
            # 执行命令
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                timeout=timeout,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
            
            # 截断输出
            stdout = result.stdout
            stderr = result.stderr
            
            if len(stdout.encode('utf-8')) > MAX_OUTPUT_SIZE:
                stdout = stdout[:MAX_OUTPUT_SIZE] + f"\n\n[输出过长，已截断至 {MAX_OUTPUT_SIZE} 字节]"
            
            if len(stderr.encode('utf-8')) > MAX_OUTPUT_SIZE:
                stderr = stderr[:MAX_OUTPUT_SIZE] + f"\n\n[错误输出过长，已截断]"
            
            return {
                "success": result.returncode == 0,
                "data": {
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": result.returncode
                },
                "error": "" if result.returncode == 0 else f"Exit code: {result.returncode}"
            }
            
        except subprocess.TimeoutExpired:
            logger.warning(f"Command timeout: {command[:50]}...")
            return {
                "success": False,
                "data": None,
                "error": f"命令执行超时 ({timeout}s)"
            }
        except Exception as e:
            logger.warning(f"Bash execution failed: {e}")
            return {
                "success": False,
                "data": None,
                "error": str(e)
            }
