"""Opt-in Host Outbox/RabbitMQ/bridge/Celery delivery, no paid model credentials."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from kombu import Connection
from sqlalchemy import func, select
from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.messaging import declare_topology
from backend.infrastructure.outbox_dispatcher import OutboxDispatcher
from backend.infrastructure.orm import Job
from backend.agent.orm import HostCreditAccount
from backend.workers.daemon import agent_bridge_once, bridge_once, schedule_once
from backend.workers.runtime import AsyncRuntime
from backend.workers.celery_app import celery_app
from test_host_media import fixture, body, catalog

DB = os.environ.get("AIVERO_DURABLE_TEST_DB")
BROKER = os.environ.get("AIVERO_DURABLE_TEST_BROKER")
pytestmark = pytest.mark.skipif(not DB or not BROKER or not os.environ.get("AIVERO_DURABLE_TEST_REDIS"),
    reason="explicit isolated DB/Redis/RabbitMQ required")


def test_host_four_direction_real_delivery_and_duplicate_receipt(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, host, _, __):
            created = await host.create(workspace, str(workspace), body(document))
            ident = uuid.UUID(created["run"]["id"])
            catalog_file = tmp_path / "catalog.json"
            catalog_file.write_text(json.dumps(catalog().config.model_dump(mode="json")))
            credentials = tmp_path / "credentials.json"
            credentials.write_text("{}")
            env = {**os.environ, "PYTHONPATH": str(Path.cwd() / "apps/api"), "AIVERO_DB_URL": DB,
                   "AIVERO_BROKER_URL": BROKER, "AIVERO_REDIS_URL": os.environ["AIVERO_DURABLE_TEST_REDIS"],
                   "AIVERO_CREDENTIALS_FILE": str(credentials), "SWARMUI_DATA_DIR": str(tmp_path),
                   "AIVERO_PRODUCTION_HOST_CATALOG_FILE": str(catalog_file), "AIVERO_RUNTIME_ENVIRONMENT": "development"}
            runtime = AsyncRuntime(DatabaseSettings(url=DB))
            log = (tmp_path / "worker.log").open("w+")
            process = subprocess.Popen([sys.executable, "-m", "celery", "-A", "backend.workers.celery_app:celery_app",
                "worker", "-Q", "agent,generation.image,exports", "--pool=solo", "--concurrency=1",
                "--loglevel=warning", "--without-gossip", "--without-mingle", "--without-heartbeat"],
                env=env, stdout=log, stderr=log)
            def pump():
                OutboxDispatcher(DatabaseSettings(url=DB), BROKER).dispatch_once(500)
                with Connection(BROKER, transport_options={"confirm_publish": True}) as connection:
                    declare_topology(connection)
                    agent_bridge_once(connection, runtime, limit=500)
                    bridge_once(connection, runtime, limit=500)
            try:
                for _ in range(45):
                    await schedule_once(factory, tmp_path)
                    await asyncio.to_thread(pump)
                    current = (await host.get(workspace, str(workspace), ident))["run"]
                    if current["status"] == "awaiting_user_final":
                        break
                    await asyncio.sleep(.2)
                else:
                    log.flush()
                    log.seek(0)
                    pytest.fail("Host delivery did not complete: " + str(current) + log.read()[-5000:])
                def duplicate():
                    with Connection(BROKER, transport_options={"confirm_publish": True}) as connection:
                        celery_app.send_task("aivideo.agent.advance", args=[str(ident)], queue="agent",
                            producer=connection.Producer(), retry=False)
                await asyncio.to_thread(duplicate)
                await asyncio.sleep(.5)
                async with factory() as db:
                    assert await db.scalar(select(func.count()).select_from(Job).where(Job.params["hostRunId"].astext == str(ident))) == 4
                    assert (await db.get(HostCreditAccount, workspace)).available == 160
                assert (await host.get(workspace, str(workspace), ident))["run"]["status"] == "awaiting_user_final"
            finally:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, 5)
                runtime.close()
                log.close()
    asyncio.run(scenario())
