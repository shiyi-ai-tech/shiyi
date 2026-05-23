# 工作沙箱目录说明

## 概述

吏员的工作目录（Workspace）是隔离的安全环境，所有文件操作必须在此目录内进行。

## 沙箱路径

```
~/.shiyi/workspace/
```

即用户主目录下的 `.shiyi/workspace` 子目录。

## 设计目的

1. **安全隔离**: 防止吏员访问敏感系统文件
2. **路径控制**: 防止路径穿越攻击
3. **权限限制**: 最小权限原则

## 目录结构

```
~/.shiyi/workspace/
├── clerk-default/      # 默认吏员工作目录（可选）
│   ├── temp/          # 临时文件
│   ├── cache/         # 缓存文件
│   └── output/        # 输出文件
└── 其他文件...
```

## 使用规范

### 1. 路径规范

- **相对路径**: 相对于沙箱根目录，如 `test.txt` 等价于 `~/.shiyi/workspace/test.txt`
- **绝对路径**: 必须是沙箱内的绝对路径，如 `/home/user/.shiyi/workspace/test.txt`

### 2. 路径穿越防护

以下路径将被拒绝：
- `../../etc/passwd`
- `~/.ssh/id_rsa`
- `/etc/shadow`
- 任何指向沙箱外部的路径

### 3. 目录操作

- 读取目录时，返回目录内文件列表
- 写入文件时，自动创建父目录
- 不允许覆盖目录为文件

## 配置

通过环境变量 `CLERK_WORKSPACE` 可自定义沙箱路径：

```bash
export CLERK_WORKSPACE=/path/to/custom/workspace/
```

## 注意事项

- 沙箱目录会在首次使用时自动创建
- 确保运行用户对沙箱目录有读写权限
- 定期清理临时文件以节省空间
