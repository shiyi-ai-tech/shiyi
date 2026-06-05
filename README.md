<div align="center">

# ShiYi

**The one who carries the Son of Heaven's forgotten things**

By remembering, I understand you.

<img src="https://www.xcmc.org.cn/qrcode.jpg" alt="ShiYi QR code" width="180" />

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-2026.06.05-orange.svg)](https://github.com/shiyi-ai-tech/shiyi)

<a href="README.md"><strong>English</strong></a> · <a href="README.zh-CN.md"><strong>简体中文</strong></a> · [Quick Start](#quick-start) · [Architecture](#architecture) · [Clerk System](#clerk-system) · [Contributing](#contributing)

</div>

---

> ShiYi is one of the "Four Sages" in the early Western Zhou, responsible for astronomy and calendars, recording state affairs, and safeguarding important documents. A close minister of the Son of Heaven and a veteran across three reigns.

ShiYi Agent simulates how the human brain receives, processes, thinks, remembers, and recalls information. It reconstructs the full workflow of conversation, memory storage, and retrieval, addressing many shortcomings in current agents. It enables the agent to truly remember you, understand you, and serve you better as your personal assistant.

---

## UI Preview

<table>
  <tr>
    <td align="center"><b>Chat (Dark Mode)</b></td>
    <td align="center"><b>Chat (Light Mode)</b></td>
  </tr>
  <tr>
    <td><img src="https://www.xcmc.org.cn/docs/images/首页-黑夜.png" alt="Chat Dark" width="400"/></td>
    <td><img src="https://www.xcmc.org.cn/docs/images/首页-白天.png" alt="Chat Light" width="400"/></td>
  </tr>
</table>

<table>
  <tr>
    <td align="center"><b>Skill Search and Install</b></td>
    <td align="center"><b>Clerk Configuration</b></td>
  </tr>
  <tr>
    <td><img src="https://www.xcmc.org.cn/docs/images/技能页面.png" alt="Skills" width="400"/></td>
    <td><img src="https://www.xcmc.org.cn/docs/images/吏员配置.png" alt="Clerk Config" width="400"/></td>
  </tr>
</table>

<details>
<summary>More Screenshots</summary>

<table>
  <tr>
    <td align="center"><b>Skill EN-ZH Mapping</b></td>
    <td align="center"><b>Tools Page</b></td>
  </tr>
  <tr>
    <td><img src="https://www.xcmc.org.cn/docs/images/技能中英对照.png" alt="Skill EN-ZH" width="400"/></td>
    <td><img src="https://www.xcmc.org.cn/docs/images/工具页面.png" alt="Tools" width="400"/></td>
  </tr>
  <tr>
    <td align="center"><b>Settings Page</b></td>
    <td align="center"><b>API Settings</b></td>
  </tr>
  <tr>
    <td><img src="https://www.xcmc.org.cn/docs/images/设置页面.png" alt="Settings" width="400"/></td>
    <td><img src="https://www.xcmc.org.cn/docs/images/设置-API设置.png" alt="API Settings" width="400"/></td>
  </tr>
  <tr>
    <td align="center"><b>Feishu Configuration</b></td>
    <td align="center"><b>WeChat Configuration</b></td>
  </tr>
  <tr>
    <td><img src="https://www.xcmc.org.cn/docs/images/飞书配置.png" alt="Feishu" width="400"/></td>
    <td><img src="https://www.xcmc.org.cn/docs/images/微信配置.png" alt="WeChat" width="400"/></td>
  </tr>
  <tr>
    <td align="center"><b>Clerk Skill Showcase</b></td>
    <td align="center"><b>Generated File Download</b></td>
  </tr>
  <tr>
    <td><img src="https://www.xcmc.org.cn/docs/images/吏员配置-技能展示.png" alt="Clerk Skills" width="400"/></td>
    <td><img src="https://www.xcmc.org.cn/docs/images/生成文件下载.png" alt="File Download" width="400"/></td>
  </tr>
</table>

</details>

---

## 1. Conversation and Memory Redesign

The ShiYi conversation mechanism recalls only the precise memories needed for the current turn and sends them to the LLM. This keeps prompts accurate and focused, improving LLM performance.

The memory system simulates the human brain with a multi-vector (tensor) storage and retrieval architecture. It records and recalls useful memories efficiently, and also stores context, time, emotion, and relations for each utterance, building a rich associative memory network.

With this design, ShiYi can:

1. Converse without relying on raw chat context. This is a full redesign of the existing conversation mechanism.
2. Keep memory consistent across sessions. Starting a new chat does not lose memory. You always talk to the same ShiYi that knows you.
3. Never compress away details, while always providing precise and effective information.
4. Prevent useful information from being diluted by noise. Even very long conversations stay coherent.
5. Know not only what you say, but why you say it.
6. Understand your emotions and respond with appropriate tone.
7. Understand the scene and choose the best conversation style automatically.
8. Understand the passage of time, and never forget.
9. Truly tailor results to you.
10. There is more it can do, but even I (the developer) do not know all its capabilities yet.

From the beginning, ShiYi was not designed as a rigid template. It uses only the simplest rules and relies on composition to pursue emergence. Like the human brain, a single neuron is simple, but together they become you and me.

## 2. The ShiYi Clerk System

ShiYi itself has no execution ability. It is a brain without limbs. Execution is handled by "clerks."

Clerks are similar to sub-agents, multi-instances (profiles), or a skill/plugin, but not the same. They do not chat. They complement the ShiYi core.

Clerks receive commands through ShiYi interfaces, share the full memory, and return execution results. They are fully pluggable.

They enable:

1. Multiple clerks running in parallel or sequence.
2. Multimodal tasks through different API configurations.
3. Combined capabilities and knowledge bases, using full user memory.
4. Flexible clerk structure as long as interface contracts are met.
5. A clerk sharing platform called "Bishu Hall" is under development, where developers can share their clerks.

## 3. Usability for Regular Users

From the beginning and throughout development, ShiYi prioritizes usability for regular users. For example, it ships with a WebUI and API configuration pages. It is still basic today, but will improve over time. We hope future contributors also consider everyday usability.

---

## Quick Start

### Requirements

- Python 3.10+
- Linux / macOS / WSL

### Install

```bash
# Clone the repo
git clone https://github.com/anty0418/shiyi-tongxun.git
cd shiyi-tongxun

# Install dependencies
pip install -e ./shiyi-common
pip install -e ./shiyi-providers
pip install -e ./shiyi-core
pip install -e ./shiyi-shell
```

If you hit missing dependencies when installing from source, install them first:

```bash
pip install fastapi uvicorn python-multipart pydantic requests jieba mcp
```

### Start

```bash
shiyi webui
```

Open `http://localhost:8530` in your browser, then configure your API Key in Settings to start chatting.

### API Configuration

ShiYi requires both an LLM API and an Embedding API. Fill in the WebUI Settings page:

| Service | Purpose | Recommended Provider |
|---------|---------|----------------------|
| LLM API | Chat and intent recognition | DeepSeek, Kimi, OpenAI-compatible APIs |
| Embedding API | Memory vectorization | SiliconFlow (BAAI/bge-m3) |

You can also use a `.env` file:

```bash
# ~/.shiyi/.env
DEEPSEEK_API_KEY=sk-your-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
SILICONFLOW_API_KEY=sk-your-key
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
```

---

## Architecture

### Four-Package Layout

```
shiyi-tongxun/
├── shiyi-common/       # Shared types and interfaces (Fragment, Intent, Provider protocols)
├── shiyi-providers/    # External providers (LLM, Embedding, MCP)
├── shiyi-core/         # Core engine (memory, perception, decision, clerk scheduling)
└── shiyi-shell/        # Shell layer (WebUI, CLI, clerks, gateway)
```

### Core Pipeline

```
User input
  ↓
Perception layer - input normalization + intent recognition (lightweight LLM)
  ↓
Memory retrieval - vector search + semantic recall + multi-route fusion
  ↓
Decision layer - prompt assembly + main LLM call + fragment extraction
  ↓
Memory storage - 4D memory crystal (fact + emotion + scene + time)
  ↓
Response output
```

### Memory Engine

| Module | Responsibility |
|--------|----------------|
| FragmentStore | Persistent storage for memory fragments |
| VectorIndex | Semantic vector indexing and retrieval |
| DecayEngine | Memory decay and importance scoring |
| RelationEngine | Entity relation network and associations |
| TriggerEngine | Memory triggers and activation |
| CacheLayer | Hot memory cache acceleration |

Only **2 LLM calls**: lightweight LLM (intent recognition) + main LLM (response generation + fragment extraction)

---

## Clerk System

Clerks are ShiYi execution units. They communicate with the core via MCP (stdin/stdout JSON-RPC).

### Clerk Structure

```
clerk-xxx/
├── clerk.json      # Clerk profile (ID, name, abilities, tool definitions)
├── worker.py       # Clerk logic (ClerkWorker + tool registration)
├── mcp_server.py   # MCP server (JSON-RPC transport)
├── soul.md         # Clerk behavior guidelines
├── skills/         # Skill files
└── knowledge/      # Knowledge base
```

### Management and Scheduling

- **WebUI management**: Settings -> Clerk panel -> enable/disable/configure
- **Steward scheduling**: multi-clerk DAG coordination, task decomposition -> route matching -> parallel/serial execution -> results aggregation
- **CLI validation**: `shiyi --dev clerk validate <clerk-dir>`

### Create a New Clerk

Use the `clerk-template/` template for rapid development:

```bash
# 1. Copy template
cp -r shiyi-shell/shiyi/shell/clerk-template/ clerk-my-clerk/

# 2. Edit clerk.json and worker.py

# 3. Validate
shiyi --dev clerk validate clerk-my-clerk/ --smoke-test
```

See the annotated template in `clerk-template/` for detailed guidance.

---

## Supported Models

| Provider | Model | Purpose |
|----------|-------|---------|
| DeepSeek | V4 / V4-Pro | Chat, intent recognition |
| Kimi | K2.6 | Chat (fallback) |
| SiliconFlow | BAAI/bge-m3 | Embedding |
| OpenAI-compatible | Any | Chat / Embedding |

Configure the corresponding API Key and Base URL to switch providers.

---

## Channel Integrations

ShiYi supports access via platform gateways for Feishu and WeChat:

| Platform | Features |
|----------|----------|
| Feishu | Group mention replies, private chat, file send/receive |
| WeChat | Personal account QR login, private chat, file send/receive |

Start the gateway:

```bash
shiyi --dev gateway feishu   # Feishu
```

---

## CLI Commands

```bash
shiyi webui                      # Start Web Chat UI
shiyi --version                  # Show version

# Developer commands (require --dev)
shiyi --dev talk "hello"          # Full conversation
shiyi --dev recall "previous"     # Memory recall
shiyi --dev remember "remember"   # Memory store
shiyi --dev stats                 # Storage stats
shiyi --dev clerk validate <dir>  # Clerk validation
shiyi --dev steward status        # Steward dashboard
```

---

## Roadmap

- [x] Human-like memory engine (Fragment + 4D info + decay + association)
- [x] Two-LLM-call pipeline (intent + main response)
- [x] WebUI multi-session windows + streaming SSE
- [x] Clerk MCP remote communication + steward DAG scheduling
- [x] Feishu / WeChat channels
- [x] Skill system (search + install + routing)
- [ ] Memory visualization and replay
- [ ] Knowledge base support (document upload and retrieval)
- [ ] Memory import/export
- [ ] WebUI authentication
- [ ] Bishu Hall (clerk marketplace)
- [ ] PWA mobile
- [ ] Skill self-evolution (Fuzi closed loop)

---

## Contributing

Contributions are welcome. Read the [Contributing Guide](CONTRIBUTING.md) to get started.

You can:

- [Report an issue](https://github.com/shiyi-ai-tech/shiyi/issues) for bugs or suggestions
- [Submit a pull request](https://github.com/shiyi-ai-tech/shiyi/pulls) to fix or add features
- Develop a clerk and share it in Bishu Hall

## Security

If you find a security issue, see the [Security Policy](SECURITY.md).

## License

[MIT License](LICENSE) - use freely, keep attribution.

Copyright (c) 2026 LiGuo, LeGang

---

## Afterword

I am not a professional developer. I am an outsider who cannot write a single line of code. Up to the time of open sourcing, all development and testing were done by agents.

I built ShiYi because I experienced many problems with existing agents. After studying, I found these issues are caused by current agent architectures and cannot be avoided. The only workaround is brute force: longer context, more compression, more retrieval. It looks like the agent remembers and understands you, but it forgets you the next moment.

One day I asked my agent whether a new agent could be built by redesigning the full conversation flow and memory system. It told me the idea was fully feasible and it could help. So I, with no development experience, started my first software project.

From idea to open source release took about two weeks. I spent five days discussing the architecture and planning with the agent, then five days developing and testing. With no development experience, there were two major redesigns and about 30 iterations.

To professionals, my ShiYi may look naive and certainly has many issues. But I wanted to see if I could help change something, even a little, or at least share a way of thinking and a record of failure.

Thanks to everyone who reads this. I hope it helps you. Thank you.
