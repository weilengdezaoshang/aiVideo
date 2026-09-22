"""Explicit isolated PostgreSQL tests for the database-only HTTP boundary."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import select

from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.orm import Job, Workspace, WorkspaceMember
from backend.production import create_app
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


def test_real_export_worker_uses_database_and_shared_assets(tmp_path):
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
