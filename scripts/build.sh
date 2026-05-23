#!/bin/bash
# 史佚 (ShiYi) 发布构建脚本 — 构建三包 wheel 并验证
# 用法: bash build.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/dist"
VENV_DIR="/tmp/shiyi-test-venv-$$"

echo "=== 1. 清理旧构建 ==="
rm -rf "$BUILD_DIR" build/ *.egg-info shiyi-*/build shiyi-*/*.egg-info

# 确保 bin 脚本存在
if ! command -v shiyi &>/dev/null; then
    echo "⚠  shiyi CLI not on PATH, installing dev version..."
    pip install -e "$PROJECT_DIR/shiyi-common" -e "$PROJECT_DIR/shiyi-core" -e "$PROJECT_DIR/shiyi-shell" -q
fi

echo "=== 2. 运行回归测试 ==="
python3 -m pytest tests/test_v0143_regression.py -v --tb=short -q 2>&1 || {
    echo "❌ 测试失败，终止构建"
    exit 1
}

echo "=== 3. 构建 wheel ==="
mkdir -p "$BUILD_DIR"

for pkg in shiyi-common shiyi-core shiyi-shell; do
    echo "  构建 $pkg..."
    cd "$PROJECT_DIR/$pkg"
    python3 -m build --wheel -o "$BUILD_DIR" 2>&1 | tail -1
done

cd "$PROJECT_DIR"
echo "  产出:"
ls -lh "$BUILD_DIR"/*.whl

echo "=== 4. 隔离环境验证 ==="
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

# 安装依赖
pip install "$BUILD_DIR"/shiyi_common-*.whl -q
pip install "$BUILD_DIR"/shiyi_core-*.whl -q
pip install "$BUILD_DIR"/shiyi_shell-*.whl -q
pip install hnswlib numpy jieba -q 2>&1 | tail -1

# 验证
echo "  导入验证..."
python3 -c "from shiyi.engine import Shiyi; print('  ✓ shiyi.engine.Shiyi')" || { echo "❌ Import failed"; exit 1; }
python3 -c "from shiyi.memory.engine import MemoryEngine; print('  ✓ shiyi.memory.MemoryEngine')" || { echo "❌ MemoryEngine failed"; exit 1; }
python3 -c "from shiyi.shell.cli import VERSION; assert VERSION == '0.14.3', 'Version mismatch'; print(f'  ✓ Version {VERSION}')"

# CLI 验证
shiyi --version 2>&1 | grep -q "0.14.3" || { echo "❌ CLI --version failed"; exit 1; }
echo "  ✓ shiyi --version"

# 跑回归测试（在隔离环境中）
echo "  回归测试..."
python3 -c "
from shiyi.memory.engine import MemoryEngine
import tempfile, os, time

d = tempfile.mkdtemp()
e = MemoryEngine(db_path=os.path.join(d,'t.db'), index_path=os.path.join(d,'i.bin'), embedding_dim=0)

# 核心 CRUD
assert e.remember('用户叫小明')
time.sleep(0.1)
r = e.recall('小明', top_k=5)
assert len(r) > 0, 'recall failed'
print('  ✓ core CRUD smoke test')

# 内容校验
assert not e.remember('')
assert not e.remember('!@#$%')
print('  ✓ content validation')

# 统计
s = e.stats()
assert 'cache' in s, 'stats missing cache'
print('  ✓ stats')
" 2>&1

echo "=== 5. 构建成功 ==="
echo "Wheel 位置: $BUILD_DIR/"
ls -lh "$BUILD_DIR"/*.whl

# 清理
deactivate 2>/dev/null || true
rm -rf "$VENV_DIR"
