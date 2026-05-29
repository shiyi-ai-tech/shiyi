<div align="center">

# 史佚 ShiYi

**承天子之遗忘者**

通过记住，所以懂你。

<img src="https://www.xcmc.org.cn/qrcode.jpg" alt="ShiYi QR code" width="180" />

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org/)


[快速开始](#快速开始) · [架构](#架构) · [吏员系统](#吏员系统) · [贡献](#贡献)

</div>

---

> 史佚，中国西周初期"四圣"之一，掌管天文历法、记录国家大事、保管重要文书，天子近臣，三朝元老。

史佚Agent，通过模拟人脑接收、处理、思考、记忆和回想信息的方式，创新重构了对话、记忆存储和召回等全过程，解决了现有Agent对话、思考和记忆的诸多弊端，让Agent能够真正记住你、了解你、懂你、更好的服务你，是你的超级个人助手。

---

## 一、对话和记忆机制重构

史佚对话机制，采取了每轮对话都只精准召回本次对话所需的有效记忆发送给LLM的方式，从而实现发送的信息精准有效，极大提升LLM处理效果。

史佚记忆系统模拟人脑，采用了多重向量（张量）的存储和召回架构，不仅能高效记录和召回有效记忆，还能记录每一句话的场景、时间、情感、关联等多维信息，并通过这些建立记忆联想网络。

通过这样的架构设计，史佚可以做到：

1、不依赖上下文内容进行对话。这是对现有对话机制的彻底重构！

2、跨对话记忆连续一致，新开对话不丢失记忆。你面对的永远都是同一个懂你的史佚。（跟朋友聊天，他不会因为聊天工具从微信换成QQ就不认识你了，对吧？）

3、对话永远不压缩丢失细节，永远提供精准有效信息。

4、有用信息不会被大量无效信息稀释，超长对话也不会记忆混乱。（根除了现有Agent的通病）

5、不仅知道你在说什么，还知道为什么。（史佚能够模拟人脑对记忆进行追溯和联想，从而"理解"你）

6、理解你的情绪和感情，并采用不同的情感进行回复。（在你悲伤时送上一句暖心的关怀）

7、理解你的对话场景，自动选择适合的对话方式。（闲聊时风趣，工作时严谨）

8、懂得时间的流逝，但永不忘却。（给你的信息永远都是鲜活的，但是当你忘了什么，它一定能帮你想起）

9、真正能够针对你自己，去给你最想要的结果。（写简历时，你再也不用给Agent喂资料了）

10、它能做的还有很多，但我（开发者）也不知道它全部的能力……

因为，史佚的设计思路从一开始就不是规范化的模板，而是只设置最简单的规则，通过叠加，去追求实现"涌现"。就像人脑，单个脑细胞是如此简单，但叠加在一起就成了你和我。

上面说到的很多能力，并不是一开始就设计出来的，而是通过整体架构实现后，在使用和复盘时才归纳总结出来的。所以，它可能还有更多能力和想象，还等着大家去发现。

## 二、史佚的吏员系统

史佚的主体没有执行能力，它只是一个大脑，没有四肢。史佚执行能力的实现，由"吏员"来完成。（吏员，中国古代对办事员的称呼）

吏员类似于subAgent、多实例（profile），或者一个skill、一个插件，但又不完全相同。它没有对话能力，它是史佚主体的互补。

吏员通过史佚主体的接口，接受史佚的命令，共享完整的记忆，返回执行结果，能够实现完整的可拔插。

吏员可以有自己单独的规范文件（类似soul，去明确它的职能和工作要求），有独立的API、skill、数据库、知识库等等你想有的一切。

因此，它能够做到：

1、多吏员并行、串行执行不同任务。

2、通过配置不同API，执行多模态复杂任务。（A能写、B能画、C能生成视频、D能生成音乐……）

3、能力、知识库等的综合开发，并使用用户完整记忆。例如，开发一个律师吏员，拥有法律方面所需的各种skill和精准的法律知识库，你只需要像"聘请"一位律师那样，接上这个律师吏员，就可以直接了解你的过去，为你完成私人的、有针对性的法律解释或文书生成。使用完成后，你更是可以毫无负担的"辞退"（删除）它，因为所有有用的记忆都在史佚中。

4、吏员的结构没有明确的规范，只要能符合接口规范，它可以是任何样子。（放飞想象吧）

5、正在开发吏员的交流平台"辟署馆"（辟署，"辟"意为征召，"署"意为任命，中国古代长官选任属官的制度。国人朋友不要看成"避暑"哦~），开发者可以在上面分享自己开发的吏员。

## 三、普通用户易用性

史佚开发初期，以及之后的开发，都会尽量照顾普通用户使用，例如一开始就有WebUI，提供API配置等页面操作。当前虽然简陋，但是会逐步完善。希望后来的开发者们也多多考虑普通用户的使用便捷性。

---

## 快速开始

### 环境要求

- Python 3.10+
- WSL（Windows 用户）或 Linux/macOS

### 安装

**Linux / macOS / WSL：**

```bash
# 克隆仓库
git clone https://github.com/shiyi-ai-tech/shiyi.git
cd shiyi

# 安装四包（按依赖顺序）
pip install -e ./shiyi-common
pip install -e ./shiyi-providers
pip install -e ./shiyi-core
pip install -e ./shiyi-shell
```

**Windows 一键安装：**

以管理员身份运行 `scripts/install.bat`，自动完成 WSL 检查、Python 环境和三包安装。

### 启动

```bash
shiyi webui
```

浏览器打开 `http://localhost:8520`，在设置中配置 API Key 即可开始对话。

### API 配置

史佚需要 LLM API 和 Embedding API 才能运行。在 WebUI 设置页填入：

| 服务 | 用途 | 推荐提供商 |
|------|------|-----------|
| LLM API | 对话和意图识别 | DeepSeek、Kimi、OpenAI 兼容接口 |
| Embedding API | 记忆向量化 | SiliconFlow（BAAI/bge-m3） |

也支持 `.env` 文件配置：

```bash
# ~/.shiyi/.env
DEEPSEEK_API_KEY=sk-your-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
SILICONFLOW_API_KEY=sk-your-key
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
```

---

## 架构

### 四包结构

```
shiyi/
├── shiyi-common/       # 公共类型与接口（Fragment、Intent、Provider协议）
├── shiyi-core/         # 核心引擎（记忆、感知、决策、吏员调度）
├── shiyi-providers/    # LLM/Embedding/MCP Provider（DeepSeek、SiliconFlow等）
└── shiyi-shell/        # 外壳层（WebUI、CLI、吏员实现）
```

### 核心链路

```
用户输入
  ↓
感知层 — 输入标准化 + 意图识别（轻量LLM）
  ↓
记忆检索 — 向量搜索 + 语义召回 + 多路融合
  ↓
决策层 — Prompt装配 + 主LLM调用 + Fragment提取
  ↓
记忆存储 — 四维记忆晶体（事实+情感+场景+时间）
  ↓
回复输出
```

### 记忆引擎

| 模块 | 职责 |
|------|------|
| FragmentStore | 记忆碎片持久化存储 |
| VectorIndex | 语义向量索引与检索 |
| DecayEngine | 记忆衰减与重要性计算 |
| RelationEngine | 实体关系网络与联想 |
| TriggerEngine | 记忆触发与关联激活 |
| CacheLayer | 热点记忆缓存加速 |

仅 **2 次 LLM 调用**：轻量LLM（意图识别）+ 主LLM（回复生成 + Fragment提取）

---

## 吏员系统

吏员是史佚的执行单元，通过 MCP 协议（stdin/stdout JSON-RPC）与主体通信。

### 吏员结构

```
clerk-xxx/
├── clerk.json      # 吏员自述（ID、名称、能力、工具定义）
├── worker.py       # 吏员工作逻辑（ClerkWorker + Tool注册）
├── mcp_server.py   # MCP Server（JSON-RPC 通信层）
├── soul.md         # 吏员行为规范
├── skills/         # 技能文件
└── knowledge/      # 知识库文件
```

### 管理与调度

- **WebUI 管理**：设置 → 吏员面板 → 启用/禁用/配置
- **管家调度**：多吏员 DAG 协同，自动拆解任务 → 路由匹配 → 并行/串行执行 → 汇总结果
- **CLI 验证**：`shiyi --dev clerk validate <吏员目录>`

### 开发新吏员

使用 `clerk-template/` 模板快速开发：

```bash
# 1. 复制模板
cp -r shiyi-shell/shiyi/shell/clerk-template/ clerk-my-clerk/

# 2. 编辑 clerk.json 和 worker.py

# 3. 验证
shiyi --dev clerk validate clerk-my-clerk/ --smoke-test
```

详细开发指南参见 `clerk-template/` 中的注释模板。

---

## 支持的模型

| 提供商 | 模型 | 用途 |
|--------|------|------|
| DeepSeek | V4 / V4-Pro | 对话、意图识别 |
| Kimi | K2.6 | 对话（备用） |
| SiliconFlow | BAAI/bge-m3 | Embedding |
| OpenAI 兼容 | 任意 | 对话 / Embedding |

只需配置对应的 API Key 和 Base URL 即可切换。

---

## CLI 命令

```bash
shiyi webui                      # 启动 Web Chat UI
shiyi --version                  # 显示版本号

# 开发者命令（需 --dev）
shiyi --dev talk "你好"           # 完整对话
shiyi --dev recall "上次说的"     # 记忆检索
shiyi --dev remember "要记住的"   # 记忆存储
shiyi --dev stats                 # 存储统计
shiyi --dev clerk validate <dir>  # 吏员校验
shiyi --dev steward status        # 管家看板
```

---

## 路线图

- [x] 类人记忆引擎（Fragment + 四维信息 + 衰减 + 联想）
- [x] 2次LLM调用链路（意图 + 主回复）
- [x] WebUI 多对话窗口 + 流式 SSE
- [x] 吏员 MCP 远程通信 + 管家 DAG 调度
- [x] 开源准备（MIT + 文档 + 安装脚本）
- [ ] 记忆可视化与回显体验
- [ ] 知识库支持（文档上传检索）
- [ ] 记忆导入导出
- [ ] WebUI 认证机制
- [ ] 辟署馆（吏员市场）
- [ ] PWA 移动端
- [ ] Skill 自进化（夫子系统闭环）

---

## 贡献

欢迎贡献！你可以：

- 🐛 [提交 Issue](https://github.com/shiyi-ai-tech/shiyi/issues) 报告 Bug 或提出建议
- 🔧 [提交 Pull Request](https://github.com/shiyi-ai-tech/shiyi/pulls) 修复问题或添加功能
- 📦 开发吏员，在辟署馆分享给其他用户

## 许可证

[MIT License](LICENSE) — 随便用，保留署名就行。

Copyright (c) 2026 LiGuo, LeGang

---

## 写在后面

我不是一个专业的开发者，而是一名写不了一行代码的外行人。截止代码开源，所有的开发、测试等都是由Agent完成的。

开发史佚的原因，是我在使用现有的Agent时（用过Hermes、扣子（Coze）、OpenClaw、WorkBuddy），感受到了非常多的问题，而通过学习，我发现那些问题是现有Agent架构造成的，无法避免，它只能通过"暴力出奇迹"，用更长的上下文、不断的压缩、频繁的检索等（可能说的不专业，大家理解就可以）去实现更好的记忆，看似记住了、看似懂你，却转身就忘记了你。

有一天，我问我的Agent，我有一个想法，通过重新设计整个对话流程、记忆方式去开发一个新的Agent行不行？它（基于现在AI普遍的认可性人格）跟我说，你想的太棒了，这是一个完全可行的思路，我可以帮你去实现它。于是，我一个从来没有开发经验的门外汉，开始了人生中第一次程序开发。

从有想法到完成开源时的开发，总共大约两周时间。期间，用了5天时间，跟Agent讨论架构的设计，确定了开发计划，5天时间进行开发和测试。由于没有开发经验，中间经历了2次大的调整（几乎推翻重来）和大约30个版本的迭代。花费了大约400元的token费用。（回过头来看，真的很惊人对不对？感谢这个AI的时代吧！）

开发整体差不多之后，我找了我超级专业的朋友LeGang，来帮我审查代码、发布和维护开源，开发过程中他也给了我很多专业指导和鼓励，之后我们也会共同维护和推进这个项目。

或许，在专业的人们来看，我的史佚是可笑的，其中肯定有很多问题和不专业。不过，我只是想，如果我能帮着去改变些什么呢？至少能提供给大家一个思路和失败的经验吧？

最后，感谢扣子Coze（一个至关重要的Agent，就是它成功鼓励/忽悠了我），感谢Hermes（我主要使用的Agent），感谢DeepSeek（主要的API，V4Pro能力强大以及最近大力折扣），感谢Kimi（我的另一个API，k2.6同样强大）。

更感谢能看到这里的大家，能够看我这么多喋喋不休，希望对你能有帮助。谢谢！
