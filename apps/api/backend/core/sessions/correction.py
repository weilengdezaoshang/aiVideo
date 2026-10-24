"""User-authorized correction phrases; same-Session evidence only."""
import re

from sqlalchemy import select

from backend.core.orm import CorePlan, CoreRun, CoreSessionMessage, CoreTask

# Ordinary rephrases such as 换成/改成 revise the request. Only explicit
# disagreement becomes a negative sample against a previous Run.
CORRECTION = re.compile(
    r"不是这个意思|不是这样|不是这个|不对|我要的是|纠正",
    re.I,
)
TERMINAL_RUN = {"completed", "failed", "cancelled"}


def detect_correction(text: str) -> bool:
    return bool(text and CORRECTION.search(text))


async def assert_same_session_run(tx, session, run_id, *, code="invalid_related_run"):
    if run_id is None:
        return None
    run = await tx.get(CoreRun, run_id)
    if run is None or run.session_id != session.id or run.user_id != session.user_id:
        raise ValueError(code)
    return run


async def infer_correction_run(tx, session, text, *, related_run_id=None, correction_of_run_id=None):
    if correction_of_run_id is not None:
        return await assert_same_session_run(tx, session, correction_of_run_id, code="invalid_correction_run")
    if related_run_id is not None:
        await assert_same_session_run(tx, session, related_run_id)
    if not detect_correction(text):
        return None
    if related_run_id is not None:
        return await assert_same_session_run(tx, session, related_run_id, code="invalid_correction_run")
    if session.active_run_id:
        active = await assert_same_session_run(tx, session, session.active_run_id, code="invalid_correction_run")
        if active is not None and active.status not in TERMINAL_RUN:
            return active
    pending = await tx.scalar(select(CoreSessionMessage.id).where(
        CoreSessionMessage.session_id == session.id, CoreSessionMessage.role == "user",
        CoreSessionMessage.processing_status == "pending").limit(1))
    if pending is not None:
        return None
    prior = await tx.scalar(select(CoreRun).where(CoreRun.session_id == session.id)
        .order_by(CoreRun.created_at.desc()).limit(1))
    if prior is None or prior.user_id != session.user_id:
        return None
    return prior


async def record_negative_sample(tx, session, run, message, text):
    """Attach prior plan/results as correction evidence. Does not create a new Run."""

    plan = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run.id)
        .order_by(CorePlan.version.desc()).limit(1))
    tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id)
        .order_by(CoreTask.created_at)))
    evidence = {
        "correction": True,
        "text": text,
        "run_id": str(run.id),
        "plan_id": str(plan.id) if plan else None,
        "plan_version": plan.version if plan else None,
        "task_ids": [str(task.id) for task in tasks],
        "message_id": str(message.id),
    }
    content = dict(message.content or {})
    content["feedback"] = True
    content["correction"] = True
    content["related"] = {k: v for k, v in {
        "plan_id": evidence["plan_id"], "task_id": evidence["task_ids"][-1] if evidence["task_ids"] else None,
    }.items() if v}
    message.content = content
    message.correction_of_run_id = run.id
    return evidence
