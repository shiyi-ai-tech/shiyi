"""
管家 (Steward) — 吏员协同调度器
═══════════════════════════════════

v0.17.0 引入。替代旧 KanbanBoard，实现多吏员任务编排：

  用户请求 → 拆解(LLM) → 路由(匹配吏员) → 调度(并行/串行) → 汇总(LLM)

设计原则：
  - 管家不执行工具，只调度吏员
  - 复用 AsyncClerkExecutor 做异步执行
  - LLM 调用通过回调注入（core 零网络依赖）
  - 数据归属：吏员产出由管家汇总后存回史佚

产物格式约定 (v0.19.0 Phase 3)：
  - 吏员间传递数据统一使用 artifacts 格式
  - 支持 text/file/image/video 四种产物类型
  - 子任务参数可引用上游任务的产物（$ref:sub_id.field）
"""

import json
import re
import uuid
import time
import logging
import threading
from enum import Enum
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════

class SubTaskState(Enum):
    """子任务状态"""
    PENDING = "pending"    # 等待依赖就绪
    READY = "ready"        # 可执行
    RUNNING = "running"    # 执行中
    DONE = "done"          # 完成
    FAILED = "failed"      # 失败


@dataclass
class SubTask:
    """管家子任务 — 一个吏员工具调用"""

    sub_id: str
    description: str           # 做什么（自然语言）
    clerk_id: str              # 分配的吏员
    tool_name: str             # 要调的工具
    tool_params: Dict[str, Any] = field(default_factory=dict)

    state: str = "pending"
    depends_on: List[str] = field(default_factory=list)  # 依赖的 sub_id 列表
    dependents: List[str] = field(default_factory=list)   # 被依赖的 sub_id 列表

    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    submitted_task_id: str = ""  # AsyncClerkExecutor 返回的 task_id，供精确状态查询

    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()

    @property
    def elapsed(self) -> float:
        if self.state == "running":
            return time.time() - self.started_at
        if self.state in ("done", "failed") and self.started_at:
            return self.completed_at - self.started_at
        return 0.0

    @property
    def is_terminal(self) -> bool:
        return self.state in ("done", "failed")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sub_id": self.sub_id,
            "description": self.description,
            "clerk_id": self.clerk_id,
            "tool_name": self.tool_name,
            "state": self.state,
            "depends_on": self.depends_on,
            "elapsed": round(self.elapsed, 2),
            "error": self.error,
        }

    def resolve_params(self, task: 'StewardTask') -> Dict[str, Any]:
        """解析参数中的 $ref:sub_id.field 引用

        引用格式：
          - $ref:sub_id.artifacts     → 返回整个artifacts列表
          - $ref:sub_id.artifacts[N] → 返回第N个artifact
          - $ref:sub_id.artifacts[N].content → 返回第N个artifact的content字段

        Args:
            task: 所属的StewardTask实例，用于查找上游结果

        Returns:
            解析后的参数字典
        """
        if not self.tool_params:
            return self.tool_params

        resolved = {}
        for key, value in self.tool_params.items():
            resolved[key] = self._resolve_value(value, task)
        return resolved

    def _resolve_value(self, value: Any, task: 'StewardTask') -> Any:
        """递归解析单个值中的引用"""
        if isinstance(value, str):
            # 匹配 $ref:sub_id.field 或 $ref:sub_id.artifacts[0].field
            # 支持方括号内的数组索引
            match = re.match(r'^\$ref:([a-zA-Z0-9_-]+)\.(.+)$', value)
            if match:
                ref_sub_id = match.group(1)
                ref_path = match.group(2)
                return self._follow_ref(ref_sub_id, ref_path, task)
            return value
        elif isinstance(value, dict):
            return {k: self._resolve_value(v, task) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._resolve_value(item, task) for item in value]
        return value

    def _follow_ref(self, ref_sub_id: str, ref_path: str, task: 'StewardTask') -> Any:
        """根据引用路径从上游任务结果中取值

        支持的路径格式：
          - artifacts          → 返回整个artifacts列表
          - artifacts[N]      → 返回第N个artifact
          - artifacts[N].field → 返回第N个artifact的field
          - result            → 兼容旧格式
        """
        ref_task = task.sub_tasks.get(ref_sub_id)
        if not ref_task or not ref_task.result:
            logger.warning("SubTask %s: 引用 %s 未找到结果", self.sub_id, ref_sub_id)
            return None

        result = ref_task.result

        # 解析路径，支持方括号索引
        # 先处理方括号，再处理点号
        current = result

        # 如果路径以 artifacts[ 或 artifacts. 开头，先跳到 artifacts
        if ref_path.startswith('artifacts'):
            if 'artifacts' not in result:
                return None
            current = result['artifacts']
            ref_path = ref_path[9:]  # 去掉 "artifacts" 前缀
            # 现在 ref_path 可能是 "", "[0]", "[0].field" 等

        # 解析剩余路径
        if not ref_path:
            return current  # 直接返回 artifacts

        # 处理方括号索引
        # ref_path 可能是 "[0]", "[0].field"
        bracket_match = re.match(r'^\[(\d+)\](.*)$', ref_path)
        if bracket_match:
            idx = int(bracket_match.group(1))
            remaining = bracket_match.group(2)

            if not isinstance(current, list) or idx >= len(current):
                return None

            current = current[idx]

            # 处理剩余的 .field 部分
            if remaining.startswith('.'):
                remaining = remaining[1:]  # 去掉前导 '.'

            if not remaining:
                return current

            # 继续解析 .field.field 格式
            for part in remaining.split('.'):
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    return None
            return current

        # 处理普通的 .field.field 格式
        for part in ref_path.split('.'):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    @property
    def artifacts(self) -> List[Dict[str, Any]]:
        """获取子任务的产物列表（新格式）

        产物格式：
          {
            "success": true,
            "artifacts": [
              {"type": "text", "content": "..."},
              {"type": "file", "path": "..."},
              {"type": "image", "path": "..."},
              {"type": "video", "path": "..."}
            ]
          }
        """
        if not self.result:
            return []
        return self.result.get('artifacts', [])

    @property
    def text_artifacts(self) -> List[str]:
        """获取所有文本产物"""
        return [a.get('content', '') for a in self.artifacts if a.get('type') == 'text' and a.get('content')]


@dataclass
class StewardTask:
    """管家任务 — 一次用户请求的完整编排"""

    task_id: str
    user_request: str          # 用户原始输入
    sub_tasks: Dict[str, SubTask] = field(default_factory=dict)

    state: str = "ready"       # ready | running | done | failed
    summary: str = ""          # 最终汇总结果

    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()

    @property
    def elapsed(self) -> float:
        if self.state == "running":
            return time.time() - self.started_at
        if self.state in ("done", "failed") and self.started_at:
            return self.completed_at - self.started_at
        return 0.0

    @property
    def progress(self) -> Dict[str, int]:
        """进度统计"""
        total = len(self.sub_tasks)
        if total == 0:
            return {"total": 0, "pending": 0, "ready": 0, "running": 0, "done": 0, "failed": 0}
        counts = {"total": total}
        for s in ("pending", "ready", "running", "done", "failed"):
            counts[s] = sum(1 for t in self.sub_tasks.values() if t.state == s)
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user_request": self.user_request[:80],
            "state": self.state,
            "progress": self.progress,
            "elapsed": round(self.elapsed, 2),
            "sub_tasks": [st.to_dict() for st in self.sub_tasks.values()],
        }


@dataclass
class PipelineStage:
    """流水线阶段 — 一个吏员完成一项任务"""
    stage_id: str
    clerk_id: str                  # 分配的吏员
    task: str                      # 任务描述
    depends_on: List[str] = field(default_factory=list)  # 依赖的阶段ID列表
    # 运行时状态
    state: str = "pending"         # pending → running → done/failed
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: float = 0.0
    completed_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "clerk_id": self.clerk_id,
            "task": self.task,
            "depends_on": self.depends_on,
            "state": self.state,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class Pipeline:
    """吏员编排流水线"""
    pipeline_id: str
    name: str
    stages: Dict[str, PipelineStage] = field(default_factory=dict)
    state: str = "pending"         # pending → running → done/failed
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0

    # 检查点：用于断点恢复
    checkpoint_stage_id: Optional[str] = None  # 最近完成的阶段ID
    checkpoint_data: Dict[str, Any] = field(default_factory=dict)  # 检查点数据

    def stage_order(self) -> List[str]:
        """按依赖拓扑排序返回阶段ID列表，可并行的放一起"""
        stages = list(self.stages.keys())
        deps = {sid: list(self.stages[sid].depends_on) for sid in stages}
        order = []
        while stages:
            # 找出当前无依赖的阶段
            ready = [s for s in stages if not any(d in stages for d in deps.get(s, []))]
            if not ready:
                raise ValueError(f"循环依赖: {stages}")
            order.extend(sorted(ready))
            for s in ready:
                stages.remove(s)
        return order

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "name": self.name,
            "state": self.state,
            "stages": [s.to_dict() for s in self.stages.values()],
            "checkpoint_stage_id": self.checkpoint_stage_id,
        }


# ═══════════════════════════════════════════════════════
# 管家核心
# ═══════════════════════════════════════════════════════

class Steward:
    """管家 — 吏员协同调度器

    使用方式：
        steward = Steward(
            clerk_registry=registry,
            executor=async_executor,
            decompose_fn=my_decompose_fn,   # LLM 拆解回调
            aggregate_fn=my_aggregate_fn,   # LLM 汇总回调
        )
        task = steward.create_task("帮我查天气并写报告")
        steward.execute(task)  # 阻塞直到全部子任务完成
    """

    def __init__(
        self,
        clerk_registry,              # ClerkRegistry 实例
        executor,                    # AsyncClerkExecutor 实例
        decompose_fn: Callable = None,   # (user_request, available_clerks) → list[dict]
        aggregate_fn: Callable = None,   # (user_request, sub_results) → str
    ):
        self._registry = clerk_registry
        self._executor = executor
        self._decompose_fn = decompose_fn
        self._aggregate_fn = aggregate_fn
        self._tasks: Dict[str, StewardTask] = {}

        # Phase 2: 后台监控
        self._heartbeats: Dict[str, float] = {}       # clerk_id → last heartbeat timestamp
        self._alerts: List[Dict[str, Any]] = []         # alert history
        self._active_alerts: Dict[str, str] = {}         # clerk_id -> alert_type for dedup
        self._monitor_running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._heartbeat_timeout = 120    # 2min 无心跳视为超时
        self._monitor_interval = 30      # 30s 轮询一次

        # Phase 3: 流水线编排
        self._pipelines: Dict[str, Pipeline] = {}

    # ──────────────────────────────
    # 任务生命周期
    # ──────────────────────────────

    def create_task(self, user_request: str) -> StewardTask:
        """创建管家任务

        自动拆解用户请求为子任务列表，匹配合适吏员和工具。
        如果未提供 decompose_fn，子任务列表为空（需手动添加）。
        """
        task_id = f"stew-{uuid.uuid4().hex[:10]}"
        task = StewardTask(task_id=task_id, user_request=user_request)
        self._tasks[task_id] = task

        # LLM 拆解
        if self._decompose_fn:
            try:
                clerks_info = self._get_clerks_info()
                sub_defs = self._decompose_fn(user_request, clerks_info)
                for sd in sub_defs:
                    self._add_sub_task(task, sd)
                logger.info("Steward task %s: decomposed into %d sub-tasks", task_id, len(sub_defs))
            except Exception as e:
                logger.error("Steward decomposition failed: %s", e)

        return task

    def execute(self, task: StewardTask) -> StewardTask:
        """执行管家任务：按 DAG 依赖顺序调度所有子任务

        阻塞直到全部子任务完成（或全部失败）。
        """
        if not task.sub_tasks:
            logger.warning("Steward task %s: no sub-tasks to execute", task.task_id)
            task.state = "done"
            task.completed_at = time.time()
            return task

        task.state = "running"
        task.started_at = time.time()
        logger.info("Steward task %s: executing %d sub-tasks", task.task_id, len(task.sub_tasks))

        # 推进直到全部终态
        while not self._all_terminal(task):
            ready = self._get_ready(task)
            if ready:
                for st in ready:
                    self._execute_sub(task, st)
            else:
                # 还有 pending 但无 ready → 依赖未满足（死锁？）
                pending = [s for s in task.sub_tasks.values() if s.state == "pending"]
                if not pending:
                    break  # 全部 running，等线程完成
                time.sleep(1)

        # 汇总
        task.state = "done" if self._all_success(task) else "failed"
        task.completed_at = time.time()

        if self._aggregate_fn:
            try:
                sub_results = [
                    {"description": st.description, "result": st.result}
                    for st in task.sub_tasks.values() if st.result
                ]
                task.summary = self._aggregate_fn(task.user_request, sub_results)
            except Exception as e:
                logger.error("Steward aggregation failed: %s", e)
                task.summary = "; ".join(
                    st.result.get("result", "")[:100]
                    for st in task.sub_tasks.values() if st.result
                )

        logger.info("Steward task %s: %s (%d/%d sub-tasks done)",
                     task.task_id, task.state, task.progress["done"], task.progress["total"])
        return task

    # ──────────────────────────────
    # 查询
    # ──────────────────────────────

    def get_task(self, task_id: str) -> Optional[StewardTask]:
        return self._tasks.get(task_id)

    def status(self) -> Dict[str, Any]:
        """管家看板总览"""
        tasks = list(self._tasks.values())
        return {
            "total_tasks": len(tasks),
            "active": sum(1 for t in tasks if t.state in ("ready", "running")),
            "done": sum(1 for t in tasks if t.state == "done"),
            "failed": sum(1 for t in tasks if t.state == "failed"),
            "tasks": [t.to_dict() for t in sorted(tasks, key=lambda x: x.created_at, reverse=True)],
        }

    def task_detail(self, task_id: str) -> Optional[Dict[str, Any]]:
        """单个任务详情"""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        d = task.to_dict()
        d["summary"] = task.summary
        d["user_request"] = task.user_request  # 完整原文
        return d

    # ──────────────────────────────
    # Phase 1: Skill 调度
    # ──────────────────────────────

    def dispatch_skill(
        self,
        skill_id: str,
        skill_content: str,
        clerk_id: str,
        task_description: str = "",
        tool_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """将 Skill 调度给指定吏员执行

        直接调用 clerk.agent_run() 将 Skill 内容注入吏员上下文执行。

        Args:
            skill_id: Skill 标识
            skill_content: SKILL.md 完整内容（注入吏员上下文）
            clerk_id: 分配的吏员 ID
            task_description: 任务描述（默认用 skill_id）
            tool_params: 工具参数（可选）

        Returns:
            {"task_id": str, "state": str, "summary": str, "error": str}
        """
        clerk = self._registry.get_clerk(clerk_id)
        if clerk is None:
            return {"task_id": "", "state": "failed", "error": f"吏员不存在: {clerk_id}"}

        desc = task_description or f"执行 Skill: {skill_id}"

        # 构建 Skill 执行提示
        task_prompt = f"{desc}\n\n## Skill: {skill_id}\n\n{skill_content}"
        if tool_params:
            task_prompt += f"\n\n## 参数\n\n```json\n{json.dumps(tool_params, ensure_ascii=False)}\n```"

        task_id = f"stew-{uuid.uuid4().hex[:10]}"
        logger.info("Dispatching skill %s to clerk %s (task %s)", skill_id, clerk_id, task_id)

        try:
            # 发起异步执行
            started = clerk.agent_run(
                task=task_prompt,
                skills=[skill_id],
                timeout=300,
            )
            clerk_task_id = started.get("task_id", "")
            if not clerk_task_id:
                return {"task_id": task_id, "state": "failed", "error": "未能获取吏员 task_id"}

            # 轮询等待完成
            max_wait = 300
            start_time = time.time()
            while time.time() - start_time < max_wait:
                status = clerk.task_status(clerk_task_id)
                if status.get("status") == "done":
                    return {
                        "task_id": task_id,
                        "state": "done",
                        "summary": status.get("result", ""),
                    }
                if status.get("status") in ("failed", "error", "cancelled"):
                    return {
                        "task_id": task_id,
                        "state": "failed",
                        "error": status.get("error", "任务失败"),
                    }
                time.sleep(1)

            return {
                "task_id": task_id,
                "state": "failed",
                "error": f"执行超时 ({max_wait}s)",
            }
        except Exception as e:
            logger.error("Skill dispatch failed: %s", e)
            return {
                "task_id": task_id,
                "state": "failed",
                "error": str(e),
            }

    # ──────────────────────────────
    # Phase 2: 吏员配置管理 + 监控
    # ──────────────────────────────

    def configure_clerk(self, clerk_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """修改吏员配置（name/description/enabled/tools/skills）"""
        from shiyi.core.clerk_creator import configure_clerk as _cc_configure
        return _cc_configure(clerk_id, updates)

    def rename_clerk(self, clerk_id: str, new_name: str) -> Dict[str, Any]:
        """重命名吏员（系统吏员不允许）"""
        from shiyi.core.clerk_creator import rename_clerk as _cc_rename
        return _cc_rename(clerk_id, new_name)

    def delete_clerk(self, clerk_id: str) -> Dict[str, Any]:
        """安全删除吏员：先注销 → 再删目录"""
        from shiyi.core.clerk_creator import delete_clerk as _cc_delete
        self._registry.unregister_clerk(clerk_id)
        return _cc_delete(clerk_id)

    def skill_assign(self, clerk_id: str, skills: List[str]) -> Dict[str, Any]:
        """给吏员分配 Skill 列表（覆盖写入）"""
        from shiyi.core.clerk_creator import skill_assign_clerk
        return skill_assign_clerk(clerk_id, skills)

    def start_clerk(self, clerk_id: str) -> Dict[str, Any]:
        """启动吏员进程并注册到 Registry"""
        from shiyi.core.clerk_creator import start_clerk as _cc_start
        return _cc_start(clerk_id, registry=self._registry)

    def stop_clerk(self, clerk_id: str) -> Dict[str, Any]:
        """停止吏员进程"""
        from shiyi.core.clerk_creator import stop_clerk as _cc_stop
        return _cc_stop(clerk_id, registry=self._registry)

    def clerk_health(self, clerk_id: Optional[str] = None) -> Dict[str, Any]:
        """吏员健康检查：在线/离线/心跳超时"""
        if clerk_id:
            clerk = self._registry.get_clerk(clerk_id)
            if clerk is None:
                return {"clerk_id": clerk_id, "status": "not_found"}
            return self._check_clerk_health(clerk_id, clerk)

        results = {}
        for cid in self._registry._clerks:
            clerk = self._registry.get_clerk(cid)
            if clerk:
                results[cid] = self._check_clerk_health(cid, clerk)
        return results

    def _check_clerk_health(self, clerk_id: str, clerk) -> Dict[str, Any]:
        """检查单个吏员健康状态"""
        status = "unknown"
        detail = {}
        if hasattr(clerk, 'is_running') and callable(clerk.is_running):
            running = clerk.is_running()
            status = "online" if running else "offline"
            detail["process_running"] = running
        if hasattr(clerk, 'last_heartbeat'):
            import time
            age = time.time() - clerk.last_heartbeat
            detail["last_heartbeat_ago"] = round(age, 1)
            if age > 120:
                status = "timeout"
        if hasattr(self._registry, 'has_running_tasks'):
            detail["has_running_tasks"] = self._registry.has_running_tasks(clerk_id)
        return {
            "clerk_id": clerk_id,
            "name": clerk.config.name if hasattr(clerk, 'config') else clerk_id,
            "status": status,
            "detail": detail,
        }

    def record_heartbeat(self, clerk_id: str, status: str = "running") -> None:
        """记录吏员心跳（由 SSE 事件回调调用）"""
        self._heartbeats[clerk_id] = time.time()

    def start_monitor(self) -> None:
        """启动后台监控线程"""
        if self._monitor_running:
            return
        self._monitor_running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info("Steward monitor started")

    def stop_monitor(self) -> None:
        """停止后台监控"""
        self._monitor_running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)

    def _monitor_loop(self) -> None:
        """后台监控循环：检查所有吏员心跳"""
        while self._monitor_running:
            try:
                for clerk_id in list(self._registry._clerks):
                    try:
                        clerk = self._registry.get_clerk(clerk_id)
                        if not clerk:
                            continue
                        # 检查进程死活
                        running = False
                        if hasattr(clerk, 'is_running') and callable(clerk.is_running):
                            running = clerk.is_running()
                        # 检查心跳超时
                        last_beat = self._heartbeats.get(clerk_id, 0)
                        now = time.time()

                        if running and last_beat > 0 and (now - last_beat) > self._heartbeat_timeout:
                            # 进程在但心跳超时
                            if self._active_alerts.get(clerk_id) == "heartbeat_timeout":
                                continue  # 已告警，不重复
                            self._active_alerts[clerk_id] = "heartbeat_timeout"
                            alert = {
                                "clerk_id": clerk_id,
                                "alert": "heartbeat_timeout",
                                "message": f"吏员进程在但心跳超时 {int(now - last_beat)}s",
                                "time": now,
                            }
                            self._alerts.append(alert)
                            logger.warning("Clerk %s heartbeat timeout: %ds", clerk_id, int(now - last_beat))
                        elif not running and last_beat > 0:
                            # 进程挂了
                            if self._active_alerts.get(clerk_id) == "process_down":
                                continue  # 已告警，不重复
                            self._active_alerts[clerk_id] = "process_down"
                            alert = {
                                "clerk_id": clerk_id,
                                "alert": "process_down",
                                "message": "吏员进程已退出",
                                "time": now,
                            }
                            self._alerts.append(alert)
                            logger.error("Clerk %s process died", clerk_id)
                        elif running and last_beat > 0 and (now - last_beat) <= self._heartbeat_timeout:
                            # 恢复了，清除告警状态
                            self._active_alerts.pop(clerk_id, None)
                    except Exception as e:
                        logger.debug("Monitor check for %s: %s", clerk_id, e)
            except Exception as e:
                logger.debug("Monitor loop error: %s", e)

            time.sleep(self._monitor_interval)

    def clerk_alerts(self, clerk_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """获取监控告警历史"""
        if clerk_id:
            return [a for a in self._alerts if a["clerk_id"] == clerk_id][-limit:]
        return self._alerts[-limit:]

    def monitor_status(self) -> Dict[str, Any]:
        """获取监控系统状态"""
        return {
            "running": self._monitor_running,
            "heartbeat_timeout": self._heartbeat_timeout,
            "monitor_interval": self._monitor_interval,
            "tracked_clerks": len(self._heartbeats),
            "alert_count": len(self._alerts),
            "recent_alerts": self._alerts[-5:] if self._alerts else [],
        }

    # ──────────────────────────────
    # Phase 3: 流水线编排
    # ──────────────────────────────

    def create_pipeline(self, name: str, stages_def: List[Dict[str, Any]]) -> Pipeline:
        """从定义创建流水线

        stages_def 格式:
        [{"stage_id": "s1", "clerk_id": "clerk_xxx", "task": "...", "depends_on": []}, ...]

        Returns:
            Pipeline 对象
        """
        pid = f"pipe-{uuid.uuid4().hex[:10]}"
        pipeline = Pipeline(pipeline_id=pid, name=name)

        for sd in stages_def:
            sid = sd["stage_id"]
            stage = PipelineStage(
                stage_id=sid,
                clerk_id=sd["clerk_id"],
                task=sd.get("task", ""),
                depends_on=sd.get("depends_on", []),
            )
            pipeline.stages[sid] = stage

        self._pipelines[pid] = pipeline
        logger.info("Pipeline %s (%s) created: %d stages", pid, name, len(pipeline.stages))
        return pipeline

    def execute_pipeline(self, pipeline_id: str) -> Dict[str, Any]:
        """执行流水线：按依赖顺序触发各阶段

        阶段间通过检查点传递结果。失败阶段后不继续。
        """
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            return {"success": False, "error": f"流水线不存在: {pipeline_id}"}

        pipeline.state = "running"
        pipeline.started_at = time.time()

        # 检查点恢复：跳过已完成的阶段
        order = pipeline.stage_order()
        skip_until_done = pipeline.checkpoint_stage_id is not None

        results = {}
        for sid in order:
            stage = pipeline.stages[sid]

            # 检查点跳过
            if skip_until_done:
                if sid == pipeline.checkpoint_stage_id:
                    skip_until_done = False
                continue

            # 检查依赖是否全部完成
            deps_ok = all(
                pipeline.stages[d].state == "done"
                for d in stage.depends_on
            )
            if not deps_ok:
                stage.state = "failed"
                stage.error = "依赖阶段未完成"
                results[sid] = {"state": "failed", "error": stage.error}
                break

            # 收集上游结果
            upstream = {}
            for d in stage.depends_on:
                ds = pipeline.stages[d]
                if ds.result:
                    upstream[d] = ds.result

            # 执行阶段
            stage.state = "running"
            stage.started_at = time.time()
            try:
                stage_result = self._execute_pipeline_stage(stage, upstream)
                stage.state = "done"
                stage.completed_at = time.time()
                stage.result = stage_result
                results[sid] = {"state": "done", "result": stage_result}
                # 检查点
                pipeline.checkpoint_stage_id = sid
                pipeline.checkpoint_data = {"results": results, "upstream": upstream}
                logger.info("Pipeline %s stage %s done", pipeline_id, sid)
            except Exception as e:
                stage.state = "failed"
                stage.error = str(e)
                stage.completed_at = time.time()
                results[sid] = {"state": "failed", "error": str(e)}
                logger.error("Pipeline %s stage %s failed: %s", pipeline_id, sid, e)
                break

        # 判定最终状态
        all_done = all(s.state == "done" for s in pipeline.stages.values())
        pipeline.state = "done" if all_done else "failed"
        pipeline.completed_at = time.time()

        return {
            "pipeline_id": pipeline_id,
            "state": pipeline.state,
            "stages": {sid: results[sid] for sid in results},
        }

    def _execute_pipeline_stage(self, stage: PipelineStage, upstream: Dict[str, Any]) -> Dict[str, Any]:
        """执行单个流水线阶段

        当前实现：通过 Steward task 机制调度吏员。
        上游结果通过 task 参数中的 _upstream 传递，吏员自行解析。
        """
        task_desc = stage.task
        upstream_summary = {}
        # 如果有上游结果，注入到任务描述中（大数据只传引用摘要）
        if upstream:
            for k, v in upstream.items():
                if isinstance(v, dict):
                    upstream_summary[k] = {
                        "summary": str(v.get("result", v))[:200],
                        "_ref": v.get("_ref", ""),  # 大数据引用
                    }
            task_desc = f"{stage.task}\n上游阶段结果: {upstream_summary}"

        # 创建单子任务并执行
        sub = SubTask(
            sub_id=f"pipe-sub-{stage.stage_id}",
            description=task_desc,
            clerk_id=stage.clerk_id,
            tool_name="task/execute",  # MCP 通用执行方法
            tool_params={"task": task_desc, "_upstream": upstream_summary},
        )

        task = StewardTask(task_id=f"pipe-task-{stage.stage_id}", user_request=task_desc)
        task.sub_tasks[sub.sub_id] = sub
        self._tasks[task.task_id] = task

        self._execute_sub(task, sub)

        return sub.result or {"result": "done"}

    def get_pipeline(self, pipeline_id: str) -> Optional[Pipeline]:
        """获取流水线状态"""
        return self._pipelines.get(pipeline_id)

    def list_pipelines(self) -> List[Dict[str, Any]]:
        """列出所有流水线"""
        return [p.to_dict() for p in self._pipelines.values()]

    # ──────────────────────────────
    # 内部
    # ──────────────────────────────

    def _get_clerks_info(self) -> List[Dict[str, Any]]:
        """获取所有可用吏员的描述信息（供 LLM 路由决策用）"""
        clerks = []
        for clerk in self._registry.list_clerks():
            tools = clerk.get("tools", []) if isinstance(clerk, dict) else []
            # tools 可能是字符串列表或字典列表，统一处理
            tool_info = []
            for t in tools:
                if isinstance(t, str):
                    tool_info.append({"name": t, "description": t})
                elif isinstance(t, dict):
                    tool_info.append({"name": t.get("name", ""), "description": t.get("description", "")})
            # 吏员 skills 从 registry._clerks 原始数据获取
            raw_clerk = self._registry._clerks.get(clerk.get("clerk_id", ""), {})
            clerk_skills = []
            raw_worker = raw_clerk.get("worker", None)
            if raw_worker and hasattr(raw_worker, 'config'):
                clerk_skills = getattr(raw_worker.config, 'skills', []) or []
            clerks.append({
                "clerk_id": clerk.get("clerk_id", "unknown"),
                "name": clerk.get("name", ""),
                "description": clerk.get("description", ""),
                "skills": clerk_skills,
            })
        return clerks

    def _add_sub_task(self, task: StewardTask, sub_def: Dict[str, Any]) -> SubTask:
        """添加子任务到管家任务"""
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        st = SubTask(
            sub_id=sub_id,
            description=sub_def.get("description", ""),
            clerk_id=sub_def.get("clerk_id", ""),
            tool_name=sub_def.get("tool_name", ""),
            tool_params=sub_def.get("tool_params", {}),
            depends_on=sub_def.get("depends_on", []),
        )
        task.sub_tasks[sub_id] = st

        # 更新依赖关系
        for dep_id in st.depends_on:
            dep = task.sub_tasks.get(dep_id)
            if dep:
                dep.dependents.append(sub_id)

        return st

    def _get_ready(self, task: StewardTask) -> List[SubTask]:
        """获取所有依赖已满足、可执行的子任务"""
        ready = []
        for st in task.sub_tasks.values():
            if st.state != "pending":
                continue
            # 检查所有依赖是否已完成
            deps_met = all(
                task.sub_tasks.get(did) and task.sub_tasks[did].state == "done"
                for did in st.depends_on
            )
            if deps_met:
                ready.append(st)
        return ready

    def _execute_sub(self, task: StewardTask, st: SubTask) -> None:
        """执行单个子任务"""
        st.state = "running"
        st.started_at = time.time()

        clerk = self._registry.get_clerk(st.clerk_id)
        if clerk is None:
            st.state = "failed"
            st.error = f"吏员不存在: {st.clerk_id}"
            st.completed_at = time.time()
            return

        # 解析 $ref 引用（替换为上游任务的产物）
        resolved_params = st.resolve_params(task)

        def _do():
            return clerk.execute(st.tool_name, resolved_params)

        # 提交到线程池
        try:
            st.submitted_task_id = self._executor.submit(
                clerk_id=st.clerk_id,
                tool_name=st.tool_name,
                params=resolved_params,
                execute_fn=_do,
            )
            # 等待执行完成（使用精确 task_id 查询）
            self._wait_for_sub(task.task_id, st.sub_id)
            # 吏员产出 → 写入主记忆
            if st.state == "done":
                self._write_sub_result_to_memory(st, task)
        except Exception as e:
            st.state = "failed"
            st.error = str(e)
            st.completed_at = time.time()
            logger.error("Steward sub-task %s/%s failed: %s", task.task_id, st.sub_id, e)

    def _wait_for_sub(self, task_id: str, sub_id: str, timeout: float = 120.0) -> None:
        """等待子任务完成（通过精确 task_id 查询 tracker）"""
        task = self._tasks.get(task_id)
        if not task:
            return
        st = task.sub_tasks.get(sub_id)
        if not st:
            return

        if not st.submitted_task_id:
            st.state = "failed"
            st.error = "未提交到执行器"
            st.completed_at = time.time()
            return

        start = time.time()
        while time.time() - start < timeout:
            tt = self._executor._tracker.get(st.submitted_task_id)
            if tt is None:
                time.sleep(1)
                continue
            status = tt.get("status", "")
            if status == "completed":
                st.state = "done"
                st.result = tt.get("result", {})
                st.completed_at = time.time()
                return
            if status == "failed":
                st.state = "failed"
                st.error = tt.get("error", "未知错误")
                st.completed_at = time.time()
                return
            time.sleep(1)

        st.state = "failed"
        st.error = f"超时 ({timeout}s)"
        st.completed_at = time.time()

    def _write_sub_result_to_memory(self, st: SubTask, task: StewardTask) -> None:
        """将吏员子任务产出写入史佚主记忆库

        提取结果中的 new_fragments（吏员显式标记的记忆碎片），
        如果没有显式标记但有文本结果，自动提取为一条记忆。
        """
        memory_engine = getattr(self._registry, '_memory_engine', None)
        if memory_engine is None or not st.result:
            return

        fragments_written = 0

        # 1. 优先处理吏员显式标记的 new_fragments
        new_fragments = st.result.get('new_fragments', [])
        if isinstance(new_fragments, list):
            for frag in new_fragments:
                content = frag.get('content', '') if isinstance(frag, dict) else str(frag)
                if not content or not content.strip():
                    continue
                try:
                    memory_engine.remember(
                        content=content,
                        source_conversation_id=task.task_id,
                        reply_context=f"[吏员:{st.clerk_id}] {st.description}",
                    )
                    fragments_written += 1
                except Exception as e:
                    logger.error("Steward memory write failed for sub %s: %s", st.sub_id, e)

        # 2. 如果没有显式 fragment 但结果有文本内容，自动提取
        if fragments_written == 0:
            text_result = st.result.get('result', '') or st.result.get('content', '')
            if isinstance(text_result, str) and len(text_result.strip()) > 20:
                try:
                    # 截断到合理长度
                    summary = text_result.strip()[:500]
                    memory_engine.remember(
                        content=f"[{st.description}] {summary}",
                        source_conversation_id=task.task_id,
                        reply_context=f"[吏员:{st.clerk_id}]",
                    )
                    fragments_written += 1
                except Exception as e:
                    logger.error("Steward memory auto-write failed for sub %s: %s", st.sub_id, e)

        if fragments_written > 0:
            logger.info("Steward: wrote %d fragments from sub %s to memory", fragments_written, st.sub_id)

    def _all_terminal(self, task: StewardTask) -> bool:
        return all(st.is_terminal for st in task.sub_tasks.values())

    def _all_success(self, task: StewardTask) -> bool:
        return all(st.state == "done" for st in task.sub_tasks.values())

    @property
    def task_count(self) -> int:
        return len(self._tasks)


# ═══════════════════════════════════════════════════════
# 默认 LLM 拆解/汇总回调（需由 shell 层注入实现）
# ═══════════════════════════════════════════════════════

def create_default_decompose_fn(
    llm_chat_fn: Callable[[List[Dict[str, str]]], str]
) -> Callable[[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """创建默认的 LLM 拆解函数

    Args:
        llm_chat_fn: LLM调用函数，接收 messages 返回文本

    Returns:
        decompose_fn：接收 (user_request, clerks_info)，返回子任务列表
    """

    SYSTEM_PROMPT = """你是一个任务拆解专家。请将用户的请求拆解为多个可执行的子任务。

每个子任务应该：
1. 描述清晰，包含"做什么"
2. 分配给合适的吏员（clerk_id）
3. 指定要调用的工具（tool_name）
4. 提供工具参数（tool_params）
5. 声明依赖关系（depends_on），使用拆解后的 sub_id（如 sub-xxx）

重要约束：
- 只拆解可以通过工具完成的任务，不要拆分自然语言对话
- 如果请求可以一步完成，返回单个子任务
- 依赖关系用子任务ID表示，例如 ["sub-xxx"]
- 所有子任务的 tool_name 必须是 clerks_info 中列出的工具

产物格式（JSON数组）：
[
  {
    "description": "用中文描述这个子任务",
    "clerk_id": "吏员ID",
    "tool_name": "工具名",
    "tool_params": {"参数名": "参数值"},
    "depends_on": ["sub-xxx"]  // 可选，依赖的其他子任务ID
  }
]"""

    def default_decompose_fn(
        user_request: str,
        clerks_info: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """LLM 拆解用户请求为子任务列表

        Args:
            user_request: 用户原始请求
            clerks_info: 可用吏员列表及其工具描述

        Returns:
            子任务定义列表
        """
        # 构造吏员描述
        clerks_desc = []
        for clerk in clerks_info:
            tools_desc = "\n".join(
                f'    - {t["name"]}: {t["description"]}'
                for t in clerk.get("tools", [])
            )
            clerks_desc.append(
                f'- {clerk["clerk_id"]}: {clerk.get("description", "")}\n'
                f'  工具:\n{tools_desc}'
            )

        user_prompt = f"""用户请求：
{user_request}

可用吏员：
{chr(10).join(clerks_desc)}

请拆解这个请求。直接返回JSON数组，不要有其他文字。"""

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            response = llm_chat_fn(messages)
            # 提取JSON数组
            response = response.strip()
            # 处理可能的markdown代码块
            if response.startswith("```"):
                lines = response.split("\n")
                response = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            # 解析JSON
            sub_defs = json.loads(response)
            if not isinstance(sub_defs, list):
                logger.error("LLM返回的不是数组: %s", type(sub_defs))
                return []
            return sub_defs
        except json.JSONDecodeError as e:
            logger.error("LLM返回JSON解析失败: %s\n原始内容: %s", e, response[:500])
            return []
        except Exception as e:
            logger.error("LLM拆解失败: %s", e)
            raise

    return default_decompose_fn


def create_default_aggregate_fn(
    llm_chat_fn: Callable[[List[Dict[str, str]]], str]
) -> Callable[[str, List[Dict[str, Any]]], str]:
    """创建默认的 LLM 汇总函数

    Args:
        llm_chat_fn: LLM调用函数，接收 messages 返回文本

    Returns:
        aggregate_fn：接收 (user_request, sub_results)，返回汇总文本
    """

    SYSTEM_PROMPT = """你是一个任务汇总专家。请根据用户请求和各个子任务的结果，生成一个完整的汇总回复。

要求：
1. 直接回答用户的问题
2. 整合各子任务的结果，形成连贯的回复
3. 如果某个子任务失败，说明情况
4. 用中文回复，简洁有条理
5. 不要重复列出每个子任务的细节，而是整合成完整的答案"""

    def default_aggregate_fn(
        user_request: str,
        sub_results: List[Dict[str, Any]]
    ) -> str:
        """LLM 汇总子任务结果

        Args:
            user_request: 用户原始请求
            sub_results: 子任务结果列表 [{"description": str, "result": dict}]

        Returns:
            汇总后的回复文本
        """
        # 构造子任务结果描述
        results_desc = []
        for i, sr in enumerate(sub_results, 1):
            desc = sr.get("description", "未知任务")
            result = sr.get("result", {})

            # 提取结果内容（支持新旧格式）
            if isinstance(result, dict):
                if 'artifacts' in result:
                    # 新格式
                    texts = []
                    for art in result.get('artifacts', []):
                        if art.get('type') == 'text':
                            texts.append(art.get('content', ''))
                    content = "\n".join(texts) if texts else str(result)
                elif 'result' in result:
                    content = result.get('result', '')
                else:
                    content = str(result)
            else:
                content = str(result)

            results_desc.append(f"任务{i}: {desc}\n结果: {content[:500]}")

        user_prompt = f"""用户请求：
{user_request}

子任务结果：
{chr(10).join(results_desc)}

请根据以上信息，生成完整的回复。直接输出回复内容，不要有其他解释。"""

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            return llm_chat_fn(messages)
        except Exception as e:
            logger.error("LLM汇总失败: %s", e)
            # fallback：简单拼接
            fallback = []
            for sr in sub_results:
                r = sr.get("result", {})
                content = r.get("result", "") or r.get("content", "") or str(r)
                fallback.append(content[:200])
            return "\n\n".join(fallback)

    return default_aggregate_fn


# ═══════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════

def format_artifacts(result: Dict[str, Any]) -> Dict[str, Any]:
    """格式化结果为标准 artifacts 格式

    兼容旧格式和新格式，将结果转换为统一的 artifacts 格式。

    Args:
        result: 原始结果

    Returns:
        标准化后的结果，包含 artifacts 列表
    """
    if 'artifacts' in result:
        return result

    # 从旧格式提取文本内容
    text = result.get('result', '') or result.get('content', '') or result.get('text', '')
    if isinstance(text, str):
        return {
            "success": True,
            "artifacts": [{"type": "text", "content": text}],
        }
    return {
        "success": True,
        "artifacts": [{"type": "text", "content": str(text)}],
    }

