"""云端文生视频:DashScope 异步任务内核、unknown/reconcile 语义与元数据端点。"""

import io
import json
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import create_app

VIDEO_CONFIG = dict(
    provider="cloud",
    cloudVendor="aliyun",
    cloudBaseUrl="https://upstream",
    cloudModel="qwen-image-2.0",
    imageApiKey="img-key",
    videoApiKey="vid-key",
)
MODEL = "wanx2.1-t2v-turbo"
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42fake-video-payload"


def cloud_client(root, video_model=MODEL, upstream=None):
    config = {**VIDEO_CONFIG, "videoModel": video_model}
    (root / "config.json").write_text(json.dumps(config))
    return TestClient(
        create_app(root, transport=httpx.MockTransport(upstream or (lambda r: httpx.Response(404))))
    )


def test_video_capabilities_gate(root):
    with cloud_client(root) as c:
        caps = c.get("/api/generation-capabilities").json()["video"]
        assert caps["supported"] is True
        assert caps["sizes"] == ["1280x720", "720x1280", "960x960"]
        assert caps["durations"] == [5]
        assert caps["referenceLimit"] == 0
    (root / "config.json").write_text(json.dumps({**VIDEO_CONFIG, "videoModel": ""}))
    with TestClient(create_app(root)) as c:
        caps = c.get("/api/generation-capabilities").json()["video"]
        assert caps["supported"] is False and "视频模型" in caps["reason"]
    (root / "config.json").write_text(json.dumps({**VIDEO_CONFIG, "cloudVendor": "zhipu"}))
    with TestClient(create_app(root)) as c:
        caps = c.get("/api/generation-capabilities").json()["video"]
        assert caps["supported"] is False and "尚未验证" in caps["reason"]


def test_video_happy_path(root):
    calls = []

    def upstream(req):
        calls.append((req.method, req.url.path))
        if req.method == "POST" and req.url.path.endswith("video-synthesis"):
            payload = json.loads(req.content)
            assert payload["model"] == MODEL
            assert payload["parameters"]["size"] == "1280*720"
            assert payload["parameters"]["duration"] == 5
            assert req.headers["x-dashscope-async"] == "enable"
            assert req.headers["authorization"] == "Bearer vid-key"
            return httpx.Response(
                200, json={"output": {"task_id": "t-1", "task_status": "PENDING"}}
            )
        if "/tasks/" in req.url.path:
            polls.append(1)
            status = "RUNNING" if len(polls) == 1 else "SUCCEEDED"
            output = {"task_id": "t-1", "task_status": status}
            if status == "SUCCEEDED":
                output["video_url"] = "https://upstream/result.mp4"
            return httpx.Response(200, json={"output": output})
        if req.url.path.endswith(".mp4"):
            return httpx.Response(200, content=VIDEO_BYTES, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    polls = []
    with cloud_client(root, upstream=upstream) as c:
        response = c.post(
            "/api/generate",
            json={
                "prompt": "一只猫跳上窗台",
                "model": MODEL,
                "kind": "video",
                "width": 1280,
                "height": 720,
                "durationSec": 5,
            },
        )
        job = finish(c, response.json()["jobId"])
        assert job["status"] == "completed", job
        assert job["externalTaskId"] == "t-1"
        assert job["images"][0]["params"]["kind"] == "video"
        assert c.get(job["images"][0]["url"]).content == VIDEO_BYTES
        assert ("POST", "/api/v1/services/aigc/video-generation/video-synthesis") in calls
        # 上游任务只创建一次
        assert sum(1 for method, path in calls if method == "POST") == 1


def test_video_validation_rejects_before_upstream(root):
    calls = []

    def upstream(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"output": {"task_id": "t", "task_status": "PENDING"}})

    with cloud_client(root, upstream=upstream) as c:
        base = {"prompt": "x", "model": MODEL, "kind": "video", "durationSec": 5}
        for size in [(512, 512), (1280, 1280)]:
            response = c.post(
                "/api/generate", json={**base, "width": size[0], "height": size[1]}
            )
            assert response.status_code == 202  # 进入队列由适配器校验
            job = finish(c, response.json()["jobId"])
            assert job["status"] == "failed" and "不支持的画幅" in job["error"]
        response = c.post("/api/generate", json={**base, "width": 960, "height": 960, "durationSec": 4})
        job = finish(c, response.json()["jobId"])
        assert job["status"] == "failed" and "不支持的时长" in job["error"]
        assert calls == []  # 校验失败不产生任何上游调用


def test_video_i2v_not_offered(root):
    def upstream(req):
        return httpx.Response(200, json={"output": {"task_id": "t", "task_status": "PENDING"}})

    with cloud_client(root, upstream=upstream) as c:
        response = c.post(
            "/api/generate",
            json={
                "prompt": "让图动起来",
                "model": MODEL,
                "kind": "video",
                "width": 1280,
                "height": 720,
                "durationSec": 5,
                "initImage": "data:image/png;base64,iVBORw0KGgo=",
            },
        )
        job = finish(c, response.json()["jobId"])
        assert job["status"] == "failed" and "图生视频暂未开放" in job["error"]


def test_video_task_failure_message(root):
    def upstream(req):
        if "/tasks/" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "t",
                        "task_status": "FAILED",
                        "message": "内容违规",
                    }
                },
            )
        return httpx.Response(
            200, json={"output": {"task_id": "t-1", "task_status": "PENDING"}}
        )

    with cloud_client(root, upstream=upstream) as c:
        response = c.post(
            "/api/generate",
            json={
                "prompt": "x",
                "model": MODEL,
                "kind": "video",
                "width": 1280,
                "height": 720,
                "durationSec": 5,
            },
        )
        job = finish(c, response.json()["jobId"])
        assert job["status"] == "failed" and "内容违规" in job["error"]


def write_running_video_job(root, ident):
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    job = {
        "id": ident,
        "status": "running",
        "params": {
            "prompt": "猫",
            "model": MODEL,
            "kind": "video",
            "width": 1280,
            "height": 720,
            "durationSec": 5,
            "seed": -1,
            "batchCount": 1,
        },
        "batchCount": 1,
        "hasInitImage": False,
        "progress": 0.4,
        "message": "生成中",
        "images": [],
        "externalTaskId": "t-restart",
        "createdAt": "2026-09-11T00:00:00.000Z",
        "startedAt": "2026-09-11T00:00:01.000Z",
    }
    (data_dir / "jobs.json").write_text(json.dumps({"jobs": [job]}))


def test_restart_marks_unknown_then_reconcile_completes(root):
    ident = str(uuid.uuid4())
    write_running_video_job(root, ident)
    creates = []

    def upstream(req):
        if req.method == "POST":
            creates.append(req.url.path)
            return httpx.Response(200, json={"output": {"task_id": "t-new"}})
        if "/tasks/t-restart" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "t-restart",
                        "task_status": "SUCCEEDED",
                        "video_url": "https://upstream/result.mp4",
                    }
                },
            )
        if req.url.path.endswith(".mp4"):
            return httpx.Response(200, content=VIDEO_BYTES, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    with cloud_client(root, upstream=upstream) as c:
        job = c.get(f"/api/jobs/{ident}").json()["job"]
        assert job["status"] == "unknown" and job["externalTaskId"] == "t-restart"
        assert "查询" in job["message"]
        # 非 unknown 任务不允许对账
        other = str(uuid.uuid4())
        response = c.post(f"/api/jobs/{other}/reconcile")
        assert response.status_code == 404
        # 对账:只查询既有任务,绝不重新创建
        response = c.post(f"/api/jobs/{ident}/reconcile")
        assert response.status_code == 202
        job = finish(c, ident)
        assert job["status"] == "completed", job
        assert job["images"][0]["url"]
        assert creates == [], "对账不得重新提交上游任务"
        # 完成后再次对账被拒绝
        assert c.post(f"/api/jobs/{ident}/reconcile").status_code == 409


def test_cancel_unknown_stops_tracking_and_tries_upstream(root):
    ident = str(uuid.uuid4())
    write_running_video_job(root, ident)
    cancels = []

    def upstream(req):
        if req.url.path.endswith("/tasks/t-restart/cancel"):
            cancels.append(1)
            return httpx.Response(
                200, json={"output": {"task_id": "t-restart", "task_status": "CANCELED"}}
            )
        return httpx.Response(404)

    with cloud_client(root, upstream=upstream) as c:
        response = c.delete(f"/api/jobs/{ident}")
        assert response.status_code == 200
        job = response.json()["job"]
        assert job["status"] == "failed" and "上游" in job["message"]
        deadline = time.monotonic() + 2
        while not cancels and time.monotonic() < deadline:
            time.sleep(0.02)
        assert cancels, "取消 unknown 任务应尽力请求上游取消"


def test_video_asset_metadata_and_poster(root, png):
    def upstream(req):
        return httpx.Response(404)

    with cloud_client(root, upstream=upstream) as c:
        video = c.post("/api/assets?kind=video&ext=mp4", content=VIDEO_BYTES).json()["asset"]["id"]
        missing = c.post(f"/api/assets/{video}/metadata", json={})
        assert missing.status_code == 400
        bad = c.post(f"/api/assets/{video}/metadata", json={"durationMs": 99999999})
        assert bad.status_code == 400
        meta = c.post(
            "/api/assets/{0}/metadata".format(video),
            json={"durationMs": 5000, "width": 1280, "height": 720},
        )
        assert meta.status_code == 200
        assert meta.json()["asset"]["durationMs"] == 5000
        poster = c.post(f"/api/assets/{video}/poster", content=png)
        assert poster.status_code == 201
        urls = poster.json()["urls"]
        assert urls["poster"] and c.get(urls["poster"]).status_code == 200
        # 图片资产不允许走视频元数据
        image = c.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
        assert c.post(f"/api/assets/{image}/metadata", json={"durationMs": 5000}).status_code == 400


def finish(client, ident):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        job = client.get(f"/api/jobs/{ident}").json()["job"]
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_poll_failure_marks_failed(root):
    def upstream(req):
        if "/tasks/" in req.url.path:
            return httpx.Response(500)
        return httpx.Response(
            200, json={"output": {"task_id": "t-1", "task_status": "PENDING"}}
        )

    with cloud_client(root, upstream=upstream) as c:
        response = c.post(
            "/api/generate",
            json={
                "prompt": "x",
                "model": MODEL,
                "kind": "video",
                "width": 1280,
                "height": 720,
                "durationSec": 5,
            },
        )
        job = finish(c, response.json()["jobId"])
        assert job["status"] == "failed" and "查询视频任务失败" in job["error"]


def test_video_submit_requires_configured_model(root):
    (root / "config.json").write_text(json.dumps({**VIDEO_CONFIG, "videoModel": ""}))
    with TestClient(create_app(root)) as c:
        response = c.post(
            "/api/generate",
            json={
                "prompt": "x",
                "model": "wanx2.1-t2v-turbo",
                "kind": "video",
                "width": 1280,
                "height": 720,
                "durationSec": 5,
            },
        )
        job = finish(c, response.json()["jobId"])
        assert job["status"] == "failed" and "视频模型" in job["error"]


@pytest.fixture
def png():
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(output, "PNG")
    return output.getvalue()


def test_video_model_can_be_saved_from_settings(root, monkeypatch):
    monkeypatch.setenv("SWARMUI_ENV", "development")
    with cloud_client(root, video_model="") as c:
        response = c.post("/api/config", json={"videoModel": MODEL})
        assert response.status_code == 200
        assert response.json()["config"]["videoModel"] == MODEL
        assert c.get("/api/generation-capabilities").json()["video"]["supported"] is True
        assert json.loads((root / "config.json").read_text())["videoModel"] == MODEL
