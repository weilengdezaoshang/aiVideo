"""Worker submit fence: matching input revision, pending intake fence, first-submitted latch."""
from sqlalchemy import select

from backend.core.events.store import db_now
from backend.core.orm import CoreRun, CoreSession, CoreSessionMessage, CoreTask


class ObsoleteRevision(ValueError):
    pass


def revision_blocks_submit(run, task) -> bool:
    current = int(getattr(run, "input_revision", None) or 1)
    fence = int(getattr(run, "submit_fence", None) or current)
    task_revision = int(getattr(task, "input_revision", None) or 1)
    return task_revision != current or fence != current


async def pending_user_messages(tx, session_id):
    """Committed pending intake rows. No Session lock; Worker already holds the Run."""

    return await tx.scalar(select(CoreSessionMessage.id).where(
        CoreSessionMessage.session_id == session_id, CoreSessionMessage.role == "user",
        CoreSessionMessage.processing_status == "pending").limit(1))


async def assert_submittable(tx, run: CoreRun, task: CoreTask):
    """Caller already holds the Run row lock. Session is never locked here.

    Intake bump uses SKIP LOCKED, so an unrelated holder of this Run can miss the
    fence write. A committed pending CoreSessionMessage still blocks submit.
    The Worker transaction that already began is the nonblocking lock winner.
    """

    await tx.refresh(run)
    if revision_blocks_submit(run, task):
        raise ObsoleteRevision("obsolete_input_revision")
    if await pending_user_messages(tx, run.session_id) is not None:
        raise ObsoleteRevision("obsolete_input_revision")
    return True


async def latch_first_submit(tx, run: CoreRun, task: CoreTask):
    if run.first_submitted_at is None:
        run.first_submitted_at = await db_now(tx)
        run.first_submitted_task_id = task.id
    return True


def ever_submitted(run: CoreRun) -> bool:
    return run.first_submitted_at is not None or run.first_submitted_task_id is not None


async def has_submitted_task(tx, run_id) -> bool:
    return await tx.scalar(select(CoreTask.id).where(CoreTask.run_id == run_id,
        CoreTask.status.in_(("submitted", "succeeded", "failed", "blocked_by_model"))).limit(1)) is not None


async def try_lock_run(tx, run_id):
    """SKIP LOCKED. Caller must already hold the Session lock. Never lock Session from a Run."""

    if run_id is None:
        return None
    return await tx.scalar(select(CoreRun).where(CoreRun.id == run_id).with_for_update(skip_locked=True))


async def bump_submit_fence(tx, session):
    run = await try_lock_run(tx, session.active_run_id)
    if run is None or run.status in {"completed", "failed", "cancelled"}:
        return run
    current = int(run.input_revision or 1)
    run.submit_fence = max(int(run.submit_fence or current), current + 1)
    return run


async def lock_session_then_run(tx, run_id, *, skip_locked=False):
    """Always Session then Run. Never acquire Session after a Run lock."""

    run = await tx.get(CoreRun, run_id)
    if run is None:
        return None, None
    session = await tx.scalar(select(CoreSession).where(CoreSession.id == run.session_id)
        .with_for_update(skip_locked=skip_locked))
    if session is None:
        return None, None
    locked = await tx.scalar(select(CoreRun).where(CoreRun.id == run_id)
        .with_for_update(skip_locked=skip_locked))
    return session, locked
