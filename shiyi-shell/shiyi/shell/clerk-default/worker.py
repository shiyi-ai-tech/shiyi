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
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

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
    safe_root = workspace.resolve()
    
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
# 工具注册表
# ============================================================================

TOOL_REGISTRY: Dict[str, Any] = {
    "file_read": FileReadTool,
    "file_write": FileWriteTool,
    "web_search": WebSearchTool,
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
            config_path: clerk.json 文件路径，默认从当前目录加载
        """
        # 确定配置路径
        if config_path:
            config_file = Path(config_path) / "clerk.json"
        else:
            config_file = Path(__file__).parent / "clerk.json"
        
        # 加载配置
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            self.config = ClerkConfig.from_json(config_data)
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
