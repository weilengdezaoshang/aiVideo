"""Opt-in durable projection: crash boundaries are the encompassing SQL transaction."""
import asyncio
import os
import uuid

import pytest
from sqlalchemy import func, select

from test_session_conversation_pg import fixture
from backend.core.contracts import CoreSettings, RunInput
from backend.core.orm import CoreSession, CoreSessionEvent, CoreSessionMessage, CoreSessionNotification
from backend.core.session_projection import project_session_once
from backend.core.session_stream import snapshot
from backend.core.sessions.resolve import resolve_session
from backend.core.store import create_run, transition

pytestmark = pytest.mark.skipif(not (os.environ.get("AIVERO_CORE_TEST_DB") or os.environ.get("AIVERO_DURABLE_TEST_DB")),
    reason="explicit isolated migrated PostgreSQL required")


def test_projection_replays_once_and_persists_completion_notification(monkeypatch):
    monkeypatch.setenv("AIVERO_SESSION_SLOW_TASK_NOTIFY_SECONDS", "0")
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            async with factory.begin() as tx:
                session, _ = await resolve_session(tx, workspace_id=workspace, user_id="projection", document_id=canvas)
                session_id = session.id
                run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace,
                    user_id="projection", request_key=uuid.uuid4().hex, request_hash="f" * 64,
                    snapshot={}, scope={}, invariants={}, version_lock={}), CoreSettings())
                for state in ("planning", "running", "completed"):
                    await transition(tx, "run", run.id, run.version, state)
            # This test owns a fresh DB namespace but may share the database with earlier cases.
            while await project_session_once(factory, limit=100):
                pass
            async with factory.begin() as tx:
                session = await tx.get(CoreSession, session_id, with_for_update=True)
                recovered = await snapshot(tx, session)
                cursor = recovered["cursor"]
                assert recovered["runs"][0]["status"] == "completed"
                events = list(await tx.scalars(select(CoreSessionEvent).where(
                    CoreSessionEvent.session_id == session_id).order_by(CoreSessionEvent.sequence)))
                assert [row.sequence for row in events] == list(range(1, cursor + 1))
                assert len({row.event_id for row in events}) == len(events)
                assert await tx.scalar(select(func.count()).select_from(CoreSessionNotification).where(
                    CoreSessionNotification.session_id == session_id)) == 1
            assert await project_session_once(factory, limit=100) is False
            async with factory() as tx:
                assert (await tx.get(CoreSession, session_id)).event_cursor == cursor
    asyncio.run(scenario())


def test_summary_is_durable_and_keeps_recent_originals(monkeypatch):
    monkeypatch.setenv("AIVERO_SESSION_SUMMARY_THRESHOLD_CHARS", "10")
    monkeypatch.setenv("AIVERO_SESSION_SUMMARY_KEEP_RECENT_MESSAGES", "1")
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            async with factory.begin() as tx:
                session, _ = await resolve_session(tx, workspace_id=workspace, user_id="summary", document_id=canvas)
                session_id = session.id
                run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace,
                    user_id="summary", request_key=uuid.uuid4().hex, request_hash="a" * 64,
                    snapshot={}, scope={}, invariants={}, version_lock={}), CoreSettings())
                for seq, text in enumerate(("角色服装保持蓝色", "已经确认保留服装", "现在改背景"), 1):
                    tx.add(CoreSessionMessage(session_id=session.id, sequence=seq, request_key=str(seq),
                        role="user", content={"text": text}, processing_status="processed", run_id=run.id))
                session.message_cursor = 3
                for state in ("planning", "failed"):
                    await transition(tx, "run", run.id, run.version, state)
            while await project_session_once(factory, limit=100):
                pass
            async with factory() as tx:
                session = await tx.get(CoreSession, session_id)
                assert session.summary_cursor == 2
                assert "角色服装保持蓝色" in session.summary
                assert "现在改背景" not in session.summary
                assert await tx.scalar(select(func.count()).select_from(CoreSessionMessage).where(
                    CoreSessionMessage.session_id == session_id)) == 3
    asyncio.run(scenario())
