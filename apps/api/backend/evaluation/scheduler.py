"""评测 run 的受控后台执行器(§14.1,修复"202 却同步等运行结束"缺陷)。

设计边界:
- 单进程部署(AGENTS.md);执行在固定大小的 ThreadPoolExecutor 中进行,
  不使用无管理线程或 fire-and-forget 任务;并发上限即 worker 数。
- 同一 run 独占执行:排队中或执行中的 runId 不会再次入队,重启恢复亦然。
- 取消:排队中直接取消;执行中的 run 通过 cancel_check 协作式停止——
  停止派发新 trial,已发出的外部请求保留预算预留并按 unknown 对账。
  执行线程真正退出后 Future 才完成,并发槽位不会提前释放。
- 重启恢复:start() 扫描 state.json;mock/replay 无外部副作用可安全重新入队;
  live 存在上游未知结果,只能标记 interrupted 等待人工对账,绝不自动重提。
- 完成后按抽样策略自动创建审核任务(必审/风险/随机,§11.2),使门禁的
  "必审未完成不得通过"真实可满足。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from .budgets import BudgetLedger, BudgetPolicy
from .reviews import ReviewSamplingPolicy, ReviewStore, create_scheduled_reviews
from .runner import EvaluationRunner

logger = logging.getLogger(__name__)


class EvaluationScheduler:
    _instances: dict[str, "EvaluationScheduler"] = {}
    _instances_guard = threading.Lock()

    def __init__(self, store: Path, max_concurrent: int = 1, project_budget: BudgetPolicy | None = None):
        self.store = Path(store)
        self.runner = EvaluationRunner(self.store.parent.parent, store=self.store)
        self.max_concurrent = max(1, max_concurrent)
        self.project_ledger = (
            BudgetLedger.open(self.store, "project-total", project_budget)
            if project_budget is not None
            else None
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrent, thread_name_prefix="eval-run"
        )
        self._lock = threading.Lock()
        self._queue: deque[str] = deque()
        self._active: dict[str, Future] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    # ---------- 生命周期 ----------

    @classmethod
    def instance(cls, store: Path) -> "EvaluationScheduler":
        key = str(Path(store).resolve())
        with cls._instances_guard:
            if key not in cls._instances:
                from .config import load_eval_config

                config = load_eval_config()
                project_budget = None
                if config.project_max_cost is not None:
                    generation = (
                        config.project_price_generation
                        if config.project_price_generation is not None
                        else config.project_max_cost
                    )
                    project_budget = BudgetPolicy(
                        maxCost=config.project_max_cost,
                        prices={
                            "generation": generation,
                            "translate": config.project_price_translate,
                            "poll": config.project_price_poll,
                            "judge": config.project_price_judge,
                        },
                    )
                cls._instances[key] = cls(Path(store), config.max_concurrent_runs, project_budget)
            return cls._instances[key]

    def start(self) -> None:
        """重启恢复(§5.4):扫描遗留的 queued/running/stopping 状态。"""
        runs_root = self.store / "runs"
        if not runs_root.is_dir():
            return
        for state_file in sorted(runs_root.glob("*/state.json")):
            run_id = state_file.parent.name
            state = json.loads(state_file.read_text(encoding="utf-8"))
            status = state.get("status")
            if status == "queued":
                self.submit(run_id)
                continue
            if status in {"running", "stopping"}:
                manifest_file = state_file.parent / "manifest.json"
                mode = "mock"
                if manifest_file.is_file():
                    mode = json.loads(manifest_file.read_text(encoding="utf-8")).get("mode", "mock")
                if mode in {"mock", "replay"}:
                    # 无外部副作用:安全恢复,重新入队
                    self.submit(run_id)
                else:
                    # live:上游结果未知,不盲目重提;trial 与预留保持待对账
                    state.update(
                        status="interrupted",
                        error="服务重启,上游结果未知;请对账 externalTaskId 后人工恢复",
                        interruptedAt=state.get("finishedAt"),
                    )
                    from backend.common import atomic_write_text

                    atomic_write_text(
                        state_file, json.dumps(state, ensure_ascii=False, indent=2)
                    )

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    # ---------- 提交与查询 ----------

    def submit(self, run_id: str) -> dict:
        """入队执行;返回 {queued: bool, reason}。重复提交被拒绝(独占执行)。"""
        with self._lock:
            if run_id in self._active or run_id in self._queue:
                return {"queued": False, "reason": "run 已在排队或执行中"}
            self._queue.append(run_id)
            future = self._executor.submit(self._execute_guarded, run_id)
            self._active[run_id] = future
            return {"queued": True, "reason": None}

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def active_runs(self) -> list[str]:
        with self._lock:
            return list(self._active)

    def cancel(self, run_id: str) -> dict:
        """取消:排队中直接取消;执行中写 stopping 并触发协作式停止。"""
        with self._lock:
            queued = run_id in self._queue
            if queued:
                self._queue.remove(run_id)
        if queued:
            # 排队中尚未执行:直接置终态;若 worker 恰好已把它弹出,_execute_guarded
            # 会因队列里找不到而放弃执行,不会出现取消后仍运行的竞态。
            self.runner._write_state(run_id, status="cancelled")
            EventLogSafe(self.runner.run_dir(run_id)).append("run.cancelled", run_id, phase="queued")
            with self._lock:
                future = self._active.pop(run_id, None)
            if future is not None:
                future.cancel()
            return {"runId": run_id, "status": "cancelled", "cancelRequested": True}
        state = self.runner.read_state(run_id)
        if state.get("status") in {"queued", "running"}:
            result = self.runner.request_cancel(run_id)
            with self._lock:
                event = self._cancel_events.get(run_id)
            if event is not None:
                event.set()
            return result
        return {
            "runId": run_id,
            "status": state.get("status", "unknown"),
            "cancelRequested": False,
        }

    # ---------- 执行 ----------

    def _execute_guarded(self, run_id: str) -> None:
        """worker 体:从队列弹出执行;无论成败,退出时才释放槽位。"""
        with self._lock:
            if run_id not in self._queue:
                # 已被取消(排队阶段):不再执行
                self._active.pop(run_id, None)
                return
            self._queue.remove(run_id)
            cancel_event = threading.Event()
            self._cancel_events[run_id] = cancel_event
        try:
            state = self.runner.execute_run(
                run_id,
                cancel_check=cancel_event.is_set,
                project_ledger=self.project_ledger,
            )
            if state.status == "completed":
                self._create_reviews(run_id)
        except Exception:
            logger.exception("评测 run 执行失败:%s", run_id)
        finally:
            with self._lock:
                self._active.pop(run_id, None)
                self._cancel_events.pop(run_id, None)

    def _create_reviews(self, run_id: str) -> None:
        """按抽样策略创建审核任务(环境变量 EVAL_REVIEW_SAMPLING 可覆盖)。"""
        try:
            from .config import load_eval_config

            config = load_eval_config()
            policy = (
                ReviewSamplingPolicy.model_validate_json(config.review_sampling)
                if config.review_sampling
                else ReviewSamplingPolicy()
            )
            state = self.runner.load_run(run_id)
            create_scheduled_reviews(ReviewStore(self.store), state, policy)
        except Exception:
            logger.exception("自动创建审核任务失败:%s", run_id)


class EventLogSafe:
    """调度器层的轻量事件记录:复用 runner 的 EventLog,失败不影响主流程。"""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir

    def append(self, event_type: str, run_id: str, **payload) -> None:
        try:
            from .runner import EventLog

            EventLog(self.run_dir).append(event_type, run_id, **payload)
        except Exception:
            logger.exception("事件写入失败:%s %s", event_type, run_id)
