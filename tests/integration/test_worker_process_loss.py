"""SIGKILL in the uncertain submission window, against an isolated local upstream."""
import asyncio
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import update

from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Job
from backend.services.acceptance import accept_generation
from backend.services.execution import ExecutionStore, utcnow
from backend.services.generation_runner import execute_phase

DB = os.environ.get("AIVERO_DURABLE_TEST_DB")
REDIS = os.environ.get("AIVERO_DURABLE_TEST_REDIS")
pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="explicit isolated DB and Redis required")


def test_worker_killed_after_upstream_acceptance_never_resubmits(tmp_path):
    accepted, release = threading.Event(), threading.Event()
    calls = []
    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers["content-length"])))
            accepted.set()
            release.wait(20)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    reference = uuid.uuid4().hex
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({reference: {"imageApiKey": "local-fake-key"}}))
    process = None

    async def scenario():
        nonlocal process
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        redis = Redis.from_url(REDIS)
        try:
            result = await accept_generation(factory, workspace_id=uuid.uuid4(), request_id=uuid.uuid4().hex,
                request_hash="same", params={"prompt": "kill-window", "model": "local-test", "width": 64, "height": 64},
                provider_snapshot={"provider": "cloud", "cloudVendor": "openai", "cloudModel": "local-test",
                    "cloudBaseUrl": f"http://127.0.0.1:{server.server_port}", "credentialRef": reference})
            ident = result["jobId"]
            store = ExecutionStore(factory)
            claim = await store.claim(ident)
            await store.advance(claim, "submit", {})
            env = {**os.environ, "PYTHONPATH": str(Path.cwd() / "apps/api"), "AIVERO_DB_URL": DB,
                "AIVERO_REDIS_URL": REDIS, "SWARMUI_DATA_DIR": str(tmp_path / "media"),
                "AIVERO_CREDENTIALS_FILE": str(credentials)}
            process = subprocess.Popen([sys.executable, "-c",
                f"from backend.workers.durable import run_provider_phase; run_provider_phase('{ident}')"], env=env)
            assert await asyncio.to_thread(accepted.wait, 10), "Local upstream did not receive submission"
            process.kill()
            await asyncio.to_thread(process.wait, 5)
            release.set()
            async with factory.begin() as session:
                row = await session.get(Job, ident)
                assert row.phase == "submitting" and row.slot_scope
                epoch, deadline = row.execution_epoch, row.execution_deadline
                await session.execute(update(Job).where(Job.id == ident).values(lease_until=utcnow() - timedelta(seconds=1)))
            await store.recover_expired()
            def forbidden(_request):
                raise AssertionError("Unknown paid submission must not be resubmitted")
            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                assert await execute_phase(ident, factory, client, redis, tmp_path, {}, set()) == {"skipped": True}
            async with factory() as session:
                row = await session.get(Job, ident)
                assert row.status == "unknown" and row.slot_scope
                assert row.execution_epoch > epoch and row.execution_deadline == deadline
            assert len(calls) == 1
        finally:
            await redis.aclose()
            await engine.dispose()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
