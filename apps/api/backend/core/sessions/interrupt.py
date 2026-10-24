"""user_interrupt revises the same Run; it does not cancel submitted work."""
from sqlalchemy import select

from backend.core.orm import CoreRun, CoreSessionMessage, CoreTask
from backend.core.store import transition
from backend.core.supervisor.service import TERMINAL


UNSUBMITTED = {"pending", "validated", "queued", "waiting_user"}


async def cancel_unsubmitted(tx, run, causation_id=None):
    for task in await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id)):
        if task.status in UNSUBMITTED:
            await transition(tx, "task", task.id, task.version, "cancelled", causation_id=causation_id)


def is_replan(invocation) -> bool:
    return (invocation.context or {}).get("phase") == "replan"


async def apply_interrupt(tx, run, invocation):
    """Preserve submitted tasks, drop obsolete follow-ups, mark a same-Run replan."""

    if is_replan(invocation) or run.status in TERMINAL:
        return False
    await cancel_unsubmitted(tx, run, invocation.event_id)
    inflight = await tx.scalar(select(CoreTask.id).where(CoreTask.run_id == run.id,
        CoreTask.status == "submitted").limit(1))
    if inflight:
        run.error = {**(run.error or {}), "interrupt_pending": True, "interrupt_revision": run.input_revision}
        return False
    run.current_plan_id = None
    run.submit_fence = int(run.input_revision or 1)
    if run.status == "paused" and run.pause_reason == "waiting_user":
        await transition(tx, "run", run.id, run.version, run.resume_state, causation_id=invocation.event_id)
    if run.status == "created":
        await transition(tx, "run", run.id, run.version, "planning", causation_id=invocation.event_id)
    return True


async def correction_evidence(tx, run):
    rows = list(await tx.scalars(select(CoreSessionMessage).where(
        CoreSessionMessage.run_id == run.id, CoreSessionMessage.input_revision == run.input_revision,
        CoreSessionMessage.role == "user").order_by(CoreSessionMessage.sequence)))
    return [{"id": str(row.id), "text": (row.content or {}).get("text"),
        "correction_of_run_id": str(row.correction_of_run_id) if row.correction_of_run_id else None}
        for row in rows]


def invocation_obsolete(invocation, run: CoreRun) -> bool:
    current = int(getattr(run, "input_revision", None) or 1)
    if int(getattr(run, "submit_fence", None) or current) != current:
        return True
    recorded = (invocation.context or {}).get("input_revision")
    if recorded is None:
        return False
    return int(recorded) != current
