#!/bin/bash
# 史佚 ShiYi — WSL 安装脚本
# Developed by LiGuo LeGang
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
echo -e "${CYAN}╔══════════════════════════════════╗${NC}"
echo -e "${CYAN}║  史佚 ShiYi — WSL 安装          ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════╝${NC}"

INSTALL_DIR="$1"
if [ -z "$INSTALL_DIR" ]; then
    INSTALL_DIR=$(pwd)
fi

echo "安装目录: $INSTALL_DIR"
echo ""

# 安装系统依赖（非必须，失败不阻塞）
echo ">>> 检查系统依赖..."
if command -v apt-get &>/dev/null; then
    sudo -n apt-get update -qq 2>/dev/null || true
    sudo -n apt-get install -y -qq python3-pip python3-venv 2>/dev/null || true
fi
# 确保 pip 可用
command -v pip3 &>/dev/null || command -v pip &>/dev/null || {
    echo "⚠ 未找到 pip，尝试安装..."
    python3 -m ensurepip --upgrade 2>/dev/null || true
}

# 安装源码包（按依赖顺序：common → providers → core → shell）
echo ">>> 安装史佚四包..."
cd "$INSTALL_DIR"

for pkg in shiyi-common shiyi-providers shiyi-core shiyi-shell; do
    dir="${pkg}"
    if [ -d "$dir" ]; then
        echo "  安装 $pkg ..."
        pip install --break-system-packages -e "$dir" >/dev/null 2>&1 || pip install -e "$dir" >/dev/null 2>&1
    fi
done

# 安装额外依赖
pip install --break-system-packages hnswlib numpy requests jieba fastapi uvicorn python-multipart pydantic >/dev/null 2>&1 || \
pip install hnswlib numpy requests jieba fastapi uvicorn python-multipart pydantic >/dev/null 2>&1 || true

# 验证
echo ""
if command -v shiyi &>/dev/null; then
    version=$(shiyi --version 2>&1)
    echo -e "${GREEN}✓ 史佚安装成功！版本: $version${NC}"
else
    echo -e "${RED}⚠ shiyi 命令未找到，请检查安装${NC}"
    exit 1
fi

# 检查吏员
python3 -c "from pathlib import Path; import shiyi.shell.webui; p=Path(shiyi.shell.webui.__file__).parent/'clerk-default'; print('吏员:', '已就绪' if p.exists() else '缺失')"

echo ""
echo -e "${GREEN}安装完成。${NC}"
echo ""

# 安装 watchdog 守护进程
echo ">>> 安装 自进化 Watchdog 守护进程..."
WATCHDOG_SERVICE="$INSTALL_DIR/shiyi-watchdog.service"
WATCHDOG_SCRIPT="$INSTALL_DIR/watchdog.py"
if [ -f "$WATCHDOG_SERVICE" ] && [ -f "$WATCHDOG_SCRIPT" ]; then
    # 复制 watchdog 脚本到安全位置
    cp "$WATCHDOG_SCRIPT" ~/.shiyi/watchdog.py
    chmod +x ~/.shiyi/watchdog.py
    # 安装 systemd 服务
    mkdir -p ~/.config/systemd/user/
    cp "$WATCHDOG_SERVICE" ~/.config/systemd/user/
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable --now shiyi-watchdog 2>/dev/null || true
    echo -e "${GREEN}✓ Watchdog 已安装并启动${NC}"
else
    echo -e "${RED}⚠ 未找到 watchdog 文件${NC}"
fi
echo "重要: 首次使用请在 ~/.shiyi/.env 配置 API Key"
echo "  DEEPSEEK_API_KEY=sk-your-key"
echo "  SILICONFLOW_API_KEY=sk-your-key"
echo ""
echo "启动命令: shiyi webui"
echo "本地访问: http://localhost:8520"
