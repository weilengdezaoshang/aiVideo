"""Explicit local 8443 Mock smoke: a new QA workspace, real HTTP and live Workers.

Never accepts an arbitrary origin/database or paid provider. Leaves the QA
workspace as evidence; no existing document or workspace is mutated.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import runpy
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


async def main():
    launcher = runpy.run_path(str(ROOT / "scripts/dev_stack.py"))
    env = launcher["load_env"]()
    origin = "https://localhost:8443"
    db_url = urlsplit(env["AIVERO_DB_URL"])
    if db_url.hostname not in {"localhost", "127.0.0.1"} or db_url.port != 55437:
        raise RuntimeError("This smoke only accepts the explicit local development database")
    catalog = json.loads((ROOT / "data/dev/host-catalog.json").read_text())
    if catalog.get("environment") != "development" or not catalog.get("enabled") or any(
        cap.get("enabled") and cap.get("binding", {}).get("engine", "provider") != "frames"
        and cap.get("binding", {}).get("providerSnapshot", {}).get("provider") != "mock"
        for cap in catalog.get("capabilities", [])
    ):
        raise RuntimeError("Mock-only development Host required")
    os.environ.update(env)
    # scripts/backend.py is an npm launcher, not the backend package.
    sys.path.insert(0, str(ROOT / "apps/api"))
    import httpx
    from backend.infrastructure.database import create_async_database_engine, create_async_session_factory
    from backend.infrastructure.orm import Workspace, WorkspaceMember
    from backend.services.identity import issue_session, SESSION_COOKIE, CSRF_COOKIE
    from backend.agent.media.credits import grant
    # Local TLS is inspected through HTTP, not by bypassing browser interstitials.
    async with httpx.AsyncClient(base_url=origin, verify=False, trust_env=False, timeout=20) as probe:
        built = await probe.get('/canvas/app.js')
        built.raise_for_status()
        expected = (ROOT / '.web-build/canvas/app.js').read_bytes()
        assert hashlib.sha256(built.content).digest() == hashlib.sha256(expected).digest()
    engine = create_async_database_engine()
    factory = create_async_session_factory(engine)
    workspace, principal = uuid.uuid4(), 'qa-8443-' + uuid.uuid4().hex
    try:
        async with factory.begin() as db:
            db.add(Workspace(id=workspace, name='8443 consolidation Mock smoke'))
            await db.flush()
            db.add(WorkspaceMember(workspace_id=workspace, principal_id=principal, role='admin'))
            await grant(db, workspace, 'qa-mock-initial', 1000, principal)
        token, csrf = await issue_session(factory, principal, 300)
        async with httpx.AsyncClient(base_url=origin, verify=False, trust_env=False, timeout=20,
            headers={'origin': origin, 'x-csrf-token': csrf,
                'cookie': f'{SESSION_COOKIE}={token}; {CSRF_COOKIE}={csrf}'}) as client:
            assert (await client.get('/api/agent/models')).json()['hostEnabled']
            doc_response = await client.post('/api/documents', json={'name': '8443 统一验收 · Mock 四方向'})
            doc_response.raise_for_status()
            doc = doc_response.json()['document']
            request_id = str(uuid.uuid4())
            body = {'clientRequestId': request_id, 'documentId': doc['id'], 'message': '生成四方向图片',
                'baseRevision': doc['revision'], 'selectedObjectIds': [], 'context': {
                    'schema_version': 1, 'request_id': request_id, 'conversation_id': str(uuid.uuid4()),
                    'doc_id': doc['id'], 'doc_version': doc['revision'], 'client_ts': 1,
                    'client_id': 'qa-8443', 'tab_id': str(uuid.uuid4()), 'message_text': '生成四方向图片',
                    'selection': {'ids': [], 'count': 0, 'truncated': False},
                    'viewport': {'bbox': [0, 0, 1280, 720], 'zoom': 1}}}
            accepted = await client.post('/api/host/turn', json=body)
            accepted.raise_for_status()
            run = accepted.json()['run']
            replay = await client.post('/api/host/turn', json=body)
            assert replay.status_code == 200 and replay.json()['run']['id'] == run['id']
            async def finished(ident, wanted):
                async with asyncio.timeout(120):
                    while True:
                        response = await client.get(f'/api/host/runs/{ident}')
                        response.raise_for_status()
                        value = response.json()['run']
                        if value['status'] == wanted:
                            return value
                        if value['status'] in {'failed', 'blocked', 'cancelled', 'needs_attention'}:
                            raise RuntimeError(f"Mock run stopped: {value['status']} {value.get('errorCode')}")
                        await asyncio.sleep(0.5)
            preview = await finished(run['id'], 'awaiting_user_final')
            saved = (await client.get(f"/api/documents/{doc['id']}")).json()['document']
            nodes = saved.get('data', saved)['objects']
            assert len(nodes) == 4 and all(node.get('assetId') and node['kind'] == 'image' for node in nodes.values())
            promoted = await client.post(f"/api/host/runs/{run['id']}/confirm-final", json={
                'clientRequestId': str(uuid.uuid4()), 'baseRevision': saved['revision']})
            promoted.raise_for_status()
            final = promoted.json()['run']
            if final['status'] == 'awaiting_confirm':
                confirmation = await client.post(f"/api/host/runs/{final['id']}/confirm", json={
                    'clientRequestId': str(uuid.uuid4()), 'approved': True})
                confirmation.raise_for_status()
            final = await finished(final['id'], 'completed')
            saved = (await client.get(f"/api/documents/{doc['id']}")).json()['document']
            nodes = saved.get('data', saved)['objects']
            assert len(nodes) == 8 and sum(node.get('badge') == 'final' for node in nodes.values()) == 4
            ledger = (await client.get(f"/api/host/runs/{final['id']}/ledger")).json()['entries']
            assert len(ledger) == 4
            report = {'origin': origin, 'provider': 'mock', 'workspaceId': str(workspace),
                'documentId': doc['id'], 'previewRunId': preview['id'], 'previewStatus': preview['status'],
                'finalRunId': final['id'], 'finalStatus': final['status'], 'canvasNodes': len(nodes),
                'finalNodes': 4, 'finalLedgerEntries': len(ledger), 'idempotentReplay': True,
                'servedBundleMatches': True}
            output = ROOT / 'data/dev/consolidation-smoke.json'
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps(report, ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
