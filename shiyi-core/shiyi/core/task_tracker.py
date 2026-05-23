"""TaskTracker — 吏员任务追踪器

跟踪吏员任务的执行状态、耗时、结果，支持异步长任务监控。
"""

import uuid
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class TaskStatus:
    """任务状态枚举"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskTracker:
    """吏员任务追踪器"""

    def __init__(self, max_history: int = 100):
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._max_history = max_history

    def start(self, clerk_id: str, tool_name: str, params: Dict[str, Any]) -> str:
        """标记任务开始

        Args:
            clerk_id: 吏员ID
            tool_name: 工具名
            params: 工具参数

        Returns:
            task_id
        """
        task_id = str(uuid.uuid4())[:8]
        self._tasks[task_id] = {
            "task_id": task_id,
            "clerk_id": clerk_id,
            "tool_name": tool_name,
            "params": params,
            "status": TaskStatus.RUNNING,
            "started_at": time.time(),
            "finished_at": None,
            "elapsed_ms": 0,
            "result": None,
            "error": None,
        }

        # 防止内存膨胀
        if len(self._tasks) > self._max_history:
            oldest = sorted(self._tasks.values(), key=lambda t: t.get("started_at", 0))[0]
            self._tasks.pop(oldest["task_id"], None)

        logger.debug("Task started: %s | %s/%s", task_id, clerk_id, tool_name)
        return task_id

    def complete(self, task_id: str, result: Dict[str, Any]) -> None:
        """标记任务完成"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        task["status"] = TaskStatus.COMPLETED if result.get("success") else TaskStatus.FAILED
        task["finished_at"] = time.time()
        task["elapsed_ms"] = round((task["finished_at"] - task["started_at"]) * 1000)
        task["result"] = result.get("result", "")
        task["error"] = result.get("error", "")

    def fail(self, task_id: str, error: str) -> None:
        """标记任务失败"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        task["status"] = TaskStatus.FAILED
        task["finished_at"] = time.time()
        task["elapsed_ms"] = round((task["finished_at"] - task["started_at"]) * 1000)
        task["error"] = error

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务信息"""
        return self._tasks.get(task_id)

    def list_recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """列出最近的任务"""
        tasks = sorted(
            self._tasks.values(),
            key=lambda t: t.get("started_at", 0),
            reverse=True,
        )
        return tasks[:limit]

    @property
    def total_count(self) -> int:
        """任务总数"""
        return len(self._tasks)

    def stats(self) -> Dict[str, Any]:
        """详细任务统计"""
        total = self.total_count
        by_status = {}
        by_clerk = {}
        total_elapsed = 0.0

        for t in self._tasks.values():
            st = t["status"]
            by_status[st] = by_status.get(st, 0) + 1
            cid = t["clerk_id"]
            by_clerk[cid] = by_clerk.get(cid, 0) + 1
            total_elapsed += t.get("elapsed_ms", 0)

        return {
            "total_tasks": total,
            "by_status": by_status,
            "by_clerk": by_clerk,
            "avg_elapsed_ms": round(total_elapsed / max(total, 1), 1),
        }
