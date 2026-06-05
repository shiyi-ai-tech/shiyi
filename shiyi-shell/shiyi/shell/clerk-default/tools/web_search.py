"""
EnhancedWebSearchTool - 增强版网页搜索

增强版工具，特性：
- 支持多种搜索引擎（Bing优先，无key回退DuckDuckGo）
- 返回结构化结果
- 支持语言和市场设置
"""

import os
import json
import re
import logging
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 5


class EnhancedWebSearchTool:
    """增强版网页搜索工具"""
    
    name = "enhanced_web_search"
    description = "增强版网页搜索，支持 Bing API 和 DuckDuckGo 回退"
    
    schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词"
            },
            "max_results": {
                "type": "integer",
                "description": f"最大返回数量（默认 {DEFAULT_MAX_RESULTS}）",
                "default": DEFAULT_MAX_RESULTS
            },
            "language": {
                "type": "string",
                "description": "搜索语言（如 zh-CN, en-US）",
                "default": "zh-CN"
            }
        },
        "required": ["query"]
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        执行网页搜索
        
        Args:
            params: {
                "query": str,        # 搜索关键词
                "max_results": int,  # 最大结果数
                "language": str      # 语言
            }
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "data": {"results": [...], "count": int}, "error": str}
        """
        query = params.get("query", "")
        max_results = int(params.get("max_results", DEFAULT_MAX_RESULTS))
        language = params.get("language", "zh-CN")
        
        if not query.strip():
            return {"success": False, "data": None, "error": "Empty query"}
        
        # 方案1: Bing Web Search API
        bing_key = os.environ.get("BING_API_KEY")
        if bing_key:
            return EnhancedWebSearchTool._search_bing(query, max_results, language, bing_key)
        
        # 方案2: DuckDuckGo HTML 抓取
        return EnhancedWebSearchTool._search_ddg(query, max_results)
    
    @staticmethod
    def _search_bing(query: str, limit: int, language: str, api_key: str) -> Dict[str, Any]:
        """使用 Bing Web Search API"""
        url = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": api_key}
        
        params = urllib.parse.urlencode({
            "q": query,
            "count": limit,
            "mkt": language,
            "responseFilter": "WebPages"
        })
        
        try:
            req = urllib.request.Request(f"{url}?{params}", headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            
            results = []
            for item in data.get("webPages", {}).get("value", [])[:limit]:
                results.append({
                    "title": item.get("name", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("snippet", ""),
                    "date_last_crawled": item.get("dateLastCrawled", "")
                })
            
            if not results:
                return {"success": True, "data": {"results": [], "count": 0}, "error": ""}
            
            return {
                "success": True,
                "data": {
                    "results": results,
                    "count": len(results),
                    "engine": "bing"
                },
                "error": ""
            }
            
        except urllib.error.HTTPError as e:
            logger.warning(f"Bing API error: {e.code}")
            return {"success": False, "data": None, "error": f"Bing API returned {e.code}"}
        except Exception as e:
            logger.warning(f"Bing search failed: {e}")
            return {"success": False, "data": None, "error": str(e)}
    
    @staticmethod
    def _search_ddg(query: str, limit: int) -> Dict[str, Any]:
        """使用 DuckDuckGo HTML 搜索"""
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            
            # 解析结果
            results = []
            
            # 提取标题和链接
            link_pattern = r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
            links = re.findall(link_pattern, html, re.DOTALL)
            
            # 提取摘要
            snippet_pattern = r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>'
            snippets = re.findall(snippet_pattern, html, re.DOTALL)
            
            for i, (href, title) in enumerate(links[:limit]):
                title_clean = re.sub(r'<[^>]+>', '', title).strip()
                snippet = ""
                if i < len(snippets):
                    snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
                
                results.append({
                    "title": title_clean or "无标题",
                    "url": href,
                    "snippet": snippet or "无摘要"
                })
            
            if not results:
                return {"success": True, "data": {"results": [], "count": 0}, "error": ""}
            
            return {
                "success": True,
                "data": {
                    "results": results,
                    "count": len(results),
                    "engine": "duckduckgo"
                },
                "error": ""
            }
            
        except Exception as e:
            logger.warning(f"DDG search failed: {e}")
            return {"success": False, "data": None, "error": f"搜索失败: {e}"}
