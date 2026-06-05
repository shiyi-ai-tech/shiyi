"""AsyncClerkExecutor — 吏员异步执行器（线程池）

v0.14.0 引入。支持将吏员工具调用提交到后台线程池执行，
不阻塞主对话线程。与 TaskTracker 集成追踪任务状态。

用法:
    executor = AsyncClerkExecutor(task_tracker, max_workers=4)
    task_id = executor.submit(
        clerk_id="clerk-default",
        tool_name="file_write",
        params={"path": "/tmp/test.txt", "content": "hello"},
        execute_fn=lambda: registry.execute("file_write", {...}),
    )
"""

import logging
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, Any, Optional, Callable

logger = logging.getLogger(__name__)


class AsyncClerkExecutor:
    """吏员异步执行器 — 线程池包装"""

    def __init__(self, task_tracker, max_workers: int = 4):
        self._tracker = task_tracker
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: Dict[str, Future] = {}

    def submit(
        self,
        clerk_id: str,
        tool_name: str,
        params: Dict[str, Any],
        execute_fn: Callable[[], Dict[str, Any]],
    ) -> str:
        """提交异步任务

        Args:
            clerk_id: 吏员标识
            tool_name: 工具名
            params: 工具参数
            execute_fn: 实际执行函数（无参，返回结果 dict）

        Returns:
            task_id
        """
        task_id = self._tracker.start(
            clerk_id=clerk_id,
            tool_name=tool_name,
            params=params,
        )

        def _wrapper():
            try:
                result = execute_fn()
                self._tracker.complete(task_id, result)
            except Exception as e:
                logger.exception("Async task %s failed", task_id)
                self._tracker.fail(task_id, str(e))
            finally:
                self._futures.pop(task_id, None)

        future = self._pool.submit(_wrapper)
        self._futures[task_id] = future
        logger.info("Async task submitted: %s (%s/%s)", task_id, clerk_id, tool_name)
        return task_id

    def shutdown(self, wait: bool = True):
        """关闭线程池"""
        self._pool.shutdown(wait=wait)
        if wait:
            logger.info("AsyncClerkExecutor shut down (waited for tasks)")
        else:
            logger.info("AsyncClerkExecutor shut down (no wait)")

    @property
    def active_count(self) -> int:
        """当前执行中任务数"""
        return sum(1 for f in self._futures.values() if f.running())

    @property
    def total_tracked(self) -> int:
        """追踪器总任务数"""
        return self._tracker.total_count

    def get_future(self, task_id: str) -> Optional[Future]:
        """获取任务 Future（用于高级用法）"""
        return self._futures.get(task_id)
