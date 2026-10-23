"""Read-only worker path on an explicit disposable Postgres. Providers stay fake."""
import asyncio
import hashlib
import io
import os
import uuid

import pytest
from cryptography.fernet import Fernet
from PIL import Image
from sqlalchemy import func, select

from backend.agent.media.credits import grant
from backend.agent.orm import HostCreditLedger
from backend.core.contracts import CoreSettings, RunInput, TaskInput
from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.core.events.contracts import EventSettings
from backend.core.events.processor import Processor
from backend.core.events.registry import Registry
from backend.core.events.store import accept
from backend.core.execution.contracts import WorkerSettings
from backend.core.execution.engine import Worker, enqueue, record_admission, scan_ready
from backend.core.execution.orm import CanvasWrite
from backend.core.orm import CoreEvent, CorePlan, CoreRun, CoreSession, CoreTask, CoreTaskDependency
from backend.core.store import create_run, create_task, transition
from backend.core.supervisor.engine import Supervisor, register
from backend.core.supervisor.orm import Invocation
from backend.core.supervisor.planners import FakePlanner
from backend.core.supervisor.service import start
from backend.core.tools import CatalogSnapshot, ToolCatalog, build_catalog
from backend.core.tools.store import publish
from backend.evaluation.reliability.infra import infrastructure
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, AssetReference, Document, Job, Workspace


def _catalog(schema=None):
    """Real catalog definitions. A schema override only tightens one result contract."""
    base = build_catalog(Catalog(CatalogConfig(version="test", environment="development", enabled=True, capabilities=[])), "read-test")
    tools = []
    for tool in base.snapshot.tools:
        if schema is not None and tool.name == "read_canvas":
            tools.append(tool.model_copy(update={"result_schema": schema}))
        else:
            tools.append(tool)
    return ToolCatalog(CatalogSnapshot(version=uuid.uuid4().hex, tools=tools))


def _png(path):
    picture = Image.new("RGB", (8, 6), "red")
    stream = io.BytesIO()
    picture.save(stream, format="PNG")
    data = stream.getvalue()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


async def _seed(factory, tmp_path, *, schema=None, readable_extra=(), bad_key=False, read_max_bytes=65536, statuses=()):
    media = tmp_path / "media"
    content = _png(media / "ok.png")
    secret = b"SECRET_OUTSIDE_FILE"
    (tmp_path / "secret.png").write_bytes(secret)
    catalog = _catalog(schema)
    config = WorkerSettings(ticket_key=Fernet.generate_key().decode(), read_max_bytes=read_max_bytes, retry_jitter=0)
    async with factory.begin() as tx:
        workspace, other = uuid.uuid4(), uuid.uuid4()
        tx.add(Workspace(id=workspace))
        tx.add(Workspace(id=other))
        await tx.flush()
        doc = Document(workspace_id=workspace, name="画布", data={"objects": {}, "order": []})
        foreign = Document(workspace_id=other, name="别的", data={"objects": {"t": {"kind": "text", "text": "OTHER_WORKSPACE"}}})
        tx.add(doc)
        tx.add(foreign)
        await tx.flush()
        asset = Asset(workspace_id=workspace, kind="image", ext="png", width=8, height=6, bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(), storage_key="../secret.png" if bad_key else "ok.png")
        outsider = Asset(workspace_id=other, kind="image", ext="png", width=1, height=1, bytes=1,
            sha256="ab" * 32, storage_key="nope.png")
        tx.add(asset)
        tx.add(outsider)
        await tx.flush()
        tx.add(AssetReference(asset_id=asset.id, owner_type="document", owner_id=str(doc.id)))
        blocked = {}
        for status in statuses:
            row = Asset(workspace_id=workspace, kind="image", ext="png", width=8, height=6, bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(), storage_key="ok.png",
                details={"status": status} if status != "hostBlocked" else {"hostBlocked": True})
            tx.add(row)
            await tx.flush()
            tx.add(AssetReference(asset_id=row.id, owner_type="document", owner_id=str(doc.id)))
            blocked[status] = row.id
        doc.data = {"objects": {
            "ok": {"kind": "image", "x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0, "assetId": str(asset.id)},
            "note": {"kind": "text", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0, "text": "SECRET_TEXT"},
            "hidden": {"kind": "image", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0, "assetId": str(outsider.id),
                       "text": "OTHER_WORKSPACE"},
        }, "order": ["note", "hidden", "ok"], "generations": {"g": {"prompt": "SECRET_PROMPT"}}}
        await grant(tx, workspace, uuid.uuid4().hex, 50, "test")
        await publish(tx, catalog, published_by="test")
        session = CoreSession(workspace_id=workspace, source_canvas_id=doc.id, canvas_id=doc.id, user_id="test", language="zh-CN")
        tx.add(session)
        await tx.flush()
        readable = [str(asset.id), str(outsider.id), *(str(value) for value in blocked.values()), *readable_extra]
        run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace, user_id="test",
            request_key=uuid.uuid4().hex, request_hash="a" * 64, snapshot={},
            scope={"tools": ["read_canvas@1", "read_asset@1", "inspect_image@1"], "readable_assets": readable, "write_canvas": False},
            invariants={}, version_lock={"tool_catalog": catalog.lock.model_dump(), "supervisor": "rule-v1"}), CoreSettings())
        await transition(tx, "run", run.id, 0, "planning")
        await transition(tx, "run", run.id, 1, "running")
        plan = CorePlan(run_id=run.id, version=1, writer="rule", proposal={}, content_hash="b" * 64,
            rationale={}, link_constraints={}, result_type="generate", stage="final", milestones=[], self_check={}, expansion_rules={}, version_lock={})
        tx.add(plan)
        await tx.flush()
        run.current_plan_id = plan.id
        ids = {"asset": asset.id, "outsider": outsider.id, "doc": doc.id, "run": run.id, "plan": plan.id,
            "workspace": workspace, "other": other, "blocked": blocked}
        return config, media, ids, catalog


async def _queue(factory, ids, catalog, name, parameters, *, dependency="none"):
    async with factory.begin() as tx:
        run = await tx.get(CoreRun, ids["run"])
        task = await create_task(tx, TaskInput(run_id=run.id, plan_id=ids["plan"], plan_version=1, task_key=name + uuid.uuid4().hex[:8],
            kind="call_tool", tool_ref=name, tool_version="1", idempotency_key=uuid.uuid4().hex, parameters=parameters, scope={}))
        await transition(tx, "task", task.id, 0, "validated")
        await record_admission(tx, task, catalog, checks={"legal": True, "types": True, "intent": True}, review_ref="review", placement={})
        await transition(tx, "task", task.id, 1, "queued")
        if dependency in {"failed", "pending"}:
            upstream = await create_task(tx, TaskInput(run_id=run.id, plan_id=ids["plan"], plan_version=1, task_key="up" + uuid.uuid4().hex[:8],
                kind="call_tool", tool_ref="read_canvas", tool_version="1", idempotency_key=uuid.uuid4().hex, parameters={}, scope={}))
            await transition(tx, "task", upstream.id, 0, "validated")
            await transition(tx, "task", upstream.id, 1, "queued")
            if dependency == "failed":
                await transition(tx, "task", upstream.id, 2, "failed")
            tx.add(CoreTaskDependency(task_id=task.id, depends_on_id=upstream.id, run_id=run.id, plan_id=ids["plan"]))
        job_id = await enqueue(tx, task.id)
        assert (await tx.get(CoreTask, task.id)).status == "queued"
        assert (await tx.get(Job, job_id)).kind == "read"
        return task.id, job_id


def test_read_tools_filter_scope_settle_once_and_fail_closed(tmp_path, disposable_postgres):
    db = disposable_postgres

    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=db))
        factory = create_async_session_factory(engine)
        config, media, ids, catalog = await _seed(factory, tmp_path)
        worker = Worker(factory, media, None, settings=config)
        canvas_id, canvas_job = await _queue(factory, ids, catalog, "read_canvas", {})
        await worker.execute(canvas_job)
        await worker.execute(canvas_job)
        async with factory() as tx:
            task = await tx.get(CoreTask, canvas_id)
            assert task.status == "succeeded"
            dumped = str(task.output)
            assert "SECRET_TEXT" not in dumped and "SECRET_PROMPT" not in dumped and "OTHER_WORKSPACE" not in dumped
            assert task.output["evidenceId"] == "E1"
            assert task.output["result"]["data"]["objectCount"] == 1
            assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.core_task_id == canvas_id)) == 0
            assert await tx.scalar(select(func.count()).select_from(CanvasWrite).where(CanvasWrite.task_id == canvas_id)) == 0
            events = list(await tx.scalars(select(CoreEvent).where(CoreEvent.task_id == canvas_id, CoreEvent.event_type == "task_succeeded")))
            assert len(events) == 1 and events[0].causation_id is not None
            assert (await tx.scalar(select(Invocation).where(Invocation.event_id == events[0].id))) is None
            assert (await tx.get(Document, ids["doc"])).data["objects"]["note"]["text"] == "SECRET_TEXT"
        own_id, own_job = await _queue(factory, ids, catalog, "read_asset", {"asset_id": str(ids["asset"])})
        await worker.execute(own_job)
        asset_id, asset_job = await _queue(factory, ids, catalog, "read_asset", {"asset_id": str(ids["outsider"])})
        await worker.execute(asset_job)
        image_id, image_job = await _queue(factory, ids, catalog, "inspect_image", {"assetId": str(ids["asset"])})
        await worker.execute(image_job)
        blocked_id, blocked_job = await _queue(factory, ids, catalog, "read_canvas", {}, dependency="failed")
        await worker.execute(blocked_job)
        await scan_ready(factory, config)
        await scan_ready(factory, config)
        async with factory() as tx:
            owned = await tx.get(CoreTask, own_id)
            assert owned.status == "succeeded" and owned.output["result"]["data"]["asset_id"] == str(ids["asset"])
            denied = await tx.get(CoreTask, asset_id)
            assert denied.status == "failed" and denied.error["code"] == "param_invalid"
            image = await tx.get(CoreTask, image_id)
            assert image.status == "succeeded"
            assert image.output["result"]["data"]["width"] == 8
            assert "SECRET_OUTSIDE_FILE" not in str(image.output)
            blocked = await tx.get(CoreTask, blocked_id)
            assert blocked.status == "failed" and blocked.output is None
            assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.core_task_id == blocked_id)) == 0
            assert await tx.scalar(select(func.count()).select_from(Job).where(Job.id == blocked_job)) == 1
        pending_id, pending_job = await _queue(factory, ids, catalog, "read_canvas", {}, dependency="pending")
        await worker.execute(pending_job)
        await worker.execute(pending_job)
        await scan_ready(factory, config)
        async with factory() as tx:
            pending = await tx.get(CoreTask, pending_id)
            assert pending.status == "queued" and pending.output is None and pending.job_id == pending_job
            assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.core_task_id == pending_id)) == 0
            assert await tx.scalar(select(func.count()).select_from(Job).where(Job.params["coreTaskId"].as_string() == str(pending_id))) == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_inspect_rejects_path_escape(tmp_path, disposable_postgres):
    db = disposable_postgres

    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=db))
        factory = create_async_session_factory(engine)
        config, media, ids, catalog = await _seed(factory, tmp_path, bad_key=True)
        worker = Worker(factory, media, None, settings=config)
        task_id, job_id = await _queue(factory, ids, catalog, "inspect_image", {"assetId": str(ids["asset"])})
        await worker.execute(job_id)
        await worker.execute(job_id)
        async with factory() as tx:
            task = await tx.get(CoreTask, task_id)
            assert task.status == "failed" and task.error["code"] == "param_invalid"
            assert task.output is None or "SECRET_OUTSIDE_FILE" not in str(task.output)
            assert await tx.scalar(select(func.count()).select_from(CoreEvent).where(CoreEvent.task_id == task_id, CoreEvent.event_type == "task_failed")) == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_schema_mismatch_and_output_limit_fail_closed(tmp_path, disposable_postgres):
    db = disposable_postgres
    strict = {"type": "object", "additionalProperties": False, "required": ["not-an-envelope"],
        "properties": {"not-an-envelope": {"type": "string"}}}

    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=db))
        factory = create_async_session_factory(engine)
        config, media, ids, catalog = await _seed(factory, tmp_path, schema=strict)
        worker = Worker(factory, media, None, settings=config)
        mismatch_id, mismatch_job = await _queue(factory, ids, catalog, "read_canvas", {})
        await worker.execute(mismatch_job)
        await worker.execute(mismatch_job)
        small = WorkerSettings(ticket_key=config.ticket_key, read_max_bytes=32, retry_jitter=0)
        limit_id, limit_job = await _queue(factory, ids, catalog, "read_asset", {"asset_id": str(ids["asset"])})
        await Worker(factory, media, None, settings=small).execute(limit_job)
        extra_id, extra_job = await _queue(factory, ids, catalog, "read_asset",
            {"asset_id": str(ids["asset"]), "note": "unreviewed"})
        await worker.execute(extra_job)
        async with factory() as tx:
            for ident, job_id in ((mismatch_id, mismatch_job), (limit_id, limit_job), (extra_id, extra_job)):
                task = await tx.get(CoreTask, ident)
                assert task.status == "failed" and task.error["code"] == "param_invalid" and task.output is None
                assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.core_task_id == ident)) == 0
                assert await tx.scalar(select(func.count()).select_from(Job).where(Job.id == job_id)) == 1
                assert (await tx.get(Job, job_id)).status == "failed"
        await engine.dispose()

    asyncio.run(scenario())


def test_read_asset_status_and_workspace_fail_closed(tmp_path, disposable_postgres):
    db = disposable_postgres

    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=db))
        factory = create_async_session_factory(engine)
        config, media, ids, catalog = await _seed(factory, tmp_path, statuses=("reported", "taken_down", "hostBlocked"))
        worker = Worker(factory, media, None, settings=config)
        cases = [("reported", ids["blocked"]["reported"]), ("taken_down", ids["blocked"]["taken_down"]),
            ("hostBlocked", ids["blocked"]["hostBlocked"]), ("workspace", ids["outsider"])]
        for _name, asset_id in cases:
            task_id, job_id = await _queue(factory, ids, catalog, "read_asset", {"asset_id": str(asset_id)})
            await worker.execute(job_id)
            async with factory() as tx:
                task = await tx.get(CoreTask, task_id)
                assert task.status == "failed" and task.output is None
                assert "width" not in str(task.output)
                assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.core_task_id == task_id)) == 0
        import backend.agent.media.inspection as inspection
        original = inspection.managed_path

        def explode(root, asset):
            raise RuntimeError("inspection failed open")

        inspection.managed_path = explode
        try:
            task_id, job_id = await _queue(factory, ids, catalog, "inspect_image", {"assetId": str(ids["asset"])})
            with pytest.raises(RuntimeError, match="inspection failed open"):
                await worker.execute(job_id)
        finally:
            inspection.managed_path = original
        async with factory() as tx:
            task = await tx.get(CoreTask, task_id)
            assert task.status != "succeeded" and (task.output is None or task.output.get("error"))
        await engine.dispose()

    asyncio.run(scenario())


def test_supervisor_consumes_real_read_plan(tmp_path, disposable_postgres):
    db = disposable_postgres

    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=db))
        factory = create_async_session_factory(engine)
        media = tmp_path / "chain"
        content = _png(media / "ok.png")
        catalog = _catalog()
        config = WorkerSettings(ticket_key=Fernet.generate_key().decode(), read_max_bytes=65536, retry_jitter=0,
            poll_seconds=0.001, retry_base_seconds=0.001, retry_max_seconds=0.01)
        message = "只读检查画布"
        async with factory.begin() as tx:
            workspace = uuid.uuid4()
            tx.add(Workspace(id=workspace))
            await tx.flush()
            await grant(tx, workspace, uuid.uuid4().hex, 50, "test")
            asset = Asset(workspace_id=workspace, kind="image", ext="png", width=8, height=6, bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(), storage_key="ok.png")
            tx.add(asset)
            await tx.flush()
            doc = Document(workspace_id=workspace, name="画布", data={"objects": {
                "ok": {"kind": "image", "x": 1.0, "y": 2.0, "width": 8.0, "height": 6.0, "assetId": str(asset.id)},
                "note": {"kind": "text", "text": "SECRET_TEXT"},
            }, "order": ["note", "ok"]})
            tx.add(doc)
            await tx.flush()
            tx.add(AssetReference(asset_id=asset.id, owner_type="document", owner_id=str(doc.id)))
            await publish(tx, catalog, published_by="test")
            run = await start(tx, workspace_id=workspace, user_id="test", document_id=doc.id, request_key="read-chain",
                request={"message": message, "selected_assets": [str(asset.id)], "anchor_id": "ok"},
                budget=50, token_budget=100, max_rounds=4, mode="auto")
            run_id = run.id
        plan = {"goal": message, "writer": "rule", "result_type": "generate", "steps": [
            {"id": "t1", "tool": "read_canvas", "parameters": {}},
            {"id": "t2", "tool": "read_asset", "parameters": {"asset_id": str(asset.id)}},
            {"id": "t3", "tool": "inspect_image", "parameters": {"assetId": str(asset.id)}},
        ]}
        supervisor = Supervisor(factory, FakePlanner([{"plan": plan, "invariants": {}, "confidence": 1, "tokens": 1,
            "raw_output": {"plan": plan}}]))
        registry = Registry()
        register(registry)
        processor = Processor(factory, EventSettings(concurrency=1, backoff_base_seconds=0.001, backoff_max_seconds=0.01), registry)
        worker = Worker(factory, media, None, settings=config)

        async def events():
            async with factory.begin() as tx:
                for event in await tx.scalars(select(CoreEvent).where(CoreEvent.run_id == run_id).order_by(CoreEvent.run_sequence)):
                    await accept(tx, {"event_id": str(event.id), "run_id": str(run_id)})
            for _ in range(40):
                if not any(await processor.tick()):
                    break

        status = None
        for _ in range(40):
            await events()
            await supervisor.tick()
            await scan_ready(factory, config)
            async with factory() as tx:
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id, CoreTask.kind == "call_tool")))
            for task in tasks:
                if task.status == "queued" and task.job_id:
                    await worker.execute(task.job_id)
            async with factory() as tx:
                status = (await tx.get(CoreRun, run_id)).status
            if status == "completed":
                break
        assert status == "completed"
        async with factory() as tx:
            tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id, CoreTask.kind == "call_tool")))
            assert sorted(task.tool_ref for task in tasks) == ["inspect_image", "read_asset", "read_canvas"]
            assert {task.status for task in tasks} == {"succeeded"}
            dumped = " ".join(str(task.output) for task in tasks)
            assert "SECRET_TEXT" not in dumped
            assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.core_run_id == run_id)) == 0
            assert await tx.scalar(select(func.count()).select_from(CanvasWrite).where(CanvasWrite.task_id.in_([task.id for task in tasks]))) == 0
            event = await tx.scalar(select(CoreEvent).where(CoreEvent.run_id == run_id, CoreEvent.event_type == "task_succeeded").limit(1))
            event_count = await tx.scalar(select(func.count()).select_from(CoreEvent).where(CoreEvent.run_id == run_id))
            invocation_count = await tx.scalar(select(func.count()).select_from(Invocation).where(Invocation.event_id == event.id))
            replay_id = event.id
        async with factory.begin() as tx:
            await accept(tx, {"event_id": str(replay_id), "run_id": str(run_id)})
        assert not any(await processor.tick())
        await supervisor.tick()
        async with factory() as tx:
            assert (await tx.get(CoreRun, run_id)).status == "completed"
            assert await tx.scalar(select(func.count()).select_from(CoreEvent).where(CoreEvent.run_id == run_id)) == event_count
            assert await tx.scalar(select(func.count()).select_from(Invocation).where(Invocation.event_id == replay_id)) == invocation_count
            assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.core_run_id == run_id)) == 0
        await engine.dispose()

    asyncio.run(scenario())


@pytest.fixture(scope="module", autouse=True)
def disposable_postgres():
    """Reuse an explicit test database. Otherwise own a temporary container and restore env."""
    keys = ("AIVERO_DURABLE_TEST_DB", "AIVERO_EXECUTION_TEST_DB")
    saved = {key: os.environ.get(key) for key in keys}
    explicit = saved["AIVERO_DURABLE_TEST_DB"] or saved["AIVERO_EXECUTION_TEST_DB"]

    def restore():
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    try:
        if explicit:
            yield explicit
            return
        with infrastructure() as urls:
            url = urls["AIVERO_RELIABILITY_DB"]
            if not url:
                raise RuntimeError("disposable database URL missing")
            os.environ["AIVERO_DURABLE_TEST_DB"] = url
            os.environ["AIVERO_EXECUTION_TEST_DB"] = url
            try:
                yield url
            finally:
                restore()
    finally:
        restore()
