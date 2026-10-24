"""Retry receipt helpers: planner hook, latest outcomes, settlement, effective progress."""
from __future__ import annotations

import copy
import uuid

from sqlalchemy import select, text

from backend.core.batch.budget import AllocationError, CreditBudget
from backend.core.batch.contracts import BatchSettings
from backend.core.batch.compile import CompileError
from backend.core.batch.orm import CoreBatchRetry
from backend.core.batch.planner import RetryPlanner, proposal_hash
from backend.core.batch.retry import latest_by_logical
from backend.core.batch.schedule import item_progress
from backend.core.events.store import emit
from backend.core.orm import CoreEvent, CoreRun, CoreSession, CoreTask
from backend.core.supervisor.contracts import Settings
from backend.core.supervisor.service import TASK_TERMINAL, TERMINAL
from backend.infrastructure.orm import Asset, Outbox

RETRY_LAYER = "rule_batch_retry"


async def planner_for_retry(tx, factory, run) -> RetryPlanner | None:
    """Return RetryPlanner when this child has a hashed receipt. Parent engine calls this from planner_for."""

    if run is None or run.parent_run_id is None:
        return None
    if ((run.snapshot or {}).get("retry") or {}).get("layer") == RETRY_LAYER:
        # A corrupt/missing ticket must fail inside this rule planner, never fall
        # through to an expensive new model interpretation of the original job.
        return RetryPlanner(factory, run.id, run.parent_run_id)
    receipt = await tx.scalar(select(CoreBatchRetry).where(
        CoreBatchRetry.child_run_id == run.id, CoreBatchRetry.parent_run_id == run.parent_run_id))
    if receipt is None:
        return None
    data = receipt.document or {}
    proposal = data.get("proposal")
    if not isinstance(proposal, dict) or proposal_hash(proposal) != data.get("proposal_hash"):
        return None
    if data.get("layer") != RETRY_LAYER:
        return None
    return RetryPlanner(factory, run.id, run.parent_run_id)


async def load_root_outcomes(tx, root: CoreRun, document: dict) -> dict[str, dict]:
    plan_id = root.current_plan_id
    tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == root.id)))
    if plan_id is not None:
        tasks = [task for task in tasks if task.plan_id == plan_id]
    overlays = [_task_layer(tasks)]
    receipts = list(await tx.scalars(select(CoreBatchRetry).where(
        CoreBatchRetry.parent_run_id == root.id).order_by(CoreBatchRetry.created_at, CoreBatchRetry.id)))
    for receipt in receipts:
        if not (receipt.document or {}).get("settled") or receipt.child_run_id is None:
            continue
        child_tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == receipt.child_run_id)))
        overlays.append(_child_layer(receipt, child_tasks))
    return latest_by_logical(document, overlays)


def _task_layer(tasks: list) -> dict[str, dict]:
    layer = {}
    for task in tasks:
        submitted = task.status in {"submitted", "succeeded", "failed", "blocked_by_model"} or task.job_id is not None
        unknown_submitted = task.status == "submitted"
        layer[task.task_key] = {
            "status": task.status,
            "output": task.output,
            "error": task.error,
            "submitted": submitted,
            "unknown_submitted": unknown_submitted,
            "reusable": None,
        }
        logical = task.stage_key
        if logical:
            layer[logical] = layer[task.task_key]
    return layer


def _child_layer(receipt: CoreBatchRetry, tasks: list) -> dict[str, dict]:
    by_key = {task.task_key: task for task in tasks}
    layer = {}
    for link in (receipt.document or {}).get("links") or []:
        task = by_key.get(link.get("task_key"))
        if task is None:
            continue
        outcome = {
            "status": task.status, "output": task.output, "error": task.error,
            "submitted": task.status in {"submitted", "succeeded", "failed", "blocked_by_model"} or task.job_id is not None,
            "unknown_submitted": task.status == "submitted",
        }
        if link.get("source_task_key"):
            layer[link["source_task_key"]] = outcome
        if link.get("logical_key"):
            layer[link["logical_key"]] = outcome
    return layer


async def mark_reusable(tx, workspace_id, outcomes: dict[str, dict]) -> dict[str, dict]:
    for outcome in outcomes.values():
        output = outcome.get("output") if isinstance(outcome, dict) else None
        if outcome.get("status") != "succeeded" or not isinstance(output, dict) or not output.get("asset_id"):
            outcome["reusable"] = False
            continue
        try:
            ident = uuid.UUID(str(output["asset_id"]))
        except (ValueError, TypeError):
            outcome["reusable"] = False
            continue
        asset = await tx.get(Asset, ident)
        if asset is None or asset.workspace_id != workspace_id:
            outcome["reusable"] = False
            continue
        if (asset.details or {}).get("hostBlocked") or (asset.details or {}).get("ephemeral"):
            outcome["reusable"] = False
            continue
        if asset.kind == "mask" and not asset.storage_key:
            outcome["reusable"] = False
            continue
        outcome["reusable"] = True
    return outcomes


async def effective_batch_progress(tx, root, root_document, root_tasks) -> dict:
    """95 successful vs 100 settled. Root historical status is not rewritten."""

    outcomes = await load_root_outcomes(tx, root, root_document)
    status = {}
    for item in root_document.get("items") or []:
        for step in item.get("steps") or []:
            key = step["task_key"]
            status[key] = (outcomes.get(key) or {}).get("status") or "pending"
    for task in root_tasks:
        status.setdefault(task.task_key, task.status)
    progress = item_progress(root_document, status,
                             {key: row.get("error") or {} for key, row in outcomes.items()})
    retryable = []
    for item in root_document.get("items") or []:
        if item["item_id"] not in progress.get("retryableItemIds", []):
            continue
        if any((outcomes.get(step["task_key"]) or {}).get("status") == "succeeded"
               for step in item.get("steps") or [] if step.get("local_id") == item["steps"][-1]["local_id"]):
            continue
        from backend.core.batch.retry import is_retryable_failure
        if any(is_retryable_failure(outcomes.get(step["task_key"]) or {}) for step in item.get("steps") or []):
            retryable.append(item["item_id"])
    progress["retryableItemIds"] = retryable
    progress["retryableCount"] = len(retryable)
    succeeded_items = 0
    for item in root_document.get("items") or []:
        steps = item.get("steps") or []
        if steps and all((outcomes.get(step["task_key"]) or {}).get("status") == "succeeded" for step in steps):
            succeeded_items += 1
    progress["succeededItems"] = succeeded_items
    return progress


def _lock_ms(settings) -> int:
    if settings is not None and getattr(settings, "lock_timeout_ms", None):
        return int(settings.lock_timeout_ms)
    return int(Settings().lock_timeout_ms)


async def reconcile_completed_retries(factory, settings=None) -> bool:
    """Session -> root -> child. Never wait on root while holding a child Run lock."""

    changed = False
    async with factory() as probe:
        pending = list(await probe.scalars(select(CoreBatchRetry.id)
            .join(CoreRun, CoreRun.id == CoreBatchRetry.child_run_id)
            .where(CoreRun.status.in_(TERMINAL), text(
                "core_batch_retries.child_run_id IS NOT NULL AND "
                "COALESCE((core_batch_retries.document ->> 'settled')::boolean, false) = false"))
            .order_by(CoreBatchRetry.created_at, CoreBatchRetry.id)
            .limit(BatchSettings().recover_limit)))
    for receipt_id in pending:
        if await _settle_one(factory, receipt_id, settings):
            changed = True
    return changed


async def _settle_one(factory, receipt_id, settings) -> bool:
    async with factory.begin() as tx:
        await tx.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{_lock_ms(settings)}ms"})
        receipt = await tx.get(CoreBatchRetry, receipt_id)
        if receipt is None or (receipt.document or {}).get("settled") or receipt.child_run_id is None:
            return False
        parent = await tx.get(CoreRun, receipt.parent_run_id)
        if parent is None:
            return False
        session = await tx.scalar(select(CoreSession).where(CoreSession.id == parent.session_id).with_for_update(skip_locked=True))
        if session is None:
            return False
        parent = await tx.scalar(select(CoreRun).where(CoreRun.id == parent.id).with_for_update(skip_locked=True))
        if parent is None:
            return False
        child = await tx.scalar(select(CoreRun).where(CoreRun.id == receipt.child_run_id).with_for_update(skip_locked=True))
        receipt = await tx.scalar(select(CoreBatchRetry).where(CoreBatchRetry.id == receipt_id).with_for_update(skip_locked=True))
        if receipt is None or (receipt.document or {}).get("settled"):
            return False
        if child is None or child.status not in TERMINAL:
            return False
        unfinished = await tx.scalar(select(CoreTask.id).where(
            CoreTask.run_id == child.id, CoreTask.status.not_in(TASK_TERMINAL)).limit(1))
        if unfinished is not None:
            return False
        actual = int(child.credits_spent)
        budget = CreditBudget(int(parent.credits_limit), int(parent.credits_reserved),
                              int(parent.credits_allocated), int(parent.credits_spent))
        try:
            next_budget = budget.settle(int(receipt.quote), actual)
        except AllocationError as exc:
            raise CompileError("unapproved_settlement") from exc
        parent.credits_reserved = next_budget.reserved
        parent.credits_allocated = next_budget.allocated
        parent.credits_spent = next_budget.spent
        document = dict(receipt.document or {})
        token_limit = int(document.get("token_limit") or 0)
        usage = (child.error or {}).get("planner_usage") or {}
        unknown = bool(usage.get("unknown") or usage.get("unknown_calls"))
        child_used = int(child.tokens_used or 0)
        if unknown:
            hold = token_limit if token_limit else int(child.tokens_limit or 0)
            document["unknown_token_hold"] = hold
            parent.tokens_reserved = max(0, int(parent.tokens_reserved) - token_limit + hold)
        else:
            parent.tokens_reserved = max(0, int(parent.tokens_reserved) - token_limit)
            parent.tokens_used = int(parent.tokens_used) + child_used
            if parent.tokens_used + parent.tokens_reserved > parent.tokens_limit:
                raise CompileError("token_budget")
        rounds = int(child.thought_rounds_used or 0)
        parent.thought_rounds_used = min(int(parent.max_thought_rounds),
                                         int(parent.thought_rounds_used) + rounds)
        document["settled"] = True
        document["actual"] = actual
        receipt.document = document
        last = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == child.id)
                               .order_by(CoreEvent.run_sequence.desc()).limit(1))
        if last is not None:
            await emit(tx, "helper_report", parent.id, causation_id=last)
        tx.add(Outbox(aggregate_type="run", aggregate_id=str(parent.id), event_type="core.changed",
                      payload={"runId": str(parent.id)}))
        await tx.flush()
        return True


def child_invariants(parent: CoreRun, item_count: int) -> dict:
    invariants = copy.deepcopy(parent.invariants or {})
    deliverable = dict(invariants.get("deliverable") or {})
    if item_count >= 1:
        deliverable["count"] = item_count
    if deliverable:
        invariants["deliverable"] = deliverable
    return invariants


def child_scope(parent: CoreRun, extra_assets: list[str]) -> dict:
    scope = copy.deepcopy(parent.scope or {})
    readable = list(scope.get("readable_assets") or [])
    for ident in extra_assets:
        if ident not in readable:
            readable.append(ident)
    scope["readable_assets"] = readable
    return scope


def retry_snapshot(parent: CoreRun, receipt_key: str) -> dict:
    snapshot = copy.deepcopy(parent.snapshot or {})
    snapshot["retry"] = {"layer": RETRY_LAYER, "parent_run_id": str(parent.id), "request_key": receipt_key}
    return snapshot
