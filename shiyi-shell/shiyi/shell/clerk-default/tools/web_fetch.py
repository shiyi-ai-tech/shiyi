"""
WebFetchTool - 网页内容抓取

增强版工具，功能：
- 抓取静态网页内容
- 支持内容长度限制
- 自动编码检测
"""

import re
import logging
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any

from ._utils import truncate_output

logger = logging.getLogger(__name__)

DEFAULT_MAX_LENGTH = 50000  # 50KB


class WebFetchTool:
    """网页内容抓取工具"""
    
    name = "web_fetch"
    description = "抓取网页内容，支持长度限制和编码自动检测"
    
    schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "网页 URL"
            },
            "max_length": {
                "type": "integer",
                "description": f"最大抓取长度（默认 {DEFAULT_MAX_LENGTH}）",
                "default": DEFAULT_MAX_LENGTH
            },
            "extract_text": {
                "type": "boolean",
                "description": "是否提取纯文本（去除HTML标签，默认 True）",
                "default": True
            }
        },
        "required": ["url"]
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        抓取网页内容
        
        Args:
            params: {
                "url": str,           # 网页URL
                "max_length": int,    # 最大长度
                "extract_text": bool  # 是否提取纯文本
            }
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "data": {"content": str, "url": str, "title": str}, "error": str}
        """
        url = params.get("url", "")
        max_length = int(params.get("max_length", DEFAULT_MAX_LENGTH))
        extract_text = params.get("extract_text", True)
        
        if not url.strip():
            return {"success": False, "data": None, "error": "Empty URL"}
        
        # URL 验证
        if not url.startswith(("http://", "https://")):
            return {"success": False, "data": None, "error": "URL 必须以 http:// 或 https:// 开头"}
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
        }
        
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_type = resp.headers.get("Content-Type", "")
                html = resp.read()
            
            # 检测编码
            encoding = "utf-8"
            if "charset=" in content_type:
                encoding = content_type.split("charset=")[-1].split(";")[0].strip()
            else:
                # 从 HTML 中检测编码
                try:
                    html_str = html.decode("latin-1")
                    encoding_match = re.search(r'<meta[^>]+charset=["\']?([^"\'\s>]+)', html_str, re.IGNORECASE)
                    if encoding_match:
                        encoding = encoding_match.group(1)
                except:
                    pass
            
            # 解码
            try:
                text = html.decode(encoding, errors="replace")
            except:
                text = html.decode("utf-8", errors="replace")
            
            # 提取标题
            title_match = re.search(r'<title[^>]*>([^<]+)</title>', text, re.IGNORECASE)
            title = title_match.group(1).strip() if title_match else ""
            
            # 提取纯文本
            if extract_text:
                # 移除 script 和 style 标签
                text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                # 移除所有 HTML 标签
                text = re.sub(r'<[^>]+>', '', text)
                # 清理空白
                text = re.sub(r'\s+', ' ', text).strip()
            
            # 截断
            truncated = len(text) > max_length
            if truncated:
                text = truncate_output(text, max_length)
            
            return {
                "success": True,
                "data": {
                    "content": text,
                    "url": url,
                    "title": title,
                    "truncated": truncated,
                    "content_type": content_type
                },
                "error": ""
            }
            
        except urllib.error.HTTPError as e:
            logger.warning(f"HTTP error: {e.code}")
            return {"success": False, "data": None, "error": f"HTTP {e.code}"}
        except urllib.error.URLError as e:
            logger.warning(f"URL error: {e.reason}")
            return {"success": False, "data": None, "error": f"URL 错误: {e.reason}"}
        except Exception as e:
            logger.warning(f"Fetch failed: {e}")
            return {"success": False, "data": None, "error": str(e)}
