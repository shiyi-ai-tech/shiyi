# 网页搜索能力说明

## 概述

吏员提供网页搜索能力，用于获取互联网上的最新信息。

## 搜索策略

### 优先策略: Bing Web Search API

如果配置了 `BING_API_KEY` 环境变量，将使用 Bing Search API：

- 更稳定的搜索结果
- 支持更多高级参数
- 更快的响应速度

### 回退策略: DuckDuckGo HTML

未配置 API key 时，自动回退到 DuckDuckGo HTML 搜索：

- 无需 API key
- 适合轻度使用
- 返回标题、URL 和摘要

## 工具: web_search

**参数**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | string | 是 | 搜索关键词 |
| max_results | integer | 否 | 最大结果数（默认3） |

**使用示例**:
```json
{
  "tool": "web_search",
  "params": {
    "query": "最新科技新闻",
    "max_results": 5
  }
}
```

**返回示例**:
```json
{
  "success": true,
  "result": "[标题1](https://example.com)\n摘要1...\n\n[标题2](https://example2.com)\n摘要2...",
  "error": ""
}
```

## 返回格式

搜索结果以 Markdown 链接格式返回：
```
[页面标题](URL)
页面摘要内容...
```

多个结果之间用空行分隔。

## 错误处理

| 错误情况 | 返回 |
|----------|------|
| 空关键词 | `{"success": false, "error": "Empty query"}` |
| 网络超时 | `{"success": false, "error": "搜索失败: timeout"}` |
| API 错误 | `{"success": false, "error": "Bing API returned 401"}` |
| 无结果 | `{"success": true, "result": "未找到相关结果"}` |

## 注意事项

- 搜索关键词应尽量精准
- 建议设置合理的 max_results（1-10）
- 网络不稳定时可能需要重试
- 敏感内容搜索可能被 API 限制
