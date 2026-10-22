"""Database checkpoints around planner calls; event handlers never wait for planners."""
import asyncio
import uuid
from datetime import timedelta
from sqlalchemy import select, text, exists, update
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import insert
from backend.agent.orm import HostCreditAccount
from backend.core.contracts import TaskInput
from backend.core.orm import CoreRun, CorePlan, CoreTask, CoreTaskDependency, CoreSession, CoreSessionMessage, CoreEvent
from backend.core.store import transition, create_task
from backend.core.tools.store import load_catalog
from backend.core.tools.contracts import VersionLock
from backend.core.events.store import emit
from backend.core.execution.engine import record_admission, now
from .orm import Invocation
from .contracts import Settings, PlannerResult, Step
from .planners import RulePlanner
from .reviewer import review
from .service import TERMINAL, TASK_TERMINAL, message, digest


async def wake(tx, context):
    if context.run.version_lock.get("supervisor") != "rule-v1" or context.run.status in TERMINAL or context.event.event_type in {"plan_rejected", "plan_written", "asset_changed", "conflict"}:
        return
    await tx.execute(insert(Invocation).values(id=uuid.uuid4(), run_id=context.run.id,
        event_id=context.event.id, attempt=0, status="pending", context={}, failures=0)
        .on_conflict_do_nothing(index_elements=[Invocation.event_id, Invocation.attempt]))


def register(registry):
    from backend.core.events.contracts import EventType
    for event_type in EventType:
        registry.register(event_type, "supervisor-v1", wake)


async def stop(tx, run, status, text_value, key):
    from backend.core.execution.orm import TaskExecution
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
    await message(tx, run, text_value, key)


class Supervisor:
    def __init__(self, factory, planner=None, *, settings=None):
        self.factory = factory
        self.planner = planner or RulePlanner()
        self.settings = settings or Settings()

    async def context(self, tx, run, invocation):
        session = await tx.get(CoreSession, run.session_id)
        messages = list(await tx.scalars(select(CoreSessionMessage).where(CoreSessionMessage.session_id == session.id)
            .order_by(CoreSessionMessage.sequence.desc()).limit(self.settings.context_messages)))
        tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id)))
        event = await tx.get(CoreEvent, invocation.event_id)
        plan = await tx.get(CorePlan, run.current_plan_id) if run.current_plan_id else None
        catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"])
        return {"tools": [t.model_dump(mode="json") for t in catalog.snapshot.tools], "invocation_key": str(invocation.id), "attempt": invocation.attempt,
            "request": run.snapshot["request"], "snapshot": run.snapshot, "summary": session.summary,
            "messages": [m.content for m in reversed(messages)], "invariants": run.invariants,
            "plan": plan.proposal if plan else None, "tasks": [{"id": t.task_key, "status": t.status} for t in tasks],
            "event": {"id": str(event.id), "type": event.event_type},
            "review_reasons": invocation.context.get("review_reasons", [])}

    async def tick(self):
        token = uuid.uuid4()
        async with self.factory.begin() as tx:
            await tx.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{self.settings.lock_timeout_ms}ms"})
            older = aliased(Invocation)
            row = (await tx.execute(select(Invocation, CoreRun).join(CoreRun, CoreRun.id == Invocation.run_id)
                .where(Invocation.status != "done", Invocation.next_at <= now(),
                    ~exists(select(older.id).where(older.run_id == Invocation.run_id, older.status != "done",
                        (older.created_at < Invocation.created_at) | ((older.created_at == Invocation.created_at) & (older.id < Invocation.id)))),
                    (Invocation.lease_until.is_(None) | (Invocation.lease_until <= now())))
                .order_by(Invocation.created_at, Invocation.id).limit(1).with_for_update(of=CoreRun, skip_locked=True))).first()
            if not row:
                return False
            invocation, run = row
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
            if event.event_type == "timeout" and event.task_id:
                timed_task = await tx.get(CoreTask, event.task_id)
                if timed_task and (timed_task.error or {}).get("code") == "submission_unknown":
                    # A lost submit response is a reconciliation wakeup, not proof of failure.
                    invocation.status = "done"
                    return True
            if event.event_type in {"timeout", "user_cancelled", "user_interrupt"}:
                await stop(tx, run, "cancelled" if event.event_type != "timeout" else "failed",
                    "本次运行已停止；已提交任务将按服务商实际结果结算。", "stopped:" + str(run.id))
                invocation.status = "done"
                return True
            if event.event_type == "budget_exceeded":
                if run.status != "paused":
                    await transition(tx, "run", run.id, run.version, "paused", reason="over_budget", causation_id=event.id)
                    await message(tx, run, "积分预算不足，已暂停。", "budget:" + str(run.id))
                invocation.status = "done"
                return True
            tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id)))
            if any(t.status in {"failed", "blocked_by_model"} for t in tasks):
                await stop(tx, run, "failed", "有步骤执行失败，已保留成功产物并停止后续步骤。", "failed:" + str(run.id))
                invocation.status = "done"
                return True
            if invocation.status == "pending" and invocation.attempt == 0:
                if run.thought_rounds_used >= run.max_thought_rounds or run.tokens_used >= run.tokens_limit:
                    await stop(tx, run, "failed", "已达到本次运行的思考或 token 上限，已停止。", "limit:" + str(run.id))
                    invocation.status = "done"
                    return True
                run.thought_rounds_used += 1
            if run.current_plan_id:
                await self.advance(tx, run, invocation, tasks)
                invocation.status = "done"
                return True
            if run.status == "created":
                await transition(tx, "run", run.id, run.version, "planning", causation_id=invocation.event_id)
            if not invocation.result:
                if not invocation.context.get("invocation_key"):
                    invocation.context = await self.context(tx, run, invocation)
                invocation.started_at = invocation.started_at or now()
                invocation.status = "calling"
                invocation.lease_token, invocation.lease_until = token, now() + timedelta(seconds=max(self.settings.lease_seconds, self.settings.planner_timeout_seconds))
                ident, context = invocation.id, invocation.context
            else:
                await self.accept(tx, run, invocation)
                return True
        # No aggregate transaction or Run lock is held across plan().
        try:
            result = await asyncio.wait_for(self.planner.plan(context), self.settings.planner_timeout_seconds)
            result = PlannerResult.model_validate(result)
        except Exception as exc:
            async with self.factory.begin() as tx:
                invocation = await tx.get(Invocation, ident, with_for_update=True)
                if invocation.lease_token == token:
                    invocation.failures += 1
                    invocation.error = type(exc).__name__
                    invocation.lease_token = invocation.lease_until = None
                    invocation.next_at = now() + timedelta(seconds=self.settings.retry_seconds)
                    if invocation.failures >= self.settings.planner_failures:
                        run = await tx.get(CoreRun, invocation.run_id, with_for_update=True)
                        await stop(tx, run, "failed", "计划生成失败，已停止本次运行。", "planner-error:" + str(run.id))
                        invocation.status = "done"
            return True
        # Save the exact raw output independently; a kill after this point cannot call the planner again.
        async with self.factory.begin() as tx:
            invocation = await tx.get(Invocation, ident, with_for_update=True)
            if invocation.lease_token != token:
                return True
            invocation.returned_at = now()
            invocation.result = result.model_dump(mode="json")
            invocation.status = "returned"
            invocation.lease_token = invocation.lease_until = None
        return True

    async def accept(self, tx, run, invocation):
        result = PlannerResult.model_validate(invocation.result)
        if result.tokens > run.tokens_limit - run.tokens_used:
            await stop(tx, run, "failed", "计划输出超过 token 预算，已停止。", "tokens:" + str(run.id))
            invocation.status = "done"
            return
        run.tokens_used += result.tokens
        catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"],
            expected_lock=VersionLock.model_validate(run.version_lock["tool_catalog"]))
        if run.mode == "plan" and not any(s.tool == "detect_subject" or s.kind == "ask_user" for s in result.plan.steps):
            # A program-written confirmation ticket goes through the same reviewer as every writer.
            ident = "t" + str(max(int(s.id[1:]) for s in result.plan.steps) + 1)
            confirmation = Step(id=ident, kind="ask_user", parameters={"question": "是否执行此计划？",
                "options": [{"id": "confirm", "label": "确认执行"}, {"id": "cancel", "label": "取消"}], "plan_confirmation": True})
            result = result.model_copy(update={"plan": result.plan.model_copy(update={"steps": [confirmation, *[
                s.model_copy(update={"depends_on": [ident]}) if not s.depends_on else s for s in result.plan.steps]]})})
        account = await tx.get(HostCreditAccount, run.workspace_id)
        verdict = review(result.plan, run, catalog, account.available if account else 0)
        if result.invariants != run.invariants:
            verdict["verdict"] = "reject"
            verdict["reasons"].append({"gate": "intent", "code": "invariants_changed"})
        invocation.reviewed_at = now()
        invocation.review = verdict
        invocation.status = "done"
        if verdict["verdict"] != "pass":
            await emit(tx, "plan_rejected", run.id, causation_id=invocation.event_id)
            if invocation.attempt < self.settings.review_retries:
                tx.add(Invocation(run_id=run.id, event_id=invocation.event_id, attempt=invocation.attempt + 1,
                    context={"review_reasons": verdict["reasons"]}))
            else:
                await stop(tx, run, "failed", "计划连续三道审核未通过，已停止；没有执行未获准的步骤。",
                    "rejected:" + str(run.id))
            return
        plan = CorePlan(run_id=run.id, version=1, writer="rule", model_call_ref=str(invocation.id),
            raw_output=result.raw_output, proposal=result.plan.model_dump(mode="json"),
            content_hash=digest(result.plan.model_dump(mode="json")), rationale={"confidence": result.confidence},
            link_constraints={"constraints": verdict["execution_constraints"]}, result_type="generate", stage="execute", milestones=[], self_check=verdict,
            expansion_rules={}, version_lock=run.version_lock)
        tx.add(plan)
        await tx.flush()
        run.current_plan_id = plan.id
        tasks = {}
        for step in result.plan.steps:
            tool = catalog.get_tool(step.tool, catalog.lock.version) if step.tool else None
            task = await create_task(tx, TaskInput(run_id=run.id, plan_id=plan.id, plan_version=plan.version,
                task_key=step.id, kind=step.kind, tool_ref=step.tool, tool_version=tool.version if tool else None,
                parameters=step.parameters, idempotency_key=str(plan.id) + ":" + step.id,
                scope=run.scope, invariants=run.invariants))
            tasks[step.id] = task
            await transition(tx, "task", task.id, task.version, "validated", causation_id=invocation.event_id)
            if tool:
                await record_admission(tx, task, catalog, checks=verdict["checks"],
                    review_ref=str(invocation.id), placement={**step.placement, **({"anchor_version": run.snapshot.get("objects", {}).get(step.placement.get("anchor_id"), {}).get("assetVersion", 0)} if step.placement.get("anchor_id") else {})})
        for step in result.plan.steps:
            for dependency in step.depends_on:
                tx.add(CoreTaskDependency(task_id=tasks[step.id].id, depends_on_id=tasks[dependency].id,
                    run_id=run.id, plan_id=plan.id))
        await tx.flush()
        await transition(tx, "run", run.id, run.version, "awaiting_confirm" if any(s.parameters.get("plan_confirmation") for s in result.plan.steps) else "running", causation_id=invocation.event_id)
        await emit(tx, "plan_written", run.id, causation_id=invocation.event_id, ids={"plan_id": plan.id})
        await self.advance(tx, run, invocation, list(tasks.values()))
        await message(tx, run, "计划已审核通过，开始执行。", "plan:" + str(plan.id))

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
            await transition(tx, "run", run.id, run.version, "completed", causation_id=invocation.event_id)
            await message(tx, run, "已收到补充信息。请携带明确要求发起下一次任务。" if all(t.kind == "ask_user" for t in tasks) else "本次任务已完成。", "complete:" + str(run.id))
            return
        by_id = {t.id: t for t in tasks}
        dependencies = list(await tx.scalars(select(CoreTaskDependency).where(CoreTaskDependency.run_id == run.id)))
        if run.status == "paused":
            return
        for task in tasks:
            if task.status != "validated" or any(by_id[d.depends_on_id].status != "succeeded" for d in dependencies if d.task_id == task.id):
                continue
            if task.kind == "ask_user":
                task.waiting = task.parameters
                await transition(tx, "task", task.id, task.version, "waiting_user", causation_id=invocation.event_id)
                await transition(tx, "run", run.id, run.version, "paused", reason="waiting_user", causation_id=invocation.event_id)
                await message(tx, run, task.parameters["question"], "ask:" + str(task.id))
                return
            if task.tool_ref == "cutout":
                continue  # Subject selection code is the sole release gate for this step.
            await transition(tx, "task", task.id, task.version, "queued", causation_id=invocation.event_id)
