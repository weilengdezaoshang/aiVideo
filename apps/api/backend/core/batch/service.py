"""Open a child run for failed batch items. Does not call a planner or a provider."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select, text

from backend.core.batch.budget import AllocationError, CreditBudget, token_headroom
from backend.core.batch.compile import CompileError
from backend.core.batch.orm import CoreBatchRetry
from backend.core.batch.planner import proposal_hash
from backend.core.batch.retry import quote_catalog, retry_plan
from backend.core.batch.retry_helpers import (
    RETRY_LAYER, child_invariants, child_scope, load_root_outcomes, mark_reusable, retry_snapshot,
)
from backend.core.batch.schedule import item_progress
from backend.core.batch.store import find_retry
from backend.core.contracts import CoreSettings, RunInput
from backend.core.events.store import emit
from backend.agent.orm import HostCreditAccount
from backend.core.orm import CoreEvent, CorePlan, CoreRun, CoreSession, CoreTask
from backend.core.sessions.fencing import pending_user_messages
from backend.core.store import create_run
from backend.core.supervisor.contracts import Plan, Settings
from backend.core.supervisor.reviewer import review
from backend.core.supervisor.service import TASK_TERMINAL, TERMINAL, digest
from backend.infrastructure.orm import Asset, AssetReference


def _payload_hash(item_ids: list[str]) -> str:
    return digest({"items": list(item_ids or [])})


async def open_batch_retry(tx, parent: CoreRun, *, request_key: str, item_ids: list[str], catalog,
                           balance: int | None = None, settings: Settings | None = None):
    settings = settings or Settings()
    await tx.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{settings.lock_timeout_ms}ms"})
    if parent.workspace_id is None:
        raise CompileError("run_missing")
    if parent.parent_run_id is not None:
        raise CompileError("not_root")
    session = await tx.scalar(select(CoreSession).where(CoreSession.id == parent.session_id).with_for_update())
    if session is None:
        raise CompileError("run_missing")
    parent = await tx.scalar(select(CoreRun).where(CoreRun.id == parent.id).with_for_update())
    if parent is None:
        raise CompileError("run_missing")
    existing = await find_retry(tx, parent.id, request_key)
    payload = _payload_hash(item_ids)
    if existing is not None:
        stored = (existing.document or {}).get("payload_hash")
        if stored is not None and stored != payload:
            raise CompileError("request_key_conflict")
        return {"replayed": True, "retry_id": str(existing.id),
                "child_run_id": existing.child_run_id and str(existing.child_run_id),
                "quote": int(existing.quote)}
    if parent.status not in TERMINAL:
        raise CompileError("run_not_settled")
    other = await tx.scalar(select(CoreRun.id).where(
        CoreRun.session_id == session.id, CoreRun.parent_run_id.is_(None),
        CoreRun.id != parent.id, CoreRun.status.not_in(TERMINAL)))
    if other is not None:
        raise CompileError("session_has_active_run")
    if await pending_user_messages(tx, session.id) is not None:
        raise CompileError("pending_user_message")
    active = list(await tx.scalars(select(CoreBatchRetry).where(CoreBatchRetry.parent_run_id == parent.id)))
    for row in active:
        if (row.document or {}).get("settled"):
            continue
        child = await tx.get(CoreRun, row.child_run_id) if row.child_run_id else None
        if child is None or child.status not in TERMINAL:
            raise CompileError("retry_in_progress")
    plan_row = await tx.get(CorePlan, parent.current_plan_id) if parent.current_plan_id else None
    document = (plan_row.expansion_rules or {}) if plan_row is not None else {}
    if not document.get("compiled"):
        raise CompileError("not_compiled")
    root_tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == parent.id)))
    if any(task.status not in TASK_TERMINAL for task in root_tasks):
        raise CompileError("run_not_settled")
    outcomes = await load_root_outcomes(tx, parent, document)
    outcomes = await mark_reusable(tx, parent.workspace_id, outcomes)
    progress = item_progress(document, {key: (row.get("status") or "pending") for key, row in outcomes.items()})
    if not progress.get("settled"):
        raise CompileError("run_not_settled")
    redo = retry_plan(document, outcomes, item_ids)
    quote = quote_catalog(redo, catalog)
    extra_assets = []
    for step in redo["steps"]:
        from backend.agent.admission.tool_types import _values
        tool = catalog.get_tool(step["tool"], catalog.lock.version)
        for port in tool.inputs:
            for value in _values(step.get("parameters") or {}, port.path.split(".")):
                if isinstance(value, dict) and value.get("asset_id"):
                    extra_assets.append(str(value["asset_id"]))
    extra_assets = list(dict.fromkeys(extra_assets))
    reused_assets = {}
    for ident in extra_assets:
        try:
            asset = await tx.get(Asset, uuid.UUID(ident))
        except (ValueError, TypeError):
            raise CompileError("asset_unreadable") from None
        if asset is None or asset.workspace_id != parent.workspace_id:
            raise CompileError("asset_unreadable")
        reused_assets[ident] = asset
    if balance is None:
        account = await tx.get(HostCreditAccount, parent.workspace_id)
        balance = int(account.available) if account is not None else 0
    remaining_tokens = token_headroom(
        int(parent.tokens_limit), int(parent.tokens_used),
        [{"token_limit": int((row.document or {}).get("token_limit") or 0),
          "settled": bool((row.document or {}).get("settled")),
          "unknown_token_hold": int((row.document or {}).get("unknown_token_hold") or 0)}
         for row in active])
    if remaining_tokens <= 0:
        raise CompileError("token_budget")
    remaining_rounds = int(parent.max_thought_rounds) - int(parent.thought_rounds_used)
    if remaining_rounds <= 0:
        raise CompileError("round_limit")
    snapshot = retry_snapshot(parent, request_key)
    assets = list(snapshot.get("assets") or [])
    known = {str(item.get("asset_id")) for item in assets if isinstance(item, dict)}
    for ident in extra_assets:
        if ident not in known:
            asset = reused_assets[ident]
            assets.append({"asset_id": ident, "owner_id": parent.user_id, "kind": asset.kind,
                           "width": asset.width, "height": asset.height})
            known.add(ident)
    snapshot["assets"] = assets
    scope = child_scope(parent, extra_assets)
    invariants = child_invariants(parent, int(redo["item_count"]))
    request = dict(snapshot.get("request") or {})
    original_message = str(request.get("message") or "")
    request["message"] = (
        f"只重做本批次失败的 {redo['item_count']} 项，保留已经成功的项目。"
        if str(request.get("language") or "zh").startswith("zh") else
        f"Retry only these {redo['item_count']} failed items; keep the successful items."
    )
    request["message"] += "\n<original_request>" + original_message + "</original_request>"
    request["selected_assets"] = list(dict.fromkeys(item["asset_id"] for item in redo["items"]))
    snapshot["request"] = request
    snapshot["retry"] = {**snapshot.get("retry", {}), "original_user_message": original_message,
                         "retained_item_count": int(document["item_count"]) - int(redo["item_count"])}
    proposal = Plan.model_validate({
        "goal": request["message"],
        "steps": redo["steps"], "result_type": "generate", "writer": "rule", "expansion": redo,
    }).model_dump(mode="json")
    view_run = SimpleNamespace(
        snapshot=snapshot, scope=scope, invariants=invariants, user_id=parent.user_id,
        credits_limit=quote, credits_spent=0, version_lock=parent.version_lock)
    verdict = review(Plan.model_validate(proposal), view_run, catalog, int(balance), invariants=invariants)
    if verdict["verdict"] != "pass":
        raise CompileError("review_rejected")
    try:
        next_budget = CreditBudget(
            int(parent.credits_limit), int(parent.credits_reserved),
            int(parent.credits_allocated), int(parent.credits_spent)).allocate(quote)
    except AllocationError as exc:
        raise CompileError("budget_rejected") from exc
    parent.credits_reserved = next_budget.reserved
    parent.credits_allocated = next_budget.allocated
    parent.credits_spent = next_budget.spent
    token_limit = remaining_tokens
    if token_limit:
        if int(parent.tokens_used) + int(parent.tokens_reserved) + token_limit > int(parent.tokens_limit):
            token_limit = max(0, int(parent.tokens_limit) - int(parent.tokens_used) - int(parent.tokens_reserved))
        parent.tokens_reserved = int(parent.tokens_reserved) + token_limit
    child = await create_run(tx, RunInput(
        session_id=parent.session_id, workspace_id=parent.workspace_id, user_id=parent.user_id,
        request_key=f"batch-retry:{parent.id}:{request_key}"[:128],
        request_hash=digest({"retry": request_key, "items": list(item_ids or []), "parent": str(parent.id)}),
        snapshot=snapshot, scope=scope, invariants=invariants,
        version_lock=dict(parent.version_lock or {}), parent_run_id=parent.id),
        CoreSettings(credits_limit=quote, tokens_limit=token_limit, max_thought_rounds=remaining_rounds))
    readable = list(scope.get("readable_assets") or [])
    for ident in readable:
        try:
            asset_id = uuid.UUID(str(ident))
        except (ValueError, TypeError):
            continue
        exists = await tx.scalar(select(AssetReference.id).where(
            AssetReference.asset_id == asset_id, AssetReference.owner_type == "core_run",
            AssetReference.owner_id == str(child.id)))
        if exists is None:
            tx.add(AssetReference(asset_id=asset_id, owner_type="core_run", owner_id=str(child.id)))
    hashed = proposal_hash(proposal)
    row = CoreBatchRetry(
        workspace_id=parent.workspace_id, parent_run_id=parent.id, child_run_id=child.id,
        request_key=request_key, quote=quote,
        document={"layer": RETRY_LAYER, "proposal": proposal, "proposal_hash": hashed,
                  "payload_hash": payload, "links": redo.get("links") or [], "items": redo["items"],
                  "token_limit": token_limit, "settled": False})
    tx.add(row)
    await tx.flush()
    session.active_run_id = child.id
    root_event = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == parent.id)
                                 .order_by(CoreEvent.run_sequence.desc()).limit(1))
    child_birth = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == child.id)
                                  .order_by(CoreEvent.run_sequence).limit(1))
    await emit(tx, "user_message", child.id, causation_id=root_event or child_birth)
    return {"replayed": False, "retry_id": str(row.id), "child_run_id": str(child.id), "quote": quote,
            "task_count": 0, "item_count": redo["item_count"]}
