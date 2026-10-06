"""Real eight-process Celery acceptance against a local, never-paid HTTP provider.

Requires an empty dedicated migrated database and a dedicated RabbitMQ vhost.
"""
import asyncio
import base64
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from PIL import Image
import pytest
from sqlalchemy import func, select

from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Job
from backend.providers.policy import ProviderPolicy
from backend.services.acceptance import accept_generation

DB = os.environ.get("AIVERO_PREFORK_TEST_DB")
BROKER = os.environ.get("AIVERO_PREFORK_TEST_BROKER")
REDIS = os.environ.get("AIVERO_PREFORK_TEST_REDIS")
pytestmark = pytest.mark.skipif(not (DB and BROKER and REDIS), reason="explicit empty prefork DB/vhost/Redis required")


def test_hundred_acceptances_eight_processes_actual_upstream_concurrency_five(tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (64, 64)).save(image, format="PNG")
    response = json.dumps({"data": [{"b64_json": base64.b64encode(image.getvalue()).decode()}]}).encode()
    lock = threading.Lock()
    state = {"active": 0, "maximum": 0, "calls": Counter()}

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                state["calls"][body["prompt"]] += 1
            try:
                time.sleep(.2)
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            finally:
                with lock:
                    state["active"] -= 1

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    media = tmp_path / "media"
    media.mkdir()
    credentials = tmp_path / "local-test-credentials.json"
    credentials.write_text(json.dumps({"fake": {"imageApiKey": "local-fake-not-a-real-key"}}))
    env = {**os.environ, "PYTHONPATH": str(Path.cwd() / "apps/api"), "AIVERO_DB_URL": DB,
        "AIVERO_BROKER_URL": BROKER, "AIVERO_REDIS_URL": REDIS, "SWARMUI_DATA_DIR": str(media),
        "AIVERO_DB_POOL_SIZE": "2", "AIVERO_DB_MAX_OVERFLOW": "1",
        "AIVERO_POLICY_CLOUD_CAPACITY": "5", "AIVERO_POLICY_PROVIDER_SUBMIT_PER_MINUTE": "10000",
        "AIVERO_POLICY_GLOBAL_CAPACITY": "200", "OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES"}
    env["AIVERO_CREDENTIALS_FILE"] = str(credentials)
    processes, logs = [], []

    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB, pool_size=2, max_overflow=1))
        factory = create_async_session_factory(engine)
        try:
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(Job)) == 0, "Use a fresh empty test database"
            workspaces = [uuid.uuid4() for _ in range(5)]
            policy = ProviderPolicy(global_capacity=200, workspace_capacity=20)
            snapshot = {"provider": "cloud", "cloudVendor": "openai", "cloudBaseUrl": endpoint,
                        "cloudModel": "local-test-only", "configVersion": "isolated", "credentialRef": "fake"}
            await asyncio.gather(*(accept_generation(factory, workspace_id=workspaces[i % 5],
                request_id=str(uuid.uuid4()), request_hash=str(i), kind="image", policy=policy,
                params={"prompt": f"local-job-{i}", "model": "local-test-only", "width": 64, "height": 64},
                provider_snapshot=snapshot) for i in range(100)))
            commands = [
                [sys.executable, "-m", "celery", "-A", "backend.workers.celery_app:celery_app", "worker",
                 "--pool=prefork", "--concurrency=8", "-Q", "generation.image", "--loglevel=warning",
                 "--without-gossip", "--without-mingle", "--hostname=isolated-prefork@%h"],
                *[[sys.executable, "-m", "backend.workers.daemon", role] for role in ("dispatcher", "scheduler", "bridge")]]
            for index, command in enumerate(commands):
                output = (tmp_path / f"process-{index}.log").open("w")
                logs.append(output)
                processes.append(subprocess.Popen(command, env=env, stdout=output, stderr=subprocess.STDOUT))
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                assert all(process.poll() is None for process in processes), f"Process exited; see {tmp_path}"
                async with factory() as session:
                    statuses = dict((await session.execute(select(Job.status, func.count()).group_by(Job.status))).all())
                assert not statuses.get("failed") and not statuses.get("unknown"), statuses
                if statuses.get("completed") == 100:
                    break
                await asyncio.sleep(1)
            assert statuses == {"completed": 100}, f"{statuses}; process logs: {tmp_path}"
            assert 1 < state["maximum"] <= 5
            assert len(state["calls"]) == 100 and set(state["calls"].values()) == {1}
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(Job).where(Job.slot_scope.is_not(None))) == 0
        finally:
            await engine.dispose()
    try:
        asyncio.run(scenario())
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for output in logs:
            output.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
