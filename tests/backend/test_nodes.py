from uuid import uuid4
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.capabilities import generation_capabilities
from backend.config import Config
from tests.backend.test_api import finish


def payload(client):
    did = client.post("/api/documents", json={"name": "节点测试"}).json()["document"]["id"]
    return dict(
        documentId=did,
        clientRef="node-test",
        requestId=str(uuid4()),
        prompt="节点图片",
        model="mock-diffusion-xl",
        width=64,
        height=64,
        batchCount=1,
    )


def test_node_reference_idempotency_and_restart(root, png):
    with TestClient(create_app(root)) as client:
        raw = payload(client)
        asset = client.post("/api/assets?ext=png", content=png).json()["asset"]
        raw["referenceAssetIds"] = [asset["id"]]
        first = client.post("/api/generate", json=raw)
        assert first.status_code == 202, first.text
        ident = first.json()["jobId"]
        assert client.post("/api/generate", json=raw).json()["jobId"] == ident
        assert client.post("/api/generate", json={**raw, "prompt": "changed"}).status_code == 409
        job = finish(client, ident)
        assert job["status"] == "completed"
        assert job["hasInitImage"]
        assert job["clientRef"] == raw["clientRef"]
        assert job["documentId"] == raw["documentId"]
    with TestClient(create_app(root)) as client:
        assert client.post("/api/generate", json=raw).json()["jobId"] == ident
        path = f"/api/generation-requests/{raw['requestId']}?documentId={raw['documentId']}"
        assert client.get(path).json()["job"]["status"] == "completed"


def test_cancel_before_submit_and_legacy_binding(client):
    raw = payload(client)
    path = f"/api/generation-requests/{raw['requestId']}?documentId={raw['documentId']}"
    assert client.delete(path).status_code == 200
    assert client.post("/api/generate", json=raw).status_code == 409
    assert client.get(path).json()["cancelled"]
    raw.pop("requestId")
    response = client.post("/api/generate", json=raw)
    assert response.status_code == 202
    assert "documentId" not in response.json()["job"]


def test_node_rejects_invalid_options_and_capabilities(client):
    raw = payload(client)
    assert client.get("/api/generation-capabilities").json()["video"]["supported"]
    assert (
        client.post("/api/generate", json={**raw, "referenceAssetIds": ["missing"]}).status_code
        == 404
    )
    for patch in [{"batchCount": 2}, {"model": "unknown"}]:
        assert client.post("/api/generate", json={**raw, **patch}).status_code == 400
    caps = generation_capabilities(Config(provider="cloud", cloudVendor="openai"), [])
    assert caps["image"]["referenceLimit"] == 0
    assert not caps["video"]["supported"]
