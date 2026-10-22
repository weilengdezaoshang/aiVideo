"""Pure tests and durable fake service tests; no paid providers."""
import asyncio

import httpx
import pytest

from backend.core.execution.contracts import WorkerSettings
from backend.core.execution.layout import choose, overlaps


def test_layout_replace_cas_and_bounded_region():
    settings = WorkerSettings(default_width=10, default_height=10, layout_gap=2, layout_steps=5)
    data = {"objects": {"a": {"x": 0, "y": 0, "width": 10, "height": 10, "assetVersion": 1}}}
    rect, conflict, replacement = choose(data, {"mode": "replace", "anchor_id": "a", "anchor_version": 1}, settings)
    assert not conflict and replacement == "a"
    rect, conflict, replacement = choose(data, {"mode": "replace", "anchor_id": "a", "anchor_version": 0}, settings)
    assert conflict and replacement is None and not overlaps(rect, data["objects"]["a"])
    region = {"x": 0, "y": 0, "width": 10, "height": 10}
    assert choose(data, {"mode": "region", "region": region, "writable_region": region}, settings)[0] is None


@pytest.mark.parametrize("mode", ["right", "below"])
def test_relative_layout_does_not_use_model_coordinates(mode):
    settings = WorkerSettings(default_width=10, default_height=10, layout_gap=2)
    node = {"x": 10, "y": 20, "width": 10, "height": 10, "assetVersion": 1}
    rect, _, _ = choose({"objects": {"a": node}}, {"mode": mode, "anchor_id": "a", "anchor_version": 1}, settings)
    assert (rect["x"], rect["y"]) == ((22, 20) if mode == "right" else (10, 32))


def test_staged_adapter_preserves_moderation_and_uncertain_submission():
    from backend.agent.media.catalog import Catalog
    from backend.agent.media.contracts import CatalogConfig
    from backend.core.tools import build_catalog
    from backend.core.execution.adapters import StagedAdapter

    async def scenario():
        config = CatalogConfig.model_validate({"version": "unit", "environment": "development", "enabled": True,
            "capabilities": [{"id": "text_to_image", "enabled": True, "outputType": "image",
                "tiers": {"preview": {"credits": 10}}, "binding": {"version": "unit", "providerSnapshot": {
                    "provider": "cloud", "configVersion": "unit", "cloudVendor": "aliyun", "cloudModel": "qwen-image-2.0",
                    "cloudBaseUrl": "https://provider.test/api/v1", "credentialRef": "test"}}}]})
        catalog = build_catalog(Catalog(config), "test")
        tool = catalog.get_tool("text_to_image_preview", "test")
        def blocked(request):
            return httpx.Response(400, json={"code": "DataInspectionFailed"})
        params = {"prompt": "synthetic test", "count": 1, "width": 512, "height": 512, "_prepared": {}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as client:
            adapter = StagedAdapter(tool, client, None, {"test": {"imageApiKey": "fake"}}, set())
            result = await adapter.submit("registration", params, None)
            assert result.failure == "blocked_by_model" and result.actual_credits == 0
        def timeout(request):
            raise httpx.ReadTimeout("lost response")
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            adapter = StagedAdapter(tool, client, None, {"test": {"imageApiKey": "fake"}}, set())
            with pytest.raises(TimeoutError):
                await adapter.submit("registration", params, None)
    asyncio.run(scenario())


def test_fake_artifacts_preserve_video_audio_and_mask_types(tmp_path):
    from backend.core.execution.fake import FakeProvider, Scenario
    from backend.services.media import inspect_media
    import io
    from PIL import Image
    async def scenario():
        fake = FakeProvider(tmp_path / "fake.sqlite", Scenario())
        result = await fake.submit("media", {}, None)
        for kind in ("video", "audio", "image"):
            data, ext = await fake.download(result.locator, kind)
            assert inspect_media(data, kind, ext)["ext"] == ext
        data, ext = await fake.download(result.locator, "mask")
        with Image.open(io.BytesIO(data)) as mask:
            assert mask.mode == "L"
    asyncio.run(scenario())
