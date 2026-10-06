"""Agent HTTP contract on isolated PostgreSQL; model calls use deterministic transports."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import select
from starlette.datastructures import Headers, QueryParams

from backend.app import create_app
from backend.agent.orm import AgentRun
from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.orm import Workspace, WorkspaceMember
from backend.production_settings import ProductionSettings
from backend.services.identity import CSRF_COOKIE, SESSION_COOKIE, Identity, issue_session

DB = os.environ.get("AIVERO_DURABLE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated PostgreSQL required")


@asynccontextmanager
async def api(tmp_path, enabled=True):
    media_routes = tmp_path / 'media-routes.json'
    media_routes.write_text(json.dumps({'image': {'provider': 'mock', 'cloudModel': 'mock-test', 'configVersion': 'v1'}}))
    routes = tmp_path / 'agent-routes.json'
    routes.write_text(json.dumps([{'id': 'deepseek', 'label': 'DeepSeek', 'provider': 'deepseek',
        'model': 'fixture-model', 'baseUrl': 'https://model.invalid/v1', 'credentialRef': 'fixture'}] if enabled else []))
    settings = ProductionSettings(public_origin='https://api.test', oidc_issuer='https://identity.test',
        oidc_client_id='test', oidc_client_secret='test', session_secret='x' * 40,
        provider_routes_file=media_routes, agent_routes_file=routes, media_root=tmp_path / 'media')
    app = create_app(settings, DatabaseSettings(url=DB))
    async with app.router.lifespan_context(app):
        workspace, principal = uuid.uuid4(), uuid.uuid4().hex
        async with app.state.factory.begin() as session:
            session.add(Workspace(id=workspace))
            await session.flush()
            session.add(WorkspaceMember(workspace_id=workspace, principal_id=principal, role='admin'))
        token, csrf = await issue_session(app.state.factory, principal)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://api.test',
            headers={'origin': 'https://api.test', 'x-csrf-token': csrf,
                     'cookie': f'{SESSION_COOKIE}={token}; {CSRF_COOKIE}={csrf}'}) as client:
            yield app, client, workspace, principal


def test_agent_acceptance_replay_before_revision_validation_and_no_doc_writes(tmp_path):
    async def scenario():
        async with api(tmp_path) as (app, client, workspace, _):
            models = (await client.get('/api/agent/models')).json()
            assert models['defaultModelId'] == 'deepseek'
            document = (await client.post('/api/documents', json={'name': 'Agent'})).json()['document']
            url = f"/api/documents/{document['id']}/agent"
            body = {'clientRequestId': uuid.uuid4().hex, 'message': '画布有哪些图片？',
                    'modelId': 'deepseek', 'baseRevision': document['revision'], 'selectedObjectIds': []}
            responses = await asyncio.gather(*(client.post(url + '/runs', json=body) for _ in range(3)))
            assert all(r.status_code in {200, 202} for r in responses), [r.text for r in responses]
            run = responses[0].json()['run']
            assert {r.json()['run']['id'] for r in responses} == {run['id']}
            # A lost POST response is recovered by request identity, even once the document changed.
            await client.post(f"/api/documents/{document['id']}", json={'objects': {}, 'order': [], 'baseRevision': document['revision']})
            recovered = (await client.get(url, params={'clientRequestId': body['clientRequestId']})).json()
            assert recovered['requestedRun']['id'] == run['id']
            app.state.agent_routes = []
            assert (await client.post(url + '/runs', json=body)).json()['run']['id'] == run['id']
            changed = await client.post(url + '/runs', json={**body, 'message': 'changed'})
            assert changed.status_code == 409
            stopped = (await client.post(f"/api/agent/runs/{run['id']}/stop")).json()['run']
            assert stopped['status'] == 'stopped'
            assert (await client.post(f"/api/agent/runs/{run['id']}/stop")).json()['run']['status'] == 'stopped'
            saved = (await client.get(f"/api/documents/{document['id']}")).json()['document']
            assert saved['revision'] == document['revision'] + 1
            assert not saved.get('chat')
            async with app.state.factory() as session:
                rows = list(await session.scalars(select(AgentRun).where(AgentRun.workspace_id == workspace)))
                assert len(rows) == 1
    asyncio.run(scenario())


def test_agent_auth_csrf_disabled_and_document_deletion(tmp_path):
    async def scenario():
        async with api(tmp_path) as (app, client, workspace, _):
            document = (await client.post('/api/documents', json={})).json()['document']
            url = f"/api/documents/{document['id']}/agent"
            body = {'clientRequestId': uuid.uuid4().hex, 'message': 'hello', 'modelId': 'deepseek',
                    'baseRevision': document['revision'], 'selectedObjectIds': []}
            assert (await client.post(url + '/runs', json=body, headers={'x-csrf-token': 'wrong'})).status_code == 403
            assert (await client.get(url, headers={'x-workspace-id': str(uuid.uuid4())})).status_code == 403
            first = await client.post(url + '/runs', json=body)
            assert first.status_code == 202, first.text
            run = first.json()['run']
            other = await client.post(url + '/runs', json={**body, 'clientRequestId': uuid.uuid4().hex})
            assert other.status_code == 409
            removed = await client.delete(f"/api/documents/{document['id']}")
            assert removed.status_code == 200, removed.text
            archived = (await client.get(f"/api/agent/runs/{run['id']}")).json()['run']
            assert archived['status'] == 'stopped'
            assert (await client.get(url)).status_code == 404
            assert (await client.get(f"/api/agent/runs/{run['id']}", headers={'x-workspace-id': str(uuid.uuid4())})).status_code == 403
        async with api(tmp_path, enabled=False) as (_, client, _, _):
            assert (await client.get('/api/agent/models')).json()['models'] == []
            doc = (await client.post('/api/documents', json={})).json()['document']
            unavailable = await client.post(f"/api/documents/{doc['id']}/agent/runs", json={**body,
                'baseRevision': doc['revision'], 'clientRequestId': uuid.uuid4().hex})
            assert unavailable.status_code in {400, 503}
    asyncio.run(scenario())


def test_sse_replays_committed_events_after_cursor_without_advancing_runtime(tmp_path):
    async def scenario():
        async with api(tmp_path) as (app, client, workspace, principal):
            document = (await client.post('/api/documents', json={})).json()['document']
            ident = uuid.UUID(document['id'])
            url = f'/api/documents/{ident}/agent'
            created = await client.post(url + '/runs', json={'clientRequestId': uuid.uuid4().hex,
                'message': 'hello', 'modelId': 'deepseek', 'baseRevision': document['revision'], 'selectedObjectIds': []})
            run = created.json()['run']
            before = (await client.get(url)).json()['cursor']
            await client.post(f"/api/agent/runs/{run['id']}/stop")
            snapshot = (await client.get(url)).json()
            endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/documents/{ident}/agent/events')

            class Request:
                query_params = QueryParams({'after': str(before)})
                headers = Headers()
                async def is_disconnected(self):
                    return False

            response = await endpoint(ident, Request(), Identity(principal=principal, workspace_id=workspace, role='admin'))
            stream = response.body_iterator
            assert 'retry' in await anext(stream)
            event = await anext(stream)
            assert 'event: agent' in event
            payload = json.loads(event.split('data: ', 1)[1])
            assert payload['seq'] > before and payload['run']['status'] == 'stopped'
            await stream.aclose()
            current = (await client.get(url)).json()
            assert current['cursor'] == snapshot['cursor']
            assert current['runs'][0]['modelCallCount'] == 0
    asyncio.run(scenario())
