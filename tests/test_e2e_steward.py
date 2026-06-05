"""E2E 测试 — Steward DAG 多步任务 + 并行调度 + 断点续传

测试场景：
1. 串行 DAG：A → B → C，验证顺序执行和结果传递
2. 并行 DAG：A → [B, C] → D，验证并行调度
3. 断点续传：中途中断后恢复
4. 容错重试：子任务失败后标记
5. Async 工具并发：engine chat() 多工具并行执行

所有测试使用 mock clerk / mock LLM，不依赖外部服务。
"""

import json
import time
import tempfile
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from shiyi.core.steward import Steward, StewardTask, SubTask
from shiyi.core.clerk_registry import ClerkRegistry
from shiyi.core.async_executor import AsyncClerkExecutor
from shiyi.core.task_tracker import TaskTracker


# ═══════════════════════════════════════════════════════
# Mock Clerk — 模拟吏员
# ═══════════════════════════════════════════════════════

class MockClerkConfig:
    def __init__(self, clerk_id="mock-clerk", name="Mock Clerk", version="1.0"):
        self.clerk_id = clerk_id
        self.name = name
        self.version = version
        self.description = "Mock clerk for testing"
        self.enabled = True
        self.skills = []


class MockClerk:
    """模拟吏员，记录调用并返回预设结果"""

    def __init__(self, clerk_id="mock-clerk", delay=0.1):
        self.config = MockClerkConfig(clerk_id=clerk_id)
        self._delay = delay
        self._call_log: List[Dict[str, Any]] = []
        self._results: Dict[str, Dict[str, Any]] = {}

    def set_result(self, tool_name: str, result: Dict[str, Any]):
        """预设工具返回结果"""
        self._results[tool_name] = result

    def get_tools(self) -> List[Dict[str, Any]]:
        return [
            {"name": "file_read", "description": "Read file", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
            {"name": "file_write", "description": "Write file", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}},
            {"name": "web_search", "description": "Search web", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
            {"name": "agent_run", "description": "Run agent task", "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}}}},
        ]

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._call_log.append({
            "tool": tool_name,
            "params": params,
            "time": time.time(),
        })
        time.sleep(self._delay)  # 模拟执行耗时

        if tool_name in self._results:
            return self._results[tool_name]

        # 默认返回
        return {"success": True, "result": f"[{tool_name}] executed with {json.dumps(params)[:50]}"}

    def status(self) -> Dict[str, Any]:
        return {
            "clerk_id": self.config.clerk_id,
            "name": self.config.name,
            "version": self.config.version,
            "mode": "local",
            "workspace": "/tmp/mock",
            "enabled": True,
        }


# ═══════════════════════════════════════════════════════
# 测试工具
# ═══════════════════════════════════════════════════════

def print_test(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}")
    if detail:
        print(f"         {detail}")


def setup_steward(tmp_dir: str) -> tuple:
    """创建带 mock clerk 的 Steward"""
    registry = ClerkRegistry()
    tracker = TaskTracker()
    executor = AsyncClerkExecutor(tracker, max_workers=4)

    clerk = MockClerk(clerk_id="clerk-default", delay=0.05)
    registry.register_clerk(clerk)

    # 简单的 decompose_fn：固定拆解
    def mock_decompose(user_request: str, clerks_info: list) -> list:
        return [
            {"description": f"处理: {user_request}", "clerk_id": "clerk-default",
             "tool_name": "agent_run", "tool_params": {"task": user_request}},
        ]

    # 简单的 aggregate_fn：拼接
    def mock_aggregate(user_request: str, sub_results: list) -> str:
        parts = []
        for r in sub_results:
            result = r.get("result", "")
            if isinstance(result, dict):
                parts.append(result.get("result", str(result))[:100])
            else:
                parts.append(str(result)[:100])
        return " | ".join(parts)

    # 使用临时目录避免污染真实数据
    steward = Steward(
        clerk_registry=registry,
        executor=executor,
        decompose_fn=mock_decompose,
        aggregate_fn=mock_aggregate,
    )
    steward._state_dir = Path(tmp_dir) / "steward"
    steward._state_dir.mkdir(parents=True, exist_ok=True)

    return steward, registry, clerk


# ═══════════════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════════════

def test_serial_dag():
    """测试 1: 串行 DAG A→B→C"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        steward, registry, clerk = setup_steward(tmp_dir)

        # 手动构建串行 DAG
        task = StewardTask(task_id="test-serial", user_request="串行测试")
        task.sub_tasks["sub-a"] = SubTask(
            sub_id="sub-a", description="步骤A", clerk_id="clerk-default",
            tool_name="file_write", tool_params={"path": "a.txt", "content": "hello"},
        )
        task.sub_tasks["sub-b"] = SubTask(
            sub_id="sub-b", description="步骤B", clerk_id="clerk-default",
            tool_name="file_read", tool_params={"path": "a.txt"},
            depends_on=["sub-a"],
        )
        task.sub_tasks["sub-c"] = SubTask(
            sub_id="sub-c", description="步骤C", clerk_id="clerk-default",
            tool_name="web_search", tool_params={"query": "test"},
            depends_on=["sub-b"],
        )
        steward._tasks[task.task_id] = task

        result = steward.execute(task)

        all_done = all(st.state == "done" for st in task.sub_tasks.values())
        execution_order = [log["tool"] for log in clerk._call_log]

        print_test("串行 DAG 执行", result.state == "done",
                    f"state={result.state}, all_done={all_done}, order={execution_order}")
        print_test("执行顺序正确", execution_order == ["file_write", "file_read", "web_search"],
                    f"actual={execution_order}")
        return result.state == "done" and execution_order == ["file_write", "file_read", "web_search"]


def test_parallel_dag():
    """测试 2: 并行 DAG A→[B,C]→D"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        steward, registry, clerk = setup_steward(tmp_dir)
        clerk._delay = 0.1  # 每次调用 0.1s

        task = StewardTask(task_id="test-parallel", user_request="并行测试")
        task.sub_tasks["sub-a"] = SubTask(
            sub_id="sub-a", description="步骤A", clerk_id="clerk-default",
            tool_name="file_write", tool_params={"path": "a.txt", "content": "data"},
        )
        task.sub_tasks["sub-b"] = SubTask(
            sub_id="sub-b", description="步骤B", clerk_id="clerk-default",
            tool_name="file_read", tool_params={"path": "a.txt"},
            depends_on=["sub-a"],
        )
        task.sub_tasks["sub-c"] = SubTask(
            sub_id="sub-c", description="步骤C", clerk_id="clerk-default",
            tool_name="web_search", tool_params={"query": "test"},
            depends_on=["sub-a"],
        )
        task.sub_tasks["sub-d"] = SubTask(
            sub_id="sub-d", description="步骤D", clerk_id="clerk-default",
            tool_name="file_write", tool_params={"path": "d.txt", "content": "result"},
            depends_on=["sub-b", "sub-c"],
        )
        steward._tasks[task.task_id] = task

        start = time.time()
        result = steward.execute(task)
        elapsed = time.time() - start

        all_done = all(st.state == "done" for st in task.sub_tasks.values())
        # 验证 B 和 C 确实并行执行了（它们的执行时间应重叠）
        b_start = None
        c_start = None
        b_end = None
        c_end = None
        for log in clerk._call_log:
            if log["tool"] == "file_read":
                b_start = log["time"]
                b_end = log["time"] + clerk._delay
            elif log["tool"] == "web_search":
                c_start = log["time"]
                c_end = log["time"] + clerk._delay

        # B 和 C 的执行时间有重叠 = 真正并行
        is_parallel = False
        if b_start and c_start:
            is_parallel = (b_start <= c_end) and (c_start <= b_end)

        print_test("并行 DAG 执行", result.state == "done",
                    f"state={result.state}, elapsed={elapsed:.2f}s")
        print_test("B+C 真正并行", is_parallel,
                    f"b=[{b_start:.2f},{b_end:.2f}] c=[{c_start:.2f},{c_end:.2f}]")
        return result.state == "done" and is_parallel


def test_persistence_and_resume():
    """测试 3: 状态持久化 + 断点续传"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        steward, registry, clerk = setup_steward(tmp_dir)

        task = StewardTask(task_id="test-persist", user_request="持久化测试")
        task.sub_tasks["sub-1"] = SubTask(
            sub_id="sub-1", description="步骤1", clerk_id="clerk-default",
            tool_name="file_write", tool_params={"path": "1.txt", "content": "data"},
        )
        steward._tasks[task.task_id] = task

        # 执行
        result = steward.execute(task)

        # 验证持久化文件
        persist_file = Path(tmp_dir) / "steward" / "test-persist.json"
        has_persist = persist_file.exists()

        if has_persist:
            data = json.loads(persist_file.read_text(encoding="utf-8"))
            has_version = data.get("version") == 1
            has_sub = "sub-1" in data.get("sub_tasks", {})
        else:
            has_version = False
            has_sub = False

        # 测试 resume
        task.state = "ready"  # 重置为可恢复
        resumed = steward.resume_task(task.task_id)

        print_test("持久化文件存在", has_persist)
        print_test("版本字段正确", has_version)
        print_test("子任务数据完整", has_sub)
        print_test("断点续传", resumed is not None and resumed.state == "done")

        return has_persist and has_version and has_sub and resumed is not None


def test_failed_subtask():
    """测试 4: 子任务失败处理"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        steward, registry, clerk = setup_steward(tmp_dir)

        # 注册一个总是失败的吏员
        fail_clerk = MockClerk(clerk_id="clerk-fail", delay=0.01)
        fail_clerk.set_result("file_read", {"success": False, "error": "文件不存在"})
        registry.register_clerk(fail_clerk)

        task = StewardTask(task_id="test-fail", user_request="失败测试")
        task.sub_tasks["sub-ok"] = SubTask(
            sub_id="sub-ok", description="成功步骤", clerk_id="clerk-default",
            tool_name="file_write", tool_params={"path": "ok.txt", "content": "data"},
        )
        task.sub_tasks["sub-fail"] = SubTask(
            sub_id="sub-fail", description="失败步骤", clerk_id="clerk-fail",
            tool_name="file_read", tool_params={"path": "nonexist.txt"},
            depends_on=["sub-ok"],
        )
        steward._tasks[task.task_id] = task

        result = steward.execute(task)

        ok_done = task.sub_tasks["sub-ok"].state == "done"
        fail_failed = task.sub_tasks["sub-fail"].state == "failed"
        task_failed = result.state == "failed"

        print_test("成功子任务标记 done", ok_done)
        print_test("失败子任务标记 failed", fail_failed)
        print_test("整体任务标记 failed", task_failed)

        return ok_done and fail_failed and task_failed


def test_concurrent_tool_execution():
    """测试 5: engine 并发工具执行（通过 ClerkRegistry）"""
    registry = ClerkRegistry()
    tracker = TaskTracker()

    # 注册 3 个 mock clerk
    for i in range(3):
        clerk = MockClerk(clerk_id=f"clerk-{i}", delay=0.1)
        registry.register_clerk(clerk)

    registry.set_task_tracker(tracker)

    # 并发执行 3 个不同吏员的工具
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start = time.time()
    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for i in range(3):
            future = pool.submit(
                registry.execute,
                "file_write" if i == 0 else ("file_read" if i == 1 else "web_search"),
                {"path": f"test{i}.txt", "query": f"query{i}"},
            )
            futures[future] = i

        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result(timeout=10)
                results[idx] = result
            except Exception as e:
                results[idx] = {"success": False, "error": str(e)}

    elapsed = time.time() - start

    all_success = all(r.get("success") for r in results.values())
    # 串行需要 3*0.1=0.3s，并行约 0.1s
    is_parallel = elapsed < 0.25

    print_test("3 个工具全部执行成功", all_success,
                f"results={len(results)}")
    print_test("并发加速", is_parallel,
                f"elapsed={elapsed:.2f}s (串行需~0.3s)")

    return all_success and is_parallel


def test_subtask_thread_safety():
    """测试 6: SubTask 线程安全"""
    sub = SubTask(sub_id="ts-test", description="thread safety", clerk_id="c1", tool_name="t1")

    errors = []

    def writer(n):
        try:
            for i in range(100):
                sub._transition("running")
                sub._transition("pending")
        except Exception as e:
            errors.append(e)

    def reader(n):
        try:
            for i in range(100):
                _ = sub.is_terminal
                _ = sub.elapsed
                _ = sub.state
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=writer, args=(i,)) for i in range(3)
    ] + [
        threading.Thread(target=reader, args=(i,)) for i in range(3)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    print_test("线程安全 — 无异常", len(errors) == 0,
                f"errors={len(errors)}")
    return len(errors) == 0


def test_recovery_count_limit():
    """测试 7: 恢复次数限制"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        steward, registry, clerk = setup_steward(tmp_dir)

        # 写一个已超过恢复上限的任务文件
        state_dir = Path(tmp_dir) / "steward"
        state_dir.mkdir(parents=True, exist_ok=True)

        over_limit_task = {
            "version": 1,
            "task_id": "stew-overlimit",
            "user_request": "超过恢复上限的任务",
            "state": "running",
            "_recovery_count": 3,
            "summary": "",
            "created_at": time.time(),
            "started_at": time.time(),
            "completed_at": 0.0,
            "sub_tasks": {
                "sub-1": {
                    "sub_id": "sub-1", "description": "test", "clerk_id": "clerk-default",
                    "tool_name": "file_write", "tool_params": {}, "state": "running",
                    "depends_on": [], "dependents": [], "result": None, "error": None,
                    "started_at": 0.0, "completed_at": 0.0,
                }
            },
        }
        (state_dir / "stew-overlimit.json").write_text(
            json.dumps(over_limit_task, ensure_ascii=False), encoding="utf-8"
        )

        # 创建新 Steward 触发恢复
        steward2 = Steward(
            clerk_registry=registry,
            executor=steward._executor,
        )
        steward2._state_dir = state_dir

        # 手动调用 _recover_tasks
        steward2._recover_tasks()

        task = steward2.get_task("stew-overlimit")
        is_failed = task is not None and task.state == "failed"

        print_test("恢复次数超限 → 标记 failed", is_failed,
                    f"state={task.state if task else 'not found'}")
        return is_failed


# ═══════════════════════════════════════════════════════
# 主测试流程
# ═══════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("史佚 E2E 测试 — Steward + 并行调度 + 持久化")
    print("=" * 60)
    print()

    results = {}

    print("测试 1: 串行 DAG")
    results["serial_dag"] = test_serial_dag()
    print()

    print("测试 2: 并行 DAG")
    results["parallel_dag"] = test_parallel_dag()
    print()

    print("测试 3: 持久化 + 断点续传")
    results["persistence"] = test_persistence_and_resume()
    print()

    print("测试 4: 子任务失败处理")
    results["failure"] = test_failed_subtask()
    print()

    print("测试 5: 并发工具执行")
    results["concurrent"] = test_concurrent_tool_execution()
    print()

    print("测试 6: SubTask 线程安全")
    results["thread_safety"] = test_subtask_thread_safety()
    print()

    print("测试 7: 恢复次数限制")
    results["recovery_limit"] = test_recovery_count_limit()
    print()

    # 汇总
    print("=" * 60)
    print("测试汇总")
    print("=" * 60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for name, result in results.items():
        status = "PASS" if result else "FAIL"
        print(f"  [{status}] {name}")

    print()
    print(f"通过: {passed}/{total}")

    if passed == total:
        print("\n所有 E2E 测试通过！")
        return 0
    else:
        print(f"\n{total - passed} 个测试失败。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
