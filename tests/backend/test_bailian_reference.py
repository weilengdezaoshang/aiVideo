"""Exercise reference upload through node/group submission and cloud result storage."""

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.evaluation.sandbox import create_sandbox_app as create_app
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
        assert body["model"] == "qwen-image-2.0"
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
            assert job["images"][0]["params"]["model"] == "qwen-image-2.0"
        assert len(calls) == (4 if group else 1)


def test_staged_final_keeps_qwen2_model_and_preview_reference(png):
    """The durable final-image path must not substitute the retired edit-plus model."""
    import asyncio
    from backend.config import Config
    from backend.models import GenParams, InitImage
    from backend.providers.staged import StagedProvider

    async def scenario():
        def upstream(request):
            body = json.loads(request.content)
            assert body["model"] == "qwen-image-2.0"
            assert body["parameters"]["size"] == "768*768"
            assert body["parameters"]["seed"] == 803535831
            content = body["input"]["messages"][0]["content"]
            assert base64.b64decode(content[0]["image"].split(",", 1)[1]) == png
            return httpx.Response(200, json={"output": {"choices": [{"message": {
                "content": [{"image": "https://upstream/final.png"}]
            }}]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            provider = StagedProvider(Config(provider="cloud", cloudVendor="aliyun",
                cloudBaseUrl="https://upstream/api/v1", cloudModel="qwen-image-2.0",
                imageApiKey="fake-only"), client)
            params = GenParams(prompt="保留主体", model="qwen-image-2.0", width=768, height=768)
            reference = InitImage(png, "png")
            prepared = await provider.prepare(params, 803535831, reference)
            result = await provider.submit(params, 803535831, prepared.payload, "final-test", reference)
            assert result.phase == "download"
            assert result.payload["url"] == "https://upstream/final.png"
    asyncio.run(scenario())
