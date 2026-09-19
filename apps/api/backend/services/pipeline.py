"""分阶段执行管线(补充要求 §七):提交 → 到期查询 → 终态,状态以数据库为唯一依据。

- 提交阶段:领取模型名额(§八,失败即延迟排队不降级放行)→ 登记上游任务 ID →
  安排下次查询(next_poll_at);
- 查询阶段:截止时间内未完成 → 退避推进(有界+抖动);完成 → 下载产物登记资产 →
  终态并释放名额;超过截止时间 → 可对账的 unknown,绝不自动重提(原已知问题 #3);
- 名额(§八.2)是上游模型执行名额,与 Worker 执行槽位不同:查询步结束即释放
  Worker 槽位(消息 ack),模型名额保持到业务终态。
"""

import asyncio
import hashlib
import random
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.infrastructure.database import DatabaseSettings

DEFAULT_SLOT_SCOPE = "stub-model"
DEFAULT_SLOT_LIMIT = 5


@dataclass(frozen=True)
class PollPolicy:
    base_backoff_s: float = 2.0
    max_backoff_s: float = 60.0
    deadline_s: float = 900.0
    jitter_ratio: float = 0.1

    def next_delay(self, attempt: int) -> float:
        delay = min(self.base_backoff_s * (2 ** max(0, attempt)), self.max_backoff_s)
        jitter = delay * self.jitter_ratio
        return delay + random.uniform(0, jitter)


@dataclass
class PollResult:
    finished: bool
    data: bytes | None = None


class StubUpstream:
    """测试用上游:按 task_id 控制未完成次数与产物(不访问真实付费服务)。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._remaining: dict[str, int] = {}
        self._default = 0

    def configure(self, finish_after: int, *, task_id: str | None = None):
        with self._lock:
            if task_id is None:
                self._default = finish_after
                self._remaining.clear()
            else:
                self._remaining[task_id] = finish_after

    def poll(self, task_id: str) -> PollResult:
        with self._lock:
            remaining = self._remaining.get(task_id, self._default)
            if remaining > 0:
                self._remaining[task_id] = remaining - 1
                return PollResult(finished=False)
        payload = f"stub-image:{task_id}".encode()
        return PollResult(finished=True, data=payload)


_stub_upstream: StubUpstream | None = None


def get_stub_upstream() -> StubUpstream:
    """进程内单例:Celery solo/线程池 worker 的多个任务共享同一 stub 状态。"""
    global _stub_upstream
    if _stub_upstream is None:
        _stub_upstream = StubUpstream()
    return _stub_upstream


async def execute_generation_step(
    job_id: uuid.UUID,
    settings: DatabaseSettings,
    upstream=None,
    policy: PollPolicy | None = None,
) -> dict:
    """提交阶段(消费 job.created):名额→提交→登记上游→安排查询;取消检查点优先。"""
    from backend.infrastructure.uow import AsyncUnitOfWork

    upstream = upstream or get_stub_upstream()
    policy = policy or PollPolicy()
    async with AsyncUnitOfWork(settings) as uow:
        job = await uow.jobs.get(job_id)
        if job is None:
            return {"cancelled": False, "missing": True}
        if job.recovery == "abandon":
            return {"cancelled": True, "upstream_cancelled": None, "errorCode": job.error_code}
        if job.status != "queued":
            return {"cancelled": False, "skipped": True, "status": job.status}
        if not await uow.jobs.try_acquire_slot(
            job_id, scope=DEFAULT_SLOT_SCOPE, limit=DEFAULT_SLOT_LIMIT
        ):
            # 名额满:保持 queued,安排稍后重试;不降级为放行(§八.5),不占 Worker 槽位等待
            next_at = datetime.now(timezone.utc) + timedelta(seconds=policy.base_backoff_s)
            await uow.jobs.set_poll_schedule(job_id, next_poll_at=next_at)
            await uow.commit()
            return {"deferred": True, "reason": "slot"}
        # stub 提交:真实管线此处调用 Provider(§十五.4 后续接入),先登记上游任务 ID
        external_task_id = f"stub-{job_id.hex[:12]}"
        await uow.jobs.transition(job_id, {"queued"}, "running")
        await uow.jobs.set_poll_schedule(
            job_id,
            external_task_id=external_task_id,
            next_poll_at=datetime.now(timezone.utc) + timedelta(seconds=policy.base_backoff_s),
        )
        await uow.commit()
        return {"submitted": True, "externalTaskId": external_task_id}


async def execute_poll_step(
    job_id: uuid.UUID,
    settings: DatabaseSettings,
    upstream=None,
    policy: PollPolicy | None = None,
) -> dict:
    """查询阶段(消费 job.poll_due):未完成退避推进;完成登记资产进终态;超截止进 unknown。"""
    from backend.infrastructure.uow import AsyncUnitOfWork

    upstream = upstream or get_stub_upstream()
    policy = policy or PollPolicy()
    async with AsyncUnitOfWork(settings) as uow:
        job = await uow.jobs.get(job_id)
        if job is None:
            return {"missing": True}
        if job.recovery == "abandon":
            await uow.jobs.release_slot(job_id)
            await uow.commit()
            return {"cancelled": True, "upstream_cancelled": None}
        if job.status != "running" or not job.external_task_id:
            return {"skipped": True, "status": job.status}
        if job.slot_scope is None:
            # 名额已丢失(如失联回收):不得继续代表上游执行,回到可对账状态
            await uow.jobs.transition(job_id, {"running"}, "unknown")
            await uow.commit()
            return {"unknown": True, "reason": "slot-lost"}

        elapsed = (datetime.now(timezone.utc) - job.created_at).total_seconds()
        if elapsed > policy.deadline_s:
            await uow.jobs.transition(job_id, {"running"}, "unknown")
            await uow.session.execute(
                job.__table__.update()
                .where(job.__table__.c.id == job_id)
                .values(error_code="UPSTREAM_UNKNOWN", recovery="reconcile")
            )
            await uow.jobs.release_slot(job_id)
            await uow.commit()
            return {"unknown": True}

        result = await asyncio.to_thread(upstream.poll, job.external_task_id)
        if not result.finished:
            next_at = datetime.now(timezone.utc) + timedelta(
                seconds=policy.next_delay(job.attempt_count)
            )
            await uow.jobs.set_poll_schedule(
                job_id, next_poll_at=next_at, bump_attempt=True
            )
            await uow.commit()
            return {"rescheduled": True}

        # 完成:下载产物(此处 stub 字节)→ 登记资产 → 终态 + 释放名额
        data = result.data or b""
        await uow.assets.add(
            job.workspace_id,
            kind="image",
            ext="png",
            storage_key=f"stub/{job_id}.png",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        assert await uow.jobs.transition(job_id, {"running"}, "completed") is True
        await uow.jobs.release_slot(job_id)
        await uow.commit()
        return {"completed": True}


def project_snapshot(job, *, asset_ids: list[str] | None = None) -> dict:
    """把 Job 行投影为缓存安全快照(字段白名单,§十)。"""
    return {
        "jobId": str(job.id),
        "workspaceId": str(job.workspace_id),
        "status": job.status,
        "messageCode": job.error_code,
        "recovery": job.recovery,
        "stateVersion": job.state_version,
        "executionEpoch": job.execution_epoch,
        "progressSeq": 0,
        "progress": job.progress,
        "updatedAt": job.updated_at.isoformat() if job.updated_at else "",
        "assetIds": asset_ids or [],
    }


def scan_due_polls(settings: DatabaseSettings, limit: int = 50) -> int:
    """调度器扫描:领取到期任务并在同一事务派发事件(§七.2);由周期任务触发(§七.6)。"""
    from backend.infrastructure.uow import AsyncUnitOfWork

    async def scan() -> int:
        async with AsyncUnitOfWork(settings) as uow:
            plan = await uow.jobs.claim_due_polls(limit=limit)
            for job_id, event_type in plan:
                await uow.outbox.enqueue(
                    aggregate_type="job",
                    aggregate_id=str(job_id),
                    event_type=event_type,
                    payload={"jobId": str(job_id)},
                )
            await uow.commit()
            return len(plan)

    return asyncio.run(scan())
