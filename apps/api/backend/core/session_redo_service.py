"""Open a redo Run from a copied plan. Does not call the planner or charge credits.

Supervisor hook: the Invocation is stored with status 'returned' and a PlannerResult.
The existing supervisor scan calls accept(), which reviews and then the worker executes.
Do not call Planner.plan from this module. Codex routes the text「再来一次」to redo_session.
"""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.contracts import CoreSettings, RunInput
from backend.core.orm import CoreEvent, CorePlan, CoreRun, CoreRunInputRevision, CoreSession, CoreSessionMessage
from backend.infrastructure.orm import Document
from backend.core.orm import CoreTask
from backend.core.session_context import effective_scope
from backend.core.session_redo import (RedoNotExecutable, RedoSettings, SeedInvalid, SeedOutOfRange,
    SeedUnchanged, SeedUnsupported, _bounds, _supported, clone_plan, occupied_seed)
from backend.core.sessions.revisions import append_revision
from backend.core.sessions.store import append_event
from backend.core.store import create_run
from backend.core.supervisor.contracts import PlannerResult
from backend.core.supervisor.orm import Invocation
from backend.core.supervisor.reviewer import effective_invariants
from backend.core.tools.catalog import ToolCatalog
from backend.core.tools.store import load_catalog
from backend.infrastructure.orm import Outbox
from backend.services.identity import Identity, authenticated

TERMINAL = {"completed", "failed", "cancelled"}
SUPERVISOR_HOOK = (
    "Invocation.status is returned and result is a PlannerResult of the copied plan. "
    "Supervisor.accept reviews it. This service never calls Planner.plan, bypasses review, "
    "or writes a credit ledger. API: build_redo_router()."
)
CREDIT_WARNING = "重做将再次审核并重新扣减生成积分，只更换受支持步骤的种子。"


class RedoRejected(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RedoRequest(BaseModel):
    request_key: str = Field(min_length=1, max_length=128)
    source_run_id: UUID
    source_plan_id: UUID
    credits_limit: int = Field(gt=0)
    tokens_limit: int = Field(gt=0)


def request_hash(body: RedoRequest) -> str:
    raw = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def choose_seed(old: set[int], settings: RedoSettings | None = None, *, bounds: tuple[int, int] | None = None) -> int:
    """Pick one free seed in the intersection of the configured range and tool bounds.

    The walk is proportional to the number of occupied seeds, not the size of the range.
    """
    settings = settings or RedoSettings()
    low, high = settings.seed_minimum, settings.seed_maximum
    if bounds is not None:
        low, high = max(low, bounds[0]), min(high, bounds[1])
    if low > high:
        raise SeedOutOfRange(low)
    occupied = sorted(value for value in old if low <= value <= high)
    span = high - low + 1
    if len(occupied) >= span:
        raise SeedOutOfRange(low)
    pick = secrets.randbelow(span - len(occupied))
    cursor = low
    for value in [*occupied, high + 1]:
        gap = value - cursor
        if pick < gap:
            return cursor + pick
        pick -= gap
        cursor = value + 1
    raise SeedOutOfRange(low)


def copied_result(plan, *, source_plan_id: UUID, seed: int, invariants: dict) -> PlannerResult:
    return PlannerResult(plan=plan, invariants=invariants, confidence=1, tokens=0, raw_output={
        "writer": "helper", "redo": True, "source_plan_id": str(source_plan_id), "seed": seed,
        "no_planner_call": True,
    })


async def _current_canvas(tx, session, workspace_id, *, document_data, document_revision):
    """Return the live flat document and its revision."""
    if document_data is not None:
        return copy.deepcopy(document_data), document_revision
    if session.canvas_id is None:
        raise RedoRejected("canvas_missing")
    document = await tx.scalar(select(Document).where(
        Document.id == session.canvas_id, Document.workspace_id == workspace_id).with_for_update())
    if document is None:
        raise RedoRejected("canvas_missing")
    return copy.deepcopy(document.data), document.revision


def _freeze_snapshot(source_snapshot: dict, live: dict, revision, recent: list, *,
                     request: dict, selection: dict | None = None) -> dict:
    """Flat live canvas plus the latest effective request and authorized asset metadata."""
    assets = []
    seen = set()
    source_assets = list((source_snapshot or {}).get("assets") or [])
    selected_assets = list((selection or {}).get("assets") or [])
    for row in [*source_assets, *selected_assets]:
        if not isinstance(row, dict):
            continue
        ident = str(row.get("asset_id") or "")
        if ident and ident in seen:
            assets = [item for item in assets if str(item.get("asset_id") or "") != ident]
        if ident:
            seen.add(ident)
        assets.append(copy.deepcopy(row))
    snapshot = {key: value for key, value in copy.deepcopy(live or {}).items()
                if key not in {"request", "assets", "recent_assets", "document_revision"}}
    snapshot["document_revision"] = revision
    snapshot["assets"] = assets
    snapshot["request"] = copy.deepcopy(request)
    snapshot["recent_assets"] = copy.deepcopy(recent)
    return snapshot


def redo_request_override(plan, effective: dict, selection: dict | None = None) -> dict:
    """Keep the approved plan goal and frozen selection. The phrase 再来一次 is not the request."""
    selection = selection or {}
    goal = plan.goal if isinstance(getattr(plan, "goal", None), str) else ""
    if not goal:
        goal = effective.get("message") if isinstance(effective.get("message"), str) else ""
    selected = selection.get("selected_assets")
    if not selected:
        selected = effective.get("selected_assets") or []
    request = copy.deepcopy(effective or {})
    request["message"] = goal
    request["messages"] = [goal] if goal else list(request.get("messages") or [])
    request["selected_assets"] = [str(item) for item in selected]
    request["selection_explicit"] = True
    request.pop("reference_clarification", None)
    return request


def _authorized_scope(scope: dict, selection: dict | None) -> dict:
    copied = dict(scope or {})
    readable = [str(item) for item in (copied.get("readable_assets") or [])]
    seen = set(readable)
    selection = selection or {}
    extra = [*(selection.get("selected_assets") or []), *(selection.get("derived_asset_ids") or [])]
    extra.extend(row.get("asset_id") for row in (selection.get("assets") or []) if isinstance(row, dict))
    for item in extra:
        ident = str(item or "")
        if ident and ident not in seen:
            readable.append(ident)
            seen.add(ident)
    copied["readable_assets"] = readable
    return copied


def _seed_bounds(plan, catalog, settings: RedoSettings):
    from backend.core.session_redo import SeedRange
    configured = SeedRange(minimum=settings.seed_minimum, maximum=settings.seed_maximum)
    tools = {tool.name: tool for tool in catalog.snapshot.tools}
    lows, highs, generative = [], [], False
    for step in plan.steps:
        tool = tools.get(step.tool) if step.tool else None
        if tool is None or not _supported(tool):
            continue
        generative = True
        low, high = _bounds(tool, configured)
        lows.append(low)
        highs.append(high)
    if not generative:
        return None
    return max(lows), min(highs)


def _old_seeds(plan, catalog) -> set[int]:
    """Seeds already used. An omitted generating seed occupies 0, matching the adapter."""
    tools = {tool.name: tool for tool in catalog.snapshot.tools}
    found = set()
    for step in plan.steps:
        tool = tools.get(step.tool) if step.tool else None
        generative = tool is not None and _supported(tool)
        if not generative:
            value = step.parameters.get("seed")
            if type(value) is int:
                found.add(value)
            continue
        value = occupied_seed(step.parameters)
        if value is not None:
            found.add(value)
    return found


async def _latest_source_input(tx, source):
    row = await tx.scalar(select(CoreRunInputRevision).where(
        CoreRunInputRevision.run_id == source.id,
        CoreRunInputRevision.revision == source.input_revision))
    effective = copy.deepcopy((row.effective_request if row is not None else None)
                              or (source.snapshot or {}).get("request") or {})
    selection = copy.deepcopy((row.selection_snapshot if row is not None else None) or {})
    scope = _authorized_scope(await effective_scope(tx, source), selection)
    return effective, selection, scope


async def redo_session(tx: AsyncSession, *, session_id: UUID, user_id: str, workspace_id: UUID,
                       body: RedoRequest, document_data: dict | None = None,
                       document_revision: int | None = None,
                       settings: RedoSettings | None = None) -> CoreRun:
    digest = request_hash(body)
    session = await tx.scalar(select(CoreSession).where(CoreSession.id == session_id).with_for_update())
    if session is None or session.workspace_id != workspace_id or session.user_id != user_id or session.closed_at:
        raise RedoRejected("session_not_found")
    existing = await tx.scalar(select(CoreRun).where(
        CoreRun.session_id == session.id, CoreRun.request_key == body.request_key))
    if existing is not None:
        if existing.request_hash != digest:
            raise RedoRejected("idempotency_conflict")
        return existing
    if session.active_run_id:
        active = await tx.get(CoreRun, session.active_run_id)
        if active is not None and active.status not in TERMINAL:
            raise RedoRejected("session_has_active_run")
    source = await tx.get(CoreRun, body.source_run_id)
    if (source is None or source.session_id != session.id or source.workspace_id != workspace_id
            or source.user_id != user_id or source.status != "completed"):
        raise RedoRejected("source_run_not_completed")
    plan_row = await tx.get(CorePlan, body.source_plan_id)
    if plan_row is None or plan_row.run_id != source.id or plan_row.result_type != "generate":
        raise RedoRejected("plan_not_executable")
    if plan_row.stage != "execute":
        raise RedoRejected("plan_not_admitted")
    if not isinstance((plan_row.link_constraints or {}).get("approved_invariants"), dict):
        raise RedoRejected("plan_not_approved")
    if (plan_row.self_check or {}).get("verdict") != "pass":
        raise RedoRejected("plan_not_approved")
    tasks = list(await tx.scalars(select(CoreTask).where(
        CoreTask.plan_id == plan_row.id, CoreTask.run_id == source.id)))
    if not tasks or any(task.status != "succeeded" for task in tasks):
        raise RedoRejected("plan_tasks_incomplete")
    from backend.core.supervisor.contracts import Plan
    from backend.core.tools.contracts import VersionLock
    catalog: ToolCatalog = await load_catalog(
        tx, source.version_lock["tool_catalog"]["version"],
        expected_lock=VersionLock.model_validate(source.version_lock["tool_catalog"]))
    original = Plan.model_validate(plan_row.proposal)
    settings = settings or RedoSettings()
    bounds = _seed_bounds(original, catalog, settings)
    if bounds is None:
        raise RedoRejected("redo_not_generative")
    seed = choose_seed(_old_seeds(original, catalog), settings, bounds=bounds)
    try:
        cloned = clone_plan(original, catalog.snapshot, seed, settings=settings)
    except (SeedUnsupported, SeedOutOfRange, SeedInvalid, SeedUnchanged, RedoNotExecutable) as exc:
        raise RedoRejected(exc.code) from exc
    for left, right in zip(original.steps, cloned.steps):
        if left.id != right.id or left.tool != right.tool or left.depends_on != right.depends_on:
            raise RedoRejected("plan_graph_changed")
        if {k: v for k, v in left.parameters.items() if k != "seed"} != {
                k: v for k, v in right.parameters.items() if k != "seed"}:
            raise RedoRejected("prompt_rewritten")
    approved = effective_invariants(source, plan_row)
    effective, selection, scope = await _latest_source_input(tx, source)
    override = redo_request_override(original, effective, selection)
    canvas, revision = await _current_canvas(
        tx, session, workspace_id, document_data=document_data, document_revision=document_revision)
    recent = list(session.recent_assets or (source.snapshot or {}).get("recent_assets") or [])
    snapshot = _freeze_snapshot(source.snapshot or {}, canvas, revision, recent, request=override, selection=selection)
    created = await create_run(tx, RunInput(
        session_id=session.id, workspace_id=workspace_id, user_id=user_id,
        request_key=body.request_key, request_hash=digest, snapshot=snapshot,
        scope=scope, invariants=approved, version_lock=dict(source.version_lock),
        mode=source.mode, input_revision=1), CoreSettings(credits_limit=body.credits_limit, tokens_limit=body.tokens_limit,
                                                         max_thought_rounds=source.max_thought_rounds))
    if int(getattr(created, "submit_fence", 1) or 1) < 1:
        created.submit_fence = 1
    session.message_cursor += 1
    user_message = CoreSessionMessage(
        session_id=session.id, sequence=session.message_cursor, request_key="redo-user:" + body.request_key,
        role="user", content={"text": "再来一次", "source_plan_id": str(plan_row.id), "source_run_id": str(source.id),
                              "source_input_revision": source.input_revision},
        processing_status="processed", request_hash=digest, intent="redo", related_run_id=source.id)
    session.message_cursor += 1
    warning = CoreSessionMessage(
        session_id=session.id, sequence=session.message_cursor, request_key="redo-warning:" + body.request_key,
        role="assistant", content={"text": CREDIT_WARNING, "source_plan_id": str(plan_row.id)},
        processing_status="processed", run_id=created.id, intent="redo", related_run_id=source.id)
    tx.add(user_message)
    tx.add(warning)
    await tx.flush()
    await append_revision(tx, created, session, [user_message], initial=True, request_override=override)
    if int(created.submit_fence or 0) < 1:
        created.submit_fence = 1
    await append_event(tx, session, event_type="redo_requested", run_id=created.id, message_id=user_message.id,
                       payload={"source_plan_id": str(plan_row.id), "source_run_id": str(source.id)})
    await append_event(tx, session, event_type="redo_warning", run_id=created.id, message_id=warning.id,
                       payload={"source_plan_id": str(plan_row.id)})
    tx.add(Outbox(aggregate_type="run", aggregate_id=str(created.id), event_type="core.changed",
                  payload={"runId": str(created.id), "workspaceId": str(workspace_id),
                           "sourcePlanId": str(plan_row.id)}))
    event = await tx.scalar(select(CoreEvent).where(CoreEvent.run_id == created.id).order_by(CoreEvent.run_sequence.desc()))
    if event is None:
        raise RedoRejected("run_event_missing")
    result = copied_result(cloned, source_plan_id=plan_row.id, seed=seed, invariants=approved)
    tx.add(Invocation(
        run_id=created.id, event_id=event.id, attempt=0, status="returned",
        context={"input_revision": created.input_revision, "redo": True,
                 "invocation_key": "redo:" + body.request_key, "request": snapshot.get("request") or {},
                 "source_plan_id": str(plan_row.id), "source_run_id": str(source.id)},
        result=result.model_dump(mode="json"), returned_at=datetime.now(timezone.utc)))
    session.active_run_id = created.id
    await tx.flush()
    return created


def build_redo_router():
    router = APIRouter()

    @router.post("/api/core/sessions/{session_id}/redo")
    async def redo(session_id: UUID, body: RedoRequest, request: Request,
                   identity: Identity = Depends(authenticated)):
        factory = request.app.state.factory
        try:
            async with factory.begin() as tx:
                run = await redo_session(tx, session_id=session_id, user_id=identity.principal,
                                         workspace_id=identity.workspace_id, body=body)
        except RedoRejected as exc:
            status = 409 if exc.code in {"idempotency_conflict", "session_has_active_run"} else 422
            raise HTTPException(status, exc.code) from exc
        return {"run_id": str(run.id), "status": run.status, "warning": CREDIT_WARNING}

    return router
