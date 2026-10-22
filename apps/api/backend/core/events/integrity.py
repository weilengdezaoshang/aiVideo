"""Evidence-based duplicate recognition and durable terminal transport failures."""
import logging
from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from backend.core.orm import CoreEvent, CoreRun, CoreTask
from .orm import EventAttempt, EventQuarantine, EventReceipt, EventReplay, RunConsumption
from .store import alert, db_now

logger = logging.getLogger(__name__)


def quarantined(event_id):
    return exists(select(EventQuarantine.id).where(EventQuarantine.event_id == event_id,
        EventQuarantine.resolved_at.is_(None), EventQuarantine.source_key.like("event:%")))


def completion_proof(event_id, run_id, sequence):
    from sqlalchemy import or_
    from sqlalchemy.orm import aliased
    attempts, streams = aliased(EventAttempt), aliased(RunConsumption)
    return or_(exists(select(attempts.id).where(attempts.event_id == event_id,
        attempts.status == "succeeded")), exists(select(streams.run_id).where(
            streams.run_id == run_id, streams.completed_sequence >= sequence)))


async def completed(tx, event, stream=None):
    if stream is None:
        stream = await tx.get(RunConsumption, event.run_id)
    proven = (stream is not None and stream.completed_sequence >= event.run_sequence) or bool(
        await tx.scalar(select(EventAttempt.id).where(EventAttempt.event_id == event.id,
            EventAttempt.status == "succeeded").limit(1)))
    if proven:
        logger.debug("core_event already_processed event_id=%s run_id=%s causation_id=%s",
            event.id, event.run_id, event.causation_id)
    return proven


async def quarantine(tx, *, event_id=None, run_id=None, causation_id=None, timer_id=None,
                     code, phase, details=None, source_key=None):
    event = await tx.get(CoreEvent, event_id) if event_id else None
    if event is not None:
        run_id, causation_id = event.run_id, event.causation_id
    key = source_key or (f"timer:{timer_id}" if timer_id else f"event:{event_id}")
    context = {"event_id": str(event_id) if event_id else None,
        "run_id": str(run_id) if run_id else None,
        "causation_id": str(causation_id) if causation_id else None,
        "timer_id": str(timer_id) if timer_id else None, "phase": phase, **(details or {})}
    await tx.execute(insert(EventQuarantine).values(source_key=key, event_id=event_id,
        run_id=run_id, causation_id=causation_id, timer_id=timer_id, code=code, context=context)
        .on_conflict_do_update(index_elements=[EventQuarantine.source_key],
            set_={"code": code, "context": EventQuarantine.context.op("||")(context), "resolved_at": None}))
    if event_id is not None and key.startswith("event:"):
        from sqlalchemy import update
        await tx.execute(update(EventAttempt).where(EventAttempt.event_id == event_id,
            EventAttempt.status == "running").values(status="failed", error=code, finished_at=await db_now(tx)))
    await alert(tx, "dead_letter", None, f"quarantine:{key}", context=context)
    return False


async def resolve_quarantine(tx, record):
    from sqlalchemy import update
    from .orm import EventAlert
    record.resolved_at = await db_now(tx)
    await tx.execute(update(EventAlert).where(EventAlert.dedupe_key == f"quarantine:{record.source_key}")
        .values(resolved_at=record.resolved_at))


async def replay_quarantine(tx, record, *, operator, reason, request_key):
    from .locking import locked
    if not operator.strip() or not reason.strip() or not request_key.strip():
        raise ValueError("replay requires operator, reason and idempotency key")
    if record.event_id is None or not record.source_key.startswith("event:"):
        raise ValueError("only canonical event quarantine can replay as an event")
    event = await tx.get(CoreEvent, record.event_id)
    if event is None or await locked(tx, CoreRun, event.run_id) is None:
        raise ValueError("restore event and Run before replay")
    record = await locked(tx, EventQuarantine, record.id)
    prior = await tx.scalar(select(EventReplay).where(EventReplay.request_key == request_key))
    if prior:
        if (prior.event_id, prior.operator, prior.reason) != (event.id, operator, reason):
            raise ValueError("replay idempotency conflict")
        return prior
    if record is None or record.resolved_at is not None:
        raise ValueError("quarantine already resolved")
    stream = await locked(tx, RunConsumption, event.run_id)
    if stream is None:
        raise ValueError("restore consumption cursor before replay")
    if await completed(tx, event, stream):
        raise ValueError("processed events never replay")
    if stream.completed_sequence + 1 != event.run_sequence:
        raise ValueError("only the head event can be replayed")
    if event.task_id and await tx.get(CoreTask, event.task_id) is None:
        raise ValueError("restore task before replay")
    receipt = await locked(tx, EventReceipt, event.id)
    if receipt is None:
        from sqlalchemy import func
        generation = max(await tx.scalar(select(func.coalesce(func.max(EventAttempt.generation), 0))
            .where(EventAttempt.event_id == event.id)),
            await tx.scalar(select(func.coalesce(func.max(EventReplay.generation), 0))
            .where(EventReplay.event_id == event.id)))
        receipt = EventReceipt(event_id=event.id, run_id=event.run_id, run_sequence=event.run_sequence,
            generation=generation, attempts=0, ready_at=await db_now(tx))
        tx.add(receipt)
        await tx.flush()
    receipt.generation += 1
    receipt.status, receipt.attempts, receipt.ready_at = "pending", 0, await db_now(tx)
    receipt.claim_token = receipt.lease_until = receipt.handler_version = None
    stream.ready_at, stream.blocked_event_id = receipt.ready_at, None
    audit = EventReplay(event_id=event.id, generation=receipt.generation,
        operator=operator, reason=reason, request_key=request_key)
    tx.add(audit)
    await resolve_quarantine(tx, record)
    await tx.flush()
    return audit


async def replay_timer_quarantine(tx, record, *, operator, reason, request_key):
    """Explicit schedule repair, with a persistent idempotent audit on its quarantine."""
    from backend.core.orm import CoreRun
    from .orm import EventTimer
    from .locking import locked
    if not operator.strip() or not reason.strip() or not request_key.strip():
        raise ValueError("replay requires operator, reason and idempotency key")
    if record.timer_id is None or record.run_id is None:
        raise ValueError("not a timer quarantine")
    if await locked(tx, CoreRun, record.run_id) is None:
        raise ValueError("restore timer Run before replay")
    record = await locked(tx, EventQuarantine, record.id)
    if record is None:
        raise ValueError("missing quarantine")
    history = record.context.get("replays", [])
    for audit in history:
        if audit["request_key"] == request_key:
            if (audit["operator"], audit["reason"]) != (operator, reason):
                raise ValueError("replay idempotency conflict")
            return audit
    if record.resolved_at is not None:
        raise ValueError("quarantine already resolved")
    timer = await locked(tx, EventTimer, record.timer_id)
    if timer is None or timer.status != "quarantined" or await tx.get(CoreEvent, timer.causation_id) is None:
        raise ValueError("restore timer and its cause before replay")
    if await tx.scalar(select(CoreEvent.id).where(CoreEvent.payload["timer_id"].astext == str(timer.id)).limit(1)):
        raise ValueError("fired timers never replay")
    timer.status, timer.next_fire_at = "scheduled", max(timer.due_at, await db_now(tx))
    timer.claim_token = timer.lease_until = None
    timer.version += 1
    audit = {"request_key": request_key, "operator": operator, "reason": reason,
        "timer_id": str(timer.id), "at": (await db_now(tx)).isoformat()}
    record.context = {**record.context, "replays": [*history, audit]}
    await resolve_quarantine(tx, record)
    await tx.flush()
    return audit
