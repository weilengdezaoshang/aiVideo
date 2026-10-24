"""Transactional Run creation, messages, answers and safe progress projections."""
import copy
import hashlib
import logging
import math
import uuid
import re
from sqlalchemy import select, or_
from backend.core.contracts import CoreSettings, RunInput
from backend.core.orm import (CoreRun, CoreSession, CoreSessionMessage, CoreEvent, CoreTask, CorePlan,
    CoreSkill, CoreAsset, CoreAssetVersion)
from backend.core.store import create_run, transition
from backend.core.events.store import emit
from backend.core.execution.orm import CanvasWrite
from backend.core.tools.orm import ToolCatalogVersion
from backend.core.tools.store import load_catalog
from backend.core.tools.catalog import canonical
from backend.agent.media.intent.negation import affirmative_text
from backend.core.review.contracts import Invariants
from backend.infrastructure.orm import Asset, AssetReference, Outbox
from .contracts import Settings
from .prompts import load_prompt
from .reviewer import effective_invariants
from .wiring import deployment_routes, freeze_selfcheck_entry, route_snapshot

_AUDIT = logging.getLogger("backend.core.supervisor.audit")

TERMINAL = {"completed", "failed", "cancelled"}
TASK_TERMINAL = {"succeeded", "failed", "blocked_by_model", "cancelled", "rejected"}


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


async def message(tx, run, text, key, *, details=None, session=None):
    """Caller must already hold the Session row lock. This does not lock Session."""

    session = session or await tx.get(CoreSession, run.session_id)
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


def _finite(value, limit):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and abs(value) <= limit)


def _viewport(raw, settings: Settings):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"x", "y", "width", "height"}:
        raise ValueError("invalid_viewport")
    if "width" not in raw or "height" not in raw:
        raise ValueError("invalid_viewport")
    origin = {"x": raw.get("x", 0), "y": raw.get("y", 0)}
    if not _finite(origin["x"], settings.viewport_origin_limit) or not _finite(origin["y"], settings.viewport_origin_limit):
        raise ValueError("invalid_viewport")
    if (not _finite(raw["width"], settings.viewport_span_limit) or not _finite(raw["height"], settings.viewport_span_limit)
            or raw["width"] <= 0 or raw["height"] <= 0):
        raise ValueError("invalid_viewport")
    return {"x": origin["x"], "y": origin["y"], "width": raw["width"], "height": raw["height"]}


async def _recent_outputs(tx, session, workspace_id):
    """Successful generation lineage only, newest completion first. Uploads are not recent outputs."""

    tasks = (await tx.execute(select(CoreTask, CanvasWrite, Asset)
        .join(CoreRun, CoreRun.id == CoreTask.run_id)
        .outerjoin(CanvasWrite, CanvasWrite.task_id == CoreTask.id)
        .outerjoin(Asset, Asset.id == CanvasWrite.asset_id)
        .where(CoreRun.session_id == session.id, CoreRun.workspace_id == workspace_id,
            CoreTask.status == "succeeded", CoreTask.kind != "ask_user")
        .order_by(CoreTask.finished_at.desc().nulls_last(), CoreTask.created_at.desc()))).all()
    versions = (await tx.execute(select(CoreAssetVersion, CoreTask)
        .join(CoreTask, CoreTask.id == CoreAssetVersion.task_id)
        .join(CoreRun, CoreRun.id == CoreAssetVersion.run_id)
        .where(CoreRun.session_id == session.id, CoreRun.workspace_id == workspace_id,
            CoreAssetVersion.workspace_id == workspace_id, CoreAssetVersion.status == "normal",
            CoreAssetVersion.category == "artifact", CoreTask.status == "succeeded")
        .order_by(CoreTask.finished_at.desc().nulls_last(), CoreTask.created_at.desc()))).all()
    ordered = []
    for task, write, asset in tasks:
        ident = str(asset.id) if asset is not None else str((task.output or {}).get("asset_id") or "")
        if not ident:
            continue
        if asset is not None and (asset.workspace_id != workspace_id or (asset.details or {}).get("hostBlocked")):
            continue
        if asset is None:
            try:
                found = await tx.get(Asset, uuid.UUID(ident))
            except (ValueError, TypeError):
                continue
            if found is None or found.workspace_id != workspace_id or (found.details or {}).get("hostBlocked"):
                continue
            asset = found
        ordered.append((task.finished_at or task.created_at, ident, asset.kind, write.node_id if write is not None else None))
    for version, task in versions:
        if version.stored_asset_id is None:
            continue
        ordered.append((task.finished_at or task.created_at, str(version.stored_asset_id), version.kind, None))
    ordered.sort(key=lambda item: item[0] or item[0], reverse=True)
    recent, seen = [], set()
    for _, asset_id, kind, node_id in ordered:
        if asset_id in seen:
            continue
        seen.add(asset_id)
        recent.append({"asset_id": asset_id, "node_id": node_id, "kind": kind, "role": "generated"})
        if len(recent) >= Settings().recent_output_limit:
            break
    return recent


async def open_run(tx, session, doc, *, workspace_id, user_id, request_key, request, budget, token_budget,
                   max_rounds, mode="auto", agent_route=None, input_revision=1, correction_of_run_id=None,
                   persist_opening_message=True, settings=None, routes=None):
    request_hash = digest({"request": request, "budget": budget, "tokens": token_budget, "rounds": max_rounds, "mode": mode})
    old = await tx.scalar(select(CoreRun).where(CoreRun.session_id == session.id, CoreRun.request_key == request_key))
    if old:
        if old.request_hash != request_hash:
            raise ValueError("request_key_conflict")
        return old
    if await tx.scalar(select(CoreRun.id).where(CoreRun.session_id == session.id, CoreRun.parent_run_id.is_(None),
        CoreRun.status.not_in(TERMINAL))):
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
    prompt = load_prompt("supervisor-v2")
    frozen_route = route_snapshot(agent_route)
    settings = settings or Settings()
    lock = {"supervisor": "llm-v1", "prompt_version": prompt.version, "prompt_hash": prompt.content_hash,
        "planner_controls": {"version": "planner-controls-v2", "prompt_version": prompt.version,
                             "prompt_hash": prompt.content_hash},
        "agent_route": frozen_route, "tool_catalog": catalog.lock.model_dump(), "capabilities": catalog.lock.model_dump(),
        "tool_descriptions": catalog.lock.model_dump(), "skills": {str(s.id): str(s.current_version_id) for s in skills if s.current_version_id},
        "asset_cards": {str(a.id): str(a.current_version_id) for a in cards if a.current_version_id},
        "selfcheck": freeze_selfcheck_entry(settings, frozen_route, routes=list(routes or deployment_routes()))}
    request = copy.deepcopy(request)
    request.pop("recent_assets", None)
    from backend.core.batch.intent import apply_batch_intent
    request = apply_batch_intent(request)
    language = request.get("language") or session.language
    if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})?", language):
        raise ValueError("invalid_language")
    session.language = language
    request["language"] = language
    viewport = _viewport(request.get("viewport"), settings)
    recent = await _recent_outputs(tx, session, workspace_id)
    session.recent_assets = recent
    if not request.get("anchor_id") and selected:
        request["anchor_id"] = next((key for key, node in doc.data.get("objects", {}).items() if node.get("assetId") == selected[0]), None)
    if request.get("anchor_id") and doc.data.get("objects", {}).get(request["anchor_id"], {}).get("assetId") not in selected:
        raise ValueError("anchor_out_of_scope")
    snapshot = {**copy.deepcopy(doc.data), "document_revision": doc.revision, "assets": assets, "request": request,
                "recent_assets": recent}
    if viewport is not None:
        snapshot["viewport"] = viewport
    readable = list(dict.fromkeys([*selected, *[item["asset_id"] for item in recent]]))
    readable_nodes = []
    objects = doc.data.get("objects") or {}
    if isinstance(objects, dict):
        for node_id, node in objects.items():
            if isinstance(node, dict) and str(node.get("kind") or node.get("type") or "").lower() == "text":
                readable_nodes.append(str(node_id))
    scope = {"tools": [t.name for t in catalog.snapshot.tools if t.available and not t.rollout_groups],
        "readable_assets": readable, "readable_cards": list(lock["asset_cards"].keys()),
        "readable_nodes": readable_nodes, "write_canvas": True}
    # Invariants come from the explicit request, never from the writer's returned ticket.
    invariants = copy.deepcopy(request.get("invariants", {}))
    if re.search(r"视频|动画|短片|\bvideo\b", affirmative_text(request["message"]), re.I):
        deliverable = invariants.setdefault("deliverable", {})
        deliverable.setdefault("modality", "video")
    invariants = Invariants.model_validate(invariants).model_dump(exclude_none=True)
    run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace_id, user_id=user_id,
        request_key=request_key, request_hash=request_hash, snapshot=snapshot, scope=scope, invariants=invariants,
        version_lock=lock, mode=mode, input_revision=input_revision, correction_of_run_id=correction_of_run_id),
        CoreSettings(credits_limit=budget, tokens_limit=token_budget, max_thought_rounds=max_rounds))
    for ident in readable:
        tx.add(AssetReference(asset_id=uuid.UUID(ident), owner_type="core_run", owner_id=str(run.id)))
    session.active_run_id = run.id
    if persist_opening_message:
        session.message_cursor += 1
        tx.add(CoreSessionMessage(session_id=session.id, sequence=session.message_cursor, request_key=request_key,
            role="user", content={"text": request["message"]}, processing_status="processed", run_id=run.id,
            request_hash=request_hash, input_revision=input_revision))
    await tx.flush()
    cause = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == run.id).order_by(CoreEvent.run_sequence).limit(1))
    await emit(tx, "user_message", run.id, causation_id=cause)
    return run


async def start(tx, *, workspace_id, user_id, document_id, request_key, request, budget, token_budget,
                max_rounds, mode="auto", agent_route=None, settings=None, routes=None):
    from backend.core.sessions.resolve import resolve_session
    session, doc = await resolve_session(tx, workspace_id=workspace_id, user_id=user_id, document_id=document_id)
    return await open_run(tx, session, doc, workspace_id=workspace_id, user_id=user_id,
        request_key=request_key, request=request, budget=budget, token_budget=token_budget,
        max_rounds=max_rounds, mode=mode, agent_route=agent_route, settings=settings, routes=routes)


async def latest_plan(tx, run_id):
    """Approved execute plans only. Reviewing/rejected/obsolete candidates are history."""

    run = await tx.get(CoreRun, run_id)
    if run is not None and run.current_plan_id is not None:
        current = await tx.get(CorePlan, run.current_plan_id)
        if current is not None and current.stage == "execute":
            return current
    return await tx.scalar(select(CorePlan).where(CorePlan.run_id == run_id,
        CorePlan.stage == "execute").order_by(CorePlan.version.desc()).limit(1))


async def progress(tx, run):
    query = select(CoreTask).where(CoreTask.run_id == run.id)
    if run.current_plan_id is not None:
        query = query.where(CoreTask.plan_id == run.current_plan_id)
    tasks = (await tx.scalars(query.order_by(CoreTask.created_at, CoreTask.task_key))).all()
    messages = (await tx.scalars(select(CoreSessionMessage).where(CoreSessionMessage.run_id == run.id,
        CoreSessionMessage.role == "assistant").order_by(CoreSessionMessage.sequence))).all()
    plan = await latest_plan(tx, run.id)
    stored = isinstance(getattr(plan, "link_constraints", None), dict) and isinstance((plan.link_constraints or {}).get("approved_invariants"), dict)
    body = {"id": str(run.id), "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
        "status": run.status, "version": run.version, "cursor": run.event_cursor,
        "credits_limit": run.credits_limit, "credits_spent": run.credits_spent,
        "thought_rounds": run.thought_rounds_used, "tokens_used": run.tokens_used,
        "known_tokens": int(((run.error or {}).get("planner_usage") or {}).get("known_tokens") or 0),
        "unknown_calls": int(((run.error or {}).get("planner_usage") or {}).get("unknown_calls") or 0),
        "constraints": effective_invariants(run, plan),
        "sourcePlan": str(plan.id) if stored else None,
        "tasks": [{"id": str(t.id), "key": t.task_key, "kind": t.kind, "status": t.status,
            "labels": [c["label"] for c in (t.output or {}).get("candidates", [])],
            "question": {k: v for k, v in (t.waiting or {}).items() if k in {"question", "options", "form"}}} for t in tasks],
        "messages": [m.content for m in messages]}
    rules = getattr(plan, "expansion_rules", None) or {}
    if rules.get("compiled"):
        from backend.core.batch.retry_helpers import effective_batch_progress
        body["batch"] = await effective_batch_progress(tx, run, rules, tasks)
    return body


def _check_form_fields(form: dict, fields: dict) -> dict:
    if not isinstance(fields, dict):
        raise ValueError("invalid_fields")
    specs = {item.get("id") or item.get("name"): item for item in form.get("fields") or []}
    if set(fields) - set(specs):
        raise ValueError("unknown_field")
    stored = {}
    for ident, spec in specs.items():
        if ident in fields:
            value = fields[ident]
        elif "default" in spec:
            value = spec.get("default")
        else:
            value = None
        if spec.get("required") and value is None:
            raise ValueError("missing_field")
        if value is None:
            continue
        kind = spec.get("type")
        if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))):
            raise ValueError("invalid_field")
        if kind == "boolean" and not isinstance(value, bool):
            raise ValueError("invalid_field")
        if kind in {"text", "choice"} and not isinstance(value, str):
            raise ValueError("invalid_field")
        if kind == "choice" and value not in (spec.get("choices") or []):
            raise ValueError("unknown_option")
        stored[ident] = value
    return stored


async def answer(tx, *, task_id, option_id, request_key, workspace_id, user_id, fields=None):
    initial = await tx.get(CoreTask, task_id)
    if not initial:
        raise LookupError("任务不存在")
    from backend.core.sessions.store import lock_session_then_run
    owned = await tx.get(CoreRun, initial.run_id)
    if owned is None:
        raise LookupError("任务不存在")
    session, run = await lock_session_then_run(tx, owned.session_id, owned.id)
    if run.workspace_id != workspace_id or run.user_id != user_id:
        raise LookupError("任务不存在")
    task = await tx.get(CoreTask, task_id, with_for_update=True, populate_existing=True)
    waiting = task.waiting or {}
    choices = {c["id"]: c for c in waiting.get("options", [])}
    form = waiting.get("form") or (task.parameters or {}).get("form")
    if form:
        if fields is None:
            raise ValueError("form_requires_fields")
        fields = _check_form_fields(form, fields)
    elif fields is not None:
        raise ValueError("fields_not_allowed")
    elif not option_id or option_id not in choices:
        raise ValueError("unknown_option")
    payload = {"answer": option_id or "", "fields": fields}
    if task.status == "succeeded" and waiting.get("answer_key") == request_key:
        if waiting.get("answer") == payload["answer"] and waiting.get("fields") == fields:
            return task
        raise ValueError("answer_conflict")
    if task.status != "waiting_user" or run.status != "paused":
        raise ValueError("question_not_waiting")
    session.message_cursor += 1
    if form:
        text = " ".join(f"{key}={value}" for key, value in fields.items())
        content = {"text": text, "fields": fields, "task_id": str(task.id), "data": True}
    else:
        content = {"text": choices[option_id]["label"] if choices else option_id, "task_id": str(task.id)}
    message = CoreSessionMessage(session_id=session.id, sequence=session.message_cursor,
        request_key=digest({"task": str(task.id), "answer_key": request_key}), role="user",
        content=content, processing_status="processed", run_id=run.id, input_revision=run.input_revision)
    tx.add(message)
    await tx.flush()
    from backend.core.sessions.store import record_decision
    await record_decision(tx, session, value={"fields": fields} if form else {
        "option_id": option_id, "label": content["text"]}, source_message_id=message.id,
        confirm_task_id=task.id, evidence={"run_id": str(run.id), "input_revision": run.input_revision})
    task.waiting = {**waiting, "answer_key": request_key, "answer": payload["answer"], **({"fields": fields} if form else {})}
    if form:
        # Invariants and the canvas snapshot are immutable. The answer message is the evidence.
        run.current_plan_id = None
    await transition(tx, "task", task.id, task.version, "succeeded")
    await emit(tx, "user_confirmed", run.id, ids={"task_id": task.id},
        causation_id=await tx.scalar(select(CoreEvent.id).where(CoreEvent.aggregate_id == task.id).order_by(CoreEvent.run_sequence.desc()).limit(1)))
    return task


_RELATED = {"plan_id", "task_id", "invocation_id", "event_id"}


def _related(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - _RELATED:
        raise ValueError("invalid_feedback")
    cleaned = {}
    for key, value in raw.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value or len(value) > 64:
            raise ValueError("invalid_feedback")
        cleaned[key] = value
    return cleaned


async def record_feedback(tx, *, run_id, workspace_id, user_id, request_key, text, correction, related=None):
    """Explicit terminal feedback. A later chat message is not recorded as a correction."""

    owned = await tx.get(CoreRun, run_id)
    if owned is None or owned.workspace_id != workspace_id or owned.user_id != user_id:
        raise LookupError("run_missing")
    from backend.core.sessions.store import lock_session_then_run
    session, run = await lock_session_then_run(tx, owned.session_id, owned.id)
    if run.status not in TERMINAL:
        raise ValueError("run_not_terminal")
    if not isinstance(text, str) or not text.strip() or len(text) > Settings().feedback_max_chars:
        raise ValueError("invalid_feedback")
    linked = _related(related)
    if "plan_id" in linked:
        found = await tx.get(CorePlan, uuid.UUID(linked["plan_id"]))
        if found is None or found.run_id != run.id:
            raise ValueError("invalid_feedback")
    if "task_id" in linked:
        found = await tx.get(CoreTask, uuid.UUID(linked["task_id"]))
        if found is None or found.run_id != run.id:
            raise ValueError("invalid_feedback")
    if "invocation_id" in linked:
        from .orm import Invocation
        found = await tx.get(Invocation, uuid.UUID(linked["invocation_id"]))
        if found is None or found.run_id != run.id:
            raise ValueError("invalid_feedback")
    if "event_id" in linked:
        found = await tx.get(CoreEvent, uuid.UUID(linked["event_id"]))
        if found is None or found.run_id != run.id:
            raise ValueError("invalid_feedback")
    key = digest({"feedback": request_key, "run": str(run.id)})
    old = await tx.scalar(select(CoreSessionMessage).where(CoreSessionMessage.session_id == run.session_id,
        CoreSessionMessage.request_key == key))
    if old:
        if old.content.get("text") != text or bool(old.content.get("correction")) != bool(correction):
            raise ValueError("feedback_conflict")
        return old
    session.message_cursor += 1
    row = CoreSessionMessage(session_id=session.id, sequence=session.message_cursor, request_key=key,
        role="user", content={"text": text, "feedback": True, "correction": bool(correction),
            "related": linked, "run_id": str(run.id)}, processing_status="processed", run_id=run.id)
    tx.add(row)
    await tx.flush()
    _AUDIT.info("supervisor feedback run=%s correction=%s plan=%s task=%s invocation=%s event=%s",
        run.id, bool(correction), linked.get("plan_id"), linked.get("task_id"),
        linked.get("invocation_id"), linked.get("event_id"))
    return row


async def list_feedback(tx, *, run_id, workspace_id, user_id):
    run = await tx.get(CoreRun, run_id)
    if run is None or run.workspace_id != workspace_id or run.user_id != user_id:
        raise LookupError("run_missing")
    rows = (await tx.scalars(select(CoreSessionMessage).where(CoreSessionMessage.run_id == run.id,
        CoreSessionMessage.role == "user").order_by(CoreSessionMessage.sequence))).all()
    return [{"id": str(row.id), "run_id": str(run.id), "correction": bool(row.content.get("correction")),
        "text": row.content.get("text"), "related": row.content.get("related") or {},
        "sequence": row.sequence} for row in rows if row.content.get("feedback") is True]
