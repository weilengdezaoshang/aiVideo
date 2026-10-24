"""Persist user messages before any planner dispatch."""
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.core.orm import CoreRun, CoreSessionMessage
from backend.core.supervisor.service import digest
from .contracts import EnqueueResult, SessionSettings, merge_deadline
from .correction import infer_correction_run, record_negative_sample
from .fencing import bump_submit_fence
from .store import append_event

MODES = {"auto", "plan"}


def normalize_text(value: str) -> str:
    return " ".join((value or "").split())


def request_identity(client_id, client_message_id):
    return digest({"client": client_id, "id": client_message_id})


def semantic_hash(content, *, intent=None, related_run_id=None, correction_of_run_id=None):
    return digest({
        "content": content,
        "intent": intent,
        "related_run_id": str(related_run_id) if related_run_id else None,
        "correction_of_run_id": str(correction_of_run_id) if correction_of_run_id else None,
    })


def validate_content(content, settings: SessionSettings) -> dict:
    if not isinstance(content, dict):
        raise ValueError("invalid_message")
    text = normalize_text(content.get("text") if isinstance(content.get("text"), str) else "")
    if not text:
        raise ValueError("empty_message")
    if len(text) > settings.max_message_chars:
        raise ValueError("message_too_long")
    mode = content.get("mode") or "auto"
    if mode not in MODES:
        raise ValueError("invalid_mode")
    cleaned = {**content, "text": text, "mode": mode}
    return cleaned


async def enqueue_message(tx, session, *, client_id, client_message_id, content, intent=None,
                          related_run_id=None, correction_of_run_id=None, settings=None, server_metadata=None):
    settings = settings or SessionSettings()
    if set(server_metadata or {}) - {"_agent_route"}:
        raise ValueError("invalid_server_metadata")
    if not client_id or not client_message_id:
        raise ValueError("missing_client_message_id")
    content = validate_content(content, settings)
    from .correction import assert_same_session_run, detect_correction
    # Identity is the caller's body. Inferred correction targets must not change
    # the hash, or a retry after the active Run moves looks like a new payload.
    request_hash = semantic_hash(content, intent=intent, related_run_id=related_run_id,
        correction_of_run_id=correction_of_run_id)
    key = request_identity(client_id, client_message_id)
    existing = await tx.scalar(select(CoreSessionMessage).where(
        CoreSessionMessage.session_id == session.id, CoreSessionMessage.client_id == client_id,
        CoreSessionMessage.client_message_id == client_message_id))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ValueError("request_key_conflict")
        return EnqueueResult(message_id=existing.id, sequence=existing.sequence,
            dispatch_at=session.dispatch_at, run_id=existing.run_id,
            replayed=True)
    if session.closed_at is not None or session.canvas_id is None:
        raise ValueError("session_closed")
    if session.active_run_id:
        active = await tx.get(CoreRun, session.active_run_id)
        if active is not None and active.session_id != session.id:
            raise ValueError("active_run_session_mismatch")
        if active is None or active.status in {"completed", "failed", "cancelled"}:
            session.active_run_id = None
    if related_run_id is not None:
        await assert_same_session_run(tx, session, related_run_id)
    inferred = await infer_correction_run(tx, session, content["text"], related_run_id=related_run_id,
        correction_of_run_id=correction_of_run_id)
    correction_id = inferred.id if inferred is not None and (
        correction_of_run_id is not None or detect_correction(content["text"])) else None
    pending = await tx.scalar(select(func.count()).select_from(CoreSessionMessage).where(
        CoreSessionMessage.session_id == session.id, CoreSessionMessage.processing_status == "pending"))
    if int(pending or 0) >= settings.queue_limit:
        raise ValueError("session_queue_limit")
    session.message_cursor += 1
    row = CoreSessionMessage(session_id=session.id, sequence=session.message_cursor, request_key=key,
        role="user", content={**content, **(server_metadata or {})}, processing_status="pending", client_id=client_id,
        client_message_id=client_message_id, request_hash=request_hash, intent=intent,
        related_run_id=related_run_id, correction_of_run_id=correction_id)
    tx.add(row)
    try:
        await tx.flush()
    except IntegrityError as exc:
        raise ValueError("request_key_conflict") from exc
    if correction_id is not None and inferred is not None:
        await record_negative_sample(tx, session, inferred, row, content["text"])
    await bump_submit_fence(tx, session)
    first = await tx.scalar(select(CoreSessionMessage.created_at).where(
        CoreSessionMessage.session_id == session.id, CoreSessionMessage.processing_status == "pending")
        .order_by(CoreSessionMessage.sequence).limit(1))
    stamp = await tx.scalar(select(func.clock_timestamp()))
    session.dispatch_at = merge_deadline(stamp, first or stamp, settings)
    await append_event(tx, session, event_type="message_enqueued", message_id=row.id,
        payload={"message_id": str(row.id)})
    return EnqueueResult(message_id=row.id, sequence=row.sequence, dispatch_at=session.dispatch_at,
        run_id=row.run_id, replayed=False)
