"""Non-blocking session dispatch. Database lease is the clock; no in-process timer."""
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from backend.core.contracts import CoreSettings
from backend.core.events.store import emit
from backend.core.orm import CoreEvent, CoreRun, CoreSession, CoreSessionMessage
from backend.core.sessions.contracts import SessionSettings
from backend.core.sessions.fencing import ever_submitted, try_lock_run
from backend.core.sessions.interrupt import cancel_unsubmitted
from backend.core.sessions.revisions import append_revision, effective_request
from backend.core.sessions.store import append_event
from backend.core.supervisor.service import TERMINAL, open_run
from backend.core.supervisor.wiring import deployment_routes, preferred_route
from backend.infrastructure.orm import Document


EXACT_REDO = frozenset({"再来一次", "再生成一次", "重做一次"})
REDO_EXPLANATION = {
    "missing_budget": "无法重做：这条消息需要明确的积分预算和 token 预算。",
    "source_run_not_completed": "无法重做：没有已完成的生成可以复制。",
    "plan_not_executable": "无法重做：最近一次完成的运行没有当前可执行计划。",
    "plan_not_admitted": "无法重做：当前计划尚未准入。",
    "plan_not_approved": "无法重做：当前计划没有已通过的审核。",
    "plan_tasks_incomplete": "无法重做：当前计划的步骤尚未全部成功。",
    "redo_not_generative": "无法重做：当前计划没有支持更换种子的生成步骤。",
    "seed_unsupported": "无法重做：该生成能力不支持更换种子。",
    "seed_out_of_range": "无法重做：没有可用的新种子。",
    "seed_unchanged": "无法重做：不能沿用与上次相同的种子。",
    "seed_invalid": "无法重做：种子无效。",
    "canvas_missing": "无法重做：当前画布不可用。",
    "idempotency_conflict": "无法重做：相同请求键对应了不同内容。",
    "session_has_active_run": "无法重做：会话仍有进行中的运行。",
    "session_not_found": "无法重做：会话不可用。",
    "plan_graph_changed": "无法重做：复制计划改变了步骤结构。",
    "prompt_rewritten": "无法重做：复制计划改写了提示词。",
    "run_event_missing": "无法重做：新运行缺少事件。",
}


def normalized_text(content) -> str:
    text = (content or {}).get("text") if isinstance(content, dict) else ""
    return " ".join(text.split()) if isinstance(text, str) else ""


def exact_redo_batch(pending) -> bool:
    return bool(pending) and all(normalized_text(message.content) in EXACT_REDO for message in pending)


def should_natural_redo(pending, *, active_run: bool) -> bool:
    """Exact redo phrases open redo_session only when no Run is active."""
    return not active_run and exact_redo_batch(pending)


def explicit_budget(content) -> tuple[int, int] | None:
    if not isinstance(content, dict) or "budget" not in content or "tokens" not in content:
        return None
    credits, tokens = content.get("budget"), content.get("tokens")
    if type(credits) is not int or type(tokens) is not int:
        return None
    if credits <= 0 or tokens <= 0:
        return None
    return credits, tokens


def redo_explanation(code: str) -> str:
    return REDO_EXPLANATION.get(code, "无法重做：" + code + "。")


async def _explain_redo(tx, session, pending, code: str):
    session.message_cursor = int(session.message_cursor or 0) + 1
    note = CoreSessionMessage(
        id=uuid.uuid4(), session_id=session.id, sequence=session.message_cursor,
        request_key="redo-reject:" + str(pending[-1].id), role="assistant",
        content={"text": redo_explanation(code), "code": code},
        processing_status="processed", intent="redo")
    tx.add(note)
    await tx.flush()
    for message in pending:
        message.processing_status = "processed"
        message.run_id = None
        if not message.intent:
            message.intent = "redo"
    await append_event(tx, session, event_type="redo_rejected", message_id=note.id,
                       payload={"message_id": str(note.id)})
    session.dispatch_at = None
    session.dispatch_lease_owner = session.dispatch_lease_until = None
    return note


async def open_exact_redo(tx, session, pending):
    """Copy the latest completed run's current plan, or store an explanation. Never calls Planner."""
    from backend.core.session_redo_service import RedoRejected, RedoRequest, redo_session

    budgets = [explicit_budget(message.content) for message in pending]
    if any(item is None for item in budgets):
        await _explain_redo(tx, session, pending, "missing_budget")
        return None
    credits, tokens = budgets[-1]
    source = await tx.scalar(select(CoreRun).where(
        CoreRun.session_id == session.id, CoreRun.workspace_id == session.workspace_id,
        CoreRun.user_id == session.user_id, CoreRun.status == "completed")
        .order_by(CoreRun.finished_at.desc(), CoreRun.id.desc()).limit(1))
    if source is None:
        await _explain_redo(tx, session, pending, "source_run_not_completed")
        return None
    if source.current_plan_id is None:
        await _explain_redo(tx, session, pending, "plan_not_executable")
        return None
    body = RedoRequest(request_key="redo:" + str(pending[-1].id), source_run_id=source.id,
                       source_plan_id=source.current_plan_id, credits_limit=credits, tokens_limit=tokens)
    try:
        async with tx.begin_nested():
            created = await redo_session(tx, session_id=session.id, user_id=session.user_id,
                                         workspace_id=session.workspace_id, body=body)
    except RedoRejected as exc:
        await _explain_redo(tx, session, pending, exc.code)
        return None
    for message in pending:
        message.processing_status = "processed"
        message.run_id = created.id
        if not message.intent:
            message.intent = "redo"
    return created


async def _reschedule(session, stamp, settings):
    session.dispatch_at = stamp + timedelta(seconds=settings.retry_seconds)
    session.dispatch_lease_owner = session.dispatch_lease_until = None


async def _wake_replan(tx, run, cause):
    run.current_plan_id = None
    run.submit_fence = int(run.input_revision or 1)
    await emit(tx, "user_message", run.id, causation_id=cause)


async def dispatch_due(factory, settings=None, agent_route=None):
    settings = settings or SessionSettings()
    core = CoreSettings()
    if agent_route is None:
        agent_route = preferred_route(deployment_routes(), None)
    token = uuid.uuid4().hex
    async with factory.begin() as tx:
        stamp = await tx.scalar(select(func.clock_timestamp()))
        row = await tx.scalar(select(CoreSession).where(
            CoreSession.dispatch_at.is_not(None), CoreSession.dispatch_at <= stamp,
            CoreSession.closed_at.is_(None),
            (CoreSession.dispatch_lease_until.is_(None) | (CoreSession.dispatch_lease_until <= stamp)))
            .order_by(CoreSession.dispatch_at, CoreSession.id).limit(1).with_for_update(skip_locked=True))
        if row is None:
            return False
        row.dispatch_lease_owner = token
        row.dispatch_lease_until = stamp + timedelta(seconds=settings.dispatch_lease_seconds)
        session_id = row.id
    async with factory.begin() as tx:
        stamp = await tx.scalar(select(func.clock_timestamp()))
        session = await tx.scalar(select(CoreSession).where(CoreSession.id == session_id)
            .with_for_update(skip_locked=True))
        if session is None:
            return True
        if session.dispatch_lease_owner != token:
            return True
        if session.dispatch_at is not None and session.dispatch_at > stamp:
            session.dispatch_lease_owner = session.dispatch_lease_until = None
            return True
        pending = list(await tx.scalars(select(CoreSessionMessage).where(
            CoreSessionMessage.session_id == session.id, CoreSessionMessage.processing_status == "pending",
            CoreSessionMessage.role == "user").order_by(CoreSessionMessage.sequence)))
        if not pending:
            session.dispatch_at = session.dispatch_lease_owner = session.dispatch_lease_until = None
            return True
        run = None
        if session.active_run_id:
            run = await try_lock_run(tx, session.active_run_id)
            if run is None:
                exists = await tx.scalar(select(CoreRun.id).where(CoreRun.id == session.active_run_id))
                if exists is not None:
                    await _reschedule(session, stamp, settings)
                    return True
                # Deleted cache target is different from a busy Run; repair the pointer.
                session.active_run_id = None
            elif run.session_id != session.id:
                raise ValueError("active_run_session_mismatch")
            elif run.status in TERMINAL:
                session.active_run_id = None
                run = None
        correction = next((m.correction_of_run_id for m in pending if m.correction_of_run_id), None)
        related = next((m.related_run_id for m in pending if m.related_run_id), None)
        opened_by_redo = False
        if run is not None and run.status == "paused" and run.pause_reason == "waiting_user":
            # A waiting subject choice can be answered by plain chat (e.g. 抠杯子)
            # without waking the planner; a miss falls through to interruption.
            from backend.core.supervisor.subjects import try_message_answer
            answered = await try_message_answer(tx, run,
                " ".join(str((m.content or {}).get("text") or "") for m in pending if m.role == "user"))
            if answered:
                for item in pending:
                    if item.processing_status == "pending":
                        item.processing_status = "processed"
                        item.run_id = run.id
                session.dispatch_at = session.dispatch_lease_owner = session.dispatch_lease_until = None
                return True
        if should_natural_redo(pending, active_run=run is not None):
            opened = await open_exact_redo(tx, session, pending)
            if opened is None:
                return True
            run = opened
            opened_by_redo = True
        if opened_by_redo:
            pass
        elif run is None:
            doc = await tx.get(Document, session.canvas_id)
            request = effective_request(pending)
            from backend.core.session_context import reference_context
            request, reference = await reference_context(tx, session, request)
            if reference and reference["status"] != "resolved":
                request["reference_clarification"] = reference
            first = pending[0].content or {}
            if "_agent_route" in first:
                from backend.agent.config import AgentRoute
                agent_route = AgentRoute.from_snapshot(first["_agent_route"]) if first["_agent_route"] else None
            run = await open_run(tx, session, doc, workspace_id=session.workspace_id, user_id=session.user_id,
                request_key=f"session:{session.id}:{pending[0].id}", request=request,
                budget=int(first["budget"]) if first.get("budget") is not None else 0,
                token_budget=int(first["tokens"]) if first.get("tokens") is not None else core.tokens_limit,
                max_rounds=int(first["rounds"]) if first.get("rounds") is not None else core.max_thought_rounds,
                mode=first.get("mode") or "auto", input_revision=1, correction_of_run_id=correction or related,
                persist_opening_message=False, agent_route=agent_route)
            await append_revision(tx, run, session, pending, user_correction=(
                {"message_ids": [str(m.id) for m in pending], "correction_of_run_id": str(correction)}
                if correction else None), initial=True)
            run.submit_fence = run.input_revision
        elif not ever_submitted(run):
            await append_revision(tx, run, session, pending, user_correction=(
                {"message_ids": [str(m.id) for m in pending]} if correction else None), initial=False)
            await cancel_unsubmitted(tx, run)
            cause = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == run.id)
                .order_by(CoreEvent.run_sequence.desc()).limit(1))
            await _wake_replan(tx, run, cause)
        else:
            await append_revision(tx, run, session, pending, user_correction={
                "message_ids": [str(m.id) for m in pending],
                "texts": [(m.content or {}).get("text") for m in pending]}, initial=False)
            cause = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == run.id)
                .order_by(CoreEvent.run_sequence.desc()).limit(1))
            await emit(tx, "user_interrupt", run.id, causation_id=cause, ids={"message_id": pending[-1].id})
            run.submit_fence = int(run.input_revision or 1)
        for message in pending:
            if message.processing_status == "pending":
                message.processing_status = "processed"
                message.run_id = run.id
        # Sessions disable autoflush; the pending query must see these updates.
        await tx.flush()
        later = await tx.scalar(select(CoreSessionMessage.id).where(
            CoreSessionMessage.session_id == session.id, CoreSessionMessage.processing_status == "pending").limit(1))
        session.dispatch_at = None if later is None else session.dispatch_at
        session.dispatch_lease_owner = session.dispatch_lease_until = None
        session.active_run_id = run.id
        await append_event(tx, session, event_type="dispatched", run_id=run.id,
            message_id=pending[-1].id, payload={"message_id": str(pending[-1].id)})
        return True
