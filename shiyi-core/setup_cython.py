"""shiyi-core Cython 编译脚本 — 在 shiyi-core 目录运行"""
import sys
from pathlib import Path
from Cython.Build import cythonize
from setuptools import setup, Extension

CORE_ROOT = Path(__file__).parent
SHIYI_DIR = CORE_ROOT / "shiyi"

# 收集所有 .py 文件
py_files = sorted(SHIYI_DIR.rglob("*.py"))
print(f"找到 {len(py_files)} 个 .py 文件")

# 构建 Extension 列表
extensions = []
for py_file in py_files:
    rel = py_file.relative_to(CORE_ROOT)  # shiyi/core/clerk_registry.py
    module_path = str(rel.with_suffix("")).replace("/", ".")  # shiyi.core.clerk_registry
    extensions.append(Extension(module_path, [str(py_file)]))

# Cythonize + build
ext_modules = cythonize(
    extensions,
    language_level="3",
    compiler_directives={"boundscheck": False, "wraparound": False},
    nthreads=4,
)

sys.argv = ["setup.py", "build_ext", "--inplace"]
setup(name="shiyi-core-cython", ext_modules=ext_modules)

# 清理：删除 .py 源码（保留 __init__.py 空壳）
for py_file in py_files:
    if py_file.name == "__init__.py":
        py_file.write_text("# Cython compiled\n")
    else:
        py_file.unlink()

# 清理 .c 中间文件
for c_file in SHIYI_DIR.rglob("*.c"):
    c_file.unlink()

so_files = sorted(SHIYI_DIR.rglob("*.so"))
print(f"\n✅ Cython 编译完成 — {len(so_files)} 个 .so")
