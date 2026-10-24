"""Exercise real SQL replay and the SSE iterator, without a browser or paid APIs."""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import pytest

from test_session_conversation_pg import fixture
from backend.core.orm import CoreSession
from backend.core.session_stream import install
from backend.core.sessions.resolve import resolve_session
from backend.core.sessions.store import append_event
from backend.services.identity import Identity

pytestmark = pytest.mark.skipif(not (os.environ.get("AIVERO_CORE_TEST_DB") or os.environ.get("AIVERO_DURABLE_TEST_DB")),
    reason="explicit isolated migrated PostgreSQL required")


async def prepared(factory, workspace, canvas):
    async with factory.begin() as tx:
        session, _ = await resolve_session(tx, workspace_id=workspace, user_id="stream", document_id=canvas)
        await append_event(tx, session, event_type="test")
        ident = session.id
    app = FastAPI()
    app.state.factory = factory
    install(app)
    endpoint = next(route.endpoint for route in app.routes if route.path.endswith("/{session_id}/events"))
    return ident, endpoint


def test_snapshot_watermark_then_live_replay_has_no_gap(monkeypatch):
    monkeypatch.setenv("AIVERO_SESSION_STREAM_REFRESH_SECONDS", "0.001")
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            session_id, endpoint = await prepared(factory, workspace, canvas)
            identity = Identity("stream", workspace, "owner")
            monkeypatch.setattr("backend.core.session_stream.authenticated", AsyncMock(return_value=identity))
            request = SimpleNamespace(query_params={}, headers={}, is_disconnected=AsyncMock(return_value=False))
            response = await endpoint(session_id, request, identity)
            iterator = response.body_iterator
            assert (await anext(iterator)).startswith("retry:")
            first = await anext(iterator)
            assert "event: snapshot" in first
            body = json.loads(first.split("data: ", 1)[1])
            cursor = body["cursor"]
            async with factory.begin() as tx:
                session = await tx.get(CoreSession, session_id, with_for_update=True)
                await append_event(tx, session, event_type="after_snapshot")
            assert (await anext(iterator)).startswith(": keepalive")
            next_event = await anext(iterator)
            assert next_event.startswith(f"id: {cursor + 1}\nevent: session")
            await iterator.aclose()
    asyncio.run(scenario())


def test_two_clients_replay_same_database_order_and_reconnect_cursor(monkeypatch):
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            session_id, endpoint = await prepared(factory, workspace, canvas)
            async with factory.begin() as tx:
                session = await tx.get(CoreSession, session_id, with_for_update=True)
                await append_event(tx, session, event_type="second")
            identity = Identity("stream", workspace, "owner")
            monkeypatch.setattr("backend.core.session_stream.authenticated", AsyncMock(return_value=identity))
            async def read(after, last=None):
                request = SimpleNamespace(query_params={"after": str(after)},
                    headers={"last-event-id": str(last)} if last else {},
                    is_disconnected=AsyncMock(return_value=False))
                response = await endpoint(session_id, request, identity)
                iterator = response.body_iterator
                await anext(iterator)
                first = await anext(iterator)
                await iterator.aclose()
                return first
            left, right = await asyncio.gather(read(0), read(0))
            assert left == right
            resumed = await read(0, 1)
            assert resumed.startswith("id: 2\nevent: session")
    asyncio.run(scenario())
