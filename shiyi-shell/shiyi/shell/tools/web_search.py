"""
网页搜索工具

使用 Bing Search API 进行网页搜索。
回退方案：使用 requests 直接搜索。
"""
import os
import json
import logging
import urllib.parse
import urllib.request
import urllib.error
from typing import Dict, Any

logger = logging.getLogger(__name__)

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "搜索关键词",
        },
        "limit": {
            "type": "integer",
            "description": "返回结果数量（默认3）",
            "default": 3,
        },
    },
    "required": ["query"],
}


def web_search_handler(args: Dict[str, Any]) -> Dict[str, Any]:
    """执行网页搜索

    优先使用 Bing API，无 key 时回退到 DuckDuckGo HTML 抓取。
    """
    query = args.get("query", "")
    limit = int(args.get("limit", 3))

    if not query.strip():
        return {"success": False, "result": "", "error": "Empty query"}

    # 方案1: Bing Web Search API
    bing_key = os.environ.get("BING_API_KEY")
    if bing_key:
        return _search_bing(query, limit, bing_key)

    # 方案2: DuckDuckGo HTML 抓取
    return _search_ddg(query, limit)


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


def _search_ddg(query: str, limit: int) -> Dict[str, Any]:
    """使用 DuckDuckGo HTML 搜索（无需 API key）"""
    import urllib.request
    import re

    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # 简单HTML解析提取标题和摘要
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


# 工具注册用
web_search_tool = {
    "name": "web_search",
    "handler": web_search_handler,
    "description": "搜索互联网获取最新信息。用于查找新闻、事实、数据等需要联网的信息。",
    "parameters": TOOL_SCHEMA,
}
