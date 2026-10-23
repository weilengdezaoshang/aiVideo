"""Full supervisor -> reviewed queue -> fake provider -> events using isolated PG."""
import asyncio
import hashlib
import io
import os
import uuid
from pathlib import Path
from backend.core.supervisor.orm import Invocation
from contextlib import asynccontextmanager

import pytest
from PIL import Image
from sqlalchemy import select, func
from cryptography.fernet import Fernet
from backend.agent.media.credits import grant
from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.agent.orm import HostCreditLedger
from backend.core.events.contracts import EventSettings
from backend.core.events.processor import Processor
from backend.core.events.registry import Registry
from backend.core.events.store import accept
from backend.core.execution.contracts import WorkerSettings, ProviderResult
from backend.core.execution.engine import Worker, scan_ready
from backend.core.execution.fake import FakeProvider, Scenario
from backend.core.execution.local import LocalAdapter
from backend.core.execution.masks import save_mask
from backend.core.orm import CoreRun, CoreTask, CoreEvent, CoreSessionMessage
from backend.core.supervisor.engine import Supervisor, register
from backend.core.supervisor.planners import FakePlanner
from backend.core.supervisor.service import start, answer, progress
from backend.core.tools import build_catalog, ToolCatalog, CatalogSnapshot
from backend.core.tools.contracts import ToolDefinition, Execution, Port, Price
from backend.core.tools.store import publish
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Workspace, Document, Asset, AssetReference

DB = os.environ.get("AIVERO_SUPERVISOR_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated supervisor DB required")


def image_bytes(mask=False, right=False):
    image = Image.new("L" if mask else "RGBA", (8, 8), 0 if mask else "white")
    if mask:
        for x in range(4 if right else 0, 8 if right else 4):
            for y in range(8):
                image.putpixel((x, y), 255)
    result = io.BytesIO()
    image.save(result, "PNG")
    return result.getvalue()


class DetectFake(LocalAdapter):
    def __init__(self, tool, factory, root, labels):
        super().__init__(tool, factory, root)
        self.labels = labels
    async def compute(self, registration_id, params):
        if self.tool.name != "detect_subject":
            return await super().compute(registration_id, params)
        from backend.core.tools.detection import ImageIdentity, RawCandidate, normalize_candidates
        source = params["image"]
        identity = ImageIdentity(asset_id=source["asset_id"], sha256=source["sha256"], width=8, height=8)
        raw = []
        for i, label in enumerate(self.labels):
            data, handle = image_bytes(True, bool(i)), registration_id + ":" + str(i)
            save_mask(self.root, handle, data)
            raw.append(RawCandidate(data, handle, label, .99))
        output = normalize_candidates(raw, identity, registration_id).model_dump(mode="json")
        return ProviderResult("succeeded", registration_id, output=output)


@asynccontextmanager
async def fixture(tmp_path, *, planner=None, labels=None, message="改图后生成视频", mode="auto", sized_video=False,
                  make_planner=None):
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    root = tmp_path / "media"
    root.mkdir(parents=True)
    config = WorkerSettings(ticket_key=Fernet.generate_key().decode(), poll_seconds=.001,
        retry_base_seconds=.001, retry_max_seconds=.01, retry_jitter=0)
    base = build_catalog(Catalog(CatalogConfig(version="test", environment="development", enabled=True, capabilities=[])), "test")
    tools = list(base.snapshot.tools)
    for name, output in [("edit", "image"), ("video", "video")]:
        properties = {"prompt": {"type": "string"}, "image": {"anyOf": [{"type": "object"}, {"type": "string"}]}}
        if output == "video" and sized_video:
            properties.update({"duration_seconds": {"type": "integer", "minimum": 1},
                "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}})
        tools.append(ToolDefinition(name=name, version="v1", description="fake only",
            parameters={"type": "object", "properties": properties, "required": ["prompt", "image"], "additionalProperties": False},
            result_schema={"type": "object"}, inputs=[Port(path="image", media_type="image")], outputs=[Port(path="output", media_type=output)],
            mode="generate", price=Price(kind="fixed", credits=10), limits={}, failures=[],
            execution=Execution(adapter="execution_test_fake", provider_idempotent=True, result_url_policy="authenticated_asset", sla_seconds=60)))
    catalog = ToolCatalog(CatalogSnapshot(version=uuid.uuid4().hex, tools=tools))
    async with factory.begin() as tx:
        workspace = Workspace()
        tx.add(workspace)
        await tx.flush()
        await grant(tx, workspace.id, "initial", 100, "owner")
        asset = Asset(workspace_id=workspace.id, kind="image", ext="png", storage_key="source.png",
            bytes=len(image_bytes()), sha256=hashlib.sha256(image_bytes()).hexdigest(), width=8, height=8, details={})
        tx.add(asset)
        await tx.flush()
        (root / "source.png").write_bytes(image_bytes())
        doc = Document(workspace_id=workspace.id, data={"objects": {"source": {"id": "source", "kind": "image", "assetId": str(asset.id), "x": 0, "y": 0, "width": 8, "height": 8}}, "order": ["source"]})
        tx.add(doc)
        await tx.flush()
        await publish(tx, catalog, published_by="test")
        run = await start(tx, workspace_id=workspace.id, user_id="owner", document_id=doc.id,
            request_key="start", request={"message": message, "selected_assets": [str(asset.id)], "anchor_id": "source"},
            budget=100, token_budget=100, max_rounds=20, mode=mode)
        run_id, workspace_id = run.id, workspace.id
    if planner == "chain":
        plan = {"goal": message, "steps": [
            {"id": "t1", "tool": "edit", "parameters": {"prompt": "edit", "image": {"asset_id": str(asset.id), "media_type": "image"}}},
            {"id": "t2", "tool": "video", "parameters": {"prompt": "video", "image": "$t1.output"}, "depends_on": ["t1"]}]}
        planner = FakePlanner([{"plan": plan, "invariants": run.invariants, "confidence": 1, "tokens": 2, "raw_output": {"plan": plan}}])
    if make_planner is not None:
        planner = make_planner(factory)
    supervisor = Supervisor(factory, planner)
    registry = Registry()
    register(registry)
    processor = Processor(factory, EventSettings(concurrency=1, backoff_base_seconds=.001, backoff_max_seconds=.01), registry)
    fake = FakeProvider(tmp_path / "provider.sqlite", Scenario(actual_credits=3))
    async def events():
        async with factory.begin() as tx:
            for event in await tx.scalars(select(CoreEvent).where(CoreEvent.run_id == run_id).order_by(CoreEvent.run_sequence)):
                await accept(tx, {"event_id": str(event.id), "run_id": str(run_id)})
        for _ in range(100):
            if not any(await processor.tick()):
                break
    async def drive(*, fail_video=False, until=None):
        for _ in range(100):
            await events()
            await supervisor.tick()
            await scan_ready(factory)
            async with factory() as tx:
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
            for task in tasks:
                if task.status not in {"queued", "submitted"}:
                    continue
                if task.tool_ref in {"detect_subject", "cutout"}:
                    provider = DetectFake(catalog.get_tool(task.tool_ref, catalog.lock.version), factory, root, labels or [])
                else:
                    provider = fake if not (fail_video and task.tool_ref == "video") else FakeProvider(tmp_path / "failure.sqlite", Scenario(outcome="param_invalid"))
                await Worker(factory, root, provider, settings=config).execute(task.job_id)
            async with factory() as tx:
                row = await tx.get(CoreRun, run_id)
                if row.status in {"completed", "failed", "cancelled", "paused"} or (until and await until(tx)):
                    return row.status
            await asyncio.sleep(.002)
        raise AssertionError("run did not settle")
    try:
        yield factory, supervisor, drive, run_id, workspace_id, root, config, fake, events
    finally:
        # Every fixture owns a workspace. Stop its unfinished scripts before a
        # later test starts a global daemon with a different scripted provider.
        from sqlalchemy import update
        from backend.core.supervisor.engine import stop
        async with factory.begin() as tx:
            owned = list(await tx.scalars(select(CoreRun).where(CoreRun.workspace_id == workspace_id)))
            for row in owned:
                if row.status not in {"completed", "failed", "cancelled"}:
                    await stop(tx, row, "cancelled", "fixture cleanup", "fixture-cleanup:" + str(row.id))
            await tx.execute(update(Invocation).where(Invocation.run_id.in_([row.id for row in owned]))
                .values(status="done", lease_token=None, lease_until=None))
        await engine.dispose()


def test_edit_then_video(tmp_path):
    async def test():
        async with fixture(tmp_path, planner="chain") as (factory, supervisor, drive, run, *_):
            assert await drive() == "completed"
            assert len(supervisor.planner.calls) == 1
            async with factory() as tx:
                assert await tx.scalar(select(AssetReference.id).where(AssetReference.owner_type == "core_run", AssetReference.owner_id == str(run)))
            async with factory() as tx:
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run).order_by(CoreTask.task_key)))
                assert [t.status for t in tasks] == ["succeeded", "succeeded"]
                assert tasks[0].output["media_type"] == "image" and tasks[1].output["media_type"] == "video"
                assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.core_run_id == run)) == 2
    asyncio.run(test())


@pytest.mark.parametrize("labels,message", [(["杯子"], "抠图"), (["白色杯子", "盘子"], "把杯子抠出来"), ([], "抠图")])
def test_subject_fast_path(tmp_path, labels, message):
    async def test():
        async with fixture(tmp_path, labels=labels, message=message) as (factory, _, drive, run, *_):
            assert await drive() == "completed"
            async with factory() as tx:
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run).order_by(CoreTask.task_key)))
                assert [t.status for t in tasks] == ["succeeded", "succeeded" if labels else "cancelled"]
                projection = await progress(tx, await tx.get(CoreRun, run))
                assert any("识别到" in m["text"] if labels else "未识别" in m["text"] for m in projection["messages"])
                assert "handle" not in str(projection)
                assert await tx.scalar(select(func.coalesce(func.sum(HostCreditLedger.charged), 0)).where(HostCreditLedger.core_run_id == run)) == 0
    asyncio.run(test())


@pytest.mark.parametrize("choice", ["0", "all"])
def test_multi_subject_answer(tmp_path, choice):
    async def test():
        async with fixture(tmp_path, labels=["白色杯子", "盘子"], message="抠图") as (factory, _, drive, run, workspace, root, *_):
            assert await drive() == "paused"
            async with factory.begin() as tx:
                question = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run, CoreTask.kind == "ask_user"))
                assert [x["label"] for x in question.waiting["options"]] == ["白色杯子", "盘子", "全部"]
                await answer(tx, task_id=question.id, option_id=choice, request_key="answer", workspace_id=workspace, user_id="owner")
                await answer(tx, task_id=question.id, option_id=choice, request_key="answer", workspace_id=workspace, user_id="owner")
            assert await drive() == "completed"
            async with factory() as tx:
                task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run, CoreTask.tool_ref == "cutout"))
                asset = await tx.get(Asset, uuid.UUID(task.output["asset_id"]))
                with Image.open(root / asset.storage_key) as image:
                    assert image.getpixel((0, 0))[3] == 255
                    assert image.getpixel((7, 0))[3] == (255 if choice == "all" else 0)
    asyncio.run(test())


def test_three_rejections_fail(tmp_path):
    bad = {"plan": {"goal": "test", "steps": [{"id": "t1", "tool": "nonexistent"}]}, "confidence": 1, "raw_output": {"bad": True}}
    planner = FakePlanner([bad] * 3)
    async def test():
        async with fixture(tmp_path, planner=planner, message="test") as (factory, _, drive, run, *_):
            assert await drive() == "failed"
            assert len(planner.calls) == 3
            async with factory() as tx:
                assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.core_run_id == run)) == 0
                assert any("审核未通过" in m.content["text"] for m in await tx.scalars(select(CoreSessionMessage).where(CoreSessionMessage.run_id == run, CoreSessionMessage.role == "assistant")))
    asyncio.run(test())


def test_t2_failure_keeps_t1(tmp_path):
    async def test():
        async with fixture(tmp_path, planner="chain") as (factory, _, drive, run, *_):
            assert await drive(fail_video=True) == "failed"
            async with factory() as tx:
                task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run, CoreTask.task_key == "t1"))
                assert task.status == "succeeded" and await tx.get(Asset, uuid.UUID(task.output["asset_id"]))
    asyncio.run(test())


def killed_checkpoint(db_url, root, config_data, script, action, ident):
    """A real subprocess commits one boundary, then dies without graceful cleanup."""
    import signal
    async def work():
        engine = create_async_database_engine(DatabaseSettings(url=db_url))
        factory = create_async_session_factory(engine)
        if action in {"planner", "plan"}:
            await Supervisor(factory, FakePlanner(script)).tick()
        elif action == "wake":
            registry = Registry()
            register(registry)
            processor = Processor(factory, EventSettings(concurrency=1), registry)
            await processor.tick()
        else:
            provider = FakeProvider(Path(root).parent / "provider.sqlite", Scenario(actual_credits=3))
            await Worker(factory, Path(root), provider, settings=WorkerSettings(**config_data)).execute(uuid.UUID(ident))
        os.kill(os.getpid(), signal.SIGKILL)
    asyncio.run(work())


@pytest.mark.parametrize("boundary", ["wake", "planner", "plan", "t1", "t2"])
def test_process_kill_at_supervisor_boundaries(tmp_path, boundary):
    async def test():
        import multiprocessing
        import signal
        async with fixture(tmp_path, planner="chain") as (factory, supervisor, drive, run, _, root, config, fake, events):
            # Only this run owns pending invocations in the explicitly isolated fixture.
            async with factory.begin() as tx:
                for event in await tx.scalars(select(CoreEvent).where(CoreEvent.run_id == run)):
                    await accept(tx, {"event_id": str(event.id), "run_id": str(run)})
            ident = ""
            if boundary != "wake":
                await events()
            if boundary in {"plan", "t1", "t2"}:
                await supervisor.tick()  # raw output checkpoint
            if boundary in {"t1", "t2"}:
                await supervisor.tick()  # reviewed Plan/queued checkpoint
                await scan_ready(factory)
                async with factory() as tx:
                    task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run, CoreTask.task_key == "t1"))
                    ident = str(task.job_id)
            if boundary == "t2":
                await Worker(factory, root, fake, settings=config).execute(uuid.UUID(ident))
                await events()
                await supervisor.tick()
                await scan_ready(factory)
                async with factory() as tx:
                    task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run, CoreTask.task_key == "t2"))
                    ident = str(task.job_id)
            process = multiprocessing.get_context("spawn").Process(target=killed_checkpoint,
                args=(DB, str(root), config.model_dump(), supervisor.planner.script, boundary, ident))
            process.start()
            await asyncio.to_thread(process.join, 15)
            assert process.exitcode == -signal.SIGKILL
            assert await drive() == "completed"
            async with factory() as tx:
                rows = list(await tx.scalars(select(HostCreditLedger).where(HostCreditLedger.core_run_id == run)))
                assert len(rows) == 2 and sum(r.charged for r in rows) == 6
                assert await tx.scalar(select(func.count()).select_from(Invocation).where(Invocation.run_id == run, Invocation.result.is_not(None))) == 1
            assert fake.counts()["submissions"] == 2
    asyncio.run(test())


def test_plan_mode_always_asks(tmp_path):
    async def test():
        async with fixture(tmp_path, labels=["杯子"], message="抠图", mode="plan") as (factory, _, drive, run, workspace, *_):
            assert await drive() == "paused"
            async with factory.begin() as tx:
                question = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run, CoreTask.kind == "ask_user"))
                await answer(tx, task_id=question.id, option_id="0", request_key="choose", workspace_id=workspace, user_id="owner")
            assert await drive() == "completed"
    asyncio.run(test())


def test_budget_and_round_limits(tmp_path):
    async def test():
        async with fixture(tmp_path, planner="chain") as (factory, _, drive, run, *_):
            async with factory.begin() as tx:
                row = await tx.get(CoreRun, run)
                row.max_thought_rounds = 1
            assert await drive() == "failed"
            async with factory() as tx:
                row = await tx.get(CoreRun, run)
                assert row.thought_rounds_used == 1 and row.tokens_used == 2
    asyncio.run(test())


def test_answer_http_scope_and_projection(tmp_path):
    async def test():
        from fastapi import FastAPI
        import httpx
        from backend.core.supervisor.api import install
        from backend.services.identity import authenticated, Identity
        async with fixture(tmp_path, labels=["白色杯子", "盘子"], message="抠图") as (factory, _, drive, run, workspace, *_):
            assert await drive() == "paused"
            app = FastAPI()
            app.state.factory = factory
            install(app)
            app.dependency_overrides[authenticated] = lambda: Identity("owner", workspace, "owner")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                projection = (await client.get(f"/api/core/runs/{run}")).json()
                question = next(t for t in projection["tasks"] if t["kind"] == "ask_user")
                assert "handle" not in str(projection) and "mask" not in str(projection)
                url = f"/api/core/tasks/{question['id']}/answer"
                body = {"option_id": "0", "request_key": "choice"}
                app.dependency_overrides[authenticated] = lambda: Identity("other", workspace, "owner")
                assert (await client.post(url, json=body)).status_code == 404
                app.dependency_overrides[authenticated] = lambda: Identity("owner", workspace, "owner")
                assert (await client.post(url, json={**body, "option_id": "forged-mask"})).status_code == 409
                assert (await client.post(url, json=body)).status_code == 200
                assert (await client.post(url, json=body)).status_code == 200
            assert await drive() == "completed"
    asyncio.run(test())


@pytest.mark.parametrize("choice,expected", [("confirm", "completed"), ("cancel", "cancelled")])
def test_general_plan_confirmation(tmp_path, choice, expected):
    async def test():
        async with fixture(tmp_path, planner="chain", mode="plan") as (factory, _, drive, run, workspace, _, _, fake, *_):
            assert await drive() == "paused"
            assert fake.counts()["submissions"] == 0
            async with factory.begin() as tx:
                question = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run, CoreTask.kind == "ask_user"))
                await answer(tx, task_id=question.id, option_id=choice, request_key="confirm", workspace_id=workspace, user_id="owner")
            assert await drive() == expected
            if choice == "cancel":
                assert fake.counts()["submissions"] == 0
    asyncio.run(test())


def test_placeholders_are_program_owned_and_replaced(tmp_path):
    async def test():
        from backend.core.execution.projection import project_pending
        async with fixture(tmp_path, planner="chain") as (factory, supervisor, drive, run, _, _, _, _, events):
            await events()
            await supervisor.tick()
            await supervisor.tick()
            await project_pending(factory, 100)
            async with factory() as tx:
                session = await tx.get(CoreRun, run)
                from backend.core.orm import CoreSession
                session = await tx.get(CoreSession, session.session_id)
                doc_id = session.canvas_id
                doc = await tx.get(Document, doc_id)
                assert len([n for n in doc.data["objects"].values() if n.get("corePlaceholder")]) == 2
            assert await drive() == "completed"
            async with factory() as tx:
                doc = await tx.get(Document, doc_id)
                assert len(doc.data["objects"]) == 3
                assert not any(n.get("corePlaceholder") or n.get("busy") for k,n in doc.data["objects"].items() if k != "source")
    asyncio.run(test())
