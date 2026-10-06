"""Real Outbox -> RabbitMQ -> bridge -> dedicated Celery worker smoke test.

Only explicit isolated endpoints are accepted. The worker has an empty credentials
registry, so the model boundary fails before any outbound HTTP can happen.
"""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from kombu import Connection

from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.outbox_dispatcher import OutboxDispatcher
from backend.infrastructure.messaging import declare_topology
from backend.workers.daemon import agent_bridge_once
from backend.workers.celery_app import celery_app
from test_agent_api import api

DB = os.environ.get('AIVERO_DURABLE_TEST_DB')
BROKER = os.environ.get('AIVERO_DURABLE_TEST_BROKER')
pytestmark = pytest.mark.skipif(not DB or not BROKER, reason='explicit isolated PostgreSQL and RabbitMQ required')


@pytest.mark.parametrize('credential_contents', ['{}', 'invalid-json'])
def test_real_dedicated_worker_consumes_wakeup_without_repeating_model_call(tmp_path, credential_contents):
    async def scenario():
        async with api(tmp_path) as (app, client, _, _):
            document = (await client.post('/api/documents', json={})).json()['document']
            accepted = await client.post(f"/api/documents/{document['id']}/agent/runs", json={
                'clientRequestId': uuid.uuid4().hex, 'message': '读取画布', 'modelId': 'deepseek',
                'baseRevision': document['revision'], 'selectedObjectIds': []})
            assert accepted.status_code == 202, accepted.text
            run_id = accepted.json()['run']['id']
            creds = tmp_path / 'credentials.json'
            creds.write_text(credential_contents)
            env = {**os.environ, 'PYTHONPATH': str(Path.cwd() / 'apps/api'), 'AIVERO_DB_URL': DB,
                   'AIVERO_BROKER_URL': BROKER, 'AIVERO_CREDENTIALS_FILE': str(creds),
                   'SWARMUI_DATA_DIR': str(tmp_path / 'media')}
            log = (tmp_path / 'worker.log').open('w+')
            process = subprocess.Popen([sys.executable, '-m', 'celery', '-A', 'backend.workers.celery_app:celery_app',
                'worker', '-Q', 'agent', '--pool=solo', '--concurrency=1', '--loglevel=warning',
                '--without-gossip', '--without-mingle', '--without-heartbeat'], env=env, stdout=log, stderr=log)
            try:
                dispatched = await asyncio.to_thread(OutboxDispatcher(DatabaseSettings(url=DB), BROKER).dispatch_once, 500)
                assert dispatched['failed'] == 0, dispatched
                def bridge():
                    with Connection(BROKER, transport_options={'confirm_publish': True}) as connection:
                        declare_topology(connection)
                        return agent_bridge_once(connection, None, limit=500)
                assert await asyncio.to_thread(bridge) >= 1
                for _ in range(100):
                    current = (await client.get('/api/agent/runs/' + run_id)).json()['run']
                    if current['status'] not in {'queued', 'running'}:
                        break
                    await asyncio.sleep(.2)
                else:
                    log.flush()
                    log.seek(0)
                    pytest.fail('Worker did not settle isolated run: ' + log.read()[-4000:])
                assert current['status'] == 'failed', current
                assert current['modelCallCount'] == 1
                assert '凭据' in current['error']
                # Repeated delivery of the already settled Run cannot create another call.
                def duplicate():
                    with Connection(BROKER, transport_options={'confirm_publish': True}) as connection:
                        celery_app.send_task('aivideo.agent.advance', args=[run_id], queue='agent',
                            producer=connection.Producer(), retry=False)
                await asyncio.to_thread(duplicate)
                await asyncio.sleep(.5)
                last = (await client.get('/api/agent/runs/' + run_id)).json()['run']
                assert last['modelCallCount'] == 1 and last['status'] == 'failed'
            finally:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, 5)
                log.close()
    asyncio.run(scenario())
