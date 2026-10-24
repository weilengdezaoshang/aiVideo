"""Database checkpoints around planner calls; event handlers never wait for planners."""
import asyncio
import logging
import time
import uuid
from datetime import timedelta
from sqlalchemy import func, select, text, exists, update, or_
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import insert
from backend.agent.orm import HostCreditAccount, HostCreditLedger
from backend.core.contracts import TaskInput
from backend.core.orm import CoreRun, CorePlan, CoreTask, CoreTaskDependency, CoreSession, CoreSessionMessage, CoreEvent
from backend.core.sessions.fencing import lock_session_then_run, pending_user_messages
from backend.core.sessions.interrupt import apply_interrupt, correction_evidence, invocation_obsolete, is_replan
from backend.core.session_context import confirmed_decisions, effective_run_view
from backend.core.store import transition, create_task
from backend.core.tools.store import load_catalog
from backend.core.tools.contracts import VersionLock
from backend.core.events.store import emit
from backend.core.execution.engine import record_admission, now
from backend.core.execution.orm import CanvasWrite
from .orm import Invocation
from .contracts import Settings, PlannerResult, Step
from .planners import FakePlanner
from .reviewer import _sources, conversion_rejection, effective_invariants, merge_proposed, review
from .tool_calls import clarify_unproven_video_duration
from .context import ContextLimitError
from .replies import (
    ask_text, blocked, clarification_field_label, completed, failed, opening, place_label,
    plan_confirmation, read_summary, review_exhausted,
)
from .service import TERMINAL, TASK_TERMINAL, latest_plan, message, digest
from .wiring import frozen_selfcheck_route, planner_for_lock, selfcheck_provider_for, watchdog_seconds
from backend.agent.providers import ModelCallError
from backend.core.selfcheck.policy import content_from_raw, read_journal
from backend.core.selfcheck.service import merge_self_check, prepare_journal, apply_parsed

_BLOCKED = frozenset({"CONTENT_BLOCKED", "MODEL_CONTENT_BLOCKED"})

_AUDIT = logging.getLogger("backend.core.supervisor.audit")


def message_data(message):
    return {key: value for key, value in (message.content or {}).items() if not key.startswith("_")}


async def locked_invocation(tx, ident):
    """Reacquire Session -> Run -> Invocation after a model request releases its locks."""
    initial = await tx.get(Invocation, ident)
    if initial is None:
        raise LookupError("planner_invocation_missing")
    _session, run = await lock_session_then_run(tx, initial.run_id)
    if _session is None or run is None:
        raise LookupError("planner_callback_run_missing")
    invocation = await tx.get(Invocation, ident, with_for_update=True, populate_existing=True)
    if invocation is None:
        raise LookupError("planner_callback_record_missing")
    return invocation, run


async def _plan_for_invocation(tx, invocation, *, reviewing_only=False):
    stmt = select(CorePlan).where(CorePlan.model_call_ref == str(invocation.id)).order_by(CorePlan.version.desc())
    if reviewing_only:
        stmt = stmt.where(CorePlan.stage == "reviewing")
    return await tx.scalar(stmt.limit(1))


def _working_journal(invocation, plan):
    bag = invocation.review if isinstance(invocation.review, dict) else None
    journal = read_journal(bag) if bag else None
    if journal is not None:
        return journal
    return read_journal(plan.self_check if plan is not None else None)


def _store_working(invocation, plan, journal):
    base = invocation.review if isinstance(invocation.review, dict) else (plan.self_check if plan is not None else {})
    invocation.review = merge_self_check(base, journal)


async def _emit_plan_version(tx, run, source, *, stage, self_check):
    version = (await tx.scalar(select(func.max(CorePlan.version)).where(CorePlan.run_id == run.id)) or 0) + 1
    plan = CorePlan(
        run_id=run.id, version=version, writer=source.writer, model_call_ref=source.model_call_ref,
        raw_output=source.raw_output, proposal=source.proposal, content_hash=source.content_hash,
        rationale=source.rationale, link_constraints=source.link_constraints, result_type=source.result_type,
        stage=stage, milestones=list(source.milestones or []), self_check=self_check,
        expansion_rules=dict(source.expansion_rules or {}), version_lock=source.version_lock,
        input_revision=source.input_revision)
    tx.add(plan)
    await tx.flush()
    return plan


async def wake(tx, context):
    if context.run.version_lock.get("supervisor") not in {"rule-v1", "llm-v1"} or context.run.status in TERMINAL or context.event.event_type in {"plan_rejected", "plan_written", "asset_changed", "conflict"}:
        return
    revision = getattr(context.run, "input_revision", 1) or 1
    if context.event.event_type == "user_interrupt" and (context.event.payload or {}).get("message_id"):
        row = await tx.get(CoreSessionMessage, uuid.UUID(context.event.payload["message_id"]))
        if row is None or row.run_id != context.run.id or row.session_id != context.run.session_id:
            from backend.core.events.processor import IntegrityFailure
            raise IntegrityFailure("interrupt_message_missing_or_mismatched")
        revision = row.input_revision or revision
    await tx.execute(insert(Invocation).values(id=uuid.uuid4(), run_id=context.run.id,
        event_id=context.event.id, attempt=0, status="pending",
        context={"input_revision": revision}, failures=0)
        .on_conflict_do_nothing(index_elements=[Invocation.event_id, Invocation.attempt]))


def register(registry):
    from backend.core.events.contracts import EventType
    for event_type in EventType:
        registry.register(event_type, "supervisor-v1", wake)


def current_plan_tasks(run, tasks):
    if run.current_plan_id is None:
        return []
    return [task for task in tasks if task.plan_id == run.current_plan_id]


async def stop(tx, run, status, text_value, key):
    from backend.core.execution.orm import TaskExecution
    session, locked = await lock_session_then_run(tx, run.id)
    run = locked or run
    await tx.execute(update(TaskExecution).where(TaskExecution.task_id.in_(
        select(CoreTask.id).where(CoreTask.run_id == run.id, CoreTask.status == "submitted")))
        .values(cancel_requested=True))
    for task in await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id)):
        if task.status in {"pending", "validated", "queued", "waiting_user"}:
            await transition(tx, "task", task.id, task.version, "cancelled")
    inflight = await tx.scalar(select(CoreTask.id).where(CoreTask.run_id == run.id, CoreTask.status == "submitted").limit(1))
    if inflight:
        run.error = {**(run.error or {}), "pending_terminal": status}
        if run.status == "paused" and run.pause_reason != "waiting_info":
            await transition(tx, "run", run.id, run.version, run.resume_state)
        if run.status != "paused":
            await transition(tx, "run", run.id, run.version, "paused", reason="waiting_info")
    elif run.status not in TERMINAL:
        await transition(tx, "run", run.id, run.version, status)
    await message(tx, run, text_value, key, session=session)


def _clarification_result(run, language=None):
    """Program-written clarify form. Language comes from the session, not the frozen snapshot."""
    message = run.snapshot["request"]["message"]
    plan = {"goal": message, "writer": "rule", "result_type": "clarify", "steps": [
        {"id": "t1", "kind": "ask_user", "parameters": {"form": {"id": "clarify", "fields": [
            {"id": "detail", "type": "text", "label": clarification_field_label(language), "required": True}]}}}]}
    return PlannerResult(plan=plan, invariants=run.invariants, confidence=1,
                         raw_output={"writer": "rule", "clarification": True}, tokens=0)


def reference_clarification(context):
    language = str(context.get("language") or "zh").lower()
    label = ("请选中要修改的产物，再补充你想做的修改。" if language.startswith("zh")
             else "Select the artifact on the canvas, then describe your change.")
    return PlannerResult(plan={"goal": context["request"]["message"], "writer": "rule",
        "result_type": "clarify", "steps": [{"id": "t1", "kind": "ask_user",
            "parameters": {"form": {"id": "reference", "fields": [
                {"id": "target", "type": "text", "label": label, "required": True}]}}}]},
        invariants=context.get("invariants") or {}, confidence=1, tokens=0,
        raw_output={"writer": "rule", "reference_clarification": True})


async def _refunded(tx, run_id):
    value = await tx.scalar(select(func.coalesce(func.sum(HostCreditLedger.refunded), 0)).where(HostCreditLedger.core_run_id == run_id))
    return int(value or 0)


async def _cancel_requested(tx, run_id):
    return await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == run_id,
        or_((CoreEvent.event_type == "user_cancelled") & CoreEvent.task_id.is_(None),
            (CoreEvent.event_type == "user_interrupt") & CoreEvent.payload["message_id"].as_string().is_(None))).limit(1))


async def _already_clarified(tx, run_id):
    rows = await tx.scalars(select(Invocation.context).where(Invocation.run_id == run_id))
    return any(isinstance(item, dict) and item.get("clarification_submitted") for item in rows)


_FAILURE_STATUSES = frozenset({"blocked", "unknown", "unavailable"})


def _planner_failure_status(result):
    raw = result.raw_output or {}
    failure = raw.get("planner_failure")
    if isinstance(failure, dict) and failure.get("status") in _FAILURE_STATUSES:
        return failure["status"]
    layer = raw.get("cascade_layer")
    if layer == "rule_blocked":
        return "blocked"
    if layer == "rule_unknown":
        return "unknown"
    if layer == "rule_unavailable":
        return "unavailable"
    return None


def _usage_view(result):
    raw = result.raw_output or {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    if usage.get("unknown") is True:
        return True, 0
    tokens = usage.get("total_tokens")
    if isinstance(tokens, int) and tokens >= 0 and not isinstance(tokens, bool):
        return False, tokens
    if result.tokens:
        return False, int(result.tokens)
    return False, 0


def _selfcheck_meter(usage):
    usage = usage if isinstance(usage, dict) else {}
    if usage.get("unknown") is True:
        return True, 0
    tokens = usage.get("total_tokens")
    valid = isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0
    if not valid:
        return True, 0
    return False, tokens


def account_selfcheck_usage(run, plan_id, usage):
    """Meter checker tokens on the Run budget. Invalid totals are unknown, never invented as 0."""

    error = dict(run.error or {})
    bag = dict(error.get("planner_usage") or {})
    accounted = [str(item) for item in bag.get("accounted_selfcheck") or []]
    unknowns = [str(item) for item in bag.get("accounted_selfcheck_unknown") or []]
    ident = str(plan_id)
    unknown, known = _selfcheck_meter(usage)

    def _persist():
        bag["accounted_selfcheck"] = accounted
        bag["accounted_selfcheck_unknown"] = unknowns
        error["planner_usage"] = bag
        run.error = error

    if ident in accounted:
        if ident in unknowns and not unknown:
            unknowns.remove(ident)
            bag["unknown_calls"] = max(0, int(bag.get("unknown_calls") or 0) - 1)
            bag["known_tokens"] = int(bag.get("known_tokens") or 0) + known
            remaining = run.tokens_limit - run.tokens_used
            charged = min(known, max(0, remaining))
            run.tokens_used += charged
            _persist()
            return known > remaining
        _persist()
        return run.tokens_limit - run.tokens_used <= 0
    bag["known_tokens"] = int(bag.get("known_tokens") or 0) + (0 if unknown else known)
    bag["unknown_calls"] = int(bag.get("unknown_calls") or 0) + (1 if unknown else 0)
    accounted.append(ident)
    if unknown:
        unknowns.append(ident)
    remaining = run.tokens_limit - run.tokens_used
    over = False
    if not unknown:
        charged = min(known, remaining)
        run.tokens_used += charged
        over = known > remaining
    _persist()
    return over


def _audit_late_selfcheck(run, invocation, *, reason, raw=None, usage=None):
    error = dict(run.error or {})
    bag = dict(error.get("planner_usage") or {})
    items = list(bag.get("selfcheck_late_audit") or [])
    payload = raw if isinstance(raw, dict) else None
    items.append({
        "invocation_id": str(invocation.id),
        "reason": reason,
        "usage": usage if isinstance(usage, dict) else {},
        "raw": payload,
    })
    bag["selfcheck_late_audit"] = items[-8:]
    error["planner_usage"] = bag
    run.error = error


def account_planner_usage(run, invocation, result):
    """Count returned usage once. invocation.result and context stay the original dump."""

    error = dict(run.error or {})
    bag = dict(error.get("planner_usage") or {})
    accounted = [str(item) for item in bag.get("accounted_invocations") or []]
    ident = str(invocation.id)
    if ident in accounted:
        remaining = run.tokens_limit - run.tokens_used
        return remaining <= 0
    unknown, known = _usage_view(result)
    bag["known_tokens"] = int(bag.get("known_tokens") or 0) + (0 if unknown else known)
    bag["unknown_calls"] = int(bag.get("unknown_calls") or 0) + (1 if unknown else 0)
    accounted.append(ident)
    bag["accounted_invocations"] = accounted
    remaining = run.tokens_limit - run.tokens_used
    over = False
    if not unknown:
        charged = min(known, remaining)
        run.tokens_used += charged
        over = known > remaining
    error["planner_usage"] = bag
    run.error = error
    return over


class Supervisor:
    def __init__(self, factory, planner=None, *, settings=None, credentials=None, local_root=None, routes=None,
                 selfcheck_provider=None, selfcheck_evidence=None, selfcheck_evidence_gate=None):
        self.factory = factory
        self.planner = planner
        self.settings = settings or Settings()
        self.credentials = credentials or {}
        self.local_root = local_root
        self.routes = list(routes or [])
        self.on_raw_stored = None
        self.selfcheck_provider = selfcheck_provider
        self.selfcheck_evidence = selfcheck_evidence
        self.selfcheck_evidence_gate = selfcheck_evidence_gate

    def _legacy(self, run):
        if (run.version_lock or {}).get("supervisor") != "llm-v1":
            return True
        return isinstance(self.planner, FakePlanner) and not self.settings.selfcheck_enabled

    def planner_for(self, run):
        return planner_for_lock(run, factory=self.factory, settings=self.settings, credentials=self.credentials,
            local_root=self.local_root, explicit=self.planner)

    def _watchdog(self, run):
        if self._legacy(run):
            return max(self.settings.lease_seconds, self.settings.planner_timeout_seconds)
        return watchdog_seconds(run, self.settings)

    async def context(self, tx, run, invocation):
        from backend.core.asset_cards import frozen_card_views
        session = await tx.get(CoreSession, run.session_id)
        view = await effective_run_view(tx, run)
        messages = list(await tx.scalars(select(CoreSessionMessage).where(CoreSessionMessage.session_id == session.id)
            .order_by(CoreSessionMessage.sequence.desc()).limit(self.settings.context_messages)))
        tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id)))
        event = await tx.get(CoreEvent, invocation.event_id)
        plan = await latest_plan(tx, run.id)
        catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"])
        ordered = list(reversed(messages))
        reasons = list(invocation.context.get("review_reasons") or [])
        owned = list(await tx.scalars(select(CoreSessionMessage).where(
            CoreSessionMessage.session_id == session.id, CoreSessionMessage.run_id == run.id,
            CoreSessionMessage.role == "user").order_by(CoreSessionMessage.sequence)))
        owned_messages = [{"role": m.role, "id": str(m.id), "run_id": str(m.run_id) if m.run_id else None,
            "content": message_data(m)} for m in owned]
        _texts, fields = _sources(view, owned_messages)
        return {"tools": [t.model_dump(mode="json") for t in catalog.snapshot.tools], "invocation_key": str(invocation.id), "attempt": invocation.attempt,
            "run_id": str(run.id),
            "request": view.snapshot["request"], "snapshot": view.snapshot, "viewport": view.snapshot.get("viewport"),
            "summary": session.summary, "language": session.language,
            "recent_outputs": list(session.recent_assets or view.snapshot.get("recent_assets") or []),
            "asset_cards": await frozen_card_views(tx, run, limit=self.settings.card_context_limit),
            "confirmed_decisions": await confirmed_decisions(tx, run.session_id),
            "form_answers": fields,
            "messages": [{"role": m.role, "id": str(m.id), "run_id": str(m.run_id) if m.run_id else None, "content": message_data(m)} for m in ordered],
            "invariants": effective_invariants(run, plan),
            "plan": plan.proposal if plan else None, "tasks": [{"id": t.task_key, "status": t.status,
                "kind": t.kind, "plan_version": t.plan_version, "input_revision": t.input_revision} for t in tasks],
            "event": {"id": str(event.id), "type": event.event_type},
            "events": [{"type": "review", "reasons": reasons}] if reasons else [],
            "scope": view.scope, "user": {"id": run.user_id}, "review_reasons": reasons,
            "prompt_version": run.version_lock.get("prompt_version"), "prompt_hash": run.version_lock.get("prompt_hash"),
            "input_revision": run.input_revision,
            "correction": await correction_evidence(tx, run),
            "remaining_budget": {"credits": run.credits_limit - run.credits_spent,
                "tokens": run.tokens_limit - run.tokens_used,
                "thought_rounds": run.max_thought_rounds - run.thought_rounds_used}}

    async def tick(self):
        token = uuid.uuid4()
        calling = False
        pending_selfcheck = False
        ident = context = planner = watchdog = None
        async with self.factory.begin() as tx:
            await tx.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{self.settings.lock_timeout_ms}ms"})
            older = aliased(Invocation)
            row = (await tx.execute(select(Invocation, CoreRun, CoreSession)
                .join(CoreRun, CoreRun.id == Invocation.run_id)
                .join(CoreSession, CoreSession.id == CoreRun.session_id)
                .where(Invocation.status != "done", Invocation.next_at <= func.now(),
                    ~exists(select(older.id).where(older.run_id == Invocation.run_id, older.status != "done",
                        (older.created_at < Invocation.created_at) | ((older.created_at == Invocation.created_at) & (older.id < Invocation.id)))),
                    (Invocation.lease_until.is_(None) | (Invocation.lease_until <= func.now())))
                .order_by(Invocation.created_at, Invocation.id).limit(1)
                .with_for_update(of=CoreSession, skip_locked=True))).first()
            if not row:
                return False
            invocation, run, session = row
            locked = await tx.scalar(select(CoreRun).where(CoreRun.id == run.id).with_for_update(skip_locked=True))
            if locked is None:
                return False
            run = locked
            if run.status in TERMINAL:
                invocation.status = "done"
                return True
            if (run.error or {}).get("pending_terminal"):
                unfinished = await tx.scalar(select(CoreTask.id).where(CoreTask.run_id == run.id,
                    CoreTask.status.not_in(TASK_TERMINAL)).limit(1))
                if not unfinished:
                    await transition(tx, "run", run.id, run.version, run.error["pending_terminal"], causation_id=invocation.event_id)
                    await message(tx, run, "在途任务已完成结算，本次运行已结束。", "settled-stop:" + str(run.id))
                invocation.status = "done"
                return True
            event = await tx.get(CoreEvent, invocation.event_id)
            broken = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run.id,
                CoreTask.status.in_(("failed", "blocked_by_model"))).order_by(CoreTask.finished_at, CoreTask.id).limit(1))
            if broken is not None and event.event_type != "user_cancelled":
                # A queued interruption is not permission to bypass a model block
                # or automatically replan after an exhausted provider failure.
                await tx.execute(update(CoreSessionMessage).where(CoreSessionMessage.session_id == run.session_id,
                    CoreSessionMessage.processing_status == "pending").values(
                        processing_status="cancelled", run_id=run.id, processed_at=now()))
                session.dispatch_at = session.dispatch_lease_owner = session.dispatch_lease_until = None
                explanation = blocked(session.language, await _refunded(tx, run.id)) if broken.status == "blocked_by_model" else failed(
                    session.language, await _refunded(tx, run.id), code=(broken.error or {}).get("code"))
                await stop(tx, run, "failed", explanation, "failed:" + str(run.id))
                invocation.status = "done"
                return True
            if event.event_type == "timeout" and event.task_id:
                timed_task = await tx.get(CoreTask, event.task_id)
                if timed_task and (timed_task.error or {}).get("code") == "submission_unknown":
                    # A lost submit response is a reconciliation wakeup, not proof of failure.
                    invocation.status = "done"
                    return True
            if event.event_type == "timeout" or (event.event_type == "user_cancelled" and event.task_id is None):
                await stop(tx, run, "cancelled" if event.event_type != "timeout" else "failed",
                    "本次运行已停止；已提交任务将按服务商实际结果结算。", "stopped:" + str(run.id))
                invocation.status = "done"
                return True
            if event.event_type == "user_interrupt" and not is_replan(invocation):
                if not (event.payload or {}).get("message_id"):
                    await stop(tx, run, "cancelled", "本次运行已停止。", "stopped:" + str(run.id))
                    invocation.status = "done"
                    return True
                if int((invocation.context or {}).get("input_revision") or 1) != int(run.input_revision or 1):
                    invocation.status = "done"
                    return True
                ready = await apply_interrupt(tx, run, invocation)
                invocation.status = "done"
                if ready and run.status not in TERMINAL:
                    await emit(tx, "user_message", run.id, causation_id=invocation.event_id)
                return True
            if event.event_type == "budget_exceeded":
                if run.status != "paused":
                    await transition(tx, "run", run.id, run.version, "paused", reason="over_budget", causation_id=event.id)
                    await message(tx, run, "积分预算不足，已暂停。", "budget:" + str(run.id))
                invocation.status = "done"
                return True
            if await pending_user_messages(tx, run.session_id) is not None:
                # Quiet-window intake already belongs to this active Run. Never
                # complete it or submit an old plan before the dispatcher revises it.
                invocation.status = "done"
                return True
            tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id)))
            live = current_plan_tasks(run, tasks)
            if any(task.status == "cancelled" and task.input_revision == run.input_revision for task in live):
                await stop(tx, run, "cancelled", "本次运行已取消，已保留完成的产物。", "cancelled:" + str(run.id))
                invocation.status = "done"
                return True
            if (run.error or {}).get("interrupt_pending") and not any(t.status == "submitted" for t in tasks):
                run.current_plan_id = None
                bag = dict(run.error or {})
                bag.pop("interrupt_pending", None)
                run.error = bag
                invocation.status = "done"
                await emit(tx, "user_message", run.id, causation_id=invocation.event_id)
                return True
            if any(t.status in {"failed", "blocked_by_model"} for t in live) and not (run.error or {}).get("interrupt_pending"):
                session = await tx.get(CoreSession, run.session_id)
                if self._legacy(run):
                    failure = "有步骤执行失败，已保留成功产物并停止后续步骤。"
                else:
                    broken = [t for t in live if t.status in {"failed", "blocked_by_model"}]
                    broken.sort(key=lambda task: (getattr(task, "created_at", None) is None, getattr(task, "created_at", None), str(task.id)))
                    code = ((broken[0].error or {}).get("code") if broken else None)
                    failure = failed(session.language if session else None, await _refunded(tx, run.id), code=code)
                await stop(tx, run, "failed", failure, "failed:" + str(run.id))
                invocation.status = "done"
                return True
            if invocation.status == "pending" and invocation.attempt == 0:
                if run.thought_rounds_used >= run.max_thought_rounds or run.tokens_used >= run.tokens_limit:
                    await stop(tx, run, "failed", "已达到本次运行的思考或 token 上限，已停止。", "limit:" + str(run.id))
                    invocation.status = "done"
                    return True
                run.thought_rounds_used += 1
            if run.current_plan_id:
                await self.advance(tx, run, invocation, live)
                invocation.status = "done"
                return True
            if run.status == "paused" and run.pause_reason == "waiting_user":
                if any(t.status == "waiting_user" for t in tasks):
                    invocation.status = "done"
                    return True
                await transition(tx, "run", run.id, run.version, run.resume_state, causation_id=invocation.event_id)
                if run.status == "awaiting_confirm":
                    await transition(tx, "run", run.id, run.version, "running", causation_id=invocation.event_id)
            if run.status == "created":
                await transition(tx, "run", run.id, run.version, "planning", causation_id=invocation.event_id)
            if not invocation.result:
                if not invocation.context.get("invocation_key"):
                    built = await self.context(tx, run, invocation)
                    built["input_revision"] = run.input_revision
                    invocation.context = built
                watchdog = self._watchdog(run)
                invocation.started_at = invocation.started_at or now()
                invocation.status = "calling"
                invocation.lease_token, invocation.lease_until = token, now() + timedelta(seconds=watchdog)
                catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"],
                    expected_lock=VersionLock.model_validate(run.version_lock["tool_catalog"]))
                ident, context = invocation.id, {**invocation.context, "catalog": catalog}
                planner = self.planner_for(run)
                calling = True
            else:
                await self.accept(tx, run, invocation)
                latest = await _plan_for_invocation(tx, invocation)
                pending_selfcheck = latest is not None and latest.stage == "reviewing"
                ident = invocation.id
        if pending_selfcheck:
            asyncio.create_task(self.run_selfcheck(ident))
            return True
        if not calling:
            return True
        # No aggregate transaction or Run lock is held across plan().
        try:
            if (context.get("request") or {}).get("reference_clarification"):
                result = reference_clarification(context)
            else:
                result = await asyncio.wait_for(planner.plan(context), watchdog)
            result = PlannerResult.model_validate(result)
        except ContextLimitError:
            async with self.factory.begin() as tx:
                invocation, run = await locked_invocation(tx, ident)
                if invocation.lease_token == token:
                    invocation.lease_token = invocation.lease_until = None
                    invocation.error = "ContextLimitError"
                    await stop(tx, run, "failed", "选中内容超出上下文上限，已停止，未调用模型。", "context-limit:" + str(run.id))
                    invocation.status = "done"
            return True
        except Exception as exc:
            unknown = bool(getattr(exc, "unknown", False))
            async with self.factory.begin() as tx:
                invocation, run = await locked_invocation(tx, ident)
                if invocation.lease_token == token:
                    invocation.failures += 1
                    invocation.error = type(exc).__name__
                    invocation.lease_token = invocation.lease_until = None
                    if await _cancel_requested(tx, run.id):
                        invocation.status = "done"
                        _AUDIT.info("supervisor cancelled run=%s invocation=%s tokens_kept=true", run.id, invocation.id)
                        return True
                    invocation.next_at = now() + timedelta(seconds=self.settings.retry_seconds)
                    if unknown or invocation.failures >= self.settings.planner_failures:
                        text_value = "模型请求结果未知，未自动重试。" if unknown else "计划生成失败，已停止本次运行。"
                        await stop(tx, run, "failed", text_value, "planner-error:" + str(run.id))
                        invocation.status = "done"
            return True
        # Save the exact raw output independently; a kill after this point cannot call the planner again.
        async with self.factory.begin() as tx:
            invocation, run = await locked_invocation(tx, ident)
            if invocation.lease_token != token:
                return True
            invocation.returned_at = now()
            invocation.result = result.model_dump(mode="json")
            invocation.lease_token = invocation.lease_until = None
            if await _cancel_requested(tx, run.id) or invocation_obsolete(invocation, run):
                if not self._legacy(run):
                    account_planner_usage(run, invocation, result)
                invocation.status = "done"
                _AUDIT.info("supervisor usage run=%s invocation=%s tokens=%s obsolete=%s", run.id, invocation.id, result.tokens, invocation_obsolete(invocation, run))
                return True
            invocation.status = "returned"
        if self.on_raw_stored is not None:
            self.on_raw_stored(ident)
        return True

    async def accept(self, tx, run, invocation):
        existing = await _plan_for_invocation(tx, invocation)
        if existing is not None:
            if existing.stage != "reviewing":
                invocation.status = "done"
            return
        result = PlannerResult.model_validate(invocation.result)
        session = await tx.get(CoreSession, run.session_id)
        language = session.language if session else None
        if await _cancel_requested(tx, run.id) or invocation_obsolete(invocation, run):
            if not self._legacy(run):
                account_planner_usage(run, invocation, result)
            version = (await tx.scalar(select(func.max(CorePlan.version)).where(CorePlan.run_id == run.id)) or 0) + 1
            plan = CorePlan(run_id=run.id, version=version, writer=result.plan.writer, model_call_ref=str(invocation.id),
                raw_output=result.raw_output, proposal=result.plan.model_dump(mode="json"),
                content_hash=digest(result.plan.model_dump(mode="json")), rationale={"confidence": result.confidence},
                link_constraints={"obsolete": True, "source_revision": invocation.context.get("input_revision")},
                result_type="answer_only" if result.plan.result_type == "answer_only" else "generate",
                stage="obsolete", milestones=[], self_check={}, expansion_rules={}, version_lock=run.version_lock,
                input_revision=int(invocation.context.get("input_revision") or run.input_revision or 1))
            tx.add(plan)
            await tx.flush()
            invocation.status = "done"
            _AUDIT.info("supervisor usage run=%s invocation=%s tokens=%s obsolete=%s", run.id, invocation.id, result.tokens, True)
            return
        over = False
        if self._legacy(run):
            if result.tokens > run.tokens_limit - run.tokens_used:
                await stop(tx, run, "failed", "计划输出超过 token 预算，已停止。", "tokens:" + str(run.id))
                invocation.status = "done"
                return
            run.tokens_used += result.tokens
        else:
            over = account_planner_usage(run, invocation, result)
        failure = None if self._legacy(run) else _planner_failure_status(result)
        view = await effective_run_view(tx, run)
        catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"],
            expected_lock=VersionLock.model_validate(run.version_lock["tool_catalog"]))
        if failure:
            account = await tx.get(HostCreditAccount, run.workspace_id)
            approved = await latest_plan(tx, run.id)
            merged = effective_invariants(run, approved)
            verdict = review(result.plan, view, catalog, account.available if account else 0, invariants=merged)
            invocation.reviewed_at = now()
            invocation.review = verdict
            version = (await tx.scalar(select(func.max(CorePlan.version)).where(CorePlan.run_id == run.id)) or 0) + 1
            plan = CorePlan(run_id=run.id, version=version, writer=result.plan.writer, model_call_ref=str(invocation.id),
                raw_output=result.raw_output, proposal=result.plan.model_dump(mode="json"),
                content_hash=digest(result.plan.model_dump(mode="json")), rationale={"confidence": result.confidence},
                link_constraints={"constraints": verdict["execution_constraints"], "approved_invariants": merged},
                result_type="answer_only", stage="execute", milestones=[], self_check=verdict,
                expansion_rules={}, version_lock=run.version_lock, input_revision=int(run.input_revision or 1))
            tx.add(plan)
            await tx.flush()
            run.current_plan_id = plan.id
            text_value = result.plan.answer or (blocked(language, await _refunded(tx, run.id)) if failure == "blocked"
                else "计划生成失败，已停止本次运行。")
            await stop(tx, run, "failed", text_value, "planner-failure:" + str(run.id))
            invocation.status = "done"
            return
        if over:
            await stop(tx, run, "failed", "计划输出超过 token 预算，已停止。", "tokens:" + str(run.id))
            invocation.status = "done"
            return
        if (run.mode == "plan" and result.plan.result_type == "generate" and result.plan.steps
                and not any(s.tool == "detect_subject" or s.kind == "ask_user" for s in result.plan.steps)):
            # A program-written confirmation ticket goes through the same reviewer as every writer.
            ident = "t" + str(max(int(s.id[1:]) for s in result.plan.steps) + 1)
            question, options = plan_confirmation(language)
            confirmation = Step(id=ident, kind="ask_user", parameters={"question": question,
                "options": options, "plan_confirmation": True})
            result = result.model_copy(update={"plan": result.plan.model_copy(update={"steps": [confirmation, *[
                s.model_copy(update={"depends_on": [ident]}) if not s.depends_on else s for s in result.plan.steps]]})})
        messages = list(await tx.scalars(select(CoreSessionMessage).where(CoreSessionMessage.session_id == run.session_id)
            .order_by(CoreSessionMessage.sequence)))
        if not self._legacy(run):
            mapped = [{"role": m.role, "id": str(m.id), "run_id": str(m.run_id) if m.run_id else None,
                "content": m.content} for m in messages]
            _texts, fields = _sources(view, mapped)
            duration_ctx = {"request": view.snapshot.get("request") or {}, "snapshot": view.snapshot,
                "run_id": str(run.id), "form_answers": fields, "language": language,
                "invariants": effective_invariants(run, await latest_plan(tx, run.id)),
                "messages": mapped}
            clarified = clarify_unproven_video_duration(result.plan, catalog, catalog.lock, duration_ctx)
            if clarified is not None:
                result = result.model_copy(update={"plan": clarified})
        approved = await latest_plan(tx, run.id)
        base = effective_invariants(run, approved)
        revision_texts = [(m.content or {}).get("text", "") for m in messages
            if m.role == "user" and m.input_revision == run.input_revision] if run.input_revision > 1 else None
        merged, conflicts = merge_proposed(view, result.invariants, messages, frozen=approved is not None,
            base=base, revision_texts=revision_texts)
        account = await tx.get(HostCreditAccount, run.workspace_id)
        captured = []
        def _capture(plan_view):
            captured.append(plan_view)
            return []
        forced = None if self._legacy(run) else conversion_rejection(result.raw_output)
        if forced is not None:
            verdict = forced
        elif conflicts:
            verdict = {"execution_constraints": [], "verdict": "reject", "reasons": conflicts, "quoted_credits": 0,
                "checks": {"legal": True, "types": True, "intent": False}}
        else:
            extras = [_capture] if self.settings.selfcheck_enabled and result.plan.result_type == "generate" else None
            verdict = review(result.plan, view, catalog, account.available if account else 0, invariants=merged,
                intent_extensions=extras)
        invocation.reviewed_at = now()
        invocation.review = verdict
        if verdict["verdict"] != "pass":
            invocation.status = "done"
            await emit(tx, "plan_rejected", run.id, causation_id=invocation.event_id)
            if invocation.attempt < self.settings.review_retries:
                tx.add(Invocation(run_id=run.id, event_id=invocation.event_id, attempt=invocation.attempt + 1,
                    context={"review_reasons": verdict["reasons"]}))
            elif (not self._legacy(run) and not invocation.context.get("clarification_submitted")
                    and not await _already_clarified(tx, run.id)):
                clarified = _clarification_result(view, language)
                tx.add(Invocation(run_id=run.id, event_id=invocation.event_id, attempt=invocation.attempt + 1,
                    status="returned", result=clarified.model_dump(mode="json"),
                    context={"clarification_submitted": True, "review_reasons": verdict["reasons"]}))
                return
            else:
                await stop(tx, run, "failed", review_exhausted(language),
                    "rejected:" + str(run.id))
            return
        stored_type = "answer_only" if result.plan.result_type == "answer_only" else "generate"
        proposal = result.plan.model_dump(mode="json")
        content_hash = digest(proposal)
        check_payload = verdict
        if captured and stored_type == "generate":
            route = frozen_selfcheck_route(run)
            kind = getattr(self.selfcheck_provider, "kind", None)
            if kind is None:
                kind = "none" if not route else "chat"
            request = (view.snapshot or {}).get("request") or {}
            journal = prepare_journal(
                captured[0], settings=self.settings, run_id=str(run.id),
                parent_run_id=str(run.parent_run_id) if run.parent_run_id else None,
                invocation_id=str(invocation.id), input_revision=int(run.input_revision or 1),
                candidate_hash=content_hash, catalog_hash=catalog.lock.content_hash,
                estimated_cost=int(verdict.get("quoted_credits") or 0), route=route,
                evidence=self.selfcheck_evidence, raw_plan=proposal, catalog=catalog,
                user_message=str(request.get("message") or result.plan.goal or ""),
                extra_refs=list(request.get("selected_assets") or []),
                provider_kind=kind, evidence_gate=self.selfcheck_evidence_gate)
            check_payload = merge_self_check(verdict, journal)
            if journal.triggered and journal.status == "reserved":
                version = (await tx.scalar(select(func.max(CorePlan.version)).where(CorePlan.run_id == run.id)) or 0) + 1
                plan = CorePlan(run_id=run.id, version=version, writer=result.plan.writer, model_call_ref=str(invocation.id),
                    raw_output=result.raw_output, proposal=proposal, content_hash=content_hash,
                    rationale={"confidence": result.confidence},
                    link_constraints={"constraints": verdict["execution_constraints"], "approved_invariants": merged},
                    result_type=stored_type, stage="reviewing", milestones=[],
                    self_check=check_payload, expansion_rules={}, version_lock=run.version_lock,
                    input_revision=int(run.input_revision or 1))
                tx.add(plan)
                await tx.flush()
                invocation.review = check_payload
                return
        invocation.status = "done"
        version = (await tx.scalar(select(func.max(CorePlan.version)).where(CorePlan.run_id == run.id)) or 0) + 1
        plan = CorePlan(run_id=run.id, version=version, writer=result.plan.writer, model_call_ref=str(invocation.id),
            raw_output=result.raw_output, proposal=proposal,
            content_hash=content_hash, rationale={"confidence": result.confidence},
            link_constraints={"constraints": verdict["execution_constraints"], "approved_invariants": merged}, result_type=stored_type, stage="execute", milestones=[], self_check=check_payload,
            expansion_rules={}, version_lock=run.version_lock, input_revision=int(run.input_revision or 1))
        tx.add(plan)
        await tx.flush()
        run.current_plan_id = plan.id
        await self._create_tasks(tx, run, invocation, result, plan, verdict, merged, catalog, view, language)

    async def run_selfcheck(self, ident):
        """Call the checker outside Session/Run locks. Persist raw before parse/admit."""

        timeout = self.settings.selfcheck_timeout_seconds
        margin = self.settings.selfcheck_checkpoint_margin_seconds
        handle = None
        async with self.factory.begin() as tx:
            invocation, run = await locked_invocation(tx, ident)
            plan = await _plan_for_invocation(tx, invocation)
            if plan is None or plan.stage != "reviewing":
                invocation.status = "done"
                if plan is None:
                    invocation.error = "selfcheck_plan_missing"
                return
            journal = _working_journal(invocation, plan)
            if journal is None:
                invocation.status = "done"
                invocation.error = "selfcheck_journal_missing"
                return
            stale = bool(await _cancel_requested(tx, run.id) or invocation_obsolete(invocation, run))
            if journal.raw is not None:
                if stale:
                    await self._finish_stale(tx, run, invocation, plan, journal)
                    return
                await self._apply_selfcheck(tx, run, invocation, plan, journal)
                return
            if journal.status == "sending":
                live = (invocation.lease_token is not None and invocation.lease_until is not None
                        and invocation.lease_until > now())
                if live:
                    return
                journal.status = "unknown"
                journal.fallback_reason = "sending_without_raw"
                if not journal.usage:
                    journal.usage = {"unknown": True}
                _store_working(invocation, plan, journal)
                if stale:
                    await self._finish_stale(tx, run, invocation, plan, journal)
                else:
                    await self._apply_selfcheck(tx, run, invocation, plan, journal)
                return
            if stale:
                await self._finish_stale(tx, run, invocation, plan, journal)
                return
            provider = self.selfcheck_provider
            if provider is None:
                provider = selfcheck_provider_for(run, credentials=self.credentials,
                    local_root=self.local_root, routes=self.routes)
            if provider is None:
                journal.status = "unavailable"
                journal.fallback_reason = "no_route"
                _store_working(invocation, plan, journal)
                await self._apply_selfcheck(tx, run, invocation, plan, journal)
                return
            token = uuid.uuid4()
            journal.status = "sending"
            journal.lease_token = str(token)
            _store_working(invocation, plan, journal)
            invocation.lease_token, invocation.lease_until = token, now() + timedelta(seconds=timeout + margin)
            handle = {
                "invocation_id": invocation.id, "plan_id": plan.id, "call_key": journal.call_key,
                "token": token, "messages": list(journal.request_messages or []), "provider": provider,
            }
        started = time.perf_counter()

        async def sink(raw):
            async with self.factory.begin() as tx:
                invocation, run = await locked_invocation(tx, handle["invocation_id"])
                usage = raw.get("usage") if isinstance(raw, dict) and isinstance(raw.get("usage"), dict) else {}
                payload = raw if isinstance(raw, dict) else {"invalid": True}
                latest = await _plan_for_invocation(tx, invocation)
                live_owner = (invocation.lease_token == handle["token"]
                              and invocation.lease_until is not None and invocation.lease_until > now())
                terminal = latest is not None and latest.stage != "reviewing"
                if not live_owner or terminal:
                    _audit_late_selfcheck(run, invocation,
                        reason="already_terminal" if terminal else "late_sink",
                        raw=payload, usage=usage)
                    account_selfcheck_usage(run, handle["plan_id"], usage)
                    if latest is None:
                        invocation.status = "done"
                        invocation.error = invocation.error or "selfcheck_plan_missing"
                    return
                plan = await tx.get(CorePlan, handle["plan_id"])
                if plan is None:
                    invocation.status = "done"
                    invocation.error = "selfcheck_plan_missing"
                    return
                journal = _working_journal(invocation, plan)
                if journal is None or journal.call_key != handle["call_key"]:
                    invocation.status = "done"
                    invocation.error = "selfcheck_key_mismatch"
                    return
                if journal.raw is None:
                    journal.raw = payload
                    journal.usage = usage
                    journal.status = "stored"
                    _store_working(invocation, plan, journal)

        provider = handle["provider"]
        error_kind = None
        returned = None
        blocked_usage = {}
        try:
            returned = await asyncio.wait_for(
                provider.complete(handle["messages"], response_sink=sink), timeout)
        except TimeoutError:
            error_kind = "timeout"
        except ModelCallError as exc:
            if exc.code in _BLOCKED:
                error_kind = "content_blocked"
                blocked_usage = exc.usage if isinstance(exc.usage, dict) else {}
            else:
                error_kind = "unknown" if getattr(exc, "unknown", False) else "unavailable"
                if isinstance(exc.usage, dict) and exc.usage:
                    blocked_usage = exc.usage
        except Exception as exc:
            error_kind = "unknown" if getattr(exc, "unknown", False) else "unavailable"
        elapsed = (time.perf_counter() - started) * 1000
        async with self.factory.begin() as tx:
            invocation, run = await locked_invocation(tx, handle["invocation_id"])
            latest = await _plan_for_invocation(tx, invocation)
            live_owner = (invocation.lease_token == handle["token"]
                          and invocation.lease_until is not None and invocation.lease_until > now())
            terminal = latest is not None and latest.stage != "reviewing"
            returned_raw = returned.raw if returned is not None and isinstance(getattr(returned, "raw", None), dict) else None
            returned_usage = returned.usage if returned is not None and isinstance(getattr(returned, "usage", None), dict) else blocked_usage
            if not live_owner or terminal:
                _audit_late_selfcheck(run, invocation,
                    reason="already_terminal" if terminal else "late_callback",
                    raw=returned_raw, usage=returned_usage)
                account_selfcheck_usage(run, handle["plan_id"], returned_usage or {})
                if latest is None:
                    invocation.status = "done"
                    invocation.error = invocation.error or "selfcheck_plan_missing"
                elif terminal:
                    invocation.status = "done"
                return
            plan = await tx.get(CorePlan, handle["plan_id"])
            if plan is None:
                invocation.status = "done"
                invocation.error = "selfcheck_plan_missing"
                return
            journal = _working_journal(invocation, plan)
            if journal is None or journal.call_key != handle["call_key"]:
                invocation.status = "done"
                invocation.error = "selfcheck_key_mismatch"
                return
            journal.timings_ms = {**(journal.timings_ms or {}), "model": elapsed}
            if journal.raw is None and returned is not None and isinstance(returned.raw, dict):
                journal.raw = returned.raw
                journal.usage = returned.usage if isinstance(returned.usage, dict) else journal.usage
                journal.status = "stored"
            if journal.raw is None and blocked_usage:
                journal.usage = {**dict(journal.usage or {}), **blocked_usage}
            stale = bool(await _cancel_requested(tx, run.id) or invocation_obsolete(invocation, run))
            if error_kind == "content_blocked":
                journal.status = "blocked"
                journal.fallback_reason = "content_blocked"
                bag = merge_self_check(plan.self_check, journal)
                account_selfcheck_usage(run, plan.id, journal.usage)
                await _emit_plan_version(tx, run, plan, stage="rejected", self_check=bag)
                invocation.review = bag
                invocation.status = "done"
                session = await tx.get(CoreSession, run.session_id)
                await stop(tx, run, "failed", blocked(session.language if session else None, await _refunded(tx, run.id)),
                    "selfcheck-blocked:" + str(run.id))
                return
            if stale:
                await self._finish_stale(tx, run, invocation, plan, journal)
                return
            if journal.raw is None:
                journal.status = "unknown" if error_kind in {None, "unknown"} else "unavailable"
                journal.fallback_reason = error_kind or "sending_without_raw"
                if not journal.usage:
                    journal.usage = {"unknown": True}
                _store_working(invocation, plan, journal)
            await self._apply_selfcheck(tx, run, invocation, plan, journal)

    async def _finish_stale(self, tx, run, invocation, plan, journal):
        account_selfcheck_usage(run, plan.id, journal.usage)
        journal.fallback_reason = journal.fallback_reason or "stale_after_interrupt"
        bag = merge_self_check(plan.self_check, journal)
        await _emit_plan_version(tx, run, plan, stage="obsolete", self_check=bag)
        invocation.review = bag
        invocation.status = "done"

    async def _apply_selfcheck(self, tx, run, invocation, plan, journal):
        over = account_selfcheck_usage(run, plan.id, journal.usage)
        if journal.raw is not None and journal.status in {"stored", "parsed", "sending", "reserved"}:
            content = content_from_raw(journal.raw)
            journal, extra, reject = apply_parsed(journal, content, journal.usage, journal.raw)
        elif journal.status in {"unknown", "unavailable", "rule_only"}:
            extra, reject = [], False
            if journal.status != "unknown":
                journal.status = "rule_only"
            journal.fallback_reason = journal.fallback_reason or journal.status
        else:
            extra, reject = [], False
        bag = merge_self_check(plan.self_check, journal)
        if extra:
            bag["verdict"] = "reject"
            bag["reasons"] = list(bag.get("reasons") or []) + extra
            bag["checks"] = {**(bag.get("checks") or {}), "intent": False}
        invocation.review = bag
        if over:
            await _emit_plan_version(tx, run, plan, stage="rejected", self_check=bag)
            invocation.status = "done"
            await stop(tx, run, "failed", "计划输出超过 token 预算，已停止。", "tokens:" + str(run.id))
            return
        if reject:
            await _emit_plan_version(tx, run, plan, stage="rejected", self_check=bag)
            invocation.status = "done"
            await emit(tx, "plan_rejected", run.id, causation_id=invocation.event_id)
            language = (await tx.get(CoreSession, run.session_id)).language if run.session_id else None
            if invocation.attempt < self.settings.review_retries:
                tx.add(Invocation(run_id=run.id, event_id=invocation.event_id, attempt=invocation.attempt + 1,
                    context={"review_reasons": bag["reasons"]}))
            elif not invocation.context.get("clarification_submitted") and not await _already_clarified(tx, run.id):
                view = await effective_run_view(tx, run)
                clarified = _clarification_result(view, language)
                tx.add(Invocation(run_id=run.id, event_id=invocation.event_id, attempt=invocation.attempt + 1,
                    status="returned", result=clarified.model_dump(mode="json"),
                    context={"clarification_submitted": True, "review_reasons": bag["reasons"]}))
            else:
                await stop(tx, run, "failed", review_exhausted(language), "rejected:" + str(run.id))
            return
        terminal = await _emit_plan_version(tx, run, plan, stage="execute", self_check=bag)
        invocation.status = "done"
        result = PlannerResult.model_validate(invocation.result)
        merged = ((plan.link_constraints or {}).get("approved_invariants")
                  or effective_invariants(run, plan))
        catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"],
            expected_lock=VersionLock.model_validate(run.version_lock["tool_catalog"]))
        view = await effective_run_view(tx, run)
        session = await tx.get(CoreSession, run.session_id)
        language = session.language if session else None
        run.current_plan_id = terminal.id
        await self._create_tasks(tx, run, invocation, result, terminal, bag, merged, catalog, view, language)

    async def _create_tasks(self, tx, run, invocation, result, plan, verdict, merged, catalog, view, language):
        if result.plan.result_type == "answer_only":
            await transition(tx, "run", run.id, run.version, "running", causation_id=invocation.event_id)
            await transition(tx, "run", run.id, run.version, "completed", causation_id=invocation.event_id)
            await message(tx, run, result.plan.answer, "answer:" + str(plan.id))
            return
        tasks = {}
        for step in result.plan.steps:
            tool = catalog.get_tool(step.tool, catalog.lock.version) if step.tool else None
            task = await create_task(tx, TaskInput(run_id=run.id, plan_id=plan.id, plan_version=plan.version,
                task_key=step.id, kind=step.kind, tool_ref=step.tool, tool_version=tool.version if tool else None,
                parameters=step.parameters, idempotency_key=str(plan.id) + ":" + step.id,
                scope=view.scope, invariants=merged, input_revision=int(run.input_revision or 1)))
            tasks[step.id] = task
            await transition(tx, "task", task.id, task.version, "validated", causation_id=invocation.event_id)
            if tool:
                await record_admission(tx, task, catalog, checks=verdict["checks"],
                    review_ref=str(invocation.id), placement={**step.placement, **({"anchor_version": view.snapshot.get("objects", {}).get(step.placement.get("anchor_id"), {}).get("assetVersion", 0)} if step.placement.get("anchor_id") else {})})
        for step in result.plan.steps:
            for dependency in step.depends_on:
                tx.add(CoreTaskDependency(task_id=tasks[step.id].id, depends_on_id=tasks[dependency].id,
                    run_id=run.id, plan_id=plan.id))
        await tx.flush()
        target = "awaiting_confirm" if any(s.parameters.get("plan_confirmation") for s in result.plan.steps) else "running"
        if run.status != target:
            await transition(tx, "run", run.id, run.version, target, causation_id=invocation.event_id)
        await emit(tx, "plan_written", run.id, causation_id=invocation.event_id, ids={"plan_id": plan.id})
        await self.advance(tx, run, invocation, list(tasks.values()))
        confirm = any(s.parameters.get("plan_confirmation") for s in result.plan.steps)
        if self._legacy(run):
            spoken = "计划已审核通过，开始执行。"
        else:
            spoken = opening(language, int(verdict.get("quoted_credits") or 0), confirm=confirm, goal=result.plan.goal)
        _AUDIT.info("supervisor plan run=%s invocation=%s plan=%s quoted=%s", run.id, invocation.id, plan.id, verdict.get("quoted_credits"))
        await message(tx, run, spoken, "plan:" + str(plan.id))

    async def advance(self, tx, run, invocation, tasks):
        from .subjects import handle_subjects
        if run.status == "paused" and run.pause_reason == "waiting_user" and not any(t.status == "waiting_user" for t in tasks):
            if any((t.waiting or {}).get("plan_confirmation") and (t.waiting or {}).get("answer") == "cancel" for t in tasks):
                await stop(tx, run, "cancelled", "已取消计划，未提交生成。", "cancel-plan:" + str(run.id))
                return
            await transition(tx, "run", run.id, run.version, run.resume_state, causation_id=invocation.event_id)
            if run.status == "awaiting_confirm":
                await transition(tx, "run", run.id, run.version, "running", causation_id=invocation.event_id)
        if await handle_subjects(tx, run, tasks, invocation.event_id, self.settings):
            return
        if tasks and all(t.status in TASK_TERMINAL for t in tasks):
            if (run.error or {}).get("interrupt_pending"):
                run.current_plan_id = None
                bag = dict(run.error or {})
                bag.pop("interrupt_pending", None)
                run.error = bag
                await emit(tx, "user_message", run.id, causation_id=invocation.event_id)
                return
            await transition(tx, "run", run.id, run.version, "completed", causation_id=invocation.event_id)
            if all(t.kind == "ask_user" for t in tasks):
                done_text = "已收到补充信息。请携带明确要求发起下一次任务。"
            elif self._legacy(run):
                done_text = "本次任务已完成。"
            else:
                session = await tx.get(CoreSession, run.session_id)
                writes = (await tx.scalars(select(CanvasWrite).where(CanvasWrite.task_id.in_([t.id for t in tasks])))).all()
                places = [place_label(row.rect) for row in writes if isinstance(row.rect, dict) and row.rect]
                language = session.language if session else None
                done_text = completed(language, places)
                summaries = [read_summary(language, task.tool_ref, task.output or {})
                    for task in tasks if task.status == "succeeded" and task.tool_ref and isinstance(task.output, dict)]
                summaries = [item for item in summaries if item]
                if summaries:
                    done_text = done_text + "\n" + "\n".join(summaries)
            await message(tx, run, done_text, "complete:" + str(run.id))
            return
        by_id = {t.id: t for t in tasks}
        dependencies = list(await tx.scalars(select(CoreTaskDependency).where(
            CoreTaskDependency.run_id == run.id, CoreTaskDependency.plan_id == run.current_plan_id)))
        if run.status == "paused":
            return
        for task in tasks:
            if task.status != "validated" or any(by_id[d.depends_on_id].status != "succeeded" for d in dependencies if d.task_id == task.id):
                continue
            if task.kind == "ask_user":
                task.waiting = task.parameters
                await transition(tx, "task", task.id, task.version, "waiting_user", causation_id=invocation.event_id)
                await transition(tx, "run", run.id, run.version, "paused", reason="waiting_user", causation_id=invocation.event_id)
                session = await tx.get(CoreSession, run.session_id)
                await message(tx, run, ask_text(task.parameters or {}, session.language if session else None), "ask:" + str(task.id))
                return
            if task.tool_ref == "cutout":
                continue  # Subject selection code is the sole release gate for this step.
            await transition(tx, "task", task.id, task.version, "queued", causation_id=invocation.event_id)
