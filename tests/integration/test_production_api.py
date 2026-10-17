"""Explicit isolated PostgreSQL tests for the database-only HTTP boundary."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import select

from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.orm import Job, Workspace, WorkspaceMember
from backend.app import create_app
from backend.production_settings import ProductionSettings
from backend.services.identity import CSRF_COOKIE, SESSION_COOKIE, issue_session

DB = os.environ.get("AIVERO_DURABLE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated PostgreSQL URL required")


@asynccontextmanager
async def api(tmp_path, cutout_infer=None):
    path = tmp_path / "provider-routes.json"
    path.write_text(json.dumps({"image": {"provider": "mock", "cloudModel": "mock-test", "configVersion": "test-v1"}}))
    settings = ProductionSettings(public_origin="https://api.test", oidc_issuer="https://id.test",
        oidc_client_id="isolated-test", oidc_client_secret="isolated-secret", session_secret="x" * 40,
        provider_routes_file=path, media_root=tmp_path / "media")
    app = create_app(settings, DatabaseSettings(url=DB), cutout_infer=cutout_infer)
    async with app.router.lifespan_context(app):
        workspace, principal = uuid.uuid4(), uuid.uuid4().hex
        async with app.state.factory.begin() as session:
            session.add(Workspace(id=workspace))
            await session.flush()
            session.add(WorkspaceMember(workspace_id=workspace, principal_id=principal, role="admin"))
        token, csrf = await issue_session(app.state.factory, principal)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.test",
            headers={"origin": "https://api.test", "x-csrf-token": csrf,
                     "cookie": f"{SESSION_COOKIE}={token}; {CSRF_COOKIE}={csrf}"}) as client:
            yield app, client, workspace


def test_membership_csrf_and_database_document_revision(tmp_path):
    async def scenario():
        async with api(tmp_path) as (app, client, workspace):
            assert (await client.get("/api/session")).status_code == 200
            assert (await client.get("/api/documents", headers={"x-workspace-id": str(uuid.uuid4())})).status_code == 403
            denied = await client.post("/api/documents", json={"name": "test"}, headers={"x-csrf-token": "wrong"})
            assert denied.status_code == 403
            first = await client.post("/api/documents", json={"name": "test"}, headers={"idempotency-key": "create-1"})
            assert first.status_code == 201
            doc = first.json()["document"]
            replay = await client.post("/api/documents", json={"name": "test"}, headers={"idempotency-key": "create-1"})
            assert replay.json()["document"]["id"] == doc["id"]
            generations = {"generation-1": {"id": "generation-1", "operationId": "operation-1",
                "spec": {"mode": "text-to-image", "params": {"prompt": "cat"}},
                "status": "completed", "result": {"src": "/images/cat.png", "ext": "png",
                    "kind": "image"}, "version": 2}}
            saved = await client.post(f"/api/documents/{doc['id']}", json={"objects": {},
                "order": [], "generations": generations, "baseRevision": 1})
            assert saved.status_code == 200 and saved.json()["document"]["revision"] == 2
            assert saved.json()["document"]["generations"] == generations
            stale = await client.post(f"/api/documents/{doc['id']}", json={"objects": {}, "order": [], "baseRevision": 1})
            assert stale.status_code == 409 and stale.json()["document"]["revision"] == 2
            assert not list((tmp_path / "media").rglob("*.json"))
    asyncio.run(scenario())


def test_concurrent_http_acceptance_batch_children_and_scoped_reads(tmp_path):
    async def scenario():
        async with api(tmp_path) as (app, client, workspace):
            payload = {"prompt": "test", "model": "mock-test", "batchCount": 3, "requestId": str(uuid.uuid4())}
            responses = await asyncio.gather(*(client.post("/api/generate", json=payload) for _ in range(8)))
            assert {response.status_code for response in responses} == {202}
            ids = {response.json()["jobId"] for response in responses}
            assert len(ids) == 1
            ident = ids.pop()
            async with app.state.factory() as session:
                children = list(await session.scalars(select(Job).where(Job.parent_id == uuid.UUID(ident))))
                assert len(children) == 3
                assert all(child.params["batchCount"] == 1 for child in children)
            denied = await client.get(f"/api/jobs/{ident}", headers={"x-workspace-id": str(uuid.uuid4())})
            assert denied.status_code == 403
            cancelled = await client.delete(f"/api/jobs/{ident}")
            assert cancelled.status_code == 200
            assert all(child["status"] == "failed" for child in cancelled.json()["job"]["children"])
            assert not (tmp_path / "media" / "jobs.json").exists()
    asyncio.run(scenario())


def test_direction_group_is_atomic_idempotent_and_visible_as_slots(tmp_path):
    async def scenario():
        async with api(tmp_path) as (app, client, workspace):
            payload = {"prompt": "test", "model": "mock-test", "clientRefs": [f"slot-{i}" for i in range(4)],
                       "requestId": str(uuid.uuid4())}
            first, replay = await asyncio.gather(*(client.post("/api/generate/group", json=payload) for _ in range(2)))
            assert first.status_code == replay.status_code == 202
            assert first.json() == replay.json()
            assert len({slot["jobId"] for slot in first.json()["slots"]}) == 4
            visible = (await client.get("/api/jobs")).json()["jobs"]
            assert {job["clientRef"] for job in visible} == set(payload["clientRefs"])
            assert {job["slot"] for job in visible} == {0, 1, 2, 3}
            assert len({job["params"]["prompt"] for job in visible}) == 4
            await client.delete("/api/jobs/" + first.json()["groupId"])
    asyncio.run(scenario())


def test_random_seed_is_resolved_at_acceptance_and_persisted(tmp_path):
    """T18 种子锁定的契约:随机种子在受理时固化为确定值并随任务参数返回。"""
    async def scenario():
        async with api(tmp_path) as (app, client, workspace):
            accepted = await client.post("/api/generate", json={"prompt": "test", "model": "mock-test",
                "requestId": str(uuid.uuid4()), "seed": -1})
            assert accepted.status_code == 202
            job_id = uuid.UUID(accepted.json()["jobId"])
            derived = int(hashlib.sha256(str(job_id).encode()).hexdigest()[:8], 16) % (2**31)
            async with app.state.factory() as session:
                row = await session.get(Job, job_id)
                assert row.params["seed"] == derived
            fixed = await client.post("/api/generate", json={"prompt": "test", "model": "mock-test",
                "requestId": str(uuid.uuid4()), "seed": 7})
            assert fixed.status_code == 202
            async with app.state.factory() as session:
                assert (await session.get(Job, uuid.UUID(fixed.json()["jobId"]))).params["seed"] == 7
            batch = await client.post("/api/generate", json={"prompt": "test", "model": "mock-test",
                "requestId": str(uuid.uuid4()), "batchCount": 3})
            assert batch.status_code == 202
            async with app.state.factory() as session:
                children = list(await session.scalars(
                    select(Job).where(Job.parent_id == uuid.UUID(batch.json()["jobId"]))))
                seeds = [child.params["seed"] for child in children]
                assert len(set(seeds)) == 3 and all(0 <= seed < 2**31 for seed in seeds)
    asyncio.run(scenario())


def test_cutout_precancel_prevents_inference_and_never_persists_mask(tmp_path, monkeypatch):
    import io
    from PIL import Image
    redis_url = os.environ.get("AIVERO_DURABLE_TEST_REDIS")
    if not redis_url:
        pytest.skip("explicit isolated Redis required")
    monkeypatch.setenv("AIVERO_REDIS_URL", redis_url)
    calls = []
    async def scenario():
        async with api(tmp_path, cutout_infer=lambda data: calls.append(data) or data) as (app, client, workspace):
            image = io.BytesIO()
            Image.new("RGB", (64, 64)).save(image, format="PNG")
            uploaded = await client.post("/api/assets?kind=image", content=image.getvalue())
            assert uploaded.status_code == 201, uploaded.text
            asset = uploaded.json()
            operation = uuid.uuid4().hex
            assert (await client.delete(f"/api/cutout/operations/{operation}")).status_code == 200
            cancelled = await client.post(f"/api/cutout/detect/{asset['asset']['id']}?operationId={operation}")
            assert cancelled.status_code == 409 and not calls
            success = await client.post(f"/api/cutout/detect/{asset['asset']['id']}?operationId={uuid.uuid4().hex}")
            assert success.status_code == 200 and success.json()["urls"]["original"].startswith("data:image/png")
            assert len(calls) == 1
            assert len(list((tmp_path / "media").rglob("receipt.json"))) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("with_audio", [False, True])
def test_real_export_worker_uses_database_and_shared_assets(tmp_path, with_audio):
    import io
    from PIL import Image
    from backend.services.export_runner import execute_export

    async def scenario():
        async with api(tmp_path) as (app, client, workspace):
            buf = io.BytesIO()
            Image.new("RGB", (160, 90), "red").save(buf, format="PNG")
            uploaded = await client.post("/api/assets?kind=image", content=buf.getvalue())
            assert uploaded.status_code == 201, uploaded.text
            asset = uploaded.json()
            doc = (await client.post("/api/documents", json={"name": "render"})).json()["document"]
            timeline = {"version": 1, "fps": 30, "width": 160, "height": 90, "clips": [{
                "id": "clip-1", "name": "red", "inFrame": 0, "outFrame": 3,
                "source": {"nodeId": "node-1", "kind": "image", "durationFrames": 30,
                           "url": asset["urls"]["original"]}}]}
            if with_audio:
                import wave
                audio_bytes = io.BytesIO()
                with wave.open(audio_bytes, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(8000)
                    wav.writeframes(b"\x00\x00" * 800)
                audio = await client.post("/api/assets?kind=audio&ext=wav", content=audio_bytes.getvalue())
                assert audio.status_code == 201, audio.text
                audio_asset = audio.json()
                timeline["audioClips"] = [{"id": "music", "name": "music", "startFrame": 0,
                    "inFrame": 0, "outFrame": 3, "volume": 1, "fadeInFrames": 0, "fadeOutFrames": 0,
                    "source": {"assetId": audio_asset["asset"]["id"], "url": audio_asset["urls"]["original"],
                        "durationFrames": 3}}]
            response = await client.post("/api/exports", json={"requestId": str(uuid.uuid4()),
                "documentId": doc["id"], "timeline": timeline})
            assert response.status_code == 202, response.text
            ident = response.json()["export"]["id"]
            result = await execute_export(uuid.UUID(ident), app.state.factory, tmp_path / "media")
            assert result == {"completed": True}
            status = await client.get(f"/api/exports/{ident}")
            assert status.json()["export"]["status"] == "completed"
            downloaded = await client.get(f"/api/exports/{ident}/download")
            assert downloaded.status_code == 200 and downloaded.content[4:8] == b"ftyp"
            assert not list((tmp_path / "media").rglob("job.json"))
    asyncio.run(scenario())


@pytest.mark.parametrize("audience,status", [("isolated-test", 303), ("wrong-client", 401)])
def test_oidc_validates_real_signed_token_and_pkce(tmp_path, audience, status):
    import time
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse
    from joserfc import jwt
    from joserfc.jwk import RSAKey
    from authlib.integrations.httpx_client._compat import httpx2 as oidc_http

    async def scenario():
        async with api(tmp_path) as (app, _client, _workspace):
            key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
            state = {}
            def oidc(request):
                if request.url.path.endswith("openid-configuration"):
                    return oidc_http.Response(200, json={"issuer": "https://id.test",
                        "authorization_endpoint": "https://id.test/authorize", "token_endpoint": "https://id.test/token",
                        "jwks_uri": "https://id.test/jwks", "id_token_signing_alg_values_supported": ["RS256"]})
                if request.url.path == "/jwks":
                    return oidc_http.Response(200, json={"keys": [key.as_dict(private=False)]})
                assert request.url.path == "/token"
                data = parse_qs(request.content.decode())
                verifier = data["code_verifier"][0]
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                assert challenge == state["code_challenge"][0]
                token = jwt.encode({"alg": "RS256", "kid": "test-key"}, {
                    "iss": "https://id.test", "sub": "user-1", "aud": audience,
                    "iat": int(time.time()), "exp": int(time.time()) + 300,
                    "nonce": state["nonce"][0]}, key)
                return oidc_http.Response(200, json={"access_token": "test-only", "token_type": "Bearer", "id_token": token})
            app.state.oauth.identity.client_kwargs["transport"] = oidc_http.MockTransport(oidc)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.test") as browser:
                login = await browser.get("/auth/login")
                assert login.status_code == 302
                state.update(parse_qs(urlparse(login.headers["location"]).query))
                callback = await browser.get("/auth/callback", params={"state": state["state"][0], "code": "test-code"})
                assert callback.status_code == status, callback.text
                if status == 303:
                    assert SESSION_COOKIE in browser.cookies
                    # A valid identity is not an automatic workspace membership.
                    assert (await browser.get("/api/session")).status_code == 403
                else:
                    assert SESSION_COOKIE not in browser.cookies
    asyncio.run(scenario())


def test_canvas_serves_restored_page_and_current_bundle(tmp_path):
    async def scenario():
        async with api(tmp_path) as (_app, client, _workspace):
            for url in ("/canvas?doc=existing-document", "/canvas/"):
                page = await client.get(url)
                assert page.status_code == 200
                assert '/canvas/app.js' in page.text
                assert '/canvas/canvas.css' in page.text
                assert '/canvas-next/' not in page.text
                assert page.headers['cache-control'] == 'no-cache'
            for url in ('/canvas-next?doc=existing-document', '/canvas-next/?doc=existing-document'):
                alias = await client.get(url)
                assert alias.status_code == 307
                assert alias.headers['location'] == 'https://api.test/canvas?doc=existing-document'
            for url in ('/canvas', '/canvas/'):
                head = await client.head(url)
                assert head.status_code == 200
                assert head.headers['content-type'].startswith('text/html')
            style = await client.get('/canvas/canvas.css')
            assert style.status_code == 200
            assert '.tool-rail' in style.text
            bundle = await client.get('/canvas/app.js')
            assert bundle.status_code == 200
            assert bundle.headers['cache-control'] == 'no-cache'
    asyncio.run(scenario())


def test_canvas_node_drafts_and_independent_timelines_roundtrip(tmp_path):
    async def scenario():
        async with api(tmp_path) as (_app, client, _workspace):
            doc = (await client.post('/api/documents', json={'name': 'node bindings'})).json()['document']
            empty = {'version': 1, 'fps': 30, 'width': 640, 'height': 360, 'clips': []}
            objects = {
                'image': {'id': 'image', 'kind': 'placeholder', 'placeholderType': 'image',
                    'mediaType': 'image', 'x': 0, 'y': 0, 'width': 320, 'height': 320,
                    'nodeDraft': {'prompt': 'saved per node', 'model': 'mock-test', 'ratio': '1:1',
                        'resolution': 1024, 'durationSec': 4, 'denoise': .6, 'references': []}},
                'timeline-a': {'id': 'timeline-a', 'kind': 'placeholder', 'placeholderType': 'timeline',
                    'x': 400, 'y': 0, 'width': 480, 'height': 160, 'timeline': empty},
                'timeline-b': {'id': 'timeline-b', 'kind': 'placeholder', 'placeholderType': 'timeline',
                    'x': 400, 'y': 200, 'width': 480, 'height': 160,
                    'timeline': {**empty, 'width': 1280, 'height': 720}},
            }
            response = await client.post(f"/api/documents/{doc['id']}", json={
                'objects': objects, 'order': list(objects), 'baseRevision': doc['revision']})
            assert response.status_code == 200, response.text
            loaded = (await client.get(f"/api/documents/{doc['id']}")).json()['document']
            assert loaded['objects'] == objects
            assert loaded['objects']['timeline-a']['timeline']['width'] == 640
            assert loaded['objects']['timeline-b']['timeline']['width'] == 1280
    asyncio.run(scenario())


def test_host_context_http_uses_canonical_auth_idempotency_and_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("AIVERO_RUNTIME_ENVIRONMENT", "development")
    async def scenario():
        from backend.agent.media.catalog import Catalog
        from backend.agent.media.contracts import CatalogConfig
        from backend.agent.media.credits import grant
        from backend.agent.media.service import MediaHost
        raw_catalog = {"version": "context-http", "environment": "development", "enabled": True,
            "capabilities": [{"id": "text_to_image", "enabled": True, "outputType": "image",
                "tiers": {"preview": {"credits": 10}}, "binding": {"version": "context-http",
                    "providerSnapshot": {"provider": "mock", "configVersion": "context-http", "cloudModel": "mock"}}}]}
        async with api(tmp_path) as (app, client, workspace):
            app.state.host = MediaHost(app.state.factory, Catalog(CatalogConfig.model_validate(raw_catalog)), tmp_path / "media")
            async with app.state.factory.begin() as db:
                await grant(db, workspace, "context-http-credit", 100, str(workspace))
            doc = (await client.post("/api/documents", json={"name": "context-http"})).json()["document"]
            ident = str(uuid.uuid4())
            request = {"clientRequestId": ident, "documentId": doc["id"], "message": "画一只狗",
                "baseRevision": doc["revision"], "selectedObjectIds": []}
            request["context"] = {"schema_version": 1, "request_id": ident, "conversation_id": str(uuid.uuid4()),
                "doc_id": doc["id"], "doc_version": doc["revision"], "client_ts": 1,
                "client_id": "client", "tab_id": "tab", "message_text": request["message"],
                "selection": {"ids": [], "count": 0, "truncated": False},
                "viewport": {"bbox": [0, 0, 1024, 720], "zoom": 1}}
            denied = await client.post("/api/host/turn", json=request, headers={"x-csrf-token": "wrong"})
            assert denied.status_code == 403
            created = await client.post("/api/host/turn", json=request)
            assert created.status_code == 202, created.text
            replay = await client.post("/api/host/turn", json=request)
            assert replay.status_code == 200 and replay.json()["run"]["id"] == created.json()["run"]["id"]
            forged = {**request, "context": {**request["context"], "role": "admin"}}
            assert (await client.post("/api/host/turn", json=forged)).status_code == 400
            stale_id = str(uuid.uuid4())
            stale = {**request, "clientRequestId": stale_id, "context": {**request["context"], "request_id": stale_id}}
            response = await client.post("/api/host/turn", json=stale)
            assert response.status_code == 409 and response.json()["code"] == "STALE_VERSION"
    asyncio.run(scenario())


def test_local_dialogue_settings_auth_csrf_redaction_and_persistence(tmp_path):
    async def scenario():
        async with api(tmp_path) as (app, client, workspace):
            url = "/api/creation/v1/llm-config"
            body = {"baseUrl": "https://model.example/v1", "model": "test-model", "apiKey": "isolated-test-only", "timeoutMs": 45000}
            assert (await client.get(url)).status_code == 403
            app.state.settings = app.state.settings.model_copy(update={"runtime_environment": "development"})
            denied = await client.post(url, json=body, headers={"x-csrf-token": "wrong"})
            assert denied.status_code == 403
            saved = await client.post(url, json=body)
            assert saved.status_code == 200 and saved.json()["keyConfigured"]
            assert "isolated-test-only" not in saved.text
            del body["apiKey"]
            body["model"] = "updated-test-model"
            assert (await client.post(url, json=body)).status_code == 200
            assert (await client.get(url)).json()["model"] == "updated-test-model"
            assert (await client.get(url, headers={"x-workspace-id": str(uuid.uuid4())})).status_code == 403
            models = (await client.get("/api/agent/models")).json()
            assert any(route["id"] == "local-dialogue" for route in models["models"])
    asyncio.run(scenario())


def test_agent_cutout_http_waits_for_canvas_confirmation_and_returns_saved_result(tmp_path, monkeypatch):
    import base64
    import io
    from PIL import Image
    from backend.agent.media.catalog import Catalog
    from backend.agent.media.contracts import CatalogConfig

    redis_url = os.environ.get("AIVERO_DURABLE_TEST_REDIS")
    if not redis_url:
        pytest.skip("explicit isolated Redis required")
    monkeypatch.setenv("AIVERO_REDIS_URL", redis_url)
    infer_calls = []
    source_bytes, mask_bytes = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(source_bytes, format="PNG")
    Image.new("L", (64, 64), 255).save(mask_bytes, format="PNG")

    def infer(data):
        infer_calls.append(data)
        return mask_bytes.getvalue()

    async def scenario():
        async with api(tmp_path, cutout_infer=infer) as (app, client, workspace):
            # Local editing needs no provider catalog entries or paid credentials.
            app.state.host.catalog = Catalog(CatalogConfig.model_validate({
                "version": "cutout-http", "environment": "development", "enabled": True, "capabilities": []}))
            assert (await client.get("/api/agent/models")).json()["hostEnabled"] is True
            uploaded = (await client.post("/api/assets?kind=image", content=source_bytes.getvalue())).json()["asset"]
            doc = (await client.post("/api/documents", json={"name": "agent-cutout"})).json()["document"]
            source = {"id": "source", "kind": "image", "assetId": uploaded["id"], "ext": "png", "x": 0, "y": 0, "width": 64, "height": 64}
            saved = await client.post(f"/api/documents/{doc['id']}", json={"objects": {"source": source}, "order": ["source"], "baseRevision": doc["revision"]})
            revision = saved.json()["document"]["revision"]
            request_id = str(uuid.uuid4())
            raw = {"clientRequestId": request_id, "documentId": doc["id"], "message": "帮我抠图，提取主体", "baseRevision": revision, "selectedObjectIds": ["source"]}
            raw["context"] = {"schema_version": 1, "request_id": request_id, "conversation_id": str(uuid.uuid4()),
                "doc_id": doc["id"], "doc_version": revision, "client_ts": 1, "client_id": "client", "tab_id": "tab",
                "message_text": raw["message"], "selection": {"ids": ["source"], "count": 1, "truncated": False},
                "viewport": {"bbox": [0, 0, 1024, 720], "zoom": 1}}
            missing_id = str(uuid.uuid4())
            missing = {**raw, "clientRequestId": missing_id, "selectedObjectIds": [], "context": {
                **raw["context"], "request_id": missing_id, "selection": {"ids": [], "count": 0, "truncated": False}}}
            clarification = await client.post("/api/host/turn", json=missing)
            assert clarification.status_code == 202, clarification.text
            clarification_run = clarification.json()["run"]
            assert clarification_run["status"] == "waiting_input" and not clarification_run.get("canvasAction")
            raw.update(message="选好了", clarifyRunId=clarification_run["id"])
            raw["context"].update(message_text="选好了", conversation_id=clarification_run["sessionId"])
            response = await client.post("/api/host/turn", json=raw)
            assert response.status_code == 202, response.text
            run = response.json()["run"]
            assert run["status"] == "waiting_approval" and run["canvasAction"]["sourceObjectId"] == "source"
            assert infer_calls == []
            replay = await client.post("/api/host/turn", json=raw)
            assert replay.status_code == 200 and replay.json()["replayed"]
            assert (await client.get(f"/api/documents/{doc['id']}/host")).json()["runs"][-1]["canvasAction"] == run["canvasAction"]
            denied = await client.post(f"/api/host/runs/{run['id']}/canvas-result", json={"resultObjectId": "result", "baseRevision": revision}, headers={"x-csrf-token": "wrong"})
            assert denied.status_code == 403
            rejected = await client.post(f"/api/host/runs/{run['id']}/canvas-result", json={"resultObjectId": "result", "baseRevision": revision})
            assert rejected.status_code == 422
            detection = await client.post(f"/api/cutout/detect/{uploaded['id']}?operationId={uuid.uuid4().hex}")
            assert detection.status_code == 200, detection.text
            assert len(infer_calls) == 1
            mask = base64.b64decode(detection.json()["urls"]["original"].split(",", 1)[1])
            applied = await client.post(f"/api/cutout/apply/{uploaded['id']}", content=mask, headers={"content-type": "image/png"})
            assert applied.status_code == 201, applied.text
            result = {**source, "id": "result", "assetId": applied.json()["asset"]["id"],
                "lineage": {"fromId": "source", "params": {"operation": "cutout"}},
                "cutout": {"originalAssetId": uploaded["id"], "originalExt": "png", "maskAssetId": ""}}
            saved = await client.post(f"/api/documents/{doc['id']}", json={"objects": {"source": source, "result": result}, "order": ["source", "result"], "baseRevision": revision})
            receipt = {"resultObjectId": "result", "baseRevision": saved.json()["document"]["revision"]}
            completed = await client.post(f"/api/host/runs/{run['id']}/canvas-result", json=receipt)
            assert completed.status_code == 200, completed.text
            assert completed.json()["run"]["status"] == "completed"
            assert completed.json()["run"]["canvasAction"]["resultAssetId"] == result["assetId"]
            assert (await client.post(f"/api/host/runs/{run['id']}/canvas-result", json=receipt)).json()["replayed"]
    asyncio.run(scenario())
