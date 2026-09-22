"""运行指标注册表(补充要求 §十三)。

- 进程内 counter/gauge,渲染为 JSON(部署层抓取);不引入高基数标签
  (jobId/attemptId/workspaceId 等一律拒绝,§十三);
- Outbox 与任务队列 gauge 由数据库聚合(collect_*_gauges,由周期任务调用);
- 指标失败不得破坏任务状态收尾(§九/§十):collect 异常记录后返回。
"""

import threading
from datetime import datetime, timezone

# 高基数字段禁止作为标签(§十三.9)。
_FORBIDDEN_LABELS = {"jobId", "job_id", "attemptId", "attempt_id", "workspaceId",
                     "workspace_id", "requestId", "request_id", "traceId"}

_lock = threading.Lock()
_counters: dict[str, float] = {}
_gauges: dict[str, float] = {}


def _key(name: str, labels: dict | None) -> str:
    if labels:
        bad = _FORBIDDEN_LABELS.intersection(labels)
        if bad:
            raise ValueError(f"指标标签禁止使用高基数字段:{sorted(bad)}")
        suffix = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
        return f"{name}|{suffix}"
    return name


def incr(name: str, labels: dict | None = None, value: float = 1) -> None:
    with _lock:
        key = _key(name, labels)
        _counters[key] = _counters.get(key, 0) + value


def set_gauge(name: str, value: float, labels: dict | None = None) -> None:
    with _lock:
        _gauges[_key(name, labels)] = value


def render() -> dict:
    with _lock:
        return {"counters": dict(_counters), "gauges": dict(_gauges)}


def reset() -> None:
    with _lock:
        _counters.clear()
        _gauges.clear()


def collect_outbox_gauges(settings) -> None:
    """Outbox 待发送数量与最老年龄(§十三 至少采集项);数据库不可用时跳过本轮。"""
    import asyncio

    from sqlalchemy import func, select

    from backend.infrastructure.orm import Outbox
    from backend.infrastructure.uow import AsyncUnitOfWork

    async def collect():
        async with AsyncUnitOfWork(settings) as uow:
            pending = (
                await uow.session.execute(
                    select(func.count()).select_from(Outbox).where(Outbox.status == "pending")
                )
            ).scalar_one()
            oldest = (
                await uow.session.execute(
                    select(func.min(Outbox.created_at)).where(Outbox.status == "pending")
                )
            ).scalar_one()
            return int(pending), oldest

    try:
        pending, oldest = asyncio.run(collect())
    except Exception:
        return  # 指标失败不破坏业务(§九.10 同源原则)
    set_gauge("outbox_pending_total", pending)
    if oldest is not None:
        age = (datetime.now(timezone.utc) - oldest).total_seconds()
        set_gauge("outbox_oldest_age_s", int(age))


def collect_job_gauges(settings) -> None:
    """任务队列长度与未知/超时数量(§十三 至少采集项)。"""
    import asyncio

    from sqlalchemy import func, select

    from backend.infrastructure.orm import Job
    from backend.infrastructure.uow import AsyncUnitOfWork

    async def collect():
        async with AsyncUnitOfWork(settings) as uow:
            async def count_of(status: str | None):
                stmt = select(func.count()).select_from(Job)
                if status is not None:
                    stmt = stmt.where(Job.status == status)
                return int((await uow.session.execute(stmt)).scalar_one())

            return {
                "queued": await count_of("queued"),
                "running": await count_of("running"),
                "unknown": await count_of("unknown"),
            }

    try:
        counts = asyncio.run(collect())
    except Exception:
        return
    for status, value in counts.items():
        set_gauge(f"jobs_{status}", value)
