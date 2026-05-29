"""
默认吏员工作器 (ClerkWorker)

支持两种模式:
1. 本地模式 (LOCAL): 作为 Python 模块导入，注册 handler 函数
2. 远程模式 (REMOTE): MCP server 骨架 (v0.13.0 填充)

安全约束:
- 文件操作限制在 ~/.shiyi/workspace/ 目录内
- 路径必须归一化，防止路径穿越攻击
"""

import os
import re
import json
import logging
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from shiyi.common.constants import (
    DEFAULT_LIGHT_LLM_MODEL,
    DEFAULT_LLM_BASE_URL,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 配置和常量
# ============================================================================

# 默认工作沙箱目录
DEFAULT_WORKSPACE = Path.home() / ".shiyi" / "workspace"

# 最大读取行数
MAX_READ_LINES = 500

# 模式枚举
class ClerkMode:
    LOCAL = "local"      # 本地模式：进程内 class 验证抽象
    REMOTE = "remote"    # 远程模式：MCP server 骨架


# ============================================================================
# 数据类
# ============================================================================

@dataclass
class ClerkConfig:
    """吏员配置"""
    clerk_id: str
    name: str
    version: str
    description: str
    capabilities: List[str]
    tools: List[Dict[str, Any]]
    enabled: bool
    created_at: str
    config_path: str
    
    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "ClerkConfig":
        return cls(
            clerk_id=data.get("clerk_id", ""),
            name=data.get("name", ""),
            version=data.get("version", ""),
            description=data.get("description", ""),
            capabilities=data.get("capabilities", []),
            tools=data.get("tools", []),
            enabled=data.get("enabled", True),
            created_at=data.get("created_at", ""),
            config_path=data.get("config_path", ""),
        )


@dataclass
class ClerkStatus:
    """吏员状态"""
    clerk_id: str
    mode: str
    workspace: str
    tools_count: int
    enabled: bool


# ============================================================================
# 沙箱安全工具
# ============================================================================

def _safe_path(filepath: str, workspace: Path) -> Path:
    """
    将路径限制在安全工作目录内
    
    Args:
        filepath: 用户提供的文件路径
        workspace: 沙箱工作目录
        
    Returns:
        归一化后的安全路径
        
    Raises:
        PermissionError: 路径穿越尝试
    """
    safe_root = (Path(workspace) if isinstance(workspace, str) else workspace).resolve()
    
    # 处理相对路径
    p = Path(filepath)
    if not p.is_absolute():
        p = safe_root / p
    
    # 归一化路径
    resolved = p.resolve()
    
    # 防止路径穿越
    if not str(resolved).startswith(str(safe_root)):
        raise PermissionError(f"路径穿越被拒绝: {filepath} -> {resolved}")
    
    return resolved


def _ensure_workspace(workspace: Path) -> None:
    """确保工作目录存在"""
    workspace.mkdir(parents=True, exist_ok=True)


# ============================================================================
# 工具实现
# ============================================================================

class FileReadTool:
    """文件读取工具"""
    
    name = "file_read"
    description = "读取文件内容或列出目录。安全限制在沙箱目录内。"
    
    schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（沙箱内）",
            },
            "limit": {
                "type": "integer",
                "description": f"最大读取行数（默认 {MAX_READ_LINES}）",
                "default": MAX_READ_LINES,
            },
        },
        "required": ["path"],
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        执行文件读取
        
        Args:
            params: {"path": str, "limit": int}
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "result": str, "error": str}
        """
        filepath = params.get("path", "")
        limit = int(params.get("limit", MAX_READ_LINES))
        
        if not filepath.strip():
            return {"success": False, "result": "", "error": "Empty path"}
        
        try:
            safe = _safe_path(filepath, workspace)
            
            if not safe.exists():
                return {"success": False, "result": "", "error": f"文件不存在: {safe}"}
            
            if safe.is_dir():
                # 列出目录内容
                entries = sorted(safe.iterdir())[:limit * 2]
                lines = [f"{'📁' if e.is_dir() else '📄'} {e.name}" for e in entries]
                return {"success": True, "result": "\n".join(lines[:limit]), "error": ""}
            
            # 读取文本文件
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


class FileWriteTool:
    """文件写入工具"""
    
    name = "file_write"
    description = "写入内容到文件。安全限制在沙箱目录内。"
    
    schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（沙箱内）",
            },
            "content": {
                "type": "string",
                "description": "要写入的内容",
            },
        },
        "required": ["path", "content"],
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        执行文件写入
        
        Args:
            params: {"path": str, "content": str}
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "result": str, "error": str}
        """
        filepath = params.get("path", "")
        content = params.get("content", "")
        
        if not filepath.strip():
            return {"success": False, "result": "", "error": "Empty path"}
        
        try:
            _ensure_workspace(workspace)
            safe = _safe_path(filepath, workspace)
            
            # 不允许覆盖目录
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


class WebSearchTool:
    """网页搜索工具"""
    
    name = "web_search"
    description = "搜索互联网获取最新信息。用于查找新闻、事实、数据等需要联网的信息。"
    
    schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词",
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量（默认3）",
                "default": 3,
            },
        },
        "required": ["query"],
    }
    
    @staticmethod
    def execute(params: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行网页搜索
        
        优先使用 Bing API，无 key 时回退到 DuckDuckGo HTML 搜索。
        
        Args:
            params: {"query": str, "max_results": int}
            
        Returns:
            {"success": bool, "result": str, "error": str}
        """
        query = params.get("query", "")
        limit = int(params.get("max_results", 3))
        
        if not query.strip():
            return {"success": False, "result": "", "error": "Empty query"}
        
        # 方案1: Bing Web Search API
        bing_key = os.environ.get("BING_API_KEY")
        if bing_key:
            return WebSearchTool._search_bing(query, limit, bing_key)
        
        # 方案2: DuckDuckGo HTML 抓取
        return WebSearchTool._search_ddg(query, limit)
    
    @staticmethod
    def _search_bing(query: str, limit: int, api_key: str) -> Dict[str, Any]:
        """使用 Bing Web Search API"""
        url = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": api_key}
        
        params = urllib.parse.urlencode({"q": query, "count": limit, "mkt": "zh-CN"})
        req = urllib.request.Request(f"{url}?{params}", headers=headers)
        
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                results = []
                for item in data.get("webPages", {}).get("value", [])[:limit]:
                    results.append({
                        "title": item.get("name", ""),
                        "url": item.get("url", ""),
                        "snippet": item.get("snippet", ""),
                    })
                
                if not results:
                    return {"success": True, "result": "未找到相关结果", "error": ""}
                
                text = "\n\n".join(
                    f"[{r['title']}]({r['url']})\n{r['snippet']}"
                    for r in results
                )
                return {"success": True, "result": text, "error": ""}
                
        except urllib.error.HTTPError as e:
            logger.warning(f"Bing API error: {e.code}")
            return {"success": False, "result": "", "error": f"Bing API returned {e.code}"}
        except Exception as e:
            logger.warning(f"Bing search failed: {e}")
            return {"success": False, "result": "", "error": str(e)}
    
    @staticmethod
    def _search_ddg(query: str, limit: int) -> Dict[str, Any]:
        """使用 DuckDuckGo HTML 搜索（无需 API key）"""
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            
            # 解析 HTML 提取标题和摘要
            results = []
            
            # 匹配 DDG HTML 结果
            links = re.findall(
                r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            )
            snippets = re.findall(
                r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            )
            
            for i, (href, title) in enumerate(links[:limit]):
                title_clean = re.sub(r"<[^>]+>", "", title).strip()
                snippet = ""
                if i < len(snippets):
                    snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
                
                results.append({
                    "title": title_clean or "无标题",
                    "url": href,
                    "snippet": snippet or "无摘要",
                })
            
            if not results:
                return {"success": True, "result": "未找到相关结果", "error": ""}
            
            text = "\n\n".join(
                f"[{r['title']}]({r['url']})\n{r['snippet']}"
                for r in results
            )
            return {"success": True, "result": text, "error": ""}
            
        except Exception as e:
            logger.warning(f"DDG search failed: {e}")
            return {"success": False, "result": "", "error": f"搜索失败: {e}"}


# ============================================================================
# RunCommand 工具
# ============================================================================

# 命令黑名单模式 (危险命令拒绝执行)
_COMMAND_BLACKLIST = [
    r"rm\s+-rf\s+/",
    r"mkfs",
    r":\(\)\s*\{\s*:\|:",
    r"dd\s+if=",
    r">\s*/dev/sd",
    r"shutdown",
    r"reboot",
    r"chmod\s+777\s+/",
    r"curl.*\|\s*(ba)?sh",
    r"wget.*\|\s*(ba)?sh",
    r"eval\s",
    r"sudo\s",
    r"\.\.[/\\]",           # 路径穿越
    r"/etc/(passwd|shadow|sudoers|ssh)",
    r"/root/",
    r"/var/log/",
    r"\$HOME/\.ssh",
    r"~/\.ssh",
    r"chattr\s",
    r"mount\s",
    r"umount\s",
    r"fdisk\s",
    r"parted\s",
]

_MAX_OUTPUT_BYTES = 100 * 1024  # 100KB
_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 300


class RunCommandTool:
    """安全命令执行工具"""

    name = "run_command"
    description = (
        "在工作沙箱内执行安全 shell 命令。支持 Python 脚本、系统命令等。"
        "自动超时 60s，输出上限 100KB。"
        "禁止执行的命令：rm -rf /, dd, mkfs, shutdown 等。"
    )

    schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令",
            },
            "workdir": {
                "type": "string",
                "description": "命令工作目录（沙箱内的相对路径，可选）",
            },
            "timeout": {
                "type": "integer",
                "description": f"超时秒数（默认 {_DEFAULT_TIMEOUT}，最大 {_MAX_TIMEOUT}）",
                "default": _DEFAULT_TIMEOUT,
            },
        },
        "required": ["command"],
    }

    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        在沙箱内执行命令

        Args:
            params: {"command": str, "workdir": str, "timeout": int}
            workspace: 沙箱工作目录

        Returns:
            {"success": bool, "result": str, "error": str}
        """
        command = params.get("command", "")
        workdir = params.get("workdir", ".")
        timeout = min(int(params.get("timeout", _DEFAULT_TIMEOUT)), _MAX_TIMEOUT)

        if not command.strip():
            return {"success": False, "result": "", "error": "Empty command"}

        # 检查命令黑名单
        for pattern in _COMMAND_BLACKLIST:
            if re.search(pattern, command):
                return {
                    "success": False,
                    "result": "",
                    "error": f"危险命令被拒绝: 匹配规则 '{pattern}'",
                }

        # 确定工作目录
        try:
            safe_wd = _safe_path(workdir, workspace)
            if not safe_wd.exists():
                safe_wd.mkdir(parents=True, exist_ok=True)
            if not safe_wd.is_dir():
                return {"success": False, "result": "", "error": f"工作目录不存在或不是目录: {workdir}"}
        except PermissionError as e:
            return {"success": False, "result": "", "error": str(e)}

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(safe_wd),
                capture_output=True,
                text=True,
                timeout=timeout,
                env={
                    **os.environ,
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": str(Path(workspace).parent),
                },
            )

            # 合并 stdout 和 stderr
            output = proc.stdout
            if proc.stderr.strip():
                output += f"\n[stderr]\n{proc.stderr}"

            # 截断过长输出
            if len(output) > _MAX_OUTPUT_BYTES:
                output = output[:_MAX_OUTPUT_BYTES]
                output += f"\n\n[输出过长，已截断至 {_MAX_OUTPUT_BYTES} 字节]"

            if not output.strip():
                output = f"(exit_code={proc.returncode}, 无输出)"

            return {
                "success": proc.returncode == 0,
                "result": output,
                "error": f"exit_code={proc.returncode}" if proc.returncode != 0 else "",
            }

        except subprocess.TimeoutExpired:
            return {"success": False, "result": "", "error": f"命令超时 ({timeout}s)"}
        except PermissionError as e:
            return {"success": False, "result": "", "error": str(e)}
        except Exception as e:
            logger.warning(f"RunCommand failed: {e}")
            return {"success": False, "result": "", "error": str(e)}


# ============================================================================
# 记忆查询工具
# ============================================================================

class RecallMemoryTool:
    """记忆查询工具 — 查询史佚记忆库
    
    注意：此工具需要在主进程中执行，MCP 模式下会返回特殊标记，
    由 ClerkRegistry 拦截并调用 MemoryEngine.recall()
    """
    
    name = "recall_memory"
    description = "查询史佚记忆库，检索与查询相关的事实记忆。支持语义搜索和深度检索模式。"
    
    schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "记忆查询文本",
            },
            "top_k": {
                "type": "integer",
                "description": "返回结果数量（默认5）",
                "default": 5,
            },
            "deep": {
                "type": "boolean",
                "description": "是否启用深度检索模式（默认False）",
                "default": False,
            },
        },
        "required": ["query"],
    }
    
    @staticmethod
    def execute(params: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行记忆查询
        
        注意：在 MCP 子进程中无法访问 MemoryEngine，
        此方法返回特殊标记由主进程处理。
        
        Args:
            params: {"query": str, "top_k": int, "deep": bool}
            
        Returns:
            {"_proxy": True, "tool": "recall_memory", "params": {...}}
        """
        return {
            "_proxy": True,
            "tool": "recall_memory",
            "params": params,
        }


class SearchConversationsTool:
    """对话记录搜索工具 — 搜索史佚聊天记录
    
    注意：此工具需要在主进程中执行，MCP 模式下会返回特殊标记，
    由 ClerkRegistry 拦截并调用对话历史管理器。
    """
    
    name = "search_conversations"
    description = "搜索史佚的对话历史记录，找到包含关键词的对话片段。"
    
    schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词",
            },
            "limit": {
                "type": "integer",
                "description": "返回结果数量（默认10）",
                "default": 10,
            },
            "session_id": {
                "type": "string",
                "description": "指定会话ID（可选，为空则搜索所有会话）",
            },
        },
        "required": ["query"],
    }
    
    @staticmethod
    def execute(params: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行对话搜索
        
        注意：在 MCP 子进程中无法访问对话历史，
        此方法返回特殊标记由主进程处理。
        
        Args:
            params: {"query": str, "limit": int, "session_id": str}
            
        Returns:
            {"_proxy": True, "tool": "search_conversations", "params": {...}}
        """
        return {
            "_proxy": True,
            "tool": "search_conversations",
            "params": params,
        }


# ============================================================================
# 工具注册表
# ============================================================================

TOOL_REGISTRY: Dict[str, Any] = {
    "file_read": FileReadTool,
    "file_write": FileWriteTool,
    "web_search": WebSearchTool,
    "run_command": RunCommandTool,
    "recall_memory": RecallMemoryTool,
    "search_conversations": SearchConversationsTool,
}


# ============================================================================
# 吏员工作器
# ============================================================================

class ClerkWorker:
    """
    默认吏员工作器
    
    Attributes:
        config: 吏员配置
        workspace: 沙箱工作目录
        mode: 运行模式 (local/remote)
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化吏员工作器
        
        Args:
            config_path: clerk.json 文件的路径，或包含 clerk.json 的目录路径，
                        默认从当前目录加载
        """
        if config_path:
            p = Path(config_path)
            if p.is_dir():
                config_file = p / "clerk.json"
            elif p.name == "clerk.json" or p.suffix == ".json":
                config_file = p
            else:
                config_file = p / "clerk.json"
        else:
            config_file = Path(__file__).parent / "clerk.json"
        
        # 加载配置
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            self.config = ClerkConfig.from_json(config_data)
            # 保存原始配置数据供 LLM/Agent 使用
            self._raw_config = config_data
        else:
            # 使用默认配置
            self.config = ClerkConfig(
                clerk_id="clerk_default_001",
                name="默认吏员",
                version="0.1.0",
                description="提供文件读写和网页搜索的基础吏员",
                capabilities=["file_read", "file_write", "web_search"],
                tools=[],
                enabled=True,
                created_at="2026-05-19",
                config_path=str(config_file.parent),
            )
            self._raw_config = {}
        
        # 确定工作目录
        workspace_env = os.environ.get("CLERK_WORKSPACE")
        if workspace_env:
            self.workspace = Path(workspace_env).expanduser()
        else:
            self.workspace = DEFAULT_WORKSPACE
        
        # 确保工作目录存在
        _ensure_workspace(self.workspace)
        
        # 设置模式（默认本地模式）
        self.mode = ClerkMode.LOCAL
        
        # 注册本地 handler
        self._handlers: Dict[str, callable] = {}
        self._register_local_handlers()
        
        logger.info(f"ClerkWorker initialized: {self.config.clerk_id}, workspace: {self.workspace}")
    
    def _register_local_handlers(self) -> None:
        """注册本地模式的 handler"""
        self._handlers = {
            "file_read": lambda params: FileReadTool.execute(params, self.workspace),
            "file_write": lambda params: FileWriteTool.execute(params, self.workspace),
            "web_search": lambda params: WebSearchTool.execute(params),
            "run_command": lambda params: RunCommandTool.execute(params, self.workspace),
            "recall_memory": lambda params: RecallMemoryTool.execute(params),
            "search_conversations": lambda params: SearchConversationsTool.execute(params),
        }
    
    def get_tools(self) -> List[Dict[str, Any]]:
        """
        返回工具 schema 列表
        
        Returns:
            工具定义列表，每项包含 name, description, inputSchema
        """
        tools = []
        for tool_name, tool_class in TOOL_REGISTRY.items():
            if tool_name in self.config.capabilities:
                tools.append({
                    "name": tool_class.name,
                    "description": tool_class.description,
                    "inputSchema": tool_class.schema,
                })
        return tools
    
    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行工具调用
        
        Args:
            tool_name: 工具名称
            params: 工具参数
            
        Returns:
            执行结果 {"success": bool, "result": str, "error": str}
        """
        if not self.config.enabled:
            return {"success": False, "result": "", "error": "Clerk is disabled"}
        
        if tool_name not in self._handlers:
            available = list(self._handlers.keys())
            return {
                "success": False,
                "result": "",
                "error": f"Unknown tool: {tool_name}. Available: {available}",
            }
        
        try:
            handler = self._handlers[tool_name]
            result = handler(params)
            return result
        except Exception as e:
            logger.error(f"Tool execution failed: {tool_name}, error: {e}")
            return {"success": False, "result": "", "error": str(e)}
    
    def status(self) -> Dict[str, Any]:
        """
        返回吏员状态
        
        Returns:
            状态信息字典
        """
        return {
            "clerk_id": self.config.clerk_id,
            "name": self.config.name,
            "version": self.config.version,
            "mode": self.mode,
            "workspace": str(self.workspace),
            "tools_count": len(self.get_tools()),
            "enabled": self.config.enabled,
            "capabilities": self.config.capabilities,
        }

    # ========================================================================
    # LLM 调用能力 (v0.17.5)
    # ========================================================================

    def _get_llm_config(self) -> Optional[Dict[str, Any]]:
        """从 clerk.json 和环境中提取 LLM 配置"""
        llm_cfg = self._raw_config.get("llm_config", {})
        if not llm_cfg:
            return None

        # 读取 API key：优先环境变量，否则从 clerk.json api_keys 找
        api_key = None
        api_keys = self._raw_config.get("api_keys", [])
        for ak in api_keys:
            env_val = os.environ.get(ak.get("name", ""))
            if env_val:
                api_key = env_val
                break

        if not api_key:
            # Fallback: 尝试 ShiYi 通用的 key
            api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("MAIN_API_KEY")

        if not api_key:
            return None

        return {
            "api_key": api_key,
            "base_url": llm_cfg.get("base_url", DEFAULT_LLM_BASE_URL),
            "model": llm_cfg.get("model", DEFAULT_LIGHT_LLM_MODEL),
            "max_tokens": llm_cfg.get("max_tokens", 2048),
            "temperature": llm_cfg.get("temperature", 0.3),
        }

    def _call_llm(self, messages: List[Dict[str, str]]) -> Optional[str]:
        """
        调用 LLM 进行推理

        Args:
            messages: 消息列表 [{"role": "system/user/assistant", "content": "..."}]

        Returns:
            LLM 响应文本，失败返回 None
        """
        cfg = self._get_llm_config()
        if not cfg:
            logger.warning("LLM not configured, cannot call")
            return None

        url = f"{cfg['base_url']}/chat/completions"
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "max_tokens": cfg["max_tokens"],
            "temperature": cfg["temperature"],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode())

            choice = body.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            return content

        except urllib.error.HTTPError as e:
            logger.warning(f"LLM HTTP error {e.code}: {e.read().decode()[:200]}")
            return None
        except Exception as e:
            logger.warning(f"LLM call failed: {e}")
            return None

    # ========================================================================
    # Skill 加载 (v0.17.5)
    # ========================================================================

    def _load_skill(self, skill_name: str) -> Optional[str]:
        """
        加载 Skill 文件内容

        从 skills/ 目录加载指定 skill 的 markdown 内容。

        Args:
            skill_name: Skill 名称 (不含 .md 后缀)

        Returns:
            Skill 内容文本，不存在返回 None
        """
        skill_dir = Path(__file__).parent / "skills"
        skill_file = skill_dir / f"{skill_name}.md"

        if not skill_file.exists():
            logger.warning(f"Skill not found: {skill_name}")
            return None

        try:
            with open(skill_file, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to load skill {skill_name}: {e}")
            return None

    def _load_skills_context(self, skill_names: List[str]) -> str:
        """
        批量加载 Skills 并返回组合上下文

        Args:
            skill_names: Skill 名称列表

        Returns:
            组合后的 skills 上下文字符串
        """
        contexts = []
        for name in skill_names:
            content = self._load_skill(name)
            if content:
                contexts.append(f"## Skill: {name}\n\n{content}")

        if not contexts:
            return ""

        return "\n\n---\n\n".join(contexts)

    # ========================================================================
    # 自主执行循环 (Micro Agent Loop, v0.17.5)
    # ========================================================================

    def run_agent_loop(
        self,
        task: str,
        skills: Optional[List[str]] = None,
        max_iterations: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        吏员自主执行循环：接收任务 → 理解 → 执行 → 汇报

        吏员用自己的 LLM 进行推理，循环调用工具完成任务。

        Args:
            task: 任务描述
            skills: 要加载的 skill 名称列表（可选，默认加载 clerk.json 中的 skills）
            max_iterations: 最大循环次数（默认从 clerk.json agent_loop.max_iterations 读取）
            timeout: 总超时秒数（默认从 clerk.json agent_loop.default_timeout 读取）

        Returns:
            {
                "success": bool,
                "result": str,       # 最终输出
                "iterations": int,   # 消耗的循环次数
                "error": str,        # 错误信息
                "trace": [...]       # 执行追踪
            }
        """
        # 检查 LLM 可用性
        cfg = self._get_llm_config()
        if not cfg:
            return {
                "success": False,
                "result": "",
                "iterations": 0,
                "error": "LLM 未配置。请设置 CLERK_DEFAULT_API_KEY 环境变量或在 clerk.json 中配置。",
                "trace": [],
            }

        # 参数默认值
        if skills is None:
            skills = self._raw_config.get("skills", [])
        agent_cfg = self._raw_config.get("agent_loop", {})
        if max_iterations is None:
            max_iterations = agent_cfg.get("max_iterations", 10)
        if timeout is None:
            timeout = agent_cfg.get("default_timeout", 300)

        # 加载 Skills 上下文
        skills_context = self._load_skills_context(skills)

        # 加载 soul
        soul_file = Path(__file__).parent / "soul.md"
        soul = ""
        if soul_file.exists():
            with open(soul_file, "r", encoding="utf-8") as f:
                soul = f.read()

        # 构建工具描述
        tools_desc = self._build_tools_description()

        # 构建 system prompt
        system_prompt = f"""{soul}

## 可用工具

{tools_desc}

## 规则

1. 逐步思考，每次只使用一个工具
2. 工具参数必须完整且精确
3. 观察工具输出后再决定下一步
4. 任务完成后用「FINAL:」开头的消息汇报结果
5. 遇到错误时分析原因并尝试替代方案
6. 无法完成任务时说明原因

{skills_context}"""

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"请完成以下任务：\n\n{task}"},
        ]

        trace = []
        start_time = time.time()

        for iteration in range(1, max_iterations + 1):
            elapsed = time.time() - start_time
            if elapsed > timeout:
                return {
                    "success": False,
                    "result": "",
                    "iterations": iteration,
                    "error": f"执行超时 ({timeout}s)",
                    "trace": trace,
                }

            # 调用 LLM
            response = self._call_llm(messages)
            if response is None:
                return {
                    "success": False,
                    "result": "",
                    "iterations": iteration,
                    "error": "LLM 调用失败",
                    "trace": trace,
                }

            trace.append({"iteration": iteration, "response": response[:500]})

            # 检查是否完成
            if response.strip().startswith("FINAL:"):
                final_result = response.strip()[6:].strip()
                return {
                    "success": True,
                    "result": final_result,
                    "iterations": iteration,
                    "error": "",
                    "trace": trace,
                }

            # 尝试解析工具调用
            tool_call = self._parse_tool_call(response)
            if tool_call:
                tool_name = tool_call["tool"]
                tool_params = tool_call["params"]
                
                result = self.execute(tool_name, tool_params)
                trace[-1]["tool_call"] = {"tool": tool_name, "params": str(tool_params)[:200]}
                trace[-1]["tool_result"] = str(result.get("result", ""))[:500]

                # 将工具结果追加到对话
                messages.append({"role": "assistant", "content": response})
                messages.append({
                    "role": "user",
                    "content": (
                        f"工具 {tool_name} 返回结果：\n"
                        f"{json.dumps(result, ensure_ascii=False)[:2000]}"
                    ),
                })
            else:
                # LLM 没有输出工具调用，追加为普通回复
                messages.append({"role": "assistant", "content": response})
                messages.append({
                    "role": "user",
                    "content": "请继续。如果任务已完成，用「FINAL:」汇报结果。",
                })

        # 达到最大循环次数
        return {
            "success": False,
            "result": "",
            "iterations": max_iterations,
            "error": f"达到最大循环次数 ({max_iterations})",
            "trace": trace,
        }

    def _build_tools_description(self) -> str:
        """构建工具描述文本（用于 system prompt）"""
        lines = []
        for tool in self.get_tools():
            name = tool["name"]
            desc = tool["description"]
            schema = tool.get("inputSchema", {})
            props = schema.get("properties", {})
            required = schema.get("required", [])

            param_lines = []
            for pname, pinfo in props.items():
                req_mark = " (必填)" if pname in required else ""
                pdesc = pinfo.get("description", "")
                param_lines.append(f"    - {pname}: {pdesc}{req_mark}")

            lines.append(f"- **{name}**: {desc}\n" + "\n".join(param_lines))

        return "\n\n".join(lines)

    def _parse_tool_call(self, response: str) -> Optional[Dict[str, Any]]:
        """
        从 LLM 响应中解析工具调用

        支持的格式：
        1. JSON: {"tool": "web_search", "params": {"query": "..."}}
        2. Markdown code block with JSON

        Returns:
            {"tool": str, "params": dict} 或 None
        """
        # 尝试直接 JSON
        try:
            parsed = json.loads(response.strip())
            if isinstance(parsed, dict) and "tool" in parsed and "params" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

        # 尝试 markdown code block
        md_match = re.search(r'```(?:json)?\s*\n(.*?)\n```', response, re.DOTALL)
        if md_match:
            try:
                parsed = json.loads(md_match.group(1).strip())
                if isinstance(parsed, dict) and "tool" in parsed and "params" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass

        return None
    
    # ========================================================================
    # 远程模式骨架 (v0.13.0 填充)
    # ========================================================================
    
    def start_remote_server(self) -> None:
        """
        启动 MCP server (骨架，待实现)
        
        v0.13.0 将实现:
        - MCP 协议解析
        - 网络通信
        - 异步任务处理
        """
        raise NotImplementedError(
            "Remote mode not implemented. "
            "Use LOCAL mode for now. Remote mode coming in v0.13.0"
        )


# ============================================================================
# 便捷函数 (用于本地模式导入)
# ============================================================================

def create_clerk(config_path: Optional[str] = None) -> ClerkWorker:
    """创建吏员工作器实例"""
    return ClerkWorker(config_path)


def register_handler(tool_name: str, handler: callable) -> None:
    """注册自定义 handler (本地模式)"""
    # 这个函数在本地模式下可以扩展工具
    raise NotImplementedError("Handler registration not implemented yet")


# ============================================================================
# 主入口 (调试用)
# ============================================================================

if __name__ == "__main__":
    # 简单测试
    logging.basicConfig(level=logging.INFO)
    
    clerk = ClerkWorker()
    print("=== Clerk Status ===")
    print(json.dumps(clerk.status(), indent=2, ensure_ascii=False))
    
    print("\n=== Available Tools ===")
    for tool in clerk.get_tools():
        print(f"- {tool['name']}: {tool['description']}")
