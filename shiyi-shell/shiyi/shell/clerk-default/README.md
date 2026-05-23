# 默认吏员 (clerk-default)

> 史佚（Shiyi）系统的执行单元，通过 MCP 协议与主引擎通信。

## 概述

默认吏员提供文件读写和网页搜索的基础能力，是吏员系统的参考实现。

### 核心理念

- **裁剪版 Agent**：无对话、无全量记忆，只收指令出结果
- **单一协议**：只用 MCP 一个标准协议通信
- **独立编号**：clerk_id 和版本号与史佚本体解耦
- **整体拔插**：MCP 协议 + 独立配置

### 当前版本

- **版本**: 0.1.0
- **模式**: 本地模式（进程内 class 验证抽象）
- **目标**: v0.13.0 实现远程 MCP server

## 目录结构

```
clerk-default/
├── clerk.json          # 吏员配置（ClerkConfig 的 JSON 版）
├── soul.md             # 吏员人格（执行指令的能力描述）
├── skills/
│   ├── file_ops.md     # 文件操作能力说明
│   └── web_search.md   # 网页搜索能力说明
├── knowledge/
│   └── workspace.md    # 工作沙箱目录说明
├── .env.example        # API key 等配置模板
├── worker.py           # 吏员主逻辑
├── test_clerk.py       # 验证脚本
└── README.md           # 本文档
```

## 快速开始

### 1. 环境准备

```bash
# 确保工作目录存在
mkdir -p ~/.shiyi/workspace
```

### 2. 安装依赖

```bash
# 无需额外依赖（使用标准库）
```

### 3. 运行验证

```bash
cd clerk-default
python test_clerk.py
```

### 4. 作为模块使用

```python
from worker import ClerkWorker, create_clerk

# 创建吏员实例
clerk = create_clerk()

# 查看状态
print(clerk.status())

# 获取工具列表
tools = clerk.get_tools()
print(tools)

# 执行工具调用
result = clerk.execute("file_write", {
    "path": "test.txt",
    "content": "Hello, Clerk!"
})

result = clerk.execute("file_read", {
    "path": "test.txt"
})
```

## clerk.json 格式规范

```json
{
  "clerk_id": "clerk_default_001",
  "name": "默认吏员",
  "version": "0.1.0",
  "description": "提供文件读写和网页搜索的基础吏员",
  "capabilities": ["file_read", "file_write", "web_search"],
  "tools": [...],
  "enabled": true,
  "created_at": "2026-05-19",
  "config_path": "clerk-default/"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| clerk_id | string | 是 | 唯一标识符 |
| name | string | 是 | 显示名称 |
| version | string | 是 | 版本号（语义化） |
| description | string | 否 | 描述 |
| capabilities | array | 是 | 能力列表 |
| tools | array | 否 | 工具定义 |
| enabled | boolean | 是 | 是否启用 |
| created_at | string | 否 | 创建日期 |

## MCP 接口说明

### 本地模式调用

```python
# execute 方法
clerk.execute(tool_name: str, params: dict) -> dict

# 返回格式
{
    "success": bool,    # 是否成功
    "result": str,      # 执行结果
    "error": str        # 错误信息（成功时为空）
}
```

### 可用工具

#### file_read

读取文件内容或列出目录。

**参数**:
```json
{
  "path": "string",    // 必填，文件路径
  "limit": 500         // 可选，最大读取行数
}
```

#### file_write

写入内容到文件。

**参数**:
```json
{
  "path": "string",    // 必填，文件路径
  "content": "string"  // 必填，写入内容
}
```

#### web_search

搜索互联网信息。

**参数**:
```json
{
  "query": "string",      // 必填，搜索关键词
  "max_results": 3        // 可选，最大结果数
}
```

## 本地模式 vs 远程模式

### 本地模式 (LOCAL)

- 作为 Python 模块直接导入
- 函数调用开销低
- 适合单进程场景
- 当前版本实现

### 远程模式 (REMOTE)

- MCP server 骨架
- 支持网络通信
- 适合分布式场景
- v0.13.0 实现

## 安全机制

### 沙箱隔离

所有文件操作限制在 `~/.shiyi/workspace/` 目录内。

### 路径穿越防护

- 归一化所有路径
- 检查归一化后的路径是否在沙箱内
- 非法路径直接返回 `PermissionError`

### 禁止的操作

- 访问沙箱外部文件
- 执行系统命令
- 覆盖目录为文件

## 如何开发新吏员

### 1. 复制模板

```bash
cp -r clerk-default clerk-myworker
```

### 2. 修改配置

编辑 `clerk-myworker/clerk.json`:
- 修改 `clerk_id`（唯一标识）
- 修改 `name`
- 调整 `capabilities`

### 3. 扩展工具

在 `worker.py` 中添加新工具类：

```python
class MyTool:
    name = "my_tool"
    description = "我的自定义工具"
    schema = {...}
    
    @staticmethod
    def execute(params: dict, workspace: Path) -> dict:
        # 实现逻辑
        return {"success": True, "result": "...", "error": ""}
```

注册到 `TOOL_REGISTRY`:

```python
TOOL_REGISTRY = {
    # ... 现有工具
    "my_tool": MyTool,
}
```

### 4. 更新文档

- 更新 `soul.md` 描述新能力
- 添加 `skills/my_tool.md` 说明使用方法
- 更新本 README

### 5. 验证

```bash
python test_clerk.py
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| BING_API_KEY | Bing 搜索 API key | 空（使用 DuckDuckGo） |
| BING_API_ENDPOINT | Bing API 端点 | https://api.bing.microsoft.com/v7.0/search |
| CLERK_WORKSPACE | 工作沙箱目录 | ~/.shiyi/workspace/ |

## 错误处理

| 错误类型 | 返回 | 说明 |
|----------|------|------|
| 空路径 | `{"success": false, "error": "Empty path"}` | path 参数为空 |
| 路径穿越 | `{"success": false, "error": "路径穿越被拒绝: ..."}` | 尝试访问沙箱外 |
| 文件不存在 | `{"success": false, "error": "文件不存在: ..."}` | 文件路径不存在 |
| 未知工具 | `{"success": false, "error": "Unknown tool: ..."}` | 工具名错误 |
| 搜索失败 | `{"success": false, "error": "搜索失败: ..."}` | 网络错误 |

## 贡献

欢迎提交 Issue 和 Pull Request。

## 许可

与史佚项目一致。
