"""Transactional Run creation, messages, answers and safe progress projections."""
import copy
import hashlib
import uuid
import re
from sqlalchemy import select, or_
from backend.core.contracts import CoreSettings, RunInput
from backend.core.orm import (CoreRun, CoreSession, CoreSessionMessage, CoreEvent, CoreTask,
    CoreSkill, CoreAsset, CoreAssetVersion)
from backend.core.store import create_run, transition
from backend.core.events.store import emit
from backend.core.tools.orm import ToolCatalogVersion
from backend.core.tools.store import load_catalog
from backend.core.tools.catalog import canonical
from backend.agent.media.intent.negation import affirmative_text
from backend.core.review.contracts import Invariants
from backend.infrastructure.orm import Document, Asset, AssetReference, Outbox

TERMINAL = {"completed", "failed", "cancelled"}
TASK_TERMINAL = {"succeeded", "failed", "blocked_by_model", "cancelled", "rejected"}


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


async def message(tx, run, text, key, *, details=None):
    session = await tx.get(CoreSession, run.session_id, with_for_update=True)
    old = await tx.scalar(select(CoreSessionMessage).where(CoreSessionMessage.session_id == session.id,
        CoreSessionMessage.request_key == key))
    if old:
        return old
    session.message_cursor += 1
    row = CoreSessionMessage(session_id=session.id, sequence=session.message_cursor, request_key=key,
        role="assistant", content={"text": text, **(details or {})}, processing_status="processed", run_id=run.id)
    tx.add(row)
    tx.add(Outbox(aggregate_type="run", aggregate_id=str(run.id), event_type="core.changed",
        payload={"runId": str(run.id), "workspaceId": str(run.workspace_id)}))
    return row


async def start(tx, *, workspace_id, user_id, document_id, request_key, request, budget, token_budget,
                max_rounds, mode="auto"):
    # Locking the canvas freezes snapshot + session selection atomically with idempotency.
    doc = await tx.get(Document, document_id, with_for_update=True)
    if not doc or doc.workspace_id != workspace_id:
        raise LookupError("画布不存在")
    session = await tx.scalar(select(CoreSession).where(CoreSession.canvas_id == doc.id,
        CoreSession.workspace_id == workspace_id, CoreSession.user_id == user_id).order_by(CoreSession.created_at).limit(1).with_for_update())
    if not session:
        session = CoreSession(workspace_id=workspace_id, canvas_id=doc.id, source_canvas_id=doc.id,
            user_id=user_id, language="zh-CN")
        tx.add(session)
        await tx.flush()
    request_hash = digest({"request": request, "budget": budget, "tokens": token_budget, "rounds": max_rounds, "mode": mode})
    old = await tx.scalar(select(CoreRun).where(CoreRun.session_id == session.id, CoreRun.request_key == request_key))
    if old:
        if old.request_hash != request_hash:
            raise ValueError("request_key_conflict")
        return old
    if await tx.scalar(select(CoreRun.id).where(CoreRun.session_id == session.id, CoreRun.status.not_in(TERMINAL))):
        raise ValueError("session_has_active_run")
    version = await tx.scalar(select(ToolCatalogVersion.version).order_by(ToolCatalogVersion.created_at.desc(), ToolCatalogVersion.version.desc()).limit(1))
    catalog = await load_catalog(tx, version)
    selected = request.get("selected_assets", [])
    assets = []
    for ident in selected:
        asset = await tx.get(Asset, uuid.UUID(ident))
        if asset is None or asset.workspace_id != workspace_id or asset.details.get("hostBlocked"):
            raise ValueError("asset_unavailable")
        assets.append({"asset_id": str(asset.id), "owner_id": user_id, "kind": asset.kind,
            "width": asset.width or None, "height": asset.height or None})
    skills = (await tx.scalars(select(CoreSkill).where(or_(CoreSkill.workspace_id == workspace_id,
        CoreSkill.workspace_id.is_(None)), or_(CoreSkill.user_id == user_id, CoreSkill.user_id.is_(None))))).all()
    cards = (await tx.scalars(select(CoreAsset).join(CoreAssetVersion, CoreAssetVersion.id == CoreAsset.current_version_id).where(CoreAssetVersion.category == "card", CoreAsset.workspace_id == workspace_id,
        or_(CoreAsset.user_id == user_id, CoreAsset.ownership == "team")))).all()
    lock = {"supervisor": "rule-v1", "tool_catalog": catalog.lock.model_dump(), "capabilities": catalog.lock.model_dump(),
        "tool_descriptions": catalog.lock.model_dump(), "skills": {str(s.id): str(s.current_version_id) for s in skills if s.current_version_id},
        "asset_cards": {str(a.id): str(a.current_version_id) for a in cards if a.current_version_id}}
    request = copy.deepcopy(request)
    if not request.get("anchor_id") and selected:
        request["anchor_id"] = next((key for key, node in doc.data.get("objects", {}).items() if node.get("assetId") == selected[0]), None)
    if request.get("anchor_id") and doc.data.get("objects", {}).get(request["anchor_id"], {}).get("assetId") not in selected:
        raise ValueError("anchor_out_of_scope")
    snapshot = {**copy.deepcopy(doc.data), "document_revision": doc.revision, "assets": assets, "request": request}
    scope = {"tools": [t.name for t in catalog.snapshot.tools if t.available and not t.rollout_groups],
        "readable_assets": selected, "write_canvas": True}
    # Invariants come from the explicit request, never from the writer's returned ticket.
    invariants = copy.deepcopy(request.get("invariants", {}))
    if re.search(r"视频|动画|短片|\bvideo\b", affirmative_text(request["message"]), re.I):
        deliverable = invariants.setdefault("deliverable", {})
        deliverable.setdefault("modality", "video")
    invariants = Invariants.model_validate(invariants).model_dump(exclude_none=True)
    run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace_id, user_id=user_id,
        request_key=request_key, request_hash=request_hash, snapshot=snapshot, scope=scope, invariants=invariants,
        version_lock=lock, mode=mode), CoreSettings(credits_limit=budget, tokens_limit=token_budget, max_thought_rounds=max_rounds))
    for ident in selected:
        tx.add(AssetReference(asset_id=uuid.UUID(ident), owner_type="core_run", owner_id=str(run.id)))
    session.message_cursor += 1
    tx.add(CoreSessionMessage(session_id=session.id, sequence=session.message_cursor, request_key=request_key,
        role="user", content={"text": request["message"]}, processing_status="processed", run_id=run.id))
    await tx.flush()
    cause = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == run.id).order_by(CoreEvent.run_sequence).limit(1))
    await emit(tx, "user_message", run.id, causation_id=cause)
    return run


async def progress(tx, run):
    tasks = (await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id).order_by(CoreTask.created_at, CoreTask.task_key))).all()
    messages = (await tx.scalars(select(CoreSessionMessage).where(CoreSessionMessage.run_id == run.id,
        CoreSessionMessage.role == "assistant").order_by(CoreSessionMessage.sequence))).all()
    return {"id": str(run.id), "status": run.status, "version": run.version, "cursor": run.event_cursor,
        "thought_rounds": run.thought_rounds_used, "tokens_used": run.tokens_used,
        "tasks": [{"id": str(t.id), "key": t.task_key, "kind": t.kind, "status": t.status,
            "labels": [c["label"] for c in (t.output or {}).get("candidates", [])],
            "question": {k: v for k, v in (t.waiting or {}).items() if k in {"question", "options"}}} for t in tasks],
        "messages": [m.content for m in messages]}


async def answer(tx, *, task_id, option_id, request_key, workspace_id, user_id):
    initial = await tx.get(CoreTask, task_id)
    if not initial:
        raise LookupError("任务不存在")
    run = await tx.get(CoreRun, initial.run_id, with_for_update=True)
    if run.workspace_id != workspace_id or run.user_id != user_id:
        raise LookupError("任务不存在")
    task = await tx.get(CoreTask, task_id, with_for_update=True, populate_existing=True)
    waiting = task.waiting or {}
    if task.status == "succeeded" and waiting.get("answer_key") == request_key and waiting.get("answer") == option_id:
        return task
    if task.status != "waiting_user" or run.status != "paused":
        raise ValueError("question_not_waiting")
    choices = {c["id"]: c for c in waiting.get("options", [])}
    if choices and option_id not in choices:
        raise ValueError("unknown_option")
    session = await tx.get(CoreSession, run.session_id, with_for_update=True)
    session.message_cursor += 1
    tx.add(CoreSessionMessage(session_id=session.id, sequence=session.message_cursor,
        request_key=digest({"task": str(task.id), "answer_key": request_key}), role="user",
        content={"text": choices[option_id]["label"] if choices else option_id, "task_id": str(task.id)},
        processing_status="processed", run_id=run.id))
    task.waiting = {**waiting, "answer_key": request_key, "answer": option_id}
    await transition(tx, "task", task.id, task.version, "succeeded")
    await emit(tx, "user_confirmed", run.id, ids={"task_id": task.id},
        causation_id=await tx.scalar(select(CoreEvent.id).where(CoreEvent.aggregate_id == task.id).order_by(CoreEvent.run_sequence.desc()).limit(1)))
    return task
