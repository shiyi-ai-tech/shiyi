"""KanbanBoard — 多吏员任务看板

v0.14.0 引入。管理异步吏员任务的完整生命周期：
- 任务注册（add_task）
- 状态流转（ready → running → done/failed）
- 看板总览（status）

设计：
- 每个任务有唯一 task_id
- 支持父子依赖（parents/children）为未来 DAG 拆解预留
- state 枚举：ready / running / done / failed
"""

import uuid
import time
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field


class TaskState(Enum):
    """任务状态"""
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class KanbanTask:
    """看板任务"""

    task_id: str
    title: str
    clerk_id: str
    tool_name: str
    state: str = "ready"  # ready | running | done | failed

    params: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    parents: List[str] = field(default_factory=list)   # 依赖的前置 task_id
    children: List[str] = field(default_factory=list)   # 依赖此任务的后续 task_id

    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()

    @property
    def elapsed(self) -> float:
        """已耗时（秒）"""
        if self.state == "running":
            return time.time() - self.started_at
        if self.state in ("done", "failed") and self.started_at:
            return self.completed_at - self.started_at
        return 0.0

    @property
    def is_terminal(self) -> bool:
        return self.state in ("done", "failed")


class KanbanBoard:
    """多吏员任务看板 — v0.14.0 任务编排"""

    def __init__(self):
        self._tasks: Dict[str, KanbanTask] = {}

    # ──────────────────────────────
    # 任务注册
    # ──────────────────────────────

    def add_task(
        self,
        title: str,
        clerk_id: str,
        tool_name: str,
        params: Optional[Dict[str, Any]] = None,
        parents: Optional[List[str]] = None,
    ) -> KanbanTask:
        """添加任务到看板

        Args:
            title: 任务简述
            clerk_id: 目标吏员
            tool_name: 工具名
            params: 工具参数
            parents: 前置依赖 task_id 列表

        Returns:
            KanbanTask 实例
        """
        task_id = f"kbt-{uuid.uuid4().hex[:10]}"
        task = KanbanTask(
            task_id=task_id,
            title=title,
            clerk_id=clerk_id,
            tool_name=tool_name,
            params=params or {},
            parents=parents or [],
        )

        # 反向注册 children
        for pid in task.parents:
            parent = self._tasks.get(pid)
            if parent:
                parent.children.append(task_id)

        self._tasks[task_id] = task
        return task

    # ──────────────────────────────
    # 状态流转
    # ──────────────────────────────

    def mark_running(self, task_id: str):
        """标记为执行中"""
        t = self._tasks.get(task_id)
        if t:
            t.state = "running"
            t.started_at = time.time()

    def mark_done(self, task_id: str, result: Dict[str, Any]):
        """标记为完成"""
        t = self._tasks.get(task_id)
        if t:
            t.state = "done"
            t.result = result
            t.completed_at = time.time()

    def mark_failed(self, task_id: str, error: str):
        """标记为失败"""
        t = self._tasks.get(task_id)
        if t:
            t.state = "failed"
            t.error = error
            t.completed_at = time.time()

    # ──────────────────────────────
    # 查询
    # ──────────────────────────────

    def get_by_id(self, task_id: str) -> Optional[KanbanTask]:
        return self._tasks.get(task_id)

    def get_ready(self) -> List[KanbanTask]:
        return [t for t in self._tasks.values() if t.state == "ready"]

    def get_running(self) -> List[KanbanTask]:
        return [t for t in self._tasks.values() if t.state == "running"]

    def get_done(self) -> List[KanbanTask]:
        return [t for t in self._tasks.values() if t.state == "done"]

    def get_failed(self) -> List[KanbanTask]:
        return [t for t in self._tasks.values() if t.state == "failed"]

    def status(self) -> Dict[str, Any]:
        """看板总览"""
        tasks = list(self._tasks.values())
        return {
            "total": len(tasks),
            "ready": len(self.get_ready()),
            "running": len(self.get_running()),
            "done": len(self.get_done()),
            "failed": len(self.get_failed()),
            "tasks": [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "state": t.state,
                    "clerk": t.clerk_id,
                    "tool": t.tool_name,
                    "elapsed": round(t.elapsed, 2),
                }
                for t in sorted(tasks, key=lambda x: x.created_at, reverse=True)
            ],
        }

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    def __len__(self) -> int:
        return self.task_count
