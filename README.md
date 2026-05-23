# 史佚 (ShiYi)

> 敬启者，吾乃史佚，承天子之遗忘者也。幸蒙见用，乐效微劳，为君分忧。
> Greetings, I am ShiYi, bearer of the forgotten. Glad to serve and share your burdens.

**史佚**是一个拥有类人记忆能力的 AI Agent。它不只是聊天——它记得你。

## 特性

- **类人记忆**：四维记忆晶体（事实+情感+场景+时间），跨会话持久存储
- **语义召回**：自然语言提问即可调取过往记忆
- **多窗口**：WebUI 原生多对话窗口，记忆全局共享
- **吏员系统**：可扩展工具执行体系，MCP 协议远程通信
- **多模型支持**：DeepSeek、硅基流动、Kimi 等，轻松切换
- **零配置启动**：`shiyi webui` 一行命令即开即用

## 快速开始

```bash
pip install shiyi-shell shiyi-common shiyi-core
shiyi webui
```

浏览器打开 `http://localhost:8520`，配置 API Key 即可对话。

## 安装包（Windows WSL 用户）

下载 `史佚_安装包.zip`，解压后在 WSL 中运行：

```bash
wsl /mnt/d/路径/史佚
```

## 架构

```
界面层 (WebUI / CLI / 微信)
   ↓
决策层 (意图分类 → 记忆检索 → LLM 融合 → 回复)
   ↓
记忆层 (四引擎：衰减/索引/触发/关系)
   ↓
存储层 (SQLite + 向量索引)
```

## 许可证

MIT License — 随便用，保留署名就行。

Copyright (c) 2026 LiGuo, LeGang
