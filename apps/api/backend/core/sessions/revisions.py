"""Append-only input revisions. Frozen Run.snapshot is never rewritten."""
from sqlalchemy import select

from backend.core.orm import CoreRunInputRevision, CoreSessionMessage
from backend.core.supervisor.service import digest
from .store import append_event

SERVER_FIELDS = ("language", "action", "tool", "node_ids", "skill_id")


def effective_request(messages: list[CoreSessionMessage]) -> dict:
    texts = []
    selected = []
    viewport = None
    budget = {}
    server = {}
    for row in messages:
        content = row.content or {}
        if isinstance(content.get("text"), str) and content["text"]:
            texts.append(content["text"])
        assets = content.get("selected_assets") or []
        if assets:
            selected = list(assets)
        if content.get("viewport"):
            viewport = content["viewport"]
        for key in ("budget", "tokens", "rounds", "mode"):
            if key in content and key not in budget:
                budget[key] = content[key]
        for key in SERVER_FIELDS:
            value = content.get(key)
            if value not in (None, "", []):
                server[key] = value
    request = {"message": " ".join(texts), "messages": texts, "selected_assets": selected,
        "selection_explicit": bool(messages and (messages[-1].content or {}).get("selected_assets")), **budget, **server}
    if viewport is not None:
        request["viewport"] = viewport
    from backend.core.batch.intent import apply_batch_intent
    return apply_batch_intent(request)


async def _owned_user_messages(tx, run):
    return list(await tx.scalars(select(CoreSessionMessage).where(
        CoreSessionMessage.run_id == run.id, CoreSessionMessage.role == "user")
        .order_by(CoreSessionMessage.sequence)))


async def append_revision(tx, run, session, messages, *, user_correction=None, decision_ids=None,
                          initial=False, request_override=None):
    revision = 1 if initial else int(run.input_revision or 0) + 1
    prior = [] if initial else await _owned_user_messages(tx, run)
    seen = {row.id for row in prior}
    ordered = list(prior)
    for message in messages:
        if message.id not in seen:
            ordered.append(message)
            seen.add(message.id)
    ordered.sort(key=lambda item: item.sequence or 0)
    if request_override is None:
        frozen_request = dict((run.snapshot or {}).get("request") or {})
        request = {**frozen_request, **effective_request(ordered)}
        if not request.get("selected_assets"):
            request["selected_assets"] = list(frozen_request.get("selected_assets") or [])
        if not initial:
            request.update(budget=run.credits_limit, tokens=run.tokens_limit, rounds=run.max_thought_rounds, mode=run.mode)
    else:
        request = dict(request_override)
    from backend.core.session_context import freeze_selection
    request, selection, decisions = await freeze_selection(tx, run, session, request)
    members = [message for message in messages if message.input_revision is None]
    ids = [str(row.id) for row in members]
    payload = {"request": request, "selection": selection, "message_ids": ids, "correction": user_correction or {}}
    row = CoreRunInputRevision(run_id=run.id, session_id=session.id, revision=revision, message_ids=ids,
        effective_request=request, selection_snapshot=selection,
        user_correction=user_correction, decision_ids=decision_ids or [row["id"] for row in decisions], context_hash=digest(payload))
    tx.add(row)
    run.input_revision = revision
    run.submit_fence = revision
    session.input_revision = revision
    for message in members:
        message.input_revision = revision
        message.run_id = run.id
        message.processing_status = "processed"
    await tx.flush()
    await append_event(tx, session, event_type="input_revision", run_id=run.id,
        payload={"input_revision_id": str(row.id)})
    return row
