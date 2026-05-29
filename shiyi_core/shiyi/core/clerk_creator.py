"""clerk_creator - 吏员按需创建系统 (v0.19.0 Phase 4)

提供交互式和非交互式创建吏员的功能。

Usage:
    from shiyi.core.clerk_creator import ClerkCreator
    creator = ClerkCreator()
    result = creator.create_interactive()  # 交互式
    result = creator.create_non_interactive(name, desc, tools, ...)  # 非交互式
"""

import json
import os
import re
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from shiyi.common.constants import (
    DEFAULT_LLM_PROVIDER,
    DEFAULT_LIGHT_LLM_MODEL,
    DEFAULT_LLM_BASE_URL,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 模板复制
# ═══════════════════════════════════════════════════════

def get_clerk_template_dir() -> Path:
    """获取吏员模板目录"""
    # 从当前文件位置推断模板目录
    current = Path(__file__).parent.parent.parent.parent
    template_dir = current / "shiyi-shell" / "shiyi" / "shell" / "clerk-template"
    if template_dir.exists():
        return template_dir
    
    # 回退到相对于home的路径
    home_template = Path.home() / ".shiyi" / "clerk-template"
    if home_template.exists():
        return home_template
    
    return template_dir  # 返回预期位置，由调用者处理不存在的情况


def get_default_mcp_template() -> str:
    """获取默认mcp_server.py模板内容 — JSON-RPC 2.0 + SSE (v1.0)"""
    return r'''"""MCP Server — v1.0 JSON-RPC 2.0 + SSE

通过 stdin/stdout JSON-RPC 2.0 暴露吏员工具、任务、健康管理。
通过 fd 3 发送 SSE 事件（心跳、进度、完成通知）。

方法：
  initialize, tools/list, tools/call, ping
  agent/run, agent/cancel
  task/execute, task/cancel, task/status (v1.0)
  clerk/health, clerk/shutdown (v1.0)
  memory/recall, memory/remember, memory/context (v1.0 proxy)
"""
import json
import sys
import os
import re
import time
import uuid
import threading
from pathlib import Path

CLERK_DIR = Path(__file__).parent
sys.path.insert(0, str(CLERK_DIR))
from worker import ClerkWorker

# SSE 输出 (fd 3)
_SSE_FD = None
_SSE_FILE = None

def _init_sse():
    global _SSE_FD, _SSE_FILE
    sse_fd_str = os.environ.get("CLERK_SSE_FD", "3")
    try:
        sse_fd = int(sse_fd_str)
        _SSE_FILE = os.fdopen(sse_fd, "w", buffering=1)
        _SSE_FD = _SSE_FILE.fileno()
        return True
    except (OSError, ValueError):
        return False

def _sse_event(event: str, data: dict):
    if _SSE_FD is None:
        return
    try:
        payload = json.dumps(data, ensure_ascii=False)
        msg = f"event: {event}\ndata: {payload}\n\n"
        os.write(_SSE_FD, msg.encode("utf-8"))
    except Exception:
        pass


class ClerkMCP:
    """JSON-RPC 2.0 MCP Server — v1.0"""

    def __init__(self, config_path=None):
        cfg = config_path or str(CLERK_DIR / "clerk.json")
        self.worker = ClerkWorker(cfg)
        self._session_id = None
        self._tasks = {}
        self._task_counter = 0
        self._current_task_id = None
        self._task_results: Dict[str, Dict[str, Any]] = {}  # task_id → {status, result, error}
        self._shutting_down = False
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    def _write(self, msg: dict):
        with self._write_lock:
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def run(self):
        for line in sys.stdin:
            if self._shutting_down:
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._dispatch(req)

    def _dispatch(self, req: dict):
        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params", {})
        if req_id is None:
            return
        try:
            handler = {
                "initialize": self._handle_init,
                "tools/list": self._handle_tools_list,
                "tools/call": self._handle_tools_call,
                "agent/run": self._handle_agent_run,
                "agent/cancel": self._handle_agent_cancel,
                "task/execute": self._handle_task_execute,
                "task/cancel": self._handle_task_cancel,
                "task/status": self._handle_task_status,
                "clerk/health": self._handle_clerk_health,
                "clerk/shutdown": self._handle_clerk_shutdown,
                "memory/recall": self._handle_memory_recall,
                "memory/remember": self._handle_memory_remember,
                "memory/context": self._handle_memory_context,
                "ping": self._handle_ping,
            }.get(method)
            if handler is None:
                self._write({"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown method: {method}"}})
                return
            result = handler(params)
            self._write({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as e:
            self._write({"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32603, "message": str(e)}})

    # ── Handlers ──────────────────────────────────────

    def _handle_init(self, params: dict) -> dict:
        self._session_id = f"sess_{uuid.uuid4().hex[:12]}"
        clerk_id = params.get("clerk_id", self.worker.config.clerk_id)
        t = threading.Thread(target=self._heartbeat_loop, daemon=True)
        t.start()
        _sse_event("notifications/initialized", {"clerk_id": clerk_id, "session_id": self._session_id})
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": f"clerk-{clerk_id}", "version": self.worker.config.version},
            "session_id": self._session_id,
            "capabilities": {"tools": {}, "memory": {"read": True, "write": True}, "tasks": {}},
        }

    def _handle_tools_list(self, params=None) -> dict:
        return {"tools": self.worker.get_tools()}

    def _handle_tools_call(self, params: dict) -> dict:
        r = self.worker.execute(params.get("name", ""), params.get("arguments", {}))
        return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)}], "isError": not r.get("success", True)}

    def _handle_ping(self, params=None) -> dict:
        return {"pong": True}

    # ── Agent ─────────────────────────────────────────

    def _handle_agent_run(self, params: dict) -> dict:
        self._task_counter += 1
        task_id = f"task_{self._task_counter}"
        with self._lock:
            self._current_task_id = task_id
        task = params.get("prompt", params.get("task", ""))
        def _run():
            try:
                _sse_event("notifications/progress", {"task_id": task_id, "percent": 0, "message": f"{self.worker.config.name} 开始执行..."})
                result = self.worker.run_agent_loop(task=task, skills=params.get("skills"), max_iterations=params.get("max_iterations"), timeout=params.get("timeout"))
                if result.get("success"):
                    _sse_event("notifications/complete", {"task_id": task_id, "result": result.get("result", ""), "iterations": result.get("iterations", 0)})
                    with self._lock:
                        self._task_results[task_id] = {"status": "done", "result": result.get("result", "")}
                else:
                    _sse_event("notifications/error", {"task_id": task_id, "code": "FAILED", "message": result.get("error", "")})
                    with self._lock:
                        self._task_results[task_id] = {"status": "failed", "error": result.get("error", "执行失败")}
            except Exception as e:
                _sse_event("notifications/error", {"task_id": task_id, "code": "CRASH", "message": str(e)})
                with self._lock:
                    self._task_results[task_id] = {"status": "failed", "error": str(e)}
            finally:
                with self._lock:
                    self._tasks.pop(task_id, None)
                    if self._current_task_id == task_id:
                        self._current_task_id = None
        t = threading.Thread(target=_run, daemon=True)
        with self._lock:
            self._tasks[task_id] = t
        t.start()
        return {"task_id": task_id, "status": "started"}

    def _handle_agent_cancel(self, params: dict) -> dict:
        tid = params.get("task_id", "")
        with self._lock:
            t = self._tasks.pop(tid, None)
        return {"task_id": tid, "status": "cancelled" if t else "not_found"}

    # ── Tasks (v1.0) ──────────────────────────────────

    def _handle_task_execute(self, params: dict) -> dict:
        task_id = params.get("task_id", f"task_{uuid.uuid4().hex[:12]}")
        with self._lock:
            self._current_task_id = task_id
        desc = params.get("description", "")
        skill = params.get("skill")
        inp = params.get("input", {})
        timeout_val = params.get("timeout", 300)
        def _run():
            try:
                _sse_event("notifications/progress", {"task_id": task_id, "percent": 0, "message": f"任务已接收: {desc[:100]}"})
                result = self.worker.run_agent_loop(task=f"{desc}\n\n输入：{json.dumps(inp, ensure_ascii=False)[:2000]}", skills=[skill] if skill else None, timeout=timeout_val)
                if result.get("success"):
                    raw_result = result.get("result")
                    result_str = str(raw_result) if raw_result is not None else ""
                    new_fragments = [{"content": result_str[:500], "importance": 0.7, "source": self.worker.config.clerk_id}] if result_str else []
                    _sse_event("notifications/complete", {"task_id": task_id, "result": result_str, "iterations": result.get("iterations", 0), "new_fragments": new_fragments})
                    with self._lock:
                        self._task_results[task_id] = {"status": "done", "result": result_str}
                else:
                    _sse_event("notifications/error", {"task_id": task_id, "code": "FAILED", "message": result.get("error", "")})
                    with self._lock:
                        self._task_results[task_id] = {"status": "failed", "error": result.get("error", "执行失败")}
            except Exception as e:
                _sse_event("notifications/error", {"task_id": task_id, "code": "CRASH", "message": str(e)})
                with self._lock:
                    self._task_results[task_id] = {"status": "failed", "error": str(e)}
        t = threading.Thread(target=_run, daemon=True)
        with self._lock:
            self._tasks[task_id] = t
        t.start()
        return {"accepted": True, "task_id": task_id}

    def _handle_task_cancel(self, params: dict) -> dict:
        tid = params.get("task_id", "")
        with self._lock:
            if tid == self._current_task_id:
                self._current_task_id = None
                return {"cancelled": True, "task_id": tid}
        return {"cancelled": False, "error": "not_found"}

    def _handle_task_status(self, params: dict) -> dict:
        tid = params.get("task_id", "")
        with self._lock:
            # 先查已完成的
            if tid in self._task_results:
                return {"task_id": tid, **self._task_results[tid]}
            # 正在运行的
            if tid == self._current_task_id:
                return {"task_id": tid, "status": "running", "clerk_id": self.worker.config.clerk_id}
        return {"task_id": tid, "status": "unknown"}

    # ── Health (v1.0) ─────────────────────────────────

    def _handle_clerk_health(self, params: dict) -> dict:
        return {"status": "shutting_down" if self._shutting_down else "healthy", "clerk_id": self.worker.config.clerk_id, "current_task": self._current_task_id, "session_id": self._session_id}

    def _handle_clerk_shutdown(self, params: dict) -> dict:
        self._shutting_down = True
        _sse_event("notifications/error", {"task_id": self._current_task_id, "code": "CLERK_SHUTDOWN", "message": f"关闭: {params.get('reason', 'user_request')}"})
        return {"shutting_down": True, "reason": params.get("reason", "user_request")}

    # ── Memory proxy (v1.0) ───────────────────────────

    def _handle_memory_recall(self, params: dict) -> dict:
        return {"_proxy": True, "tool": "memory/recall", "params": params}
    def _handle_memory_remember(self, params: dict) -> dict:
        return {"_proxy": True, "tool": "memory/remember", "params": params}
    def _handle_memory_context(self, params: dict) -> dict:
        return {"_proxy": True, "tool": "memory/context", "params": params}

    # ── Heartbeat ─────────────────────────────────────

    def _heartbeat_loop(self):
        while not self._shutting_down:
            time.sleep(30)
            if not self._shutting_down:
                _sse_event("notifications/heartbeat", {"clerk_id": self.worker.config.clerk_id, "status": "running", "current_task": self._current_task_id, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})


if __name__ == "__main__":
    _init_sse()
    config_path = sys.argv[1] if len(sys.argv) > 1 else str(CLERK_DIR / "clerk.json")
    ClerkMCP(config_path).run()
'''


def get_default_soul_md(name: str, description: str) -> str:
    """生成默认soul.md内容"""
    return f'''# {name} - 吏员人格档案

## 身份
- **名称**: {name}
- **角色**: 专业工具执行者
- **数据边界**: shiyi_only

## 行为准则
1. 专注于执行分配的工具任务
2. 严格按照参数schema执行
3. 执行结果通过标准格式返回
4. 不主动发起网络请求
5. 不存储用户数据到本地

## 能力描述
{description}

## 限制
- 仅在 $SHIYI_WORKSPACE 内操作文件
- API Key 从环境变量读取
- 不输出敏感信息到日志
'''


# ═══════════════════════════════════════════════════════
# 工具定义验证
# ═══════════════════════════════════════════════════════

def validate_tool_definition(tool: Dict[str, Any]) -> Tuple[bool, str]:
    """验证工具定义是否符合规范"""
    if not isinstance(tool, dict):
        return False, "工具必须是字典"
    
    # 检查必填字段
    if "name" not in tool:
        return False, "缺少name字段"
    if "description" not in tool:
        return False, "缺少description字段"
    if "inputSchema" not in tool:
        return False, "缺少inputSchema字段"
    
    # 验证name格式
    name = tool["name"]
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        return False, f"工具名'{name}'必须是小写字母开头，只含字母、数字、下划线"
    
    # 验证inputSchema
    schema = tool["inputSchema"]
    if not isinstance(schema, dict):
        return False, "inputSchema必须是字典"
    if schema.get("type") != "object":
        return False, "inputSchema.type必须是'object'"
    
    return True, "OK"


def validate_clerk_config(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """验证吏员配置"""
    errors = []
    
    # 必填字段
    required = ["clerk_id", "name", "version", "description", "tools"]
    for field in required:
        if field not in config:
            errors.append(f"缺少必填字段: {field}")
    
    # 验证clerk_id格式
    clerk_id = config.get("clerk_id", "")
    if clerk_id and not re.match(r"^clerk_\d+$", clerk_id):
        errors.append(f"clerk_id格式错误，应为 clerk_001 格式: {clerk_id}")
    
    # 验证工具定义
    tools = config.get("tools", [])
    if not isinstance(tools, list):
        errors.append("tools必须是数组")
    else:
        for i, tool in enumerate(tools):
            valid, msg = validate_tool_definition(tool)
            if not valid:
                errors.append(f"工具[{i}] {msg}")
    
    # 验证capabilities与tools对应
    caps = set(config.get("capabilities", []))
    tool_names = {t.get("name") for t in tools}
    missing = caps - tool_names
    if missing:
        errors.append(f"capabilities中定义的工具不存在: {missing}")
    
    return len(errors) == 0, errors


# ═══════════════════════════════════════════════════════
# 交互式创建
# ═══════════════════════════════════════════════════════

def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """询问是否问题"""
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        response = input(prompt + suffix).strip().lower()
        if not response:
            return default
        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False
        print("请输入 y 或 n")


def ask_choice(prompt: str, options: List[str], default: int = 0) -> int:
    """让用户选择"""
    print(prompt)
    for i, opt in enumerate(options):
        marker = " (默认)" if i == default else ""
        print(f"  [{i}] {opt}{marker}")
    
    while True:
        response = input(f"请选择 [0-{len(options)-1}]: ").strip()
        if not response:
            return default
        try:
            idx = int(response)
            if 0 <= idx < len(options):
                return idx
        except ValueError:
            pass
        print(f"请输入 0 到 {len(options)-1} 之间的数字")


def ask_tools() -> List[Dict[str, Any]]:
    """交互式收集工具定义"""
    tools = []
    
    print("\n━━━ 工具定义 ━━")
    print("每个工具需要：名称、描述、参数schema")
    print("按回车结束工具定义，输入 n 跳过剩余工具\n")
    
    while True:
        name = input("工具名称 (英文小写+下划线, 空输入结束): ").strip()
        if not name:
            break
        
        # 验证名称格式
        if not re.match(r"^[a-z][a-z0-9_]*$", name):
            print(f"⚠️  名称格式错误，重新输入")
            continue
        
        description = input("工具描述: ").strip()
        if not description:
            print("⚠️  描述不能为空")
            continue
        
        # 参数定义
        print("━━━ 参数定义 (空输入结束) ━━")
        params = {"type": "object", "properties": {}, "required": []}
        required_params = []
        
        while True:
            param_name = input("  参数名称 (空输入结束): ").strip()
            if not param_name:
                break
            
            param_type = input("  参数类型 [string/number/integer/boolean] (默认string): ").strip() or "string"
            if param_type not in ("string", "number", "integer", "boolean"):
                param_type = "string"
            
            param_desc = input("  参数描述: ").strip()
            
            param_required = ask_yes_no("  是否必填?", default=True)
            if param_required:
                required_params.append(param_name)
            
            params["properties"][param_name] = {
                "type": param_type,
                "description": param_desc
            }
        
        params["required"] = required_params
        
        tool = {
            "name": name,
            "description": description,
            "inputSchema": params
        }
        tools.append(tool)
        
        if not ask_yes_no("继续添加工具?"):
            break
    
    return tools


def ask_api_keys() -> List[str]:
    """交互式收集需要的API Key"""
    print("\n━━━ API Key 配置 ━━")
    print("常见的API Key类型（直接回车结束）:\n")
    
    common_keys = [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY", 
        "GOOGLE_API_KEY",
        "AZURE_OPENAI_KEY",
        "HUGGINGFACE_TOKEN",
        "COZE_API_KEY",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
    ]
    
    selected = []
    for key in common_keys:
        if ask_yes_no(f"需要 {key}?", default=False):
            selected.append(key)
    
    print(f"\n已选择: {selected if selected else '无'}")
    return selected


# ═══════════════════════════════════════════════════════
# 创建核心
# ═══════════════════════════════════════════════════════

def generate_clerk_id(clerks_dir: Optional[str] = None) -> str:
    """自动生成clerk_id，格式: clerk_001, clerk_002, ...
    
    基于已有吏员目录自增序号，不依赖name。name是用户可见的中英文名称，clerk_id是内部标识。
    """
    base_dir = Path(clerks_dir) if clerks_dir else Path.home() / ".shiyi" / "clerks"
    
    # 扫描已有clerk_id的最大序号
    max_seq = 0
    if base_dir.exists():
        for d in base_dir.iterdir():
            if d.is_dir() and d.name.startswith("clerk_"):
                m = re.search(r"clerk_(\d+)$", d.name)
                if m:
                    seq = int(m.group(1))
                    if seq > max_seq:
                        max_seq = seq
    
    return f"clerk_{max_seq + 1:03d}"


class ClerkCreator:
    """吏员创建器"""
    
    def __init__(self, output_dir: Optional[str] = None):
        """
        Args:
            output_dir: 吏员输出目录，默认 ~/.shiyi/clerks/<clerk_id>/
        """
        self.output_base = Path(output_dir) if output_dir else Path.home() / ".shiyi" / "clerks"
        self.output_base.mkdir(parents=True, exist_ok=True)
    
    def _tool_name_from_id(self, clerk_id: str) -> str:
        """从clerk_id生成工具名前缀，如 clerk_001 -> tool_001"""
        m = re.search(r"(\d+)$", clerk_id)
        seq = m.group(1) if m else "001"
        return f"tool_{seq}"
    
    def create_interactive(self) -> Dict[str, Any]:
        """交互式创建吏员"""
        print("\n" + "═" * 50)
        print("  史佚吏员创建向导")
        print("═" * 50)
        
        # 1. 名称
        print("\n━━━ 基础信息 ━━")
        name = input("吏员名称 (中英文均可): ").strip()
        if not name:
            return {"success": False, "error": "名称不能为空"}
        
        # 2. 描述
        desc = input("吏员描述: ").strip()
        if not desc:
            desc = f"{name} - 专业工具执行者"
        
        # 3. 版本
        version = input("版本号 (默认 0.1.0): ").strip() or "0.1.0"
        if not re.match(r"^\d+\.\d+\.\d+$", version):
            version = "0.1.0"
        
        # 4. 能力类型
        cap_type = ask_choice(
            "能力类型:",
            ["通用", "多模态（视觉/语音）", "专业领域"],
            default=0
        )
        cap_types = ["general", "multimodal", "specialized"]
        selected_cap = cap_types[cap_type]
        
        # 5. 工具定义
        tools = ask_tools()
        if not tools:
            print("⚠️  未定义工具，创建一个默认占位工具")
            # clerk_id此时还没生成，先用临时标记
            tools = [{
                "name": "execute",
                "description": f"执行{name}相关操作",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作描述"}
                    },
                    "required": ["action"]
                }
            }]
        
        # 6. 是否需要LLM
        requires_llm = ask_yes_no("吏员内部需要调用大模型?", default=False)
        
        # 7. API Keys
        api_keys = []
        if requires_llm:
            api_keys = ask_api_keys()
        
        # 8. 数据边界
        print("\n━━━ 数据边界 ━━")
        print("  shiyi_only - 不本地存储任何用户数据（推荐）")
        print("  operational - 可存自身运行状态")
        data_policy = "shiyi_only"
        if ask_yes_no("需要存储运行状态 (operational)?", default=False):
            data_policy = "operational"
        
        # 生成clerk_id（基于已有目录自增）
        clerk_id = generate_clerk_id(str(self.output_base))
        
        # 为默认工具名加上clerk_id序号前缀
        tool_prefix = self._tool_name_from_id(clerk_id)
        for t in tools:
            if t["name"] == "execute":
                t["name"] = f"{tool_prefix}_execute"
        
        # 构建配置
        config = {
            "clerk_id": clerk_id,
            "name": name,
            "version": version,
            "description": desc,
            "capabilities": [t["name"] for t in tools],
            "tools": tools,
            "enabled": True,
            "created_at": datetime.now().strftime("%Y-%m-%d"),
            "api_keys": api_keys,
            "requires_llm": requires_llm,
            "data_policy": data_policy,
            "skills": [],
            "_capability_type": selected_cap,
        }
        
        # 保存
        return self._save_clerk(clerk_id, config)
    
    def create_non_interactive(
        self,
        name: str,
        desc: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        requires_llm: bool = False,
        api_keys: Optional[List[str]] = None,
        model_name: str = "",
        base_url: str = "",
        provider: str = "",
        data_policy: str = "shiyi_only",
        version: str = "0.1.0",
    ) -> Dict[str, Any]:
        """非交互式创建吏员
        
        Args:
            name: 吏员名称
            desc: 吏员描述
            tools: 工具定义列表
            requires_llm: 是否需要LLM
            api_keys: 需要的API Key名称列表（如 ["DEEPSEEK_API_KEY"]）
            model_name: LLM模型名称（如 deepseek-chat, gpt-4o 等）
            base_url: API base URL（如 https://api.deepseek.com/v1）
            provider: LLM provider（如 deepseek, openai）
            data_policy: 数据边界
            version: 版本号
        """
        if not name:
            return {"success": False, "error": "名称不能为空"}
        
        if not desc:
            desc = f"{name} - 专业工具执行者"
        
        # 生成clerk_id（基于已有目录自增）
        clerk_id = generate_clerk_id(str(self.output_base))
        
        # 默认工具（包含基础工具 + 代理记忆工具）
        if not tools:
            tool_prefix = self._tool_name_from_id(clerk_id)
            tools = [{
                "name": f"{tool_prefix}_execute",
                "description": f"执行{name}相关操作",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作描述"}
                    },
                    "required": ["action"]
                }
            }]
        
        # api_keys 格式修正：["KEY1"] → [{"name":"KEY1","description":"...","required":false}]
        api_key_objects = []
        if api_keys:
            for ak in api_keys:
                if isinstance(ak, dict):
                    api_key_objects.append(ak)
                else:
                    api_key_objects.append({
                        "name": str(ak),
                        "description": f"{ak} 环境变量",
                        "required": False,
                    })
        
        config = {
            "clerk_id": clerk_id,
            "name": name,
            "version": version,
            "description": desc,
            "capabilities": [t["name"] for t in tools],
            "tools": tools,
            "enabled": True,
            "created_at": datetime.now().strftime("%Y-%m-%d"),
            "api_keys": api_key_objects,
            "requires_llm": requires_llm,
            "data_policy": data_policy,
            "skills": [],
        }
        
        # 如果需要 LLM，生成完整的 llm_config
        if requires_llm:
            config["llm_config"] = {
                "provider": provider or DEFAULT_LLM_PROVIDER,
                "model": model_name or DEFAULT_LIGHT_LLM_MODEL,
                "base_url": base_url or DEFAULT_LLM_BASE_URL,
                "max_tokens": 2048,
                "temperature": 0.3,
            }
        elif model_name:
            # 即使不需要 LLM 也记录模型名（用户可能后续开启）
            config["model_name"] = model_name
        
        return self._save_clerk(clerk_id, config)
    
    def create_from_skill(
        self,
        skill_name: str,
        skill_desc: str = "",
        inferred_tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """从Skill需求创建吏员
        
        Args:
            skill_name: 技能名称（中英文均可，作为吏员name）
            skill_desc: 技能描述
            inferred_tools: 从技能推断的工具定义
        """
        # 生成clerk_id（基于已有目录自增）
        clerk_id = generate_clerk_id(str(self.output_base))
        
        if not skill_desc:
            skill_desc = f"{skill_name}技能的专业执行者"
        
        tools = inferred_tools
        if not tools:
            tool_prefix = self._tool_name_from_id(clerk_id)
            tools = [{
                "name": f"{tool_prefix}_execute",
                "description": f"执行{skill_name}技能相关操作",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作描述"}
                    },
                    "required": ["action"]
                }
            }]
        
        config = {
            "clerk_id": clerk_id,
            "name": skill_name,
            "version": "0.1.0",
            "description": skill_desc,
            "capabilities": [t["name"] for t in tools],
            "tools": tools,
            "enabled": True,
            "created_at": datetime.now().strftime("%Y-%m-%d"),
            "api_keys": [],
            "requires_llm": False,
            "data_policy": "shiyi_only",
            "skills": [skill_name],
        }
        
        return self._save_clerk(clerk_id, config)
    
    def _save_clerk(self, clerk_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """保存吏员到目录"""
        clerk_dir = self.output_base / clerk_id
        clerk_dir.mkdir(parents=True, exist_ok=True)
        
        # 验证配置
        valid, errors = validate_clerk_config(config)
        if not valid:
            return {
                "success": False,
                "error": f"配置验证失败: {'; '.join(errors)}"
            }
        
        # 保存clerk.json（移除内部字段）
        save_config = {k: v for k, v in config.items() if not k.startswith("_")}
        cj_path = clerk_dir / "clerk.json"
        with open(cj_path, "w", encoding="utf-8") as f:
            json.dump(save_config, f, indent=2, ensure_ascii=False)
        
        # 生成worker.py
        worker_content = self._generate_worker(config)
        wp_path = clerk_dir / "worker.py"
        with open(wp_path, "w", encoding="utf-8") as f:
            f.write(worker_content)
        
        # 保存mcp_server.py
        mcp_path = clerk_dir / "mcp_server.py"
        with open(mcp_path, "w", encoding="utf-8") as f:
            f.write(get_default_mcp_template())
        
        # 保存soul.md
        soul_path = clerk_dir / "soul.md"
        with open(soul_path, "w", encoding="utf-8") as f:
            f.write(get_default_soul_md(config["name"], config["description"]))
        
        return {
            "success": True,
            "clerk_id": clerk_id,
            "clerk_dir": str(clerk_dir),
            "message": f"吏员已创建: {clerk_dir}"
        }
    
    def _generate_worker(self, config: Dict[str, Any]) -> str:
        """生成worker.py内容 — 包含基础工具 + 记忆代理工具"""
        tools = config.get("tools", [])
        tool_names = [t["name"] for t in tools]
        
        # 始终追加记忆代理工具
        has_recall = any(t["name"] == "recall_memory" for t in tools)
        has_search = any(t["name"] == "search_conversations" for t in tools)
        if not has_recall:
            tools.append({
                "name": "recall_memory",
                "description": "搜索史佚的记忆库，查找与查询相关的事实记忆（带情感标签、时间戳和向量相似度）",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词或问题"},
                        "top_k": {"type": "integer", "description": "返回结果数量（默认5）", "default": 5},
                    },
                    "required": ["query"],
                },
            })
            tool_names.append("recall_memory")
        if not has_search:
            tools.append({
                "name": "search_conversations",
                "description": "搜索史佚的对话历史记录，找到包含关键词的对话片段",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "limit": {"type": "integer", "description": "返回结果数量（默认10）", "default": 10},
                    },
                    "required": ["query"],
                },
            })
            tool_names.append("search_conversations")
        
        # 生成工具注册表
        registry_entries = []
        for name in tool_names:
            if name in ("recall_memory", "search_conversations"):
                registry_entries.append(f'    "{name}": "{name}",')
            else:
                registry_entries.append(f'    "{name}": "_execute_{name}",')
        registry_str = "\n".join(registry_entries) if registry_entries else "    # 无工具"
        
        # 生成工具方法（不含代理工具，它们在 ClerkWorker 基类中处理）
        method_entries = []
        for tool in tools:
            name = tool["name"]
            if name in ("recall_memory", "search_conversations"):
                continue
            desc = tool.get("description", "")
            schema = tool.get("inputSchema", {})
            required = schema.get("required", [])
            
            method = f'''
    def _execute_{name}(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """工具: {name}
        
        描述: {desc}
        必填参数: {required}
        
        TODO: 实现此工具的逻辑
        """
        raise NotImplementedError("工具 {name} 尚未实现 - 请在 worker.py 中补全 _execute_{name}() 方法")
'''
            method_entries.append(method)
        
        methods_str = "\n".join(method_entries)
        
        return f'''"""worker.py - {config['name']} 吏员

从史佚吏员创建系统自动生成。
请实现 _execute_* 方法中的逻辑。

clerk_id: {config['clerk_id']}
version: {config['version']}
description: {config['description']}

内置代理工具（由主进程执行，无需实现）：
  - recall_memory: 搜索记忆库（返回 _proxy 标记，由 ClerkRegistry 拦截执行）
  - search_conversations: 搜索对话历史（同上）
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

from shiyi.common.constants import (
    DEFAULT_LLM_PROVIDER,
    DEFAULT_LIGHT_LLM_MODEL,
    DEFAULT_LLM_BASE_URL,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 吏员配置
# ═══════════════════════════════════════════════════════

class ClerkConfig:
    """吏员配置"""

    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            config_file = Path(config_path)
        else:
            config_file = Path(__file__).parent / "clerk.json"

        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.clerk_id = data.get("clerk_id", "unknown")
        self.name = data.get("name", "未命名吏员")
        self.version = data.get("version", "0.1.0")
        self.description = data.get("description", "")
        self.created_at = data.get("created_at", "")
        self.tools_def = data.get("tools", [])
        self.capabilities = data.get("capabilities", [])
        self.enabled = data.get("enabled", True)
        self.api_keys = data.get("api_keys", [])
        self.requires_llm = data.get("requires_llm", False)
        self.data_policy = data.get("data_policy", "shiyi_only")
        self.skills = data.get("skills", [])
        self.llm_config = data.get("llm_config", {{}})


# ═══════════════════════════════════════════════════════
# 代理工具（由主进程执行）
# ═══════════════════════════════════════════════════════

# 注意：recall_memory 和 search_conversations 在 MCP 子进程中无法访问
# MemoryEngine，因此返回 _proxy 标记，由 ClerkRegistry 拦截并调用主进程。
# ClerkWorker.execute() 已内置处理这两个工具，无需额外实现。

# ═══════════════════════════════════════════════════════
# 工具注册
# ═══════════════════════════════════════════════════════

TOOL_REGISTRY = {{
{registry_str}
}}


# ═══════════════════════════════════════════════════════
# 吏员工作器
# ═══════════════════════════════════════════════════════

class ClerkWorker:
    """吏员工作器"""

    def __init__(self, config_path: Optional[str] = None):
        self.config = ClerkConfig(config_path)
        self._config_path = Path(config_path).parent if config_path else Path(__file__).parent
        self._log: List[Dict[str, Any]] = []

    def get_tools(self) -> List[Dict[str, Any]]:
        """返回工具列表"""
        return self.config.tools_def

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具调用
        
        特殊处理：recall_memory / search_conversations 返回 _proxy 标记，
        由 ClerkRegistry 拦截后调用主进程的 MemoryEngine。
        """
        # 代理工具 — 返回标记，由主进程执行
        if tool_name in ("recall_memory", "search_conversations"):
            return {{"_proxy": True, "tool": tool_name, "params": params}}
        
        handler = TOOL_REGISTRY.get(tool_name)
        if not handler:
            return {{"error": f"未知工具: {{tool_name}}", "success": False}}

        try:
            method = getattr(self, handler, None)
            if not method:
                return {{"error": f"处理器未实现: {{handler}}", "success": False}}

            result = method(params)
            result["success"] = True
            self._log.append({{
                "tool": tool_name,
                "params": params,
                "time": datetime.now().isoformat(),
            }})
            return result
        except NotImplementedError as e:
            return {{"error": str(e), "success": False}}
        except Exception as e:
            logger.exception(f"Tool {{tool_name}} failed")
            return {{"error": str(e), "success": False}}

    def run_agent(self, task_prompt: str) -> Dict[str, Any]:
        """自主执行循环 — 如需 LLM，子类可覆盖此方法
        
        当前默认实现：返回占位结果。
        当 clerk.json 中 requires_llm=True 且配置了 llm_config 时，
        子类可实现完整的 LLM 自主循环。
        """
        return {{
            "success": True,
            "result": f"吏员 {{self.config.name}} 收到任务：{{task_prompt[:200]}}",
            "note": "此吏员尚未实现 run_agent()，请检查 worker.py",
        }}

    def status(self) -> Dict[str, Any]:
        """返回吏员状态"""
        return {{
            "clerk_id": self.config.clerk_id,
            "name": self.config.name,
            "version": self.config.version,
            "enabled": self.config.enabled,
            "data_policy": self.config.data_policy,
            "log_count": len(self._log),
        }}


# ═══════════════════════════════════════════════════════
# 工具实现
# ═══════════════════════════════════════════════════════

{methods_str}
'''


# ═══════════════════════════════════════════════════════
# 吏员删除
# ═══════════════════════════════════════════════════════

def delete_clerk(clerk_id: str, clerks_dir: Optional[str] = None) -> Dict[str, Any]:
    """删除吏员
    
    Args:
        clerk_id: 吏员ID
        clerks_dir: 吏员目录，默认 ~/.shiyi/clerks/
        
    Returns:
        删除结果
    """
    base_dir = Path(clerks_dir) if clerks_dir else Path.home() / ".shiyi" / "clerks"
    clerk_id = _sanitize_clerk_id(clerk_id)
    clerk_dir = base_dir / clerk_id
    
    # 检查是否为系统吏员（不允许删除）
    if clerk_id == "clerk_001":
        return {
            "success": False,
            "error": "系统吏员不允许删除"
        }
    
    if not clerk_dir.exists():
        return {
            "success": False,
            "error": f"吏员不存在: {clerk_id}",
            "clerk_dir": str(clerk_dir)
        }
    
    try:
        # 删除目录
        shutil.rmtree(clerk_dir)
        return {
            "success": True,
            "message": f"已删除吏员: {clerk_id}",
            "clerk_dir": str(clerk_dir)
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"删除失败: {str(e)}",
            "clerk_dir": str(clerk_dir)
        }


def _sanitize_clerk_id(clerk_id: str) -> str:
    """消毒 clerk_id，拒绝路径穿越字符"""
    if not clerk_id or not clerk_id.strip():
        raise ValueError("clerk_id 不能为空")
    sanitized = clerk_id.strip()
    if any(c in sanitized for c in ('/', '\\', '..')):
        raise ValueError(f"clerk_id 包含非法字符: {clerk_id!r}")
    return sanitized


def configure_clerk(clerk_id: str, updates: Dict[str, Any],
                    clerks_dir: Optional[str] = None) -> Dict[str, Any]:
    """修改吏员配置

    修改 clerk.json 中的可配置字段（name, description, enabled, tools, skills）。

    Args:
        clerk_id: 吏员ID
        updates: 要更新的字段字典 {(name: str), (description: str), (enabled: bool), (tools: list), (skills: list)}
        clerks_dir: 吏员目录，默认 ~/.shiyi/clerks/

    Returns:
        配置结果
    """
    base_dir = Path(clerks_dir) if clerks_dir else Path.home() / ".shiyi" / "clerks"
    clerk_id = _sanitize_clerk_id(clerk_id)
    clerk_dir = base_dir / clerk_id
    cj_path = clerk_dir / "clerk.json"

    if not cj_path.exists():
        return {"success": False, "error": f"吏员不存在: {clerk_id}"}

    allowed_keys = {"name", "description", "enabled", "tools", "skills"}

    try:
        with open(cj_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        changed = {}
        for key, value in updates.items():
            if key not in allowed_keys:
                continue
            if key in config and config[key] == value:
                continue
            config[key] = value
            changed[key] = value

        if not changed:
            return {"success": True, "message": "无变更", "clerk_id": clerk_id}

        with open(cj_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        return {
            "success": True,
            "message": f"已更新吏员配置: {clerk_id}",
            "changed": changed,
        }
    except Exception as e:
        return {"success": False, "error": f"配置失败: {str(e)}"}


def rename_clerk(clerk_id: str, new_name: str,
                 clerks_dir: Optional[str] = None) -> Dict[str, Any]:
    """重命名吏员

    只修改 clerk.json 中的 name 字段（用户可见名称），不改变 clerk_id。

    Args:
        clerk_id: 吏员ID
        new_name: 新名称
        clerks_dir: 吏员目录，默认 ~/.shiyi/clerks/

    Returns:
        重命名结果
    """
    # 系统吏员保护
    if clerk_id == "clerk_001":
        return {"success": False, "error": "系统吏员不允许重命名"}

    if not new_name or not new_name.strip():
        return {"success": False, "error": "名称不能为空"}

    return configure_clerk(clerk_id, {"name": new_name.strip()}, clerks_dir)


def skill_assign_clerk(clerk_id: str, skills: List[str],
                       clerks_dir: Optional[str] = None) -> Dict[str, Any]:
    """给吏员分配/更新 Skill 列表

    Args:
        clerk_id: 吏员ID
        skills: Skill ID 列表（覆盖写入）
        clerks_dir: 吏员目录，默认 ~/.shiyi/clerks/

    Returns:
        分配结果
    """
    return configure_clerk(clerk_id, {"skills": skills}, clerks_dir)


def list_clerks(clerks_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出所有吏员
    
    Args:
        clerks_dir: 吏员目录，默认 ~/.shiyi/clerks/
        
    Returns:
        吏员列表
    """
    base_dir = Path(clerks_dir) if clerks_dir else Path.home() / ".shiyi" / "clerks"
    
    if not base_dir.exists():
        return []
    
    clerks = []
    for clerk_path in sorted(base_dir.iterdir()):
        if not clerk_path.is_dir():
            continue
        cj = clerk_path / "clerk.json"
        if not cj.exists():
            continue
        try:
            with open(cj, "r", encoding="utf-8") as f:
                data = json.load(f)
            clerks.append({
                "clerk_id": data.get("clerk_id", clerk_path.name),
                "name": data.get("name", "未命名"),
                "version": data.get("version", "0.0.0"),
                "description": data.get("description", ""),
                "enabled": data.get("enabled", True),
                "path": str(clerk_path),
                "tool_count": len(data.get("tools", [])),
            })
        except Exception:
            continue
    
    return clerks


def start_clerk(clerk_id: str, clerks_dir: Optional[str] = None,
                registry: Any = None) -> Dict[str, Any]:
    """启动吏员进程并注册到 Registry

    定位吏员目录 → 创建 RemoteClerk → spawn 子进程 → 注册。

    Args:
        clerk_id: 吏员ID
        clerks_dir: 吏员目录，默认 ~/.shiyi/clerks/
        registry: 可选，ClerkRegistry 实例（用于注册）

    Returns:
        {"success": bool, "clerk_id": str, "name": str, "pid": int}
    """
    clerk_id = _sanitize_clerk_id(clerk_id)
    base_dir = Path(clerks_dir) if clerks_dir else Path.home() / ".shiyi" / "clerks"
    clerk_dir = base_dir / clerk_id

    if not clerk_dir.exists():
        return {"success": False, "error": f"吏员目录不存在: {clerk_dir}"}

    mcp_script = clerk_dir / "mcp_server.py"
    if not mcp_script.exists():
        return {"success": False, "error": f"mcp_server.py 不存在: {mcp_script}"}

    config_path = clerk_dir / "clerk.json"
    if not config_path.exists():
        return {"success": False, "error": f"clerk.json 不存在: {config_path}"}

    try:
        from shiyi.core.clerk_connector import RemoteClerk

        remote = RemoteClerk(
            server_script=str(mcp_script),
            config_path=str(config_path),
        )
        # 触发连接（_ensure_started 会 spawn 子进程）
        tools = remote.get_tools()
        pid = remote._proc.pid if remote._proc else 0

        # 注册到 Registry
        if registry is not None:
            registry.register_clerk(remote)

        return {
            "success": True,
            "clerk_id": clerk_id,
            "name": remote.config.name,
            "pid": pid,
            "tool_count": len(tools),
        }
    except Exception as e:
        logger.error("Failed to start clerk %s: %s", clerk_id, e)
        return {"success": False, "error": str(e)}


def stop_clerk(clerk_id: str, registry: Any = None) -> Dict[str, Any]:
    """停止吏员进程

    Args:
        clerk_id: 吏员ID
        registry: 可选，ClerkRegistry 实例（用于注销）

    Returns:
        {"success": bool, "clerk_id": str}
    """
    clerk_id = _sanitize_clerk_id(clerk_id)

    if registry is not None:
        clerk_data = registry._clerks.get(clerk_id)
        if clerk_data:
            worker = clerk_data.get("worker")
            if hasattr(worker, "_proc") and worker._proc:
                try:
                    worker._proc.terminate()
                    worker._proc.wait(timeout=5)
                except Exception:
                    try:
                        worker._proc.kill()
                    except Exception:
                        pass
            registry.unregister_clerk(clerk_id)
            return {"success": True, "clerk_id": clerk_id}

    return {"success": False, "error": f"吏员未注册: {clerk_id}"}


# ═══════════════════════════════════════════════════════
# 入口函数
# ═══════════════════════════════════════════════════════

def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description="史佚吏员创建工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")
    
    # create
    create_p = subparsers.add_parser("create", help="创建吏员")
    create_p.add_argument("name", nargs="?", help="吏员名称")
    create_p.add_argument("--desc", "-d", default="", help="吏员描述")
    create_p.add_argument("--tools", "-t", default="", help="工具列表（逗号分隔）")
    create_p.add_argument("--no-interactive", action="store_true", help="非交互模式")
    create_p.add_argument("--output", "-o", default="", help="输出目录")
    
    # delete
    delete_p = subparsers.add_parser("delete", help="删除吏员")
    delete_p.add_argument("clerk_id", help="吏员ID")
    delete_p.add_argument("--force", "-f", action="store_true", help="强制删除")
    
    # list
    list_p = subparsers.add_parser("list", help="列出吏员")
    
    args = parser.parse_args()
    
    if args.command == "create":
        creator = ClerkCreator(args.output) if args.output else ClerkCreator()
        
        if args.no_interactive or args.name:
            # 非交互模式
            tools = []
            if args.tools:
                for t in args.tools.split(","):
                    t = t.strip()
                    if t:
                        tools.append({
                            "name": t,
                            "description": f"执行{t}操作",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"action": {"type": "string", "description": "操作"}},
                                "required": ["action"]
                            }
                        })
            
            result = creator.create_non_interactive(
                name=args.name or "custom",
                desc=args.desc,
                tools=tools if tools else None
            )
        else:
            # 交互模式
            result = creator.create_interactive()
        
        if result.get("success"):
            print(f"✅ {result['message']}")
            print(f"   目录: {result['clerk_dir']}")
        else:
            print(f"❌ {result.get('error', '创建失败')}")
    
    elif args.command == "delete":
        if not args.force:
            confirm = input(f"确认删除吏员 '{args.clerk_id}'? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("取消删除")
                return
        
        result = delete_clerk(args.clerk_id)
        if result.get("success"):
            print(f"✅ {result['message']}")
        else:
            print(f"❌ {result.get('error', '删除失败')}")
    
    elif args.command == "list":
        clerks = list_clerks()
        if not clerks:
            print("暂无吏员")
            return
        print(f"共有 {len(clerks)} 个吏员:\n")
        for c in clerks:
            status = "✓" if c["enabled"] else "✗"
            print(f"  [{status}] {c['clerk_id']}")
            print(f"      名称: {c['name']}")
            print(f"      工具: {c['tool_count']} 个")
            print()
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
