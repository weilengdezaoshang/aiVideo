"""Item isolation over a compiled batch document. No I/O."""
from __future__ import annotations

_TERMINAL = {"succeeded", "failed", "blocked_by_model", "cancelled", "rejected"}
_BREAKS_ITEM = {"failed", "blocked_by_model"}
_BREAK_CODES = {"no_subject", "timeout", "upstream_failed"}


def successor_task_keys(document: dict, tasks_by_key: dict) -> list[str]:
    """Unsubmitted successors inside an item whose earlier step has already failed.

    Other items are ignored. Provider-submitted tasks are left for reconciliation.
    """

    keys: list[str] = []
    definitions = {step["id"]: step for step in document.get("steps") or []}
    for item in document.get("items") or []:
        members = {step["step_id"]: step["task_key"] for step in item.get("steps") or []}
        dependents: dict[str, list[str]] = {step_id: [] for step_id in members}
        for step_id in members:
            for dependency in definitions.get(step_id, {}).get("depends_on") or []:
                if dependency in members:
                    dependents[dependency].append(step_id)
        broken = []
        for step in item.get("steps") or []:
            key = step.get("task_key")
            task = tasks_by_key.get(key)
            if task is None:
                continue
            status = task.status
            code = (task.error or {}).get("code") if isinstance(task.error, dict) else None
            if status in _BREAKS_ITEM or (
                status == "cancelled" and code in _BREAK_CODES
            ):
                broken.append(step["step_id"])
        visited = set(broken)
        while broken:
            for child in dependents.get(broken.pop(), []):
                if child in visited:
                    continue
                visited.add(child)
                broken.append(child)
                task = tasks_by_key.get(members[child])
                if task is not None and task.status not in _TERMINAL and task.status != "submitted":
                    keys.append(members[child])
    return keys


def stage_marker(task) -> str | None:
    """Batch tasks carry ``stage_key``. Ordinary tasks return None and must not touch quota tables."""

    marker = getattr(task, "stage_key", None)
    return marker if isinstance(marker, str) and marker.startswith("i:") else None


async def settle_marked_dispatch(tx, run, task) -> str | None:
    """``worker.finish`` hook. Signature: ``(tx, run, task) -> str | None``.

    A missing marker means this is not a batch task. Unknown submits are not released.
    """

    if stage_marker(task) is None:
        return None
    code = (getattr(task, "error", None) or {}).get("code") if isinstance(getattr(task, "error", None), dict) else None
    if code == "submission_unknown":
        return "unknown"
    if getattr(task, "status", None) not in {"succeeded", "failed", "blocked_by_model", "cancelled", "rejected"}:
        return "not_terminal"
    from backend.core.batch.store import settle_dispatch
    return await settle_dispatch(tx, run.id, task.task_key)

def canvas_placement(placement, *, tool: str | None, step_id: str, batching: bool, depended_ids) -> dict:
    """Approved DAG decides canvas writes. Model ``suppress_canvas`` is never kept."""

    cleaned = {key: value for key, value in dict(placement or {}).items() if key != "suppress_canvas"}
    if batching and step_id in depended_ids:
        cleaned["suppress_canvas"] = True
    return cleaned




async def reconcile_terminal_dispatches(factory) -> bool:
    """Release durable slots even when cancellation has already ended the Run.

    Unknown submissions remain in flight until the execution reconciler resolves
    them. A busy quota is deferred without delaying other supervisor work.
    """
    from sqlalchemy import select
    from backend.core.batch.contracts import BatchSettings
    from backend.core.batch.orm import CoreBatchDispatch
    from backend.core.batch.store import release_reserved, settle_dispatch
    from backend.core.orm import CoreTask

    changed = False
    async with factory.begin() as tx:
        rows = list(await tx.scalars(select(CoreBatchDispatch)
            .join(CoreTask, (CoreTask.run_id == CoreBatchDispatch.run_id)
                  & (CoreTask.task_key == CoreBatchDispatch.task_key))
            .where(CoreBatchDispatch.state.in_(("reserved", "submitted")),
                   CoreTask.status.in_(_TERMINAL))
            .order_by(CoreBatchDispatch.created_at, CoreBatchDispatch.id)
            .limit(BatchSettings().recover_limit)
            .with_for_update(skip_locked=True, of=CoreBatchDispatch)))
        for row in rows:
            task = await tx.scalar(select(CoreTask).where(
                CoreTask.run_id == row.run_id, CoreTask.task_key == row.task_key))
            if task is None or (task.error or {}).get("code") == "submission_unknown":
                continue
            release = settle_dispatch if row.state == "submitted" else release_reserved
            result = await release(tx, row.run_id, row.task_key)
            changed = changed or result in {"settled", "released"}
    return changed
