import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from backend.common import read_json, write_json
from backend.config import Config, load_config, patch_config, public_config
from backend.jobs import Jobs
from backend.models import GenParams, parse_image, parse_params
from backend.planning import directions, rule_plan
from backend.providers.base import Generated, Provider
from backend.providers.comfyui import ComfyProvider
from backend.providers.cloud import CloudProvider
from backend.providers.workflows import image_workflow, ltx_workflow, wan_workflow
from backend.storage import History, Documents, apply_mask
from backend.traces import Traces

CASES = json.loads((Path(__file__).parent / "fixtures/comfy-workflows.json").read_text())


@pytest.mark.parametrize(
    "case", CASES, ids=lambda c: f"{c['type']}-{c.get('ref')}-{c.get('mask')}-{c.get('low')}"
)
def test_workflows_match_previous_backend(case):
    p = GenParams.model_validate(case["p"])
    if case["type"] == "image":
        result = image_workflow(p, 42, case["ref"], case.get("mask"))
    elif case["type"] == "ltx":
        result = ltx_workflow(p, 42, "model", case["ref"])
    else:
        result = wan_workflow(p, 42, "model", "clip", "vae", case["ref"], case.get("low"))
    assert result == case["expected"]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"prompt": "x"},
        {"prompt": "", "model": "m"},
        {"prompt": "x" * 4001, "model": "m"},
    ],
)
def test_invalid_params(raw):
    with pytest.raises(ValueError):
        parse_params(raw)


def test_normalization_and_reference_validation():
    p = parse_params(
        dict(prompt=" x ", model=" m ", width=513, height=99999, steps=-2, fps=999, seed=-5)
    )
    assert (p.width, p.height, p.steps, p.fps, p.seed) == (512, 2048, 1, 30, -1)
    for value in [
        1,
        "https://example.com/image.png",
        "data:image/png;base64,====",
        "data:image/jpeg;base64,YQ==",
    ]:
        with pytest.raises(ValueError):
            parse_image(value, mask=True)


def test_environment_and_masked_config(root, monkeypatch):
    write_json(root / "config.json", {"provider": "mock", "imageApiKey": "file-secret"})
    monkeypatch.setenv("SWARMUI_IMAGE_API_KEY", "environment-secret")
    c = load_config(root)
    patched, overridden = patch_config(c, {"imageApiKey": "new-file-secret"}, root)
    assert patched.imageApiKey == "environment-secret"
    assert overridden == ["imageApiKey"]
    assert read_json(root / "config.json", {})["imageApiKey"] == "new-file-secret"
    assert "environment-secret" not in json.dumps(public_config(patched))
    with pytest.raises(ValueError):
        patch_config(c, {"provider": "wrong"})
    with pytest.raises(ValueError):
        patch_config(c, {"cloudBaseUrl": "file:///secret"})


def test_isolated_config_override(root, monkeypatch):
    """隔离实例用 SWARMUI_CONFIG 指向独立配置，不读取仓库根的 config.json。"""
    write_json(root / "config.json", {"provider": "mock", "imageApiKey": "repo-secret"})
    isolated = root / "isolated"
    isolated.mkdir()
    write_json(isolated / "instance.json", {"provider": "mock"})
    monkeypatch.setenv("SWARMUI_CONFIG", str(isolated / "instance.json"))
    c = load_config(root)
    assert c.provider == "mock"
    assert c.imageApiKey == "", "隔离实例不得继承仓库配置中的密钥"
    monkeypatch.delenv("SWARMUI_CONFIG")
    assert load_config(root).imageApiKey == "repo-secret"


def test_document_rejects_bad_shapes(root):
    docs = Documents(root / "documents")
    doc = docs.create()
    for payload in [
        {"objects": [], "order": []},
        {"objects": {}, "order": [1]},
        {"objects": {}, "order": [], "groups": []},
    ]:
        with pytest.raises(ValueError):
            docs.save(doc["id"], payload)
    assert docs.get("../config") is None
    assert not docs.remove("../config")


def test_mask_size_and_alpha(png):
    output = io.BytesIO()
    Image.new("L", (1, 1), 255).save(output, "PNG")
    with pytest.raises(ValueError, match="尺寸"):
        apply_mask(png, output.getvalue())


def test_direction_constraints_and_agent_scope():
    plan = directions("小猫，水墨，不要文字")
    assert len(plan["directions"]) == 4
    assert all("水墨" in d["prompt"] and "不要文字" in d["prompt"] for d in plan["directions"])
    assert rule_plan("抠图")["ok"]
    assert not rule_plan("抠图再生成视频")["ok"]
    assert not rule_plan("你好")["ok"]


def test_comfy_http_upload_poll_download(png):
    async def scenario():
        seen = []

        def handle(request):
            seen.append(request.url.path)
            if request.url.path == "/upload/image":
                assert b"filename=" in request.content
                return httpx.Response(200, json={"name": "reference.png"})
            if request.url.path == "/prompt":
                assert (
                    json.loads(request.content)["prompt"]["10"]["inputs"]["image"]
                    == "reference.png"
                )
                return httpx.Response(200, json={"prompt_id": "pid"})
            if request.url.path == "/history/pid":
                return httpx.Response(
                    200,
                    json={
                        "pid": {
                            "outputs": {
                                "9": {
                                    "images": [
                                        {"filename": "out.png", "subfolder": "", "type": "output"}
                                    ]
                                }
                            }
                        }
                    },
                )
            if request.url.path == "/view":
                return httpx.Response(200, content=png)
            raise AssertionError(request.url)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            from backend.models import InitImage

            provider = ComfyProvider(Config(), client)
            result = await provider.generate(
                GenParams(prompt="x", model="m"), 3, lambda *_: None, InitImage(png, "png")
            )
            assert result.data == png and result.ext == "png"
        assert seen == ["/upload/image", "/prompt", "/history/pid", "/view"]

    asyncio.run(scenario())


def test_queue_hot_switch_and_cancel_before_first_tick(root):
    class Fake(Provider):
        capacity = 1

        def __init__(self, name):
            self.name = name
            self.calls = 0

        async def status(self):
            return {"ok": True}

        async def models(self):
            return []

        async def generate(
            self, p, seed, progress, image=None, mask=None, external=None, external_task_id=None
        ):
            self.calls += 1
            await asyncio.sleep(0.01)
            return Generated(b"data", "png")

    async def scenario():
        old, new = Fake("old"), Fake("new")
        jobs = Jobs(old, History(root), root / "jobs.json", Traces(root / "traces.jsonl"))
        p = GenParams(prompt="x", model="m", batchCount=2)
        early = jobs.create(p)
        jobs.cancel(early["id"])
        await asyncio.sleep(0.01)
        assert not jobs.running and not jobs.references
        first = jobs.create(p)
        await asyncio.sleep(0.002)
        jobs.provider = new
        second = jobs.create(p)
        await asyncio.sleep(0.1)
        assert first["status"] == second["status"] == "completed"
        assert old.calls == new.calls == 2
        assert all(x["provider"] == "old" for x in first["images"])
        assert all(x["provider"] == "new" for x in second["images"])
        assert not jobs.running and not jobs.references
        await jobs.close()

    asyncio.run(scenario())


def test_polling_timeout_with_external_task_enters_reconcilable_unknown(root):
    """轮询超时但已上报上游任务 ID 的任务进入可对账的 unknown 状态,对账只查询不重新付费提交。"""

    class TimeoutExternal(Provider):
        capacity = 1

        def __init__(self):
            self.name = "fake"
            self.calls = []

        async def status(self):
            return {"ok": True}

        async def models(self):
            return []

        async def generate(
            self, p, seed, progress, image=None, mask=None, external=None, external_task_id=None
        ):
            self.calls.append(external_task_id)
            if external:
                external("upstream-task-1")  # 上游付费任务已创建
            raise TimeoutError()  # 模拟轮询超时,结果未知

    async def scenario():
        provider = TimeoutExternal()
        jobs = Jobs(provider, History(root), root / "jobs.json", Traces(root / "traces.jsonl"))
        job = jobs.create(GenParams(prompt="x", model="m"))
        while job["status"] in {"queued", "running"}:
            await asyncio.sleep(0.01)
        assert job["status"] == "unknown", job
        assert job["externalTaskId"] == "upstream-task-1"

        resumed = jobs.resume(job["id"])  # 用户显式对账
        assert resumed is not None
        while resumed["status"] in {"queued", "running"}:
            await asyncio.sleep(0.01)
        assert provider.calls == [None, "upstream-task-1"]  # 对账沿用同一上游 ID
        await jobs.close()

    asyncio.run(scenario())


def test_cancel_running_job_attempts_upstream_cancel(root):
    """取消已派发上游的运行中任务时,用原 Provider 尽力取消上游并标注结果待确认。"""

    class CancelAware(Provider):
        capacity = 1

        def __init__(self):
            self.name = "fake"
            self.cancel_calls = []

        async def status(self):
            return {"ok": True}

        async def models(self):
            return []

        async def cancel_external(self, task_id):
            self.cancel_calls.append(task_id)
            return True

        async def generate(
            self, p, seed, progress, image=None, mask=None, external=None, external_task_id=None
        ):
            if external:
                external("upstream-task-1")
            await asyncio.sleep(5)  # 长轮询,等待被取消

    async def scenario():
        provider = CancelAware()
        jobs = Jobs(provider, History(root), root / "jobs.json", Traces(root / "traces.jsonl"))
        job = jobs.create(GenParams(prompt="x", model="m"))
        while not job.get("externalTaskId"):
            await asyncio.sleep(0.01)
        jobs.cancel(job["id"])
        deadline = asyncio.get_event_loop().time() + 2
        while not provider.cancel_calls and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert provider.cancel_calls == ["upstream-task-1"]
        assert job["status"] == "failed"
        assert "待确认" in job["message"]
        await jobs.close()

    asyncio.run(scenario())


def test_cloud_edit_and_error_redaction(png):
    from backend.models import InitImage
    from backend.providers.policy import ProviderFailure

    async def scenario():
        config = Config(
            provider="cloud",
            cloudVendor="aliyun",
            cloudBaseUrl="https://upstream/api/v1",
            cloudModel="qwen-image-2.0",
            imageApiKey="private-key",
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(401, json={"message": "bad private-key"})
            )
        ) as client:
            provider = CloudProvider(config, client)
            p = GenParams(prompt="x", model="x")
            url, payload = provider.request(p, 5, InitImage(png, "png"), None)
            assert payload["model"] == "qwen-image-edit-plus"
            assert payload["input"]["messages"][0]["content"][0]["image"].startswith("data:")
            with pytest.raises(ProviderFailure) as error:
                await provider.generate(p, 5, lambda *_: None)
            assert "private-key" not in str(error.value)
            assert error.value.code.value == "UPSTREAM_AUTH"

    asyncio.run(scenario())
