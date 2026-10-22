"""Durable timers. Lock order is always Run, then timer."""
import logging
import uuid
from datetime import timedelta
from sqlalchemy import or_, select, update

from backend.core.event_context import current_event_id
from backend.core.orm import CoreRun
from backend.core.states import VersionConflict
from .contracts import EventType, timer_delay
from .orm import EventAlert, EventTimer
from .store import alert, db_now, emit
from .locking import LockBusy, bounded, locked, lock_error
from .integrity import quarantine

logger = logging.getLogger(__name__)


async def schedule(tx, run_id, event_type, due_at, *, idempotency_key, causation_id=None,
                   task_id=None, expected_version=None):
    event_type = EventType(event_type)
    cause = causation_id or current_event_id.get()
    if cause is None or due_at.tzinfo is None or not idempotency_key:
        raise ValueError("timer requires cause, timezone and idempotency key")
    if await locked(tx, CoreRun, run_id) is None:
        raise ValueError("unknown run")
    old = await tx.scalar(select(EventTimer).where(EventTimer.run_id == run_id,
        EventTimer.idempotency_key == idempotency_key))
    if old:
        if (old.event_type, old.due_at, old.causation_id, old.task_id, old.expected_version) != (
                event_type.value, due_at, cause, task_id, expected_version):
            raise ValueError("timer idempotency conflict")
        return old
    row = EventTimer(run_id=run_id, event_type=event_type.value, due_at=due_at,
        idempotency_key=idempotency_key, causation_id=cause, task_id=task_id, next_fire_at=due_at,
        expected_version=expected_version)
    tx.add(row)
    await tx.flush()
    return row


async def cancel(tx, timer_id, expected_version):
    row = await tx.get(EventTimer, timer_id)
    if row is None:
        raise ValueError("unknown timer")
    run_id, cause = row.run_id, row.causation_id
    if await locked(tx, CoreRun, run_id) is None:
        await quarantine(tx, timer_id=timer_id, run_id=row.run_id, causation_id=row.causation_id,
            code="missing_run", phase="timer_cancel")
        return None
    row = await locked(tx, EventTimer, timer_id)
    if row is None:
        await quarantine(tx, timer_id=timer_id, run_id=run_id, causation_id=cause,
            code="missing_timer", phase="timer_cancel")
        return None
    if row.version != expected_version:
        raise VersionConflict("timer version conflict")
    if row.status != "scheduled":
        raise ValueError("timer already settled")
    row.status, row.version = "cancelled", row.version + 1
    row.claim_token = row.lease_until = None
    await tx.flush()
    return row


class Timers:
    def __init__(self, factory, settings):
        self.factory, self.settings = factory, settings

    async def claim(self):
        async with self.factory.begin() as tx:
            await bounded(tx, self.settings)
            stamp = await db_now(tx)
            row = await tx.scalar(select(EventTimer).where(EventTimer.status == "scheduled",
                EventTimer.due_at <= stamp, EventTimer.next_fire_at <= stamp,
                or_(EventTimer.lease_until.is_(None), EventTimer.lease_until <= stamp))
                .order_by(EventTimer.next_fire_at, EventTimer.id).limit(1).with_for_update(skip_locked=True))
            if row is None:
                return None
            row.claim_token = uuid.uuid4()
            row.lease_until = stamp + timedelta(seconds=self.settings.timer_lease_seconds)
            return row.id, row.run_id, row.causation_id, row.claim_token

    async def defer(self, claim):
        ident, run_id, cause, token = claim
        async with self.factory.begin() as tx:
            await bounded(tx, self.settings)
            row = await locked(tx, EventTimer, ident)
            if row is None:
                return await quarantine(tx, timer_id=ident, run_id=run_id, causation_id=cause,
                    code="missing_timer", phase="timer_defer")
            if row.status != "scheduled" or row.claim_token != token:
                logger.debug("timer stale claim timer_id=%s run_id=%s", ident, run_id)
                return False
            row.lock_skip_count += 1
            row.next_fire_at = max(row.due_at, await db_now(tx) + timedelta(
                seconds=timer_delay(self.settings, row.lock_skip_count)))
            row.claim_token = row.lease_until = None
            # Retry metadata does not change the cancellation CAS version.
            if row.lock_skip_count >= self.settings.timer_skip_alert_threshold:
                await alert(tx, "backlog", None, f"timer-lock:{ident}", context={
                    "timer_id": str(ident), "run_id": str(run_id), "causation_id": str(cause),
                    "lock_skip_count": row.lock_skip_count})
            return False

    async def fire(self, claim):
        ident, run_id, cause, token = claim
        async with self.factory.begin() as tx:
            await bounded(tx, self.settings)
            run = await locked(tx, CoreRun, run_id)
            if run is None:
                await quarantine(tx, timer_id=ident, run_id=run_id, causation_id=cause,
                    code="missing_run", phase="timer_fire")
                row = await locked(tx, EventTimer, ident)
                if row is not None and row.claim_token == token:
                    row.status = "quarantined"
                    row.claim_token = row.lease_until = None
                return False
            row = await locked(tx, EventTimer, ident)
            if row is None:
                return await quarantine(tx, timer_id=ident, run_id=run_id, causation_id=cause,
                    code="missing_timer", phase="timer_fire")
            if row.status != "scheduled" or row.claim_token != token:
                logger.debug("timer already settled/stale timer_id=%s run_id=%s", ident, run_id)
                return False
            stamp = await db_now(tx)
            if row.due_at > stamp or row.next_fire_at > stamp:
                row.claim_token = row.lease_until = None
                return False
            ids = {"timer_id": row.id}
            if row.task_id:
                ids["task_id"] = row.task_id
            result = await emit(tx, row.event_type, run_id, causation_id=cause, ids=ids)
            row.status, row.event_id, row.fired_at = "fired", result.id, stamp
            row.version += 1
            row.claim_token = row.lease_until = None
            await tx.execute(update(EventAlert).where(EventAlert.dedupe_key == f"timer-lock:{ident}")
                .values(resolved_at=stamp))
            return True

    async def tick(self):
        count = 0
        for _ in range(self.settings.batch_size):
            try:
                claim = await self.claim()
            except Exception as exc:
                if lock_error(exc):
                    break
                raise
            if claim is None:
                break
            try:
                count += bool(await self.fire(claim))
            except Exception as exc:
                if not isinstance(exc, LockBusy) and not lock_error(exc):
                    raise
                try:
                    await self.defer(claim)
                except Exception as deferred:
                    if not isinstance(deferred, LockBusy) and not lock_error(deferred):
                        raise
                    # The durable lease recovers a concurrently locked timer.
        return count
