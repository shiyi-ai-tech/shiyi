# 贡献指南

感谢你对史佚 (ShiYi) 的关注！欢迎任何形式的贡献。

## 如何贡献

### 报告 Bug

1. 在 [Issues](https://github.com/anty0418/shiyi-tongxun/issues) 页面搜索是否已有相同问题
2. 如果没有，创建新 Issue，包含：
   - **Bug 描述**：发生了什么
   - **复现步骤**：如何触发
   - **期望行为**：应该怎样
   - **环境信息**：Python 版本、操作系统
   - **日志输出**：相关的错误信息

### 提交 Pull Request

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/my-feature`
3. 提交改动：`git commit -m "feat: 添加XXX功能"`
4. 推送分支：`git push origin feature/my-feature`
5. 提交 Pull Request

### 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式：

| 前缀 | 用途 |
|------|------|
| `feat:` | 新功能 |
| `fix:` | 修复 Bug |
| `docs:` | 文档变更 |
| `refactor:` | 重构（不新增功能、不修复 Bug） |
| `test:` | 测试相关 |
| `chore:` | 构建/工具变更 |

### 开发吏员

吏员是史佚的扩展单元，开发新吏员是最佳的贡献方式：

1. 使用 `shiyi-shell/shiyi/shell/clerk-template/` 作为模板
2. 编辑 `clerk.json`（吏员配置）和 `worker.py`（工作逻辑）
3. 用 `shiyi --dev clerk validate <目录> --smoke-test` 验证
4. 提交 PR 或在辟署馆分享

## 开发环境搭建

```bash
# 克隆仓库
git clone https://github.com/anty0418/shiyi-tongxun.git
cd shiyi-tongxun

# 安装依赖（可编辑模式）
pip install -e ./shiyi-common
pip install -e ./shiyi-providers
pip install -e ./shiyi-core
pip install -e ./shiyi-shell

# 配置 API Key
cp sample.env .env
# 编辑 .env 填入你的 DEEPSEEK_API_KEY

# 运行测试
python -m pytest tests/ -v

# 启动开发服务器
shiyi webui
```

## 代码风格

- Python 3.10+ 兼容
- 遵循 PEP 8
- 类型注解推荐但不强制
- 中文注释和文档字符串欢迎
- 每个模块文件顶部应有简短的职责说明

## 项目结构

```
shiyi-tongxun/
├── shiyi-common/       # 公共类型与接口
├── shiyi-providers/    # LLM / Embedding / MCP 调用器
├── shiyi-core/         # 核心引擎（记忆、感知、决策、吏员调度）
├── shiyi-shell/        # 外壳层（WebUI、CLI、吏员实现、网关）
├── tests/              # 测试用例
├── docs/               # 文档和截图
└── scripts/            # 辅助脚本
```

## 许可证

提交贡献即表示你同意代码在 MIT License 下发布。
