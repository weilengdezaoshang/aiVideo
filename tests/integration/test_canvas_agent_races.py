"""Adversarial deletion and transaction-order tests; models are local scripts."""
import asyncio
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.agent.orm import AgentEvent, AgentModelCall, AgentRun, AgentSession, AgentToolCall
from backend.agent.runtime import now
from backend.infrastructure.orm import AssetReference, Document
from test_canvas_agent_runtime import DB, ROUTE, ScriptedModel, answer, body, fixture, invocation

pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL URL required")


@pytest.mark.parametrize("before_delete", ["running", "needs_attention"])
def test_delete_inflight_run_archives_cancel_resistant_late_response(tmp_path, before_delete):
    async def scenario():
        engine, factory, service, ws, doc, asset = await fixture(tmp_path)
        started, cancelled, release_late = asyncio.Event(), asyncio.Event(), asyncio.Event()
        execution = None

        async def cancellation_resistant_provider():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release_late.wait()
                # The upstream completed despite best-effort local cancellation.
                return answer(calls=[invocation("inspect_image", {"assetId": str(asset)})])

        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(cancellation_resistant_provider)
            execution = asyncio.create_task(service.advance(ident, "old-worker", model, media_root=tmp_path))
            await asyncio.wait_for(started.wait(), 2)
            if before_delete == "needs_attention":
                async with factory.begin() as db:
                    (await db.get(AgentRun, ident)).lease_until = now() - timedelta(seconds=1)
                await service.recover_due()
            assert (await service.get_run(ws, ident))["status"] == before_delete
            async with factory.begin() as db:
                document = await db.get(Document, doc, with_for_update=True)
                await service.close_document(db, doc)
                await db.delete(document)
            await asyncio.wait_for(cancelled.wait(), 3)
            release_late.set()
            await asyncio.wait_for(execution, 3)
            assert model.calls == 1
            assert (await service.advance(ident, "replacement", model, media_root=tmp_path))["skipped"]
            async with factory() as db:
                run = await db.get(AgentRun, ident)
                session = await db.get(AgentSession, run.session_id)
                assert run.status == ("stopped" if before_delete == "running" else "needs_attention")
                assert session.document_id is None and session.source_document_id == doc and session.closed_at
                assert await db.scalar(select(AssetReference.id).where(AssetReference.asset_id == asset,
                    AssetReference.owner_type == "agent_run", AssetReference.owner_id == str(ident))) is not None
                assert await db.scalar(select(func.count()).select_from(AgentToolCall).where(AgentToolCall.run_id == ident)) == 0
                call = await db.scalar(select(AgentModelCall).where(AgentModelCall.run_id == ident))
                assert call.status == "late"
                assert call.raw_response == {"test": "mock"}
                assert call.usage == {"total_tokens": 7}
                assert call.request_id == "mock-request"
                assert call.response["tool_calls"][0]["function"]["name"] == "inspect_image"
                assert call.finished_at is not None
        finally:
            release_late.set()
            if execution is not None and not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            await engine.dispose()
    asyncio.run(scenario())


def test_session_sequence_is_commit_order_and_snapshot_waits_for_transaction():
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        locked, commit_a = asyncio.Event(), asyncio.Event()
        stop_pid = asyncio.Future()
        tasks = []
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])

            async def transaction_a():
                async with factory.begin() as db:
                    session, run = await service._lock_run(db, ident, ws)
                    run.status = "running"
                    await service._event(db, session, run, "test.uncommitted")
                    await db.flush()
                    locked.set()
                    await commit_a.wait()

            async def transaction_b_stop():
                async with factory.begin() as db:
                    stop_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
                    session, run = await service._lock_run(db, ident, ws)
                    return await service._stop(db, session, run)

            a = asyncio.create_task(transaction_a())
            tasks.append(a)
            await locked.wait()
            b = asyncio.create_task(transaction_b_stop())
            tasks.append(b)
            pid = await stop_pid
            # Verify a PostgreSQL lock wait, rather than assuming task scheduling.
            for _ in range(100):
                async with factory() as db:
                    wait = await db.scalar(text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid})
                if wait == "Lock":
                    break
                await asyncio.sleep(.01)
            else:
                raise AssertionError("Transaction B did not wait for A's Session lock")
            snapshot_task = asyncio.create_task(service.snapshot(ws, doc))
            tasks.append(snapshot_task)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(snapshot_task), .05)
            assert not b.done()
            async with factory() as db:
                run = await db.get(AgentRun, ident)
                assert run.status == "queued"
                visible = list(await db.scalars(select(AgentEvent.seq).where(AgentEvent.session_id == run.session_id)))
                assert visible == [1]
            commit_a.set()
            await a
            stopped, snapshot = await asyncio.gather(b, snapshot_task)
            assert stopped["status"] == "stopped" and stopped["stateVersion"] == 3
            events = (await service.events(ws, doc))["events"]
            assert [event["seq"] for event in events] == [1, 2, 3]
            assert [event["type"] for event in events] == ["run.created", "test.uncommitted", "run.stopped"]
            # A snapshot is one committed event prefix, regardless of lock wake order.
            assert snapshot["runs"][0] == events[snapshot["cursor"] - 1]["run"]
            final = await service.snapshot(ws, doc)
            assert final["cursor"] == 3 and final["runs"][0] == stopped
        finally:
            commit_a.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.dispose()
    asyncio.run(scenario())
