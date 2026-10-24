"""Skill playbook integration on an explicit disposable Postgres. Providers stay fake.

Covers the acceptance criteria: a selected playbook tightens scope.tools, the
reviewer rejects tickets outside the allowlist, compiled phase rules reject
incomplete plans, and a run saved as a personal skill replays with new inputs.
"""
import asyncio
import io
import os
import uuid

import pytest
from PIL import Image
from cryptography.fernet import Fernet
from sqlalchemy import select

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import Binding, Capability, CatalogConfig, TierSpec
from backend.core.contracts import TaskInput
from backend.core.execution.contracts import ProviderResult, WorkerSettings
from backend.core.execution.engine import Worker, enqueue, record_admission
from backend.core.orm import CoreRun, CoreSession, CoreSkill, CoreTask
from backend.core.store import create_task, transition
from backend.core.skills.draft import draft_from_run, fill_parameters
from backend.core.skills.store import seed_official, upsert_skill
from backend.core.supervisor.contracts import Plan as SupervisorPlan, Step
from backend.core.supervisor.engine import run_skill_rules, _run_skill_version_id
from backend.core.supervisor.reviewer import review
from backend.core.supervisor.service import open_run
from backend.core.tools import build_catalog
from backend.core.tools.store import publish
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Document, Workspace

DB = os.environ.get("AIVERO_DURABLE_TEST_DB") or os.environ.get("AIVERO_CORE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL URL required")

ECO_ALLOWLIST = {"detect_subject", "cutout", "image_edit_preview", "image_edit_final",
                 "text_to_image_preview", "text_to_image_final", "image_analyze",
                 "create_asset_card"}


def _png_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (16, 12), "blue").save(stream, format="PNG")
    return stream.getvalue()


def _catalog():
    ident = "skills-pg-" + uuid.uuid4().hex[:10]
    snapshot = {"provider": "mock", "configVersion": ident,
                "cloudModel": "mock-image", "videoModel": "mock-video"}
    capability = Capability(id="text_to_image", enabled=True, outputType="image", maxRefImages=0,
        tiers={"preview": TierSpec(credits=10), "final": TierSpec(credits=20)},
        binding=Binding(version=ident, providerSnapshot=snapshot))
    edit = Capability(id="image_edit", enabled=True, outputType="image", maxRefImages=1,
        tiers={"preview": TierSpec(credits=10), "final": TierSpec(credits=20)},
        binding=Binding(version=ident, providerSnapshot=snapshot))
    video = Capability(id="text_to_video", enabled=True, outputType="video", maxRefImages=0,
        tiers={"preview": TierSpec(credits=30)}, binding=Binding(version=ident, providerSnapshot=snapshot))
    return build_catalog(Catalog(CatalogConfig(version=ident, environment="development",
        enabled=True, capabilities=[capability, edit, video])), ident)


async def _seed_workspace(factory):
    workspace = uuid.uuid4()
    async with factory.begin() as tx:
        tx.add(Workspace(id=workspace))
        await tx.flush()
        doc = Document(workspace_id=workspace, name="画布", data={"objects": {}, "order": []}, revision=1)
        tx.add(doc)
        await tx.flush()
        await seed_official(tx)
        from backend.agent.media.credits import grant
        await grant(tx, workspace, uuid.uuid4().hex, 10 ** 6, "test")
        catalog = _catalog()
        await publish(tx, catalog, published_by="test")
    return workspace, doc.id, catalog


class CaptureProvider:
    idempotent = True
    query_registration = True
    cancellable = False

    def __init__(self, root):
        self.root = root
        self.submitted = []

    async def submit(self, registration_id, params, idempotency_key):
        self.submitted.append(params)
        return ProviderResult("succeeded", registration_id, data=_png_bytes(), ext="png", actual_credits=1)

    async def query(self, task_id, registration_id):
        return None

    async def download(self, locator, kind):
        raise ValueError("no deferred artifacts")

    async def cancel(self, task_id):
        raise ValueError("not cancellable")


def test_selected_skill_tightens_scope_and_reviewer_rejects_outside(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            workspace, doc_id, catalog = await _seed_workspace(factory)
            async with factory.begin() as tx:
                eco = await tx.scalar(select(CoreSkill).where(CoreSkill.name == "电商主图"))
                assert eco is not None
                skill_id = eco.id
            # 显式选择：request.skill_id 命中官方手册。
            async with factory.begin() as tx:
                run = await open_run(tx, *(await _session_pair(tx, workspace, doc_id)),
                    workspace_id=workspace, user_id="u1", request_key=uuid.uuid4().hex,
                    request={"message": "给这张产品图做白底图和换背景", "skill_id": str(skill_id)},
                    budget=1000, token_budget=65536, max_rounds=8)
                assert set(run.scope["tools"]) == ECO_ALLOWLIST
                assert run.scope["skill"]["skill_id"] == str(skill_id)
                assert str(skill_id) in run.version_lock["skills"]
                run_id = run.id
            # 白名单外的单子被审核员退回（legal gate）。
            outside = SupervisorPlan(goal="给这张产品图做白底图和换背景", steps=[
                Step(id="t1", tool="text_to_video_preview",
                     parameters={"prompt": "视频", "count": 1, "width": 512, "height": 512,
                                 "duration_seconds": 4, "fps": 16})])
            async with factory() as tx:
                run = await tx.get(CoreRun, run_id)
                verdict = review(outside, run, catalog, 10 ** 6)
            assert verdict["verdict"] == "reject"
            assert "tool_out_of_scope" in [issue["code"] for issue in verdict["reasons"]]
            # 手册阶段规则就位：缺抠图阶段的计划被 intent 规则退回。
            async with factory() as tx:
                run = await tx.get(CoreRun, run_id)
                rules = await run_skill_rules(tx, run)
                assert rules
                incomplete = SupervisorPlan(goal="给这张产品图做白底图和换背景", steps=[
                    Step(id="t1", tool="text_to_image_preview",
                         parameters={"prompt": "直接出图", "count": 1, "width": 512, "height": 512})])
                verdict = review(incomplete, run, catalog, 10 ** 6, intent_extensions=rules)
            assert verdict["verdict"] == "reject"
            assert "skill_phase_missing" in [issue["code"] for issue in verdict["reasons"]]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_saved_skill_replays_with_new_product(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            workspace, doc_id, catalog = await _seed_workspace(factory)
            settings = WorkerSettings(ticket_key=Fernet.generate_key().decode(), retry_jitter=0)
            # 第一次运行：显式选电商手册，跑一张白名单内的预览图并完成 Run。
            async with factory.begin() as tx:
                eco = await tx.scalar(select(CoreSkill).where(CoreSkill.name == "电商主图"))
                skill_id = eco.id
                session, doc = await _session_pair(tx, workspace, doc_id)
                first_session = session.id
                run = await open_run(tx, session, doc, workspace_id=workspace, user_id="u1",
                    request_key=uuid.uuid4().hex,
                    request={"message": "橘猫摆件主图", "skill_id": str(skill_id)},
                    budget=1000, token_budget=65536, max_rounds=8)
                ids = {"plan": run.current_plan_id}
                assert ids["plan"] is None  # 计划由测试直接落一条最小计划
                await _activate_run(tx, run)
                ids["plan"] = await _minimal_plan(tx, run)
                first_run = run.id
            await _run_generation_with_params(factory, tmp_path, ids, catalog, first_run,
                {"prompt": "橘猫摆件主图", "count": 1, "width": 512, "height": 512},
                settings=settings, expect_prompt="橘猫摆件主图")
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, first_run)
                await transition(tx, "run", run.id, run.version, "completed")
            # 从成功 Run 生成草稿并保存为「我的技能」。
            async with factory.begin() as tx:
                spec = await draft_from_run(tx, first_run)
                assert spec.defaults["{{prompt}}"] == "橘猫摆件主图"
                skill, version = await upsert_skill(tx, spec, workspace_id=workspace, user_id="u1")
                my_skill_id = skill.id
                my_version_id = version.id
            # 换产品套用：显式选「我的技能」，新提示词进同一流程（复用会话）。
            async with factory.begin() as tx:
                session = await tx.get(CoreSession, first_session)
                doc = await tx.get(Document, doc_id)
                run2 = await open_run(tx, session, doc, workspace_id=workspace, user_id="u1",
                    request_key=uuid.uuid4().hex,
                    request={"message": "柯基摆件主图", "skill_id": str(my_skill_id)},
                    budget=1000, token_budget=65536, max_rounds=8)
                assert set(run2.scope["tools"]) == {"text_to_image_preview"}
                assert run2.scope["skill"]["version_id"] == str(my_version_id)
                await _activate_run(tx, run2)
                ids2 = {"plan": await _minimal_plan(tx, run2)}
                second_run = run2.id
            templated = [{"tool": "text_to_image_preview", "kind": "call_tool",
                          "parameters": {"prompt": "{{prompt}}", "count": "{{count}}",
                                         "width": "{{width}}", "height": "{{height}}"},
                          "depends_on": []}]
            filled = fill_parameters(templated, {"{{prompt}}": "柯基摆件主图", "{{count}}": 1,
                                                 "{{width}}": 512, "{{height}}": 512})
            task_id = await _run_generation_with_params(factory, tmp_path, ids2, catalog,
                second_run, filled[0]["parameters"], settings=settings,
                expect_prompt="柯基摆件主图")
            async with factory() as tx:
                task = await tx.get(CoreTask, task_id)
                assert task.status == "succeeded"
                assert task.skill_version_id == my_version_id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


async def _session_pair(tx, workspace, doc_id):
    from backend.core.orm import CoreSession
    doc = await tx.get(Document, doc_id)
    session = CoreSession(workspace_id=workspace, source_canvas_id=doc_id, canvas_id=doc_id,
        user_id="u1", language="zh-CN")
    tx.add(session)
    await tx.flush()
    return session, doc


async def _activate_run(tx, run):
    await transition(tx, "run", run.id, run.version, "planning")
    await transition(tx, "run", run.id, run.version, "running")


async def _minimal_plan(tx, run):
    from backend.core.orm import CorePlan
    plan = CorePlan(run_id=run.id, version=1, writer="user", proposal={}, content_hash="b" * 64,
        rationale={}, link_constraints={}, result_type="generate", stage="final",
        milestones=[], self_check={}, expansion_rules={}, version_lock={})
    tx.add(plan)
    await tx.flush()
    run.current_plan_id = plan.id
    return plan.id


async def _run_generation_with_params(factory, media, ids, catalog, run_id, parameters, *,
                                       settings, expect_prompt=None):
    async with factory.begin() as tx:
        run = await tx.get(CoreRun, run_id)
        task = await create_task(tx, TaskInput(run_id=run.id, plan_id=ids["plan"], plan_version=1,
            task_key="t1", kind="call_tool", tool_ref="text_to_image_preview",
            tool_version=catalog.get_tool("text_to_image_preview", catalog.lock.version).version,
            idempotency_key=uuid.uuid4().hex, parameters=parameters, scope={},
            skill_version_id=_run_skill_version_id(run)))
        await transition(tx, "task", task.id, 0, "validated")
        await record_admission(tx, task, catalog, checks={"legal": True, "types": True, "intent": True},
            review_ref="review", placement={})
        await transition(tx, "task", task.id, 1, "queued")
        job_id = await enqueue(tx, task.id)
        task_id = task.id
    capture = CaptureProvider(media)
    worker = Worker(factory, media, capture, settings=settings)
    await worker.execute(job_id)
    async with factory() as tx:
        task = await tx.get(CoreTask, task_id)
        assert task.status == "succeeded", task.error
    assert any(params.get("prompt") == expect_prompt for params in capture.submitted)
    return task_id
