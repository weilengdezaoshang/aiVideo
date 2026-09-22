"""图生图参考强度(referenceWeight)与组图生图的契约与行为测试。"""

import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.models import REFERENCE_WEIGHT_DENOISE, parse_params, with_reference_weight


def test_reference_weight_parse_contract():
    assert parse_params({"prompt": "x", "model": "m"}).referenceWeight == ""
    assert (
        parse_params({"prompt": "x", "model": "m", "referenceWeight": " HIGH "}).referenceWeight
        == "high"
    )
    for bad in ["strong", 1, ["low"]]:
        with pytest.raises(ValueError):
            parse_params({"prompt": "x", "model": "m", "referenceWeight": bad})
    assert parse_params({"prompt": "x", "model": "m", "referenceWeight": None}).referenceWeight == ""


def test_reference_weight_maps_denoise_only_with_image():
    p = parse_params({"prompt": "x", "model": "m", "denoise": 1, "referenceWeight": "low"})
    assert with_reference_weight(p, True).denoise == REFERENCE_WEIGHT_DENOISE["low"]
    assert with_reference_weight(p, False).denoise == 1
    plain = parse_params({"prompt": "x", "model": "m"})
    assert with_reference_weight(plain, True) is plain


def test_capabilities_report_weight_adjustability(root):
    with TestClient(create_app(root)) as c:
        image = c.get("/api/generation-capabilities").json()["image"]
        assert image["referenceWeightAdjustable"] is True
    cloud = dict(
        provider="cloud",
        cloudVendor="aliyun",
        cloudBaseUrl="https://upstream",
        cloudModel="m",
        imageApiKey="fake-only",
    )
    noop = httpx.MockTransport(lambda r: httpx.Response(200))
    with TestClient(create_app(root, transport=noop)) as c:
        c.post("/api/config", json=cloud)
        image = c.get("/api/generation-capabilities").json()["image"]
        assert image["referenceWeightAdjustable"] is False


def test_generate_with_reference_weight(client, png):
    asset = client.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
    response = client.post(
        "/api/generate",
        json={
            "prompt": "同主体换构图",
            "model": "mock",
            "width": 64,
            "height": 64,
            "referenceAssetIds": [asset],
            "referenceWeight": "high",
        },
    )
    job = finish(client, response.json()["jobId"])
    assert job["status"] == "completed"
    assert job["hasInitImage"]
    assert job["params"]["referenceWeight"] == "high"
    # 无参考图却携带强度 → 明确报错,不静默忽略
    stray = client.post(
        "/api/generate",
        json={"prompt": "x", "model": "mock", "referenceWeight": "low"},
    )
    assert stray.status_code == 400 and "参考强度" in stray.json()["error"]
    # 非法枚举 → 400
    bad = client.post(
        "/api/generate",
        json={"prompt": "x", "model": "mock", "referenceWeight": "strong"},
    )
    assert bad.status_code == 400


def test_cloud_rejects_reference_weight(root, png):
    upstream_calls = []

    def upstream(req):
        upstream_calls.append(req.url.path)
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    with TestClient(create_app(root, transport=httpx.MockTransport(upstream))) as c:
        asset_id = c.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
        c.post(
            "/api/config",
            json=dict(
                provider="cloud",
                cloudVendor="aliyun",
                cloudBaseUrl="https://upstream",
                cloudModel="qwen-image-2.0",
                imageApiKey="fake-only",
            ),
        )
        response = c.post(
            "/api/generate",
            json={
                "prompt": "编辑",
                "model": "qwen-image-edit-plus",
                "referenceAssetIds": [asset_id],
                "referenceWeight": "medium",
            },
        )
        assert response.status_code == 400
        assert "不支持参考强度" in response.json()["error"]
        assert upstream_calls == []  # 校验失败不产生任何上游调用


def test_group_with_reference_image(client, png):
    asset = client.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
    response = client.post(
        "/api/generate/group",
        json={
            "prompt": "小猫",
            "model": "mock",
            "clientRefs": ["a", "b", "c", "d"],
            "width": 64,
            "height": 64,
            "referenceAssetIds": [asset],
            "referenceWeight": "low",
        },
    )
    assert response.status_code == 202
    slots = response.json()["slots"]
    assert len(slots) == 4
    for slot in slots:
        job = finish(client, slot["jobId"])
        assert job["status"] == "completed"
        assert job["hasInitImage"]
        assert job["params"]["referenceWeight"] == "low"
    # 缺参考图的强度 → 400 且零任务创建
    before = len(client.get("/api/jobs?all=1").json()["jobs"])
    stray = client.post(
        "/api/generate/group",
        json={
            "prompt": "小猫",
            "model": "mock",
            "clientRefs": ["a", "b", "c", "d"],
            "referenceWeight": "low",
        },
    )
    assert stray.status_code == 400
    after = len(client.get("/api/jobs?all=1").json()["jobs"])
    assert before == after
    # 多于一张参考图 → 400
    extra = client.post(
        "/api/generate/group",
        json={
            "prompt": "小猫",
            "model": "mock",
            "clientRefs": ["a", "b", "c", "d"],
            "referenceAssetIds": [asset, asset],
        },
    )
    assert extra.status_code == 400


def test_group_cloud_vendor_gating(root, png):
    def upstream(req):
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    with TestClient(create_app(root, transport=httpx.MockTransport(upstream))) as c:
        asset = c.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
        c.post(
            "/api/config",
            json=dict(
                provider="cloud",
                cloudVendor="zhipu",
                cloudBaseUrl="https://upstream",
                cloudModel="cogview-4-250304",
                imageApiKey="fake-only",
            ),
        )
        response = c.post(
            "/api/generate/group",
            json={
                "prompt": "小猫",
                "model": "cogview-4-250304",
                "clientRefs": ["a", "b", "c", "d"],
                "referenceAssetIds": [asset],
            },
        )
        assert response.status_code == 400 and "暂不支持参考图" in response.json()["error"]


def test_request_hash_covers_reference_weight(root, png):
    with TestClient(create_app(root)) as c:
        did = c.post("/api/documents", json={"name": "t"}).json()["document"]["id"]
        asset = c.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
        request_id = str(uuid.uuid4())
        payload = {
            "documentId": did,
            "requestId": request_id,
            "clientRef": "node-1",
            "prompt": "x",
            "model": "mock-diffusion-xl",
            "width": 64,
            "height": 64,
            "referenceAssetIds": [asset],
            "referenceWeight": "medium",
        }
        first = c.post("/api/generate", json=payload)
        assert first.status_code == 202
        again = c.post("/api/generate", json=payload)
        assert again.status_code == 202 and again.json()["jobId"] == first.json()["jobId"]
        changed = c.post("/api/generate", json={**payload, "referenceWeight": "high"})
        assert changed.status_code == 409


def finish(client, ident):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        job = client.get(f"/api/jobs/{ident}").json()["job"]
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")
