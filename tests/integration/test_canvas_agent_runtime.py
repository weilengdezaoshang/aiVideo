"""Opt-in Agent persistence tests against an explicitly migrated isolated PG.

Every model response is scripted; no paid provider credentials are loaded.
"""
import asyncio
from datetime import timedelta
import hashlib
import io
import json
import os
import threading
import uuid

from fastapi import HTTPException
from PIL import Image
import pytest
from sqlalchemy import func, select

from backend.agent.config import AgentRoute
from backend.agent.orm import AgentEvent, AgentModelCall, AgentRun, AgentSession
from backend.agent.providers import ModelCallError, ModelResponse
from backend.agent.runtime import AgentService, now
from backend.agent import runtime as runtime_module
from backend.agent import tools
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, AssetReference, Document, Workspace

DB = os.environ.get("AIVERO_DURABLE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL URL required")
ROUTE = AgentRoute(id="test", label="Test", provider="deepseek", model="test-model",
    baseUrl="https://mock.invalid/v1", credentialRef="not-a-real-credential")


def answer(text="完成", calls=None, finish=None):
    calls = calls or []
    return ModelResponse(None if calls else text, calls, {"total_tokens": 7},
        finish or ("tool_calls" if calls else "stop"), {"test": "mock"}, "mock-request")


def invocation(name="read_canvas", args=None, ident="c1"):
    args = args or {}
    return {"id": ident, "name": name, "arguments": args, "raw_arguments": json.dumps(args)}


class ScriptedModel:
    def __init__(self, *script):
        self.script, self.calls, self.inputs = list(script), 0, []

    async def complete(self, route, messages, schemas):
        self.inputs.append(messages)
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return await step() if callable(step) else step


async def fixture(tmp_path=None):
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    workspace, document, asset = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    objects = {"title": {"id": "title", "kind": "text", "x": 0, "y": 0,
        "width": 100, "height": 30, "text": "画布标题", "maskDraft": "must-not-copy"}}
    media = None
    if tmp_path:
        buffer = io.BytesIO()
        Image.new("RGB", (32, 24), "blue").save(buffer, "PNG")
        media = buffer.getvalue()
        (tmp_path / "image.png").write_bytes(media)
        objects["photo"] = {"id": "photo", "kind": "image", "x": 10, "y": 50,
            "width": 320, "height": 240, "assetId": str(asset), "src": "secret-url"}
    async with factory.begin() as db:
        db.add(Workspace(id=workspace, name="Agent test"))
        await db.flush()
        db.add(Document(id=document, workspace_id=workspace, name="Canvas", revision=1,
            data={"objects": objects, "order": list(objects), "chat": [{"text": "old UI chat"}]}))
        if media:
            db.add(Asset(id=asset, workspace_id=workspace, kind="image", ext="png", width=999,
                height=999, bytes=len(media), sha256=hashlib.sha256(media).hexdigest(), storage_key="image.png"))
    return engine, factory, AgentService(factory), workspace, document, asset


def body(**changes):
    return {"clientRequestId": uuid.uuid4().hex, "message": "查看画布", "modelId": "test",
        "baseRevision": 1, "selectedObjectIds": ["title"], **changes}


async def run_to_end(service, workspace, ident, model, tmp_path):
    for _ in range(30):
        value = await service.get_run(workspace, ident)
        if value["status"] not in {"queued", "running"}:
            return value
        await service.advance(ident, uuid.uuid4().hex, model, media_root=tmp_path)
    raise AssertionError("Agent did not finish within bounded test phases")


def test_model_tools_final_receipts_and_frozen_real_metadata(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, asset = await fixture(tmp_path)
        try:
            created = await service.create_run(ws, doc, body(), ROUTE)
            ident = uuid.UUID(created["run"]["id"])
            model = ScriptedModel(answer(calls=[invocation(), invocation("inspect_image", {"assetId": str(asset)}, "c2")]), answer("图片为 32×24。"))
            # A later edit cannot replace the Run's input snapshot.
            async with factory.begin() as db:
                row = await db.get(Document, doc)
                row.data, row.revision = {"objects": {}, "order": []}, 2
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed" and model.calls == 2
            assert result["baseRevision"] == 1 and result["toolCallCount"] == 2
            observed = result["toolCalls"][1]["output"]["result"]["data"]
            assert (observed["width"], observed["height"]) == (32, 24)
            assert [c["evidenceId"] for c in result["toolCalls"]] == ["E1", "E2"]
            assert [m["role"] for m in model.inputs[1]][-3:] == ["assistant", "tool", "tool"]
            first = json.loads(model.inputs[1][-2]["content"])
            assert first["status"] == "ok" and first["trust"] == "untrusted_canvas_data"
            assert first["source"]["revision"] == 1 and first["result"]["data"]["revision"] == 1
            async with factory() as db:
                run = await db.get(AgentRun, ident)
                encoded = json.dumps(run.snapshot)
                assert "maskDraft" not in encoded
                assert "secret-url" not in encoded and "old UI chat" not in encoded
                assert (await db.get(Document, doc)).revision == 2
                assert await db.scalar(select(func.count()).select_from(AssetReference).where(
                    AssetReference.owner_type == "agent_run", AssetReference.owner_id == str(ident))) == 1
                calls = list(await db.scalars(select(AgentModelCall).where(AgentModelCall.run_id == ident)))
                assert all(c.raw_response == {"test": "mock"} and c.usage["total_tokens"] == 7 for c in calls)
            snapshot = await service.snapshot(ws, doc, created["run"]["clientRequestId"])
            events = await service.events(ws, doc)
            assert [e["seq"] for e in events["events"]] == list(range(1, snapshot["cursor"] + 1))
            assert events["events"][-1]["run"] == result
            assert snapshot["requestedRun"] == result
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_admission_idempotency_active_run_scope_revision_and_missing_route():
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            payload = body()
            responses = await asyncio.gather(*(service.create_run(ws, doc, payload, ROUTE) for _ in range(6)))
            assert len({x["run"]["id"] for x in responses}) == 1
            assert sum(not x["replayed"] for x in responses) == 1
            with pytest.raises(HTTPException) as conflict:
                await service.create_run(ws, doc, body(), ROUTE)
            assert conflict.value.status_code == 409
            with pytest.raises(HTTPException):
                await service.create_run(ws, doc, {**payload, "message": "changed"}, ROUTE)
            async with factory.begin() as db:
                (await db.get(Document, doc)).revision = 2
            assert (await service.create_run(ws, doc, payload, None))["replayed"]
            ident = uuid.UUID(responses[0]["run"]["id"])
            with pytest.raises(HTTPException) as denied:
                await service.stop_run(uuid.uuid4(), ident)
            assert denied.value.status_code == 404
            await service.stop_run(ws, ident)
            with pytest.raises(HTTPException) as stale:
                await service.create_run(ws, doc, body(), ROUTE)
            assert stale.value.status_code == 409
            with pytest.raises(HTTPException) as unavailable:
                await service.create_run(ws, doc, body(baseRevision=2), None)
            assert unavailable.value.status_code == 503
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_stopped_run_does_not_call_model_and_inflight_is_cancelled(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            await service.stop_run(ws, ident)
            untouched = ScriptedModel()
            assert (await service.advance(ident, "owner", untouched, media_root=tmp_path))["skipped"]
            assert untouched.calls == 0
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            started, cancelled = asyncio.Event(), asyncio.Event()

            async def slow():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            model = ScriptedModel(slow)
            execution = asyncio.create_task(service.advance(ident, "owner", model, media_root=tmp_path))
            await asyncio.wait_for(started.wait(), 2)
            await service.stop_run(ws, ident)
            await asyncio.wait_for(execution, 3)
            result = await service.get_run(ws, ident)
            assert cancelled.is_set() and result["status"] == "stopped"
            assert result["modelCalls"][0]["status"] == "unknown"
            assert result["toolCalls"] == []
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_lost_model_sender_never_retries_but_lost_read_tool_can_resume(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            await service._prepare(ident, "dead-worker")
            async with factory.begin() as db:
                (await db.get(AgentRun, ident)).lease_until = now() - timedelta(seconds=1)
            await service.recover_due()
            model = ScriptedModel()
            assert (await service.get_run(ws, ident))["status"] == "needs_attention"
            assert (await service.advance(ident, "replacement", model, media_root=tmp_path))["skipped"]
            assert model.calls == 0
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(answer(calls=[invocation()]), answer())
            await service.advance(ident, "worker", model, media_root=tmp_path)
            old = await service._prepare(ident, "dead-tool-worker")
            assert old["kind"] == "tool"
            async with factory.begin() as db:
                (await db.get(AgentRun, ident)).lease_until = now() - timedelta(seconds=1)
            await service.recover_due()
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed" and result["toolCallCount"] == 1
            assert model.calls == 2
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_expired_model_response_before_scanner_does_not_resend(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])

            async def late():
                async with factory.begin() as db:
                    (await db.get(AgentRun, ident)).lease_until = now() - timedelta(seconds=1)
                return answer(calls=[invocation()])

            model = ScriptedModel(late)
            await service.advance(ident, "worker", model, media_root=tmp_path)
            await service.recover_due()
            assert (await service.get_run(ws, ident))["status"] == "needs_attention"
            assert (await service.advance(ident, "replacement", model, media_root=tmp_path))["skipped"]
            assert model.calls == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_limits_reject_entire_tool_batch_and_bound_model_loop(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(answer(calls=[invocation(ident=str(i)) for i in range(13)]))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "limit_reached" and result["toolCalls"] == []
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(*(answer(calls=[invocation(ident=str(i))]) for i in range(8)))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "limit_reached" and model.calls == 8
            assert result["toolCallCount"] == 8
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_protocol_error_receipt_and_truncation_are_not_success(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(ModelCallError("INVALID_MODEL_RESPONSE", "bad response",
                usage={"total_tokens": 9}, raw_response={"usage": {"total_tokens": 9}}, request_id="r1"))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "failed" and result["modelCalls"][0]["usage"]["total_tokens"] == 9
            async with factory() as db:
                call = await db.scalar(select(AgentModelCall).where(AgentModelCall.run_id == ident))
                assert call.request_id == "r1" and call.raw_response["usage"]["total_tokens"] == 9
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            result = await run_to_end(service, ws, ident, ScriptedModel(answer("partial", finish="length")), tmp_path)
            assert result["status"] == "limit_reached" and result["reply"] is None
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_deletion_closes_session_preserves_receipts_and_pins(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, asset = await fixture(tmp_path)
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            async with factory.begin() as db:
                document = await db.get(Document, doc, with_for_update=True)
                await service.close_document(db, doc)
                await db.delete(document)
            async with factory() as db:
                session = await db.scalar(select(AgentSession).where(AgentSession.source_document_id == doc))
                assert session.document_id is None and session.closed_at is not None
                assert (await db.get(AgentRun, ident)).status == "stopped"
                assert await db.scalar(select(AssetReference.id).where(AssetReference.asset_id == asset,
                    AssetReference.owner_type == "agent_run")) is not None
                assert await db.scalar(select(func.count()).select_from(AgentEvent).where(AgentEvent.session_id == session.id)) == 2
            with pytest.raises(HTTPException):
                await service.snapshot(ws, doc)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_unknown_tool_is_a_tool_receipt_not_a_model_claim(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(answer(calls=[invocation("write_canvas")]), answer("当前工具不支持写入"))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["toolCalls"][0]["status"] == "failed"
            envelope = result["toolCalls"][0]["output"]
            assert envelope["status"] == "error" and envelope["error"]["code"] == "UNKNOWN_TOOL"
            assert envelope["error"]["retryable"] is False
            assert "read_canvas" in envelope["error"]["details"]["allowedTools"]
            assert json.loads(model.inputs[-1][-1]["content"])["error"]["code"] == "UNKNOWN_TOOL"
            async with factory() as db:
                assert (await db.get(Document, doc)).revision == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_stopping_native_tool_waits_for_real_thread_and_never_continues(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        started, release = threading.Event(), threading.Event()

        class BlockingTools:
            tool_schemas = staticmethod(tools.tool_schemas)

            @staticmethod
            def execute_envelope(*args, **kwargs):
                started.set()
                assert release.wait(5)
                return {"measured": True}
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(answer(calls=[invocation()]))
            await service.advance(ident, "worker", model, media_root=tmp_path)
            task = asyncio.create_task(service.advance(ident, "tool-worker", model,
                BlockingTools, tmp_path))
            assert await asyncio.to_thread(started.wait, 2)
            await service.stop_run(ws, ident)
            assert not task.done()
            release.set()
            await task
            result = await service.get_run(ws, ident)
            assert result["status"] == "stopped" and result["toolCalls"][0]["status"] == "late"
            assert model.calls == 1
        finally:
            release.set()
            await engine.dispose()
    asyncio.run(scenario())


def test_event_pagination_never_skips_and_snapshots_cover_a_committed_prefix():
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            for _ in range(51):
                created = await service.create_run(ws, doc, body(), ROUTE)
                await service.stop_run(ws, uuid.UUID(created["run"]["id"]))
            first = await service.events(ws, doc)
            second = await service.events(ws, doc, first["cursor"])
            assert len(first["events"]) == 100 and first["cursor"] == 100
            assert [e["seq"] for e in second["events"]] == [101, 102]
            assert second["cursor"] == 102
            snapshot = await service.snapshot(ws, doc)
            assert snapshot["cursor"] == 102 and len(snapshot["runs"]) == 50
            assert snapshot["runs"][-1] == second["events"][-1]["run"]
            assert (await service.events(ws, doc, 102))["events"] == []
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_history_is_ten_final_pairs_and_context_overflow_does_not_call_model(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            for index in range(11):
                created = await service.create_run(ws, doc, body(message=f"问题{index}"), ROUTE)
                ident = uuid.UUID(created["run"]["id"])
                await run_to_end(service, ws, ident, ScriptedModel(answer(f"回答{index}")), tmp_path)
            created = await service.create_run(ws, doc, body(message="最新问题"), ROUTE)
            ident = uuid.UUID(created["run"]["id"])
            async with factory() as db:
                run = await db.get(AgentRun, ident)
                assert len(run.history) == 20
                assert run.history[0]["content"] == "问题1"
                assert all(message["role"] in {"user", "assistant"} for message in run.history)
            await service.stop_run(ws, ident)
            limited = ROUTE.model_copy(update={"context_max_bytes": 4096})
            created = await service.create_run(ws, doc, body(message="长" * 3000), limited)
            ident = uuid.UUID(created["run"]["id"])
            model = ScriptedModel()
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "limit_reached" and model.calls == 0
            assert result["modelCallCount"] == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_duplicate_claims_and_snapshot_read_during_model_are_consistent(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        ready, release = asyncio.Event(), asyncio.Event()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])

            async def blocked():
                ready.set()
                await release.wait()
                return answer()

            model = ScriptedModel(blocked)
            task = asyncio.create_task(service.advance(ident, "first", model, media_root=tmp_path))
            await ready.wait()
            competing, snap, event_page = await asyncio.gather(
                service.advance(ident, "second", model, media_root=tmp_path),
                service.snapshot(ws, doc), service.events(ws, doc))
            assert competing == {"skipped": True} and model.calls == 1
            assert snap["cursor"] == event_page["cursor"] == 2
            assert snap["runs"][0] == event_page["events"][-1]["run"]
            assert snap["runs"][0]["modelCalls"][0]["status"] == "sending"
            release.set()
            await task
            assert (await service.get_run(ws, ident))["status"] == "completed"
        finally:
            release.set()
            await engine.dispose()
    asyncio.run(scenario())


def test_tool_timeout_keeps_native_thread_and_heartbeat_until_actual_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "TOOL_TIMEOUT_S", .02)
    monkeypatch.setattr(runtime_module, "TOOL_HEARTBEAT_S", .01)

    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        started, release = threading.Event(), threading.Event()

        class SlowTools:
            tool_schemas = staticmethod(tools.tool_schemas)

            @staticmethod
            def execute_envelope(*args, **kwargs):
                started.set()
                assert release.wait(5)
                return {"done": True}
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(answer(calls=[invocation()]))
            await service.advance(ident, "first", model, media_root=tmp_path)
            task = asyncio.create_task(service.advance(ident, "tool", model, SlowTools, tmp_path))
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.sleep(.06)
            assert not task.done()
            async with factory() as db:
                run = await db.get(AgentRun, ident)
                assert run.lease_owner == "tool" and run.lease_until > now()
            assert (await service.advance(ident, "duplicate", model, media_root=tmp_path))["skipped"]
            release.set()
            await task
            result = await service.get_run(ws, ident)
            assert result["toolCalls"][0]["status"] == "failed"
            assert result["modelCallCount"] == 1
            await service.stop_run(ws, ident)
        finally:
            release.set()
            await engine.dispose()
    asyncio.run(scenario())
