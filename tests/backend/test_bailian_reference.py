"""Exercise reference upload through node/group submission and cloud result storage."""

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from tests.backend.test_api import finish
from tests.backend.test_nodes import payload


@pytest.mark.parametrize("group", [False, True])
def test_bailian_reference_roundtrip(root, png, group):
    calls = []

    def upstream(request):
        if request.url.path == "/result.png":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "qwen-image-edit-plus"
        content = body["input"]["messages"][0]["content"]
        assert base64.b64decode(content[0]["image"].split(",", 1)[1]) == png
        assert "保留主体" in content[1]["text"]
        return httpx.Response(200, json={"output": {"choices": [{"message": {
            "content": [{"image": "https://upstream/result.png"}]
        }}]}})

    with TestClient(create_app(root, transport=httpx.MockTransport(upstream))) as client:
        raw = payload(client)
        asset = client.post("/api/assets?ext=png", content=png).json()["asset"]
        client.post("/api/config", json={
            "provider": "cloud", "cloudVendor": "aliyun",
            "cloudBaseUrl": "https://upstream", "cloudModel": "qwen-image-2.0",
            "imageApiKey": "fake-only",
        }).raise_for_status()
        raw.update(prompt="保留主体，背景改成森林", model="qwen-image-2.0",
                   width=1024, height=1024, referenceAssetIds=[asset["id"]])
        if group:
            raw["clientRefs"] = [f"slot-{i}" for i in range(4)]
        response = client.post("/api/generate/group" if group else "/api/generate", json=raw)
        assert response.status_code == 202, response.text
        identifiers = ([slot["jobId"] for slot in response.json()["slots"]]
                       if group else [response.json()["jobId"]])
        for ident in identifiers:
            job = finish(client, ident)
            assert job["status"] == "completed", job
            assert job["hasInitImage"]
            assert len(job["images"]) == 1
            assert job["images"][0]["params"]["model"] == "qwen-image-edit-plus"
        assert len(calls) == (4 if group else 1)
