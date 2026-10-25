"""Run-control acceptance: amend, over-budget resume/stop, cancel, costs, subject choice."""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import Binding, Capability, CatalogConfig, TierSpec
from backend.agent.media.credits import grant
from backend.agent.orm import HostCreditLedger
from backend.core.contracts import TaskInput
from backend.core.orm import (CoreEvent, CorePlan, CoreRun, CoreSession, CoreSessionMessage, CoreTask)
from backend.infrastructure.orm import Workspace
from backend.core.store import create_task, transition
from backend.core.supervisor.engine import Supervisor
from backend.core.supervisor.service import open_run, run_budget_action, run_cancel
from backend.core.supervisor.subjects import (expire_stale_choices, try_message_answer)
from backend.core.tools import build_catalog
from backend.core.tools.store import publish
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Document

DB = os.environ.get("AIVERO_DURABLE_TEST_DB") or os.environ.get("AIVERO_CORE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL URL required")


def _catalog():
    ident = "controls-pg-" + uuid.uuid4().hex[:10]
    snapshot = {"provider": "mock", "configVersion": ident,
                "cloudModel": "mock-image", "videoModel": "mock-video"}
    caps = [
        Capability(id="text_to_image", enabled=True, outputType="image", maxRefImages=0,
            tiers={"preview": TierSpec(credits=10), "final": TierSpec(credits=20)},
            binding=Binding(version=ident, providerSnapshot=snapshot)),
        Capability(id="text_to_video", enabled=True, outputType="video", maxRefImages=0,
            tiers={"preview": TierSpec(credits=30)},
            binding=Binding(version=ident, providerSnapshot=snapshot)),
    ]
    return build_catalog(Catalog(CatalogConfig(version=ident, environment="development",
        enabled=True, capabilities=caps)), ident)


async def _seed(factory, *, budget=1000):
    workspace = uuid.uuid4()
    async with factory.begin() as tx:
        tx.add(Workspace(id=workspace))
        await tx.flush()
        doc = Document(workspace_id=workspace, name="画布", data={"objects": {}, "order": []}, revision=1)
        tx.add(doc)
        await tx.flush()
        await grant(tx, workspace, uuid.uuid4().hex, 10 ** 6, "test")
        catalog = _catalog()
        await publish(tx, catalog, published_by="test")
        session = CoreSession(workspace_id=workspace, source_canvas_id=doc.id, canvas_id=doc.id,
            user_id="u1", language="zh-CN")
        tx.add(session)
        await tx.flush()
    return workspace, doc.id, session.id, catalog


async def _open_run(factory, ids, *, mode="auto", budget=1000, message="画两张海报", selected=()):
    workspace, doc_id, session_id, catalog = ids
    async with factory.begin() as tx:
        session = await tx.get(CoreSession, session_id)
        doc = await tx.get(Document, doc_id)
        run = await open_run(tx, session, doc, workspace_id=workspace, user_id="u1",
            request_key=uuid.uuid4().hex, request={"message": message,
                "selected_assets": [str(item) for item in selected]}, budget=budget,
            token_budget=65536, max_rounds=8, mode=mode)
        await transition(tx, "run", run.id, run.version, "planning")
        if mode != "plan":  # 计划模式停在 planning，落计划后走 awaiting_confirm
            await transition(tx, "run", run.id, run.version, "running")
        return run.id


async def _plan_with_steps(factory, run_id, steps, *, confirmation=False, goal="画两张海报"):
    """Hand-write a plan and tasks the way the supervisor would."""
    async with factory.begin() as tx:
        run = await tx.get(CoreRun, run_id)
        version = (await tx.scalar(select(func.max(CorePlan.version))
            .where(CorePlan.run_id == run_id)) or 0) + 1
        all_steps = steps
        if confirmation:
            all_steps = [{"id": "t9", "kind": "ask_user",
                          "parameters": {"question": "是否执行此计划？", "options": [
                              {"id": "confirm", "label": "执行"}, {"id": "cancel", "label": "取消"}],
                              "plan_confirmation": True}, "depends_on": []}] + [
                {**step, "depends_on": ["t9", *step.get("depends_on", [])]} for step in steps]
        plan = CorePlan(run_id=run_id, version=version, writer="llm",
            proposal={"goal": goal, "steps": all_steps, "result_type": "generate", "writer": "llm"},
            content_hash="b" * 64, rationale={}, link_constraints={}, result_type="generate",
            stage="execute", milestones=[], self_check={}, expansion_rules={}, version_lock={})
        tx.add(plan)
        await tx.flush()
        run.current_plan_id = plan.id
        if confirmation:
            await transition(tx, "run", run.id, run.version, "awaiting_confirm")
        return plan.id, all_steps


async def _task(factory, run_id, plan_id, step, *, status="validated", catalog=None):
    async with factory.begin() as tx:
        tool = None
        if step.get("tool"):
            tool = catalog.get_tool(step["tool"], catalog.lock.version)
        task = await create_task(tx, TaskInput(run_id=run_id, plan_id=plan_id,
            plan_version=1, task_key=step["id"], kind=step.get("kind", "call_tool"),
            tool_ref=step.get("tool"), tool_version=tool.version if tool else None,
            parameters=step.get("parameters", {}), scope={},
            idempotency_key=f"{plan_id}:{step['id']}"))
        ladder = {"waiting_user": ["validated", "waiting_user"],
                  "submitted": ["validated", "queued", "submitted"],
                  "succeeded": ["validated", "queued", "submitted", "succeeded"],
                  "queued": ["validated", "queued"]}
        for target in ladder.get(status, [status]):
            await transition(tx, "task", task.id, task.version, target)
        return task.id


def test_amend_deletes_step_with_dependents_and_repasses_review(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            ids = await _seed(factory)
            run_id = await _open_run(factory, ids, mode="plan")
            steps = [
                {"id": "t1", "tool": "text_to_image_preview",
                 "parameters": {"prompt": "方向一", "count": 1, "width": 512, "height": 512}, "depends_on": []},
                {"id": "t2", "tool": "text_to_image_preview",
                 "parameters": {"prompt": "方向二", "count": 1, "width": 512, "height": 512},
                 "depends_on": ["t1"]},
                {"id": "t3", "tool": "text_to_image_final",
                 "parameters": {"prompt": "正片", "count": 1, "width": 512, "height": 512}, "depends_on": []},
            ]
            plan_id, _ = await _plan_with_steps(factory, run_id, steps, confirmation=True)
            from backend.core.execution.engine import record_admission
            catalog = ids[3]
            for step in steps:
                task_id = await _task(factory, run_id, plan_id, step, catalog=catalog)
                async with factory.begin() as tx:
                    task = await tx.get(CoreTask, task_id)
                    await record_admission(tx, task, catalog,
                        checks={"legal": True, "types": True, "intent": True},
                        review_ref="review", placement={})
            await _task(factory, run_id, plan_id,
                {"id": "t9", "kind": "ask_user", "parameters": {}}, status="waiting_user")
            supervisor = Supervisor(factory)
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run_id)
                result = await supervisor.amend(tx, run, {"delete_steps": ["t1"]})
                assert result["reviewed"] is True
                assert result["removed"] == ["t1"]
                assert result["removed_transitive"] == ["t2"]
                new_plan = await tx.get(CorePlan, uuid.UUID(result["plan_id"]))
                assert new_plan.self_check["verdict"] == "pass"
                kept = {step["id"] for step in new_plan.proposal["steps"]}
                assert kept == {"t9", "t3"}
            async with factory() as tx:
                old_tasks = list(await tx.scalars(select(CoreTask).where(
                    CoreTask.plan_id == plan_id)))
                assert all(task.status == "cancelled" for task in old_tasks)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_over_budget_extend_resumes_and_stop_refunds(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            ids = await _seed(factory)
            run_id = await _open_run(factory, ids, budget=5)
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run_id)
                await transition(tx, "run", run.id, run.version, "paused", reason="over_budget")
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run_id)
                result = await run_budget_action(tx, run, "extend", 100)
                assert result["status"] == "running"
            async with factory() as tx:
                run = await tx.get(CoreRun, run_id)
                assert run.status == "running" and run.credits_limit == 105
                events = list(await tx.scalars(select(CoreEvent.event_type)
                    .where(CoreEvent.run_id == run_id)))
                assert "run_resumed" in events
            # stop 分支：另造一个 over_budget run（新会话避免活动 Run 冲突）
            ids2 = await _seed(factory)
            run2 = await _open_run(factory, ids2, budget=5)
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run2)
                await transition(tx, "run", run.id, run.version, "paused", reason="over_budget")
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run2)
                result = await run_budget_action(tx, run, "stop")
                assert result["status"] == "cancelled"
            async with factory() as tx:
                run = await tx.get(CoreRun, run2)
                assert run.status == "cancelled"
                texts = [(m.content or {}).get("text", "") for m in await tx.scalars(
                    select(CoreSessionMessage).where(CoreSessionMessage.run_id == run2))]
                assert any("已停止" in text and "已退还" in text for text in texts)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_cancel_run_cascades_and_reports_costs(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            ids = await _seed(factory)
            run_id = await _open_run(factory, ids)
            steps = [
                {"id": "t1", "tool": "text_to_video_preview",
                 "parameters": {"prompt": "视频", "count": 1, "width": 512, "height": 512,
                                "duration_seconds": 4, "fps": 16}, "depends_on": []},
                {"id": "t2", "tool": "text_to_image_preview",
                 "parameters": {"prompt": "图", "count": 1, "width": 512, "height": 512}, "depends_on": []},
            ]
            plan_id, _ = await _plan_with_steps(factory, run_id, steps)
            catalog = ids[3]
            t1 = await _task(factory, run_id, plan_id, steps[0], status="submitted", catalog=catalog)
            t2 = await _task(factory, run_id, plan_id, steps[1], status="queued", catalog=catalog)
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run_id)
                result = await run_cancel(tx, run)
                assert result["status"] == "cancelled"
            async with factory() as tx:
                first, second = await tx.get(CoreTask, t1), await tx.get(CoreTask, t2)
                assert second.status == "cancelled"  # 未开始：直接退
                assert first.status == "submitted"  # 不可取消的在途：等跑完按实际结算
                run = await tx.get(CoreRun, run_id)
                assert run.status == "paused" and run.pause_reason == "waiting_info"
                texts = [(m.content or {}).get("text", "") for m in await tx.scalars(
                    select(CoreSessionMessage).where(CoreSessionMessage.run_id == run_id))]
                assert any("已产生费用" in text and "在途任务" in text for text in texts)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_costs_sum_matches_ledger_and_run(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            ids = await _seed(factory)
            run_id = await _open_run(factory, ids)
            from backend.agent.media.credits import reserve_core, settle_core
            steps = [
                {"id": "t1", "tool": "text_to_image_preview",
                 "parameters": {"prompt": "a", "count": 1, "width": 512, "height": 512}, "depends_on": []},
                {"id": "t2", "tool": "text_to_image_preview",
                 "parameters": {"prompt": "b", "count": 1, "width": 512, "height": 512}, "depends_on": []},
            ]
            plan_id, _ = await _plan_with_steps(factory, run_id, steps)
            catalog = ids[3]
            first = await _task(factory, run_id, plan_id, steps[0], status="queued", catalog=catalog)
            second = await _task(factory, run_id, plan_id, steps[1], status="queued", catalog=catalog)
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run_id)
                task = await tx.get(CoreTask, first)
                await reserve_core(tx, run, task, 10)
                await transition(tx, "task", task.id, task.version, "submitted")
                task2 = await tx.get(CoreTask, second)
                await reserve_core(tx, run, task2, 10)
                await transition(tx, "task", task2.id, task2.version, "submitted")
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run_id)
                task = await tx.get(CoreTask, first)
                await settle_core(tx, run, task, 10, outcome="succeeded")
                await transition(tx, "task", task.id, task.version, "succeeded")
                task2 = await tx.get(CoreTask, second)
                await settle_core(tx, run, task2, 0, outcome="failed")
                await transition(tx, "task", task2.id, task2.version, "failed")
            async with factory() as tx:
                run = await tx.get(CoreRun, run_id)
                ledgers = list(await tx.scalars(select(HostCreditLedger)
                    .where(HostCreditLedger.core_run_id == run_id)))
                assert sum(row.charged for row in ledgers) == 10
                assert sum(row.refunded for row in ledgers) == 10
                assert run.credits_spent == 10
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)
                    .order_by(CoreTask.task_key)))
                assert sum(int(t.actual_credits or 0) for t in tasks) == run.credits_spent
        finally:
            await engine.dispose()

    asyncio.run(scenario())


async def _subject_waiting(factory, run_id, image, goal="抠个图"):
    """detect succeeded + cutout validated + waiting subject-choice question."""
    from backend.core.supervisor.subjects import handle_subjects
    steps = [
        {"id": "t1", "tool": "detect_subject", "parameters": {"image": image, "target_description": ""},
         "depends_on": []},
        {"id": "t2", "tool": "cutout",
         "parameters": {"image": image, "mask": "$t1.output"}, "depends_on": ["t1"]},
    ]
    plan_id, _ = await _plan_with_steps(factory, run_id, steps, goal=goal)
    async with factory() as tx:
        from backend.core.tools.contracts import VersionLock
        from backend.core.tools.store import load_catalog
        run = await tx.get(CoreRun, run_id)
        catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"],
            expected_lock=VersionLock.model_validate(run.version_lock["tool_catalog"]))
    detect = await _task(factory, run_id, plan_id, steps[0], status="succeeded", catalog=catalog)
    cutout = await _task(factory, run_id, plan_id, steps[1], status="validated", catalog=catalog)
    async with factory.begin() as tx:
        task = await tx.get(CoreTask, detect)
        task.output = {"result_type": "detected", "candidate_count": 2, "granularity": "combined_foreground",
            "candidates": [{"mask": {"handle": "h1"}, "label": "杯子", "confidence": None,
                            "confidence_source": "unknown"},
                {"mask": {"handle": "h2"}, "label": "托盘", "confidence": None,
                 "confidence_source": "unknown"}]}
        run = await tx.get(CoreRun, run_id)
        cause = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == run_id)
            .order_by(CoreEvent.run_sequence.desc()).limit(1))
        from backend.core.supervisor.contracts import Settings
        await handle_subjects(tx, run, [task, await tx.get(CoreTask, cutout)], cause, Settings())
    async with factory() as tx:
        question = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run_id,
            CoreTask.kind == "ask_user", CoreTask.status == "waiting_user"))
        return question.id if question else None, detect, cutout


async def _make_asset(factory, ids):
    import hashlib as _hashlib
    from backend.infrastructure.orm import Asset
    workspace, doc_id, session_id, _catalog = ids
    data = b"fake-png-bytes"
    async with factory.begin() as tx:
        row = Asset(workspace_id=workspace, kind="image", ext="png", width=8, height=6,
            bytes=len(data), sha256=_hashlib.sha256(data).hexdigest(), storage_key="subject.png")
        tx.add(row)
        await tx.flush()
        return row.id


async def _apply_choice(factory, run_id):
    """Simulate the supervisor wake that runs after user_confirmed."""
    from backend.core.supervisor.subjects import handle_subjects
    from backend.core.supervisor.contracts import Settings
    async with factory.begin() as tx:
        run = await tx.get(CoreRun, run_id)
        tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
        cause = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == run_id)
            .order_by(CoreEvent.run_sequence.desc()).limit(1))
        await handle_subjects(tx, run, tasks, cause, Settings())
        run = await tx.get(CoreRun, run_id, populate_existing=True)
        if run.status == "paused" and run.pause_reason == "waiting_user" and not any(
                t.status == "waiting_user" for t in tasks):
            from backend.core.store import transition
            await transition(tx, "run", run.id, run.version, run.resume_state or "running",
                causation_id=cause)
            if run.resume_state == "awaiting_confirm":
                await transition(tx, "run", run.id, run.version, "running", causation_id=cause)


async def _close_run(factory, run_id):
    """Terminate a scenario run so the session can open the next one."""
    from backend.core.store import transition
    async with factory.begin() as tx:
        run = await tx.get(CoreRun, run_id)
        for task in await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id,
                CoreTask.status.not_in(("succeeded", "failed", "cancelled", "blocked_by_model")))):
            await transition(tx, "task", task.id, task.version, "cancelled")
        run = await tx.get(CoreRun, run_id, populate_existing=True)
        if run.status not in ("completed", "failed", "cancelled"):
            source = run.status
            if source == "paused":
                await transition(tx, "run", run.id, run.version, "cancelled")
            else:
                await transition(tx, "run", run.id, run.version, "cancelled")


def test_subject_choice_click_and_chat_and_ttl(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            ids = await _seed(factory)
            asset_id = await _make_asset(factory, ids)
            image = {"asset_id": str(asset_id), "media_type": "image"}
            # 1) 点选继续抠图
            run_a = await _open_run(factory, ids, message="抠个图", selected=[asset_id])
            question_id, detect_id, cutout_id = await _subject_waiting(factory, run_a, image)
            assert question_id is not None
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run_a)
                from backend.core.supervisor.service import answer
                await answer(tx, task_id=question_id, option_id="0",
                    request_key="pick:" + str(question_id), workspace_id=ids[0], user_id="u1")
            await _apply_choice(factory, run_a)
            async with factory() as tx:
                detect = await tx.get(CoreTask, detect_id)
                assert (detect.waiting or {}).get("selected_output")
                assert (detect.waiting or {}).get("selected_handles") == ["h1"]
            await _close_run(factory, run_a)
            # 2) 直接发消息匹配选项（不调模型）
            run_b = await _open_run(factory, ids, message="抠另一个", selected=[asset_id])
            question_id, detect_id, _cut = await _subject_waiting(factory, run_b, image, goal="抠另一个")
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run_b)
                from backend.core.supervisor.orm import Invocation
                before = await tx.scalar(select(func.count()).select_from(Invocation)
                    .where(Invocation.run_id == run_b))
                matched = await try_message_answer(tx, run, "抠托盘")
                assert matched is True
                after = await tx.scalar(select(func.count()).select_from(Invocation)
                    .where(Invocation.run_id == run_b))
                assert before == after  # 没有唤醒 planner
            await _apply_choice(factory, run_b)
            async with factory() as tx:
                question = await tx.get(CoreTask, question_id)
                assert question.status == "succeeded"
                detect = await tx.get(CoreTask, detect_id)
                assert (detect.waiting or {}).get("selected_handles") == ["h2"]
            await _close_run(factory, run_b)
            # 3) TTL 超时取消不扣费
            run_c = await _open_run(factory, ids, message="再抠一个", selected=[asset_id])
            question_id, _d, _c = await _subject_waiting(factory, run_c, image, goal="再抠一个")
            async with factory.begin() as tx:
                question = await tx.get(CoreTask, question_id)
                question.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
            expired = await expire_stale_choices(factory)
            assert expired >= 1
            async with factory() as tx:
                question = await tx.get(CoreTask, question_id)
                assert question.status == "cancelled"
                run = await tx.get(CoreRun, run_c)
                assert run.status == "cancelled"
                ledgers = list(await tx.scalars(select(HostCreditLedger)
                    .where(HostCreditLedger.core_run_id == run_c)))
                assert ledgers == []  # 未预扣、未扣费
                texts = [(m.content or {}).get("text", "") for m in await tx.scalars(
                    select(CoreSessionMessage).where(CoreSessionMessage.run_id == run_c))]
                assert any("超时未选择" in text and "未扣费" in text for text in texts)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
