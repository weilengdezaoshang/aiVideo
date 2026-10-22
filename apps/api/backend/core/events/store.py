"""Claim-check emission, durable inbox and administrative replay. Caller owns transactions."""
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backend.core.event_context import current_event_id
from backend.core.orm import CoreEvent, CoreRun, CoreTask
from .contracts import ClaimCheck, EventType
from .orm import EventAlert, EventDeadLetter, EventReceipt, EventReplay, RunConsumption

logger = logging.getLogger(__name__)


async def db_now(tx):
    from sqlalchemy import func
    return await tx.scalar(select(func.clock_timestamp()))


async def emit(tx, event_type: EventType, run_id, *, causation_id=None, ids=None):
    event_type = EventType(event_type)
    cause = causation_id or current_event_id.get()
    if cause is None:
        raise ValueError("domain events require causation_id")
    payload = {key: str(uuid.UUID(str(value))) for key, value in (ids or {}).items()}
    if any(not key.endswith("_id") for key in payload):
        raise ValueError("claim-check payload keys must be ids")
    event = CoreEvent(event_kind="domain", aggregate_type="run", aggregate_id=run_id,
        event_type=event_type.value, run_id=run_id, task_id=uuid.UUID(payload["task_id"]) if "task_id" in payload else None,
        plan_id=uuid.UUID(payload["plan_id"]) if "plan_id" in payload else None, causation_id=cause, payload=payload)
    tx.add(event)
    await tx.flush()
    await tx.refresh(event)
    await tx.get(CoreRun, run_id, populate_existing=True)
    return event


async def alert(tx, kind, event_id, key, *, context=None):
    stamp = await db_now(tx)
    await tx.execute(insert(EventAlert).values(kind=kind, event_id=event_id, dedupe_key=key,
        occurrences=1, last_seen_at=stamp, context=context or {}).on_conflict_do_update(index_elements=[EventAlert.dedupe_key],
        set_={"last_seen_at": stamp, "occurrences": EventAlert.occurrences + 1, "resolved_at": None}))
    logger.error("core_event_alert kind=%s event_id=%s", kind, event_id)


async def accept(tx, raw):
    claim = ClaimCheck.model_validate(raw)
    from .integrity import completed, quarantine, quarantined
    event = await tx.get(CoreEvent, claim.event_id)
    if await tx.scalar(select(quarantined(claim.event_id))):
        logger.debug("core_event quarantined notification acknowledged event_id=%s run_id=%s", claim.event_id, claim.run_id)
        return claim.event_id
    if event is None:
        await quarantine(tx, event_id=claim.event_id, run_id=claim.run_id,
            code="missing_event", phase="receive")
        return claim.event_id
    if event.run_id != claim.run_id:
        # Keep the bad notification without quarantining the genuine event.
        await quarantine(tx, event_id=event.id, run_id=event.run_id, code="run_mismatch", phase="receive",
            source_key=f"claim:{event.id}:{claim.run_id}", details={"claimed_run_id": str(claim.run_id)})
        return event.id
    if await tx.get(CoreRun, claim.run_id) is None:
        await quarantine(tx, event_id=event.id, run_id=claim.run_id,
            code="missing_run", phase="receive")
        return claim.event_id
    if await completed(tx, event):
        return event.id
    stamp = await db_now(tx)
    await tx.execute(insert(RunConsumption).values(run_id=event.run_id, ready_at=stamp)
        .on_conflict_do_nothing(index_elements=[RunConsumption.run_id]))
    await tx.execute(insert(EventReceipt).values(event_id=event.id, run_id=event.run_id,
        run_sequence=event.run_sequence, ready_at=stamp).on_conflict_do_nothing(index_elements=[EventReceipt.event_id]))
    return event.id


async def replay(tx, event_id, *, operator, reason, request_key):
    if not operator.strip() or not reason.strip() or not request_key.strip():
        raise ValueError("replay requires operator, reason and idempotency key")
    old = await tx.scalar(select(EventReplay).where(EventReplay.request_key == request_key))
    if old:
        if (old.event_id, old.operator, old.reason) != (event_id, operator, reason):
            raise ValueError("replay idempotency conflict")
        return old
    event = await tx.get(CoreEvent, event_id)
    if event is None:
        raise ValueError("unknown event")
    from .locking import locked
    from .integrity import quarantine
    # This is an explicit, single-event administrative operation, not a batch
    # daemon. Serialize concurrent idempotent replay requests under a bounded lock.
    from .contracts import EventSettings
    from sqlalchemy import text
    await tx.execute(text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": f"{EventSettings().admin_lock_timeout_ms}ms"})
    if await tx.get(CoreRun, event.run_id, with_for_update=True) is None:
        await quarantine(tx, event_id=event_id, run_id=event.run_id, code="missing_run", phase="replay")
        return None
    old = await tx.scalar(select(EventReplay).where(EventReplay.request_key == request_key))
    if old:
        if (old.event_id, old.operator, old.reason) != (event_id, operator, reason):
            raise ValueError("replay idempotency conflict")
        return old
    receipt = await locked(tx, EventReceipt, event_id)
    stream = await locked(tx, RunConsumption, event.run_id)
    dead = await locked(tx, EventDeadLetter, event_id)
    if stream is None:
        await quarantine(tx, event_id=event_id, code="missing_cursor", phase="replay")
        return None
    if receipt is None:
        from .integrity import completed
        if await completed(tx, event, stream):
            raise ValueError("processed events never replay")
        await quarantine(tx, event_id=event_id, code="missing_receipt", phase="replay")
        return None
    if receipt.status == "dead" and dead is None:
        await quarantine(tx, event_id=event_id, code="missing_dead_letter", phase="replay")
        return None
    if receipt.status != "dead" or dead is None or stream.blocked_event_id != event_id:
        raise ValueError("only the dead head event can be replayed; processed events never replay")
    stamp = await db_now(tx)
    receipt.generation += 1
    receipt.attempts, receipt.status, receipt.ready_at = 0, "pending", stamp
    receipt.claim_token = receipt.lease_until = receipt.handler_version = None
    stream.blocked_event_id, stream.ready_at = None, stamp
    dead.resolved_at = stamp
    from sqlalchemy import update
    await tx.execute(update(EventAlert).where(EventAlert.event_id == event_id,
        EventAlert.kind == "dead_letter", EventAlert.resolved_at.is_(None)).values(resolved_at=stamp))
    record = EventReplay(event_id=event_id, generation=receipt.generation, operator=operator,
        reason=reason, request_key=request_key)
    tx.add(record)
    await tx.flush()
    return record


class HandlerTransaction:
    """Restricted transaction surface: effects must commit with their receipt."""
    def __init__(self, session):
        self.__session = session

    async def execute(self, statement, *args, **kwargs):
        import re
        if re.search(r"\b(COMMIT|ROLLBACK|BEGIN|SAVEPOINT|PREPARE|VACUUM|CREATE|ALTER|DROP|TRUNCATE|CALL)\b", str(statement), re.I):
            raise ValueError("transaction/DDL control is forbidden in handlers")
        return await self.__session.execute(statement, *args, **kwargs)

    async def scalar(self, statement, *args, **kwargs):
        return (await self.execute(statement, *args, **kwargs)).scalar()

    async def scalars(self, statement, *args, **kwargs):
        return (await self.execute(statement, *args, **kwargs)).scalars()

    async def get(self, *args, **kwargs):
        return await self.__session.get(*args, **kwargs)

    def add(self, row):
        self.__session.add(row)

    async def flush(self):
        await self.__session.flush()

    async def emit(self, event_type, run_id, **kwargs):
        return await emit(self.__session, event_type, run_id, **kwargs)

    async def transition(self, *args, **kwargs):
        from backend.core.store import transition
        return await transition(self.__session, *args, **kwargs)

    async def schedule(self, *args, **kwargs):
        from .timers import schedule
        return await schedule(self.__session, *args, **kwargs)

    async def cancel_timer(self, *args, **kwargs):
        from .timers import cancel
        return await cancel(self.__session, *args, **kwargs)

    async def commit(self):
        raise ValueError("handlers cannot commit")

    async def rollback(self):
        raise ValueError("handlers cannot rollback")


async def latest_task(tx, event):
    return await tx.get(CoreTask, event.task_id, populate_existing=True) if event.task_id else None



async def causal_chain(tx, event_id, max_depth):
    """Bounded traversal, including cross-Run helper reports; flag truncation explicitly."""
    rows, seen = [], set()
    while event_id is not None and len(rows) < max_depth:
        if event_id in seen:
            raise ValueError("causation cycle")
        seen.add(event_id)
        event = await tx.get(CoreEvent, event_id)
        if event is None:
            raise ValueError("unknown causal event")
        rows.append({"event_id": str(event.id), "run_id": str(event.run_id),
            "event_type": event.event_type, "run_sequence": event.run_sequence})
        event_id = event.causation_id
    return {"events": rows, "truncated": event_id is not None}
