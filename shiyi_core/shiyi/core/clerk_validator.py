"""
ClerkValidator — 吏员开发规范校验器 (v0.17.2)
══════════════════════════════════════════

校验吏员目录是否符合史佚吏员框架规范：
  1. clerk.json 字段完整性
  2. 工具定义与 capabilities 一致性
  3. worker.py 存在且接口完整
  4. mcp_server.py 存在
  5. 可选：导入 worker.py 做烟雾测试

用法：
  from shiyi.core.clerk_validator import ClerkValidator
  v = ClerkValidator("/path/to/clerk-myclerk")
  result = v.validate()
  print(result.report())
"""

import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional


# ═══════════════════════════════════════════════════════
# clerk.json schema 定义
# ═══════════════════════════════════════════════════════

CLERK_JSON_REQUIRED_FIELDS = [
    "clerk_id",
    "name",
    "version",
    "description",
    "tools",
    "capabilities",
]

CLERK_JSON_OPTIONAL_FIELDS = [
    "enabled",
    "api_keys",
    "requires_llm",
    "knowledge_base",
    "skills",
    "created_at",
]

# clerk_id 合法格式：clerk_<alphannum>_<alphannum>_<inum>
CLERK_ID_PATTERN = re.compile(r"^clerk_[a-zA-Z][a-zA-Z0-9]*_[a-zA-Z][a-zA-Z0-9]*_\d+$")

# 语义版本号格式
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

# tool name 合法格式
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


# ═══════════════════════════════════════════════════════
# 校验结果
# ═══════════════════════════════════════════════════════

class ValidationResult:
    """单条校验结果"""

    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.passes: List[str] = []

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def add_pass(self, msg: str) -> None:
        self.passes.append(msg)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def report(self, verbose: bool = False) -> str:
        """生成校验报告"""
        lines = []
        total = len(self.passes) + len(self.warnings) + len(self.errors)

        if self.ok:
            lines.append(f"✅ 校验通过 ({total} 项检查，{len(self.warnings)} 个提醒)")
        else:
            lines.append(f"❌ 校验失败 ({len(self.errors)} 个错误，{len(self.warnings)} 个提醒)")

        for e in self.errors:
            lines.append(f"  ❌ {e}")

        for w in self.warnings:
            lines.append(f"  ⚠️  {w}")

        if verbose:
            for p in self.passes:
                lines.append(f"  ✅ {p}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════
# 校验器
# ═══════════════════════════════════════════════════════

class ClerkValidator:
    """吏员目录校验器"""

    def __init__(self, clerk_dir: str):
        self.clerk_dir = Path(clerk_dir)
        self.result = ValidationResult()

    def validate(self, smoke_test: bool = False) -> ValidationResult:
        """完整校验

        Args:
            smoke_test: 是否尝试 import worker.py 做烟雾测试（可能 import 失败如果依赖不全）
        """
        self.result = ValidationResult()

        # 第 1 层：目录存在
        if not self.clerk_dir.exists():
            self.result.add_error(f"目录不存在: {self.clerk_dir}")
            return self.result
        self.result.add_pass(f"目录存在: {self.clerk_dir.name}")

        # 第 2 层：必需文件
        self._check_required_files()

        # 第 3 层：clerk.json 字段
        self._validate_clerk_json()

        # 第 4 层：工具定义一致性
        self._validate_tools_consistency()

        # 第 5 层：worker.py 接口
        self._validate_worker_interface()

        # 第 6 层：数据边界校验（v0.17.2 新增）
        self._validate_data_boundary()

        # 第 7 层：烟雾测试（可选）
        if smoke_test:
            self._smoke_test()

        return self.result

    def _check_required_files(self) -> None:
        """检查必需文件是否存在"""
        for fname in ["clerk.json", "worker.py", "mcp_server.py"]:
            if (self.clerk_dir / fname).exists():
                self.result.add_pass(f"文件存在: {fname}")
            else:
                self.result.add_error(f"缺少文件: {fname}")

    def _validate_clerk_json(self) -> None:
        """校验 clerk.json 结构"""
        clerk_file = self.clerk_dir / "clerk.json"
        if not clerk_file.exists():
            return

        # 能解析
        try:
            with open(clerk_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            self.result.add_error(f"clerk.json 不是合法 JSON: {e}")
            return
        except Exception as e:
            self.result.add_error(f"clerk.json 读取失败: {e}")
            return
        self.result.add_pass("clerk.json 是合法 JSON")

        # 必填字段
        for field in CLERK_JSON_REQUIRED_FIELDS:
            if field in data:
                self.result.add_pass(f"必填字段 {field} 存在")
            else:
                self.result.add_error(f"缺少必填字段: {field}")

        # clerk_id 格式
        clerk_id = data.get("clerk_id", "")
        if clerk_id:
            if CLERK_ID_PATTERN.match(clerk_id):
                self.result.add_pass(f"clerk_id 格式正确: {clerk_id}")
            else:
                self.result.add_warning(
                    f"clerk_id 格式建议 'clerk_<开发者>_<功能>_<编号>': {clerk_id}"
                )

        # version 格式
        version = data.get("version", "")
        if version:
            if SEMVER_PATTERN.match(version):
                self.result.add_pass(f"version 格式正确: {version}")
            else:
                self.result.add_warning(f"version 不是语义版本号格式: {version}")

        # tools 是数组且非空
        tools = data.get("tools", [])
        if not isinstance(tools, list):
            self.result.add_error("tools 必须是 JSON 数组")
        elif len(tools) == 0:
            self.result.add_error("tools 数组不能为空（至少定义一个工具）")
        else:
            self.result.add_pass(f"tools 定义了 {len(tools)} 个工具")

        # capabilities 是数组且非空
        caps = data.get("capabilities", [])
        if not isinstance(caps, list):
            self.result.add_error("capabilities 必须是 JSON 数组")
        elif len(caps) == 0:
            self.result.add_error("capabilities 数组不能为空")

        # api_keys 是数组
        api_keys = data.get("api_keys")
        if api_keys is not None and not isinstance(api_keys, list):
            self.result.add_error("api_keys 必须是 JSON 数组")

        # requires_llm 是布尔
        rllm = data.get("requires_llm")
        if rllm is not None and not isinstance(rllm, bool):
            self.result.add_warning("requires_llm 应为布尔值 (true/false)")

        # skills 是数组
        skills = data.get("skills")
        if skills is not None and not isinstance(skills, list):
            self.result.add_error("skills 必须是 JSON 数组")

    def _validate_tools_consistency(self) -> None:
        """检查 tools 和 capabilities 一致"""
        clerk_file = self.clerk_dir / "clerk.json"
        if not clerk_file.exists():
            return

        try:
            with open(clerk_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        tool_names = [t["name"] for t in data.get("tools", []) if "name" in t]
        caps = data.get("capabilities", [])

        # tools[].name 与 capabilities 一一对应
        extra_tools = set(tool_names) - set(caps)
        extra_caps = set(caps) - set(tool_names)

        if not extra_tools and not extra_caps:
            self.result.add_pass("tools 与 capabilities 一一对应")
        else:
            if extra_tools:
                self.result.add_error(f"tools 中有但 capabilities 中无: {extra_tools}")
            if extra_caps:
                self.result.add_error(f"capabilities 中有但 tools 中无: {extra_caps}")

        # 每个 tool 的 inputSchema 校验
        for tool in data.get("tools", []):
            tname = tool.get("name", "?")
            schema = tool.get("inputSchema", {})

            if not isinstance(schema, dict):
                self.result.add_error(f"工具 {tname}: inputSchema 必须是对象")
                continue

            if schema.get("type") != "object":
                self.result.add_warning(f"工具 {tname}: inputSchema.type 应为 'object'")

            props = schema.get("properties", {})
            if not props:
                self.result.add_warning(f"工具 {tname}: inputSchema.properties 为空（工具无参数？）")

            # description 不为空
            desc = tool.get("description", "")
            if not desc or len(desc) < 5:
                self.result.add_warning(f"工具 {tname}: description 太短（LLM 需要好的描述来判断何时调用）")

        # tool name 格式
        for name in tool_names:
            if TOOL_NAME_PATTERN.match(name):
                self.result.add_pass(f"工具名格式正确: {name}")
            else:
                self.result.add_warning(f"工具名建议小写+下划线: {name}")

    def _validate_worker_interface(self) -> None:
        """检查 worker.py 是否有必需的接口"""
        worker_file = self.clerk_dir / "worker.py"
        if not worker_file.exists():
            return

        try:
            with open(worker_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            self.result.add_error("worker.py 读取失败")
            return

        self.result.add_pass("worker.py 可读取")

        # 检查必要的类/方法签名
        checks = [
            ("ClerkWorker", r"class ClerkWorker\b"),
            ("get_tools 方法", r"def get_tools\("),
            ("execute 方法", r"def execute\("),
            ("status 方法", r"def status\("),
            ("ClerkConfig", r"class ClerkConfig\b"),
            ("TOOL_REGISTRY", r"TOOL_REGISTRY\s*[:=]"),
        ]

        for label, pattern in checks:
            if re.search(pattern, content):
                self.result.add_pass(f"worker.py: {label} 存在")
            else:
                self.result.add_error(f"worker.py: 缺少 {label}")

    def _validate_data_boundary(self) -> None:
        """校验吏员数据边界合规（v0.17.2）

        规则：
        1. clerk.json 必须有 data_policy 字段
        2. file_write 必须限定 $SHIYI_WORKSPACE
        3. worker.py 不得初始化独立用户数据库
        4. knowledge_base 若存在必须声明为 operational
        """
        import re

        clerk_file = self.clerk_dir / "clerk.json"
        if not clerk_file.exists():
            return

        try:
            with open(clerk_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        # ── 第 1 层：data_policy 字段 ──
        data_policy = data.get("data_policy", "")
        valid_policies = {"shiyi_only", "operational"}
        if not data_policy:
            self.result.add_error(
                "数据边界: 缺少 data_policy 字段。"
                "必须声明为 'shiyi_only' 或 'operational'。"
                "详见 clerk.json 模板中的【数据边界规则】注释。"
            )
        elif data_policy not in valid_policies:
            self.result.add_error(
                f"数据边界: data_policy=\"{data_policy}\" 无效。"
                f"合法值: {', '.join(sorted(valid_policies))}"
            )
        else:
            self.result.add_pass(f"数据边界: data_policy=\"{data_policy}\"")

        # ── 第 2 层：file_write 工具限定 ──
        for tool in data.get("tools", []):
            tname = tool.get("name", "")
            if "write" in tname.lower() or "save" in tname.lower():
                path_desc = (
                    tool.get("inputSchema", {})
                    .get("properties", {})
                    .get("path", {})
                    .get("description", "")
                )
                workspace_refs = ["SHIYI_WORKSPACE", "shiyi/workspace", "~/.shiyi"]
                if not any(ref in path_desc for ref in workspace_refs):
                    self.result.add_error(
                        f"数据边界: 工具 {tname} 的 path 参数描述须明确 "
                        f"限定在 $SHIYI_WORKSPACE 内（当前: \"{path_desc[:60]}\"）"
                    )
                else:
                    self.result.add_pass(
                        f"数据边界: {tname} path 限定在史佚 workspace"
                    )

        # ── 第 3 层：worker.py 不初始化独立用户数据库 ──
        worker_file = self.clerk_dir / "worker.py"
        if not worker_file.exists():
            return

        try:
            with open(worker_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return

        # 检测独立数据库初始化（排除指向 ~/.shiyi 的）
        db_init_patterns = [
            (r"sqlite3\.connect\(", "SQLite 数据库连接"),
            (r"create_engine\(", "SQLAlchemy 数据库引擎"),
            (r"chromadb\.(Client|PersistentClient)\(", "ChromaDB 向量库"),
            (r"faiss\.(Index|write_index)\(", "FAISS 向量索引"),
            (r"lancedb\.connect\(", "LanceDB 向量库"),
            (r"pymongo\.MongoClient\(", "MongoDB 连接"),
            (r"redis\.(Redis|StrictRedis)\(", "Redis 连接"),
        ]

        shiyi_pattern = re.compile(r"(shiyi|SHIYI_WORKSPACE|/shiyi/)")

        for pattern, db_type in db_init_patterns:
            for match in re.finditer(pattern, content):
                # 取匹配行上下文（前后各 100 字符）
                start = max(0, match.start() - 100)
                end = min(len(content), match.end() + 200)
                context = content[start:end]

                if not shiyi_pattern.search(context):
                    # 不在 shiyi 路径下——可能存储用户数据
                    line_num = content[:match.start()].count("\n") + 1
                    self.result.add_error(
                        f"数据边界: worker.py 第 {line_num} 行发现 {db_type}，"
                        f"路径不在 ~/.shiyi/ 下。吏员禁止本地存储用户数据。"
                        f"若为纯 operational 状态（非用户内容），请在 data_policy 中声明 'operational'。"
                    )

        self.result.add_pass("数据边界: worker.py 无独立用户数据库")

        # ── 第 4 层：knowledge_base 检查 ──
        kb = data.get("knowledge_base", "")
        if kb:
            comment_context = ""
            for key in data:
                if key.startswith("_comment") and "data_policy" in str(data.get(key, "")):
                    comment_context = str(data[key])
            if "operational" not in data_policy:
                self.result.add_warning(
                    f"数据边界: knowledge_base=\"{kb}\" 存在但 data_policy 非 'operational'。"
                    f"如 knowledge_base 仅存吏员运行状态（非用户数据），"
                    f"请将 data_policy 改为 'operational'。"
                )

    def _smoke_test(self) -> None:
        """尝试导入 worker.py 做烟雾测试"""
        import sys
        import io
        from contextlib import redirect_stdout, redirect_stderr

        clerk_dir_str = str(self.clerk_dir.resolve())
        if clerk_dir_str not in sys.path:
            sys.path.insert(0, clerk_dir_str)

        try:
            # 静默导入
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                # 动态导入
                import importlib
                worker = importlib.import_module("worker")

                # 实例化
                cw = worker.ClerkWorker()

                # 检查 tools
                tools = cw.get_tools()
                if len(tools) > 0:
                    self.result.add_pass(f"烟雾测试: get_tools() 返回 {len(tools)} 个工具")
                else:
                    self.result.add_warning("烟雾测试: get_tools() 返回 0 个工具")

                # 检查 status
                st = cw.status()
                if st.get("clerk_id"):
                    self.result.add_pass(f"烟雾测试: status() 返回 clerk_id={st['clerk_id']}")
                else:
                    self.result.add_error("烟雾测试: status() 缺少 clerk_id")

        except ImportError as e:
            self.result.add_warning(f"烟雾测试: worker.py 导入失败（可能缺少依赖）: {e}")
        except Exception as e:
            self.result.add_error(f"烟雾测试: 运行时异常: {e}")
        finally:
            if clerk_dir_str in sys.path:
                sys.path.remove(clerk_dir_str)


# ═══════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════

def validate_clerk_dir(clerk_dir: str, smoke_test: bool = False) -> ValidationResult:
    """便捷入口：校验一个吏员目录"""
    return ClerkValidator(clerk_dir).validate(smoke_test=smoke_test)
