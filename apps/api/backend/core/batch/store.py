"""Global quota updates. One try-lock on quota_key='global'; no in-process semaphore."""
from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError

from backend.core.batch.contracts import BatchSettings
from backend.core.batch.orm import CoreBatchDispatch, CoreBatchQuota, CoreBatchRetry
from backend.core.batch.schedule import claim_decision

GLOBAL_QUOTA_KEY = "global"


def _lock_unavailable(exc: BaseException) -> bool:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen:
        seen.append(current)
        if getattr(current, "sqlstate", None) == "55P03" or getattr(current, "pgcode", None) == "55P03":
            return True
        current = getattr(current, "orig", None) or current.__cause__
    text_value = str(exc).lower()
    return "55p03" in text_value or "could not obtain lock" in text_value


async def ensure_quota(tx, workspace_id=None, settings: BatchSettings | None = None) -> CoreBatchQuota:
    """Insert the single global row. ``workspace_id`` is ignored; the cap is process-wide."""

    settings = settings or BatchSettings()
    row = await tx.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == GLOBAL_QUOTA_KEY))
    if row is not None:
        return row
    table = CoreBatchQuota.__table__
    statement = pg_insert(table).values(
        id=uuid.uuid4(), quota_key=GLOBAL_QUOTA_KEY, workspace_id=None,
        inflight=0, limit_value=settings.global_concurrency)
    statement = statement.on_conflict_do_nothing(index_elements=["quota_key"])
    await tx.execute(statement)
    return await tx.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == GLOBAL_QUOTA_KEY))


async def _lock_global(tx) -> str | None:
    """Try-lock the global row. Returns ``quota_busy`` instead of waiting on another run."""

    try:
        async with tx.begin_nested():
            locked = await tx.scalar(select(CoreBatchQuota).where(
                CoreBatchQuota.quota_key == GLOBAL_QUOTA_KEY).with_for_update(nowait=True))
    except DBAPIError as exc:
        if _lock_unavailable(exc):
            return "quota_busy"
        raise
    if locked is None:
        return "quota_busy"
    return None


async def claim_dispatch(tx, *, workspace_id, run_id, task_key: str, credits: int,
                         budget_left: int, settings: BatchSettings | None = None) -> str:
    """Reserve one global in-flight slot. Does not queue or ack the task."""

    settings = settings or BatchSettings()
    existing = await tx.scalar(select(CoreBatchDispatch).where(
        CoreBatchDispatch.run_id == run_id, CoreBatchDispatch.task_key == task_key))
    if existing is not None:
        return "duplicate:" + existing.state
    await ensure_quota(tx, workspace_id, settings)
    busy = await _lock_global(tx)
    if busy:
        return busy
    quota = await tx.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == GLOBAL_QUOTA_KEY))
    run_open = int(await tx.scalar(text(
        """SELECT count(*) FROM core_batch_dispatches
           WHERE run_id = :run AND state IN ('reserved','submitted')"""), {"run": run_id}) or 0)
    capped = settings.model_copy(update={"global_concurrency": int(quota.limit_value)})
    decision = claim_decision(inflight=quota.inflight, run_inflight=run_open, credits=credits,
                              budget_left=budget_left, existing=None, settings=capped)
    if decision == "budget_rejected":
        tx.add(CoreBatchDispatch(workspace_id=workspace_id, run_id=run_id, task_key=task_key,
                                 idempotency_key=f"{run_id}:{task_key}", state="rejected_budget", credits=credits))
        await tx.flush()
        return "budget_rejected"
    if decision != "reserve":
        return decision
    changed = await tx.execute(text(
        """UPDATE core_batch_quotas SET inflight = inflight + 1
           WHERE quota_key = :key AND inflight + 1 <= limit_value
           RETURNING inflight"""), {"key": GLOBAL_QUOTA_KEY})
    if changed.first() is None:
        return "concurrency_rejected"
    tx.add(CoreBatchDispatch(workspace_id=workspace_id, run_id=run_id, task_key=task_key,
                             idempotency_key=f"{run_id}:{task_key}", state="reserved", credits=credits))
    await tx.flush()
    return "reserved"


async def ack_dispatch(tx, run_id, task_key: str) -> str:
    row = await tx.scalar(select(CoreBatchDispatch).where(
        CoreBatchDispatch.run_id == run_id, CoreBatchDispatch.task_key == task_key).with_for_update())
    if row is None:
        return "missing"
    if row.state in {"submitted", "settled"}:
        return "duplicate:" + row.state
    if row.state != "reserved":
        return row.state
    row.state = "submitted"
    await tx.flush()
    return "submitted"


async def _release_global(tx) -> None:
    await tx.execute(text(
        """UPDATE core_batch_quotas SET inflight = inflight - 1
           WHERE quota_key = :key AND inflight > 0"""), {"key": GLOBAL_QUOTA_KEY})


async def settle_dispatch(tx, run_id, task_key: str) -> str:
    """Release a slot only after this run recorded a submitted dispatch.

    A second call sees ``settled`` and does not decrement again. Missing rows,
    including an unknown submit that never acked, do not release.
    """

    row = await tx.scalar(select(CoreBatchDispatch).where(
        CoreBatchDispatch.run_id == run_id, CoreBatchDispatch.task_key == task_key).with_for_update())
    if row is None:
        return "not_submitted"
    if row.state == "settled":
        return "duplicate:settled"
    if row.state != "submitted":
        return "not_submitted"
    if await _lock_global(tx):
        return "quota_busy"
    row.state = "settled"
    await _release_global(tx)
    await tx.flush()
    return "settled"


async def release_reserved(tx, run_id, task_key: str) -> str:
    """Drop a reservation that never reached ack. Submitted rows stay put."""

    row = await tx.scalar(select(CoreBatchDispatch).where(
        CoreBatchDispatch.run_id == run_id, CoreBatchDispatch.task_key == task_key).with_for_update())
    if row is None:
        return "missing"
    if row.state == "settled":
        return "duplicate:settled"
    if row.state != "reserved":
        return "not_reserved"
    if await _lock_global(tx):
        return "quota_busy"
    row.state = "settled"
    await _release_global(tx)
    await tx.flush()
    return "released"


async def claim_dispatch_many(tx, *, workspace_id, run_id, items: list[tuple[str, int]],
                              budget_left: int, limit: int, settings: BatchSettings | None = None) -> list[str]:
    """Lock the global row once and reserve at most ``limit`` new slots. Full quota does not query per task."""

    settings = settings or BatchSettings()
    if not items:
        return []
    keys = [key for key, _credits in items]
    existing_rows = list(await tx.scalars(select(CoreBatchDispatch).where(
        CoreBatchDispatch.run_id == run_id, CoreBatchDispatch.task_key.in_(keys))))
    existing = {row.task_key: row.state for row in existing_rows}
    await ensure_quota(tx, workspace_id, settings)
    if await _lock_global(tx):
        return ["quota_busy" for _item in items]
    quota = await tx.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == GLOBAL_QUOTA_KEY))
    run_open = int(await tx.scalar(text(
        """SELECT count(*) FROM core_batch_dispatches
           WHERE run_id = :run AND state IN ('reserved','submitted')"""), {"run": run_id}) or 0)
    room = min(int(limit), int(quota.limit_value) - int(quota.inflight), settings.max_per_run - run_open)
    room = max(room, 0)
    taken = 0
    decisions = []
    for key, credits in items:
        state = existing.get(key)
        if state in {"reserved", "submitted", "settled"}:
            decisions.append("duplicate:" + state)
            continue
        if credits > budget_left:
            tx.add(CoreBatchDispatch(workspace_id=workspace_id, run_id=run_id, task_key=key,
                                     idempotency_key=f"{run_id}:{key}", state="rejected_budget", credits=credits))
            decisions.append("budget_rejected")
            continue
        if taken >= room:
            decisions.append("concurrency_rejected")
            continue
        tx.add(CoreBatchDispatch(workspace_id=workspace_id, run_id=run_id, task_key=key,
                                 idempotency_key=f"{run_id}:{key}", state="reserved", credits=credits))
        decisions.append("reserved")
        taken += 1
    if taken:
        changed = await tx.execute(text(
            """UPDATE core_batch_quotas SET inflight = inflight + :n
               WHERE quota_key = :key AND inflight + :n <= limit_value
               RETURNING inflight"""), {"key": GLOBAL_QUOTA_KEY, "n": taken})
        if changed.first() is None:
            raise RuntimeError("batch quota room changed under the global lock")
    await tx.flush()
    return decisions


async def recover_reserved(tx, workspace_id, limit: int) -> list[CoreBatchDispatch]:
    return list(await tx.scalars(select(CoreBatchDispatch).where(
        CoreBatchDispatch.workspace_id == workspace_id, CoreBatchDispatch.state == "reserved")
        .order_by(CoreBatchDispatch.created_at, CoreBatchDispatch.id).limit(limit)))


async def find_retry(tx, parent_run_id, request_key: str) -> CoreBatchRetry | None:
    return await tx.scalar(select(CoreBatchRetry).where(
        CoreBatchRetry.parent_run_id == parent_run_id, CoreBatchRetry.request_key == request_key))
