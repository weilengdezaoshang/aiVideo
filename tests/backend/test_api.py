import base64
import io
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import create_app
from backend.common import write_json


def finish(client, ident):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        job = client.get(f"/api/jobs/{ident}").json()["job"]
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_pages_and_errors(client):
    for path in [
        "/",
        "/workspace",
        "/workspace/",
        "/canvas",
        "/canvas/",
        "/canvas-next/app.js",
    ]:
        assert client.get(path).status_code == 200, path
    for removed in ["/legacy", "/legacy/", "/index.html", "/app.js", "/style.css"]:
        assert client.get(removed).status_code == 404, removed
    assert "/legacy" not in client.get("/workspace").text
    assert client.get("/api/health").json()["provider"] == "mock"
    assert client.get("/api/models").json()["models"]
    assert client.get("/api/samplers").json()["samplers"]
    assert client.get("/api/missing").status_code == 404
    assert client.post("/api/generate", content="{").status_code == 400
    assert client.post("/api/generate", json=[]).status_code == 400
    assert client.get("/api/jobs/missing").status_code == 404
    assert client.post("/api/assets", content=b"").status_code == 400


def test_documents_revision_idempotency_and_restart(root):
    with TestClient(create_app(root)) as c:
        doc = c.post(
            "/api/documents", json={"name": "项目"}, headers={"Idempotency-Key": "same"}
        ).json()["document"]
        assert (
            c.post("/api/documents", json={}, headers={"Idempotency-Key": "same"}).json()[
                "document"
            ]["id"]
            == doc["id"]
        )
        path = "/api/documents/" + doc["id"]
        payload = dict(
            name="节点",
            objects={"x": {"id": "x", "kind": "draft", "mediaType": "video"}},
            order=["x"],
            baseRevision=1,
            chat=[],
            groups={},
        )
        assert c.post(path, json=payload).json()["document"]["revision"] == 2
        conflict = c.post(path, json=payload)
        assert conflict.status_code == 409
        assert conflict.json()["document"]["revision"] == 2
    with TestClient(create_app(root)) as c:
        assert c.get(path).json()["document"]["objects"]["x"]["mediaType"] == "video"
        assert c.get("/api/documents").json()["documents"][0]["objectCount"] == 1
        assert c.put(path + "/rename", json={"name": "改名"}).json()["document"]["revision"] == 3
        assert c.delete(path).json()["removed"]
        assert c.get(path).status_code == 404


def test_image_job_history_and_assets(client, png):
    asset = client.post("/api/assets?ext=png", content=png).json()
    assert asset["asset"]["width"] == 32
    assert client.get(asset["urls"]["original"]).content == png
    assert client.get(asset["urls"]["thumb256"]).status_code == 200
    assert client.get("/api/assets/" + asset["asset"]["id"]).status_code == 200
    assert len(client.get("/api/assets").json()["assets"]) == 1
    ref = "data:image/png;base64," + base64.b64encode(png).decode()
    response = client.post(
        "/api/generate",
        json={
            "prompt": "测试图片",
            "model": "mock",
            "initImage": ref,
            "clientRef": "node-1",
            "width": 64,
            "height": 64,
        },
    )
    assert response.status_code == 202
    job = finish(client, response.json()["jobId"])
    assert job["status"] == "completed", job
    assert job["clientRef"] == "node-1"
    assert job["hasInitImage"]
    image = job["images"][0]
    assert Image.open(io.BytesIO(client.get(image["url"]).content)).size == (64, 64)
    assert client.put("/api/images/" + image["id"] + "/star", json={}).json()["image"]["starred"]
    assert len(client.get("/api/history?starred=1&q=测试").json()["images"]) == 1
    assert (
        client.post("/api/feedback", json={"imageId": image["id"], "action": "deleted"}).status_code
        == 200
    )
    assert client.get("/api/metrics").json()["metrics"]["generations"]["completed"] == 1
    assert client.delete("/api/images/" + image["id"]).status_code == 200
    assert client.get(image["url"]).status_code == 404


def test_video_and_group(client):
    r = client.post(
        "/api/generate",
        json={"prompt": "视频", "model": "mock", "kind": "video", "width": 64, "height": 64},
    )
    job = finish(client, r.json()["jobId"])
    assert job["status"] == "completed"
    assert job["images"][0]["params"]["kind"] == "video"
    response = client.post(
        "/api/generate/group",
        json={
            "prompt": "水墨，小猫，不要文字",
            "model": "mock",
            "clientRefs": ["a", "b", "c", "d"],
            "width": 64,
            "height": 64,
        },
    )
    assert response.status_code == 202
    slots = response.json()["slots"]
    assert len({slot["dimension"] for slot in slots}) == 4
    for slot in slots:
        assert finish(client, slot["jobId"])["status"] == "completed"
    before = len(client.get("/api/jobs?all=1").json()["jobs"])
    assert (
        client.post(
            "/api/generate/group", json={"prompt": "猫", "model": "mock", "clientRefs": ["a"]}
        ).status_code
        == 400
    )
    assert len(client.get("/api/jobs?all=1").json()["jobs"]) == before


def test_cancel_and_recovery(root):
    with TestClient(create_app(root)) as c:
        ids = [
            c.post(
                "/api/generate", json={"prompt": "cancel", "model": "mock", "batchCount": 16}
            ).json()["jobId"]
            for _ in range(4)
        ]
        for ident in ids:
            assert c.delete("/api/jobs/" + ident).status_code == 200
        for ident in ids:
            assert finish(c, ident)["status"] == "failed"
        # Cancelling queued tasks must release capacity for the next request.
        ident = c.post(
            "/api/generate", json={"prompt": "next", "model": "mock", "width": 64, "height": 64}
        ).json()["jobId"]
        assert finish(c, ident)["status"] == "completed"
    with TestClient(create_app(root)) as c:
        assert c.get("/api/jobs/" + ident).json()["job"]["status"] == "completed"
    jobs_file = root / "data/jobs.json"
    raw = json.loads(jobs_file.read_text())
    raw["jobs"][0]["status"] = "running"
    write_json(jobs_file, raw)
    with TestClient(create_app(root)) as c:
        restored = c.get("/api/jobs/" + raw["jobs"][0]["id"]).json()["job"]
        assert restored["status"] == "failed" and "重启" in restored["error"]


@pytest.mark.parametrize("vendor", ["aliyun", "openai", "zhipu", "siliconflow"])
def test_cloud_protocol_and_hot_model(root, png, vendor):
    requests = []

    def upstream(req):
        if req.url.path == "/image.png":
            assert "authorization" not in req.headers
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        payload = json.loads(req.content)
        requests.append(payload)
        assert req.headers["authorization"] == "Bearer fake-only"
        if vendor == "aliyun":
            assert req.url.path == "/api/v1/services/aigc/multimodal-generation/generation"
            assert payload["parameters"]["prompt_extend"] is False
            result = {
                "output": {
                    "choices": [{"message": {"content": [{"image": "https://upstream/image.png"}]}}]
                }
            }
        elif vendor == "siliconflow":
            result = {"images": [{"url": "https://upstream/image.png"}]}
        else:
            result = {"data": [{"b64_json": base64.b64encode(png).decode()}]}
        return httpx.Response(200, json=result)

    with TestClient(create_app(root, transport=httpx.MockTransport(upstream))) as c:
        patch = dict(
            provider="cloud",
            cloudVendor=vendor,
            cloudBaseUrl="https://upstream",
            cloudModel="model-one",
            imageApiKey="fake-only",
        )
        assert c.post("/api/config/test", json=patch).json()["ok"]
        assert c.get("/api/config").json()["config"]["provider"] == "mock"
        response = c.post("/api/config", json=patch)
        assert response.status_code == 200
        assert "fake-only" not in response.text
        for model in ["model-one", "model-two"]:
            c.post("/api/config", json={"cloudModel": model})
            r = c.post(
                "/api/generate",
                json={"prompt": "图片", "model": "ignored", "width": 512, "height": 512},
            )
            job = finish(c, r.json()["jobId"])
            assert job["status"] == "completed", job
            assert job["images"][0]["params"]["model"] == model
            assert requests[-1]["model"] == model
        video = c.post("/api/generate", json={"prompt": "视频", "model": "x", "kind": "video"})
        assert finish(c, video.json()["jobId"])["status"] == "failed"


def test_cutout_session_requires_manual_confirmation(root, png):
    output = io.BytesIO()
    Image.new("L", (32, 24), 128).save(output, "PNG")
    mask = output.getvalue()
    calls = []

    def infer(source):
        calls.append(source)
        assert source == png
        return mask

    with TestClient(create_app(root, cutout_infer=infer)) as c:
        ident = c.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
        assert (
            c.post("/api/agent/sessions", json={"docId": "d", "request": "抠图"}).status_code == 422
        )
        session = c.post(
            "/api/agent/sessions", json={"docId": "d", "assetId": ident, "request": "抠出主体"}
        ).json()["session"]
        sid = session["id"]
        assert c.post(f"/api/cutout/apply/{ident}?sessionId={sid}", content=mask).status_code == 409
        prepared = c.post(f"/api/agent/sessions/{sid}/prepare").json()
        assert prepared["session"]["status"] == "waiting_mask"
        c.post(f"/api/agent/sessions/{sid}/prepare")
        assert len(calls) == 1
        result = c.post(f"/api/cutout/apply/{ident}?sessionId={sid}", content=mask)
        assert result.status_code == 201, result.text
        image = Image.open(io.BytesIO(c.get(result.json()["urls"]["original"]).content))
        assert image.getpixel((0, 0)) == (255, 0, 0, 128)
        assert c.get("/api/agent/sessions/" + sid).json()["session"]["status"] == "completed"
        assert c.post(f"/api/cutout/apply/{ident}?sessionId={sid}", content=mask).status_code == 409
    with TestClient(create_app(root)) as c:
        assert c.get("/api/agent/sessions/" + sid).json()["session"]["status"] == "completed"


def test_node_styles_are_served_and_frontend_revalidates(client):
    page = client.get('/canvas/')
    assert '/canvas-next/canvas-next.css' in page.text
    assert '/canvas-next/app.js' in page.text
    style = client.get('/canvas-next/canvas-next.css')
    assert style.status_code == 200
    assert '.next-shell' in style.text and '.next-canvas' in style.text
    assert style.headers['cache-control'] == 'no-cache'
    assert client.get('/canvas-next/app.js').headers['cache-control'] == 'no-cache'
    assert page.headers['cache-control'] == 'no-cache'
