"""Durable session journal, decisions, summaries, artifacts and notifications."""
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from backend.infrastructure.orm import Outbox

from backend.core.orm import (CoreSession, CoreSessionArtifact, CoreSessionDecision, CoreSessionEvent,
    CoreSessionNotification, CoreSessionSummary)


async def append_event(tx, session, *, event_type, causation_id=None, run_id=None, task_id=None,
                       message_id=None, version_id=None, payload=None):
    checked = {}
    for key, value in (payload or {}).items():
        if not key.endswith("_id"):
            raise ValueError("session_event_payload_must_be_ids")
        checked[key] = str(UUID(str(value)))
    session.event_cursor += 1
    row = CoreSessionEvent(session_id=session.id, sequence=session.event_cursor, event_id=uuid4(),
        causation_id=causation_id, event_type=event_type, run_id=run_id, task_id=task_id,
        message_id=message_id, version_id=version_id, payload=checked)
    tx.add(row)
    await tx.flush()
    tx.add(Outbox(aggregate_type="session", aggregate_id=str(session.id), event_type="core.changed",
        payload={"sessionId": str(session.id), "eventId": str(row.event_id)}))
    await tx.flush()
    return row


async def append_artifact(tx, session, *, ordinal, task_id, output_port="default", completed_at=None,
                          core_asset_id=None, core_asset_version_id=None, stored_asset_id=None,
                          run_id=None, canvas_node_id=None, position=None):
    row = CoreSessionArtifact(session_id=session.id, ordinal=ordinal, task_id=task_id,
        output_port=output_port, completed_at=completed_at, core_asset_id=core_asset_id,
        core_asset_version_id=core_asset_version_id, stored_asset_id=stored_asset_id, run_id=run_id,
        canvas_node_id=canvas_node_id, position=position)
    tx.add(row)
    await tx.flush()
    return row


async def next_artifact_ordinal(tx, session_id: UUID) -> int:
    current = await tx.scalar(select(CoreSessionArtifact.ordinal).where(
        CoreSessionArtifact.session_id == session_id).order_by(CoreSessionArtifact.ordinal.desc()).limit(1))
    return int(current or 0) + 1


async def record_decision(tx, session, *, value, source_message_id=None, confirm_task_id=None,
                          supersedes_id=None, evidence=None):
    row = CoreSessionDecision(session_id=session.id, source_message_id=source_message_id,
        confirm_task_id=confirm_task_id, value=value, evidence=evidence or {})
    tx.add(row)
    await tx.flush()
    if supersedes_id is not None:
        previous = await tx.get(CoreSessionDecision, supersedes_id, with_for_update=True)
        if previous is None or previous.session_id != session.id:
            raise ValueError("decision_missing")
        if previous.superseded_by_id is not None:
            raise ValueError("decision_already_replaced")
        row.version = previous.version + 1
        previous.superseded_by_id = row.id
        # Confirmed values stay immutable; only the successor pointer changes.
    await append_event(tx, session, event_type="decision_recorded", message_id=source_message_id,
        payload={"decision_id": str(row.id)})
    return row


async def persist_summary(tx, session, *, from_sequence, to_sequence, body, decision_ids, input_hash,
                          writer, invocation_id=None):
    row = CoreSessionSummary(session_id=session.id, from_sequence=from_sequence, to_sequence=to_sequence,
        body=body, decision_ids=decision_ids, input_hash=input_hash, writer=writer,
        invocation_id=invocation_id)
    tx.add(row)
    await tx.flush()
    if to_sequence > session.summary_cursor:
        session.summary_cursor = to_sequence
        session.summary = body
    await append_event(tx, session, event_type="summary_written", payload={"summary_id": str(row.id)})
    return row


async def notify(tx, session, *, user_id, event_id, kind, body):
    result = await tx.execute(insert(CoreSessionNotification).values(
        id=uuid4(), session_id=session.id, user_id=user_id, event_id=event_id, kind=kind, body=body
    ).on_conflict_do_nothing(index_elements=[CoreSessionNotification.event_id, CoreSessionNotification.user_id])
        .returning(CoreSessionNotification.id))
    ident = result.scalar()
    if ident is None:
        return await tx.scalar(select(CoreSessionNotification).where(
            CoreSessionNotification.event_id == event_id, CoreSessionNotification.user_id == user_id))
    return await tx.get(CoreSessionNotification, ident)


async def mark_read(tx, notification_id: UUID, user_id: str, stamp):
    row = await tx.get(CoreSessionNotification, notification_id, with_for_update=True)
    if row is None or row.user_id != user_id:
        raise LookupError("notification_missing")
    if row.read_at is None:
        row.read_at = stamp
    return row


async def lock_session(tx, session_id: UUID) -> CoreSession:
    session = await tx.get(CoreSession, session_id, with_for_update=True)
    if session is None:
        raise LookupError("session_missing")
    return session


async def lock_session_then_run(tx, session_id: UUID, run_id: UUID):
    from backend.core.orm import CoreRun
    session = await lock_session(tx, session_id)
    run = await tx.get(CoreRun, run_id, with_for_update=True)
    if run is None or run.session_id != session.id:
        raise LookupError("run_missing")
    return session, run
