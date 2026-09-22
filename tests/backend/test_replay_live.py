"""严格回放与预算验收(§21 REPLAY-01..08;P1 纵向切片)。

live 录制使用注入的 stub 上游 transport(zhipu 兼容协议形状),不产生真实费用;
真实云端录制属于"待真实验收"范围。回放执行真实业务代码
(app → jobs → provider),只替换 HTTP 边界。
"""

import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from backend.evaluation.artifacts import ObjectStore
from backend.evaluation.replay import (
    NetworkForbidden,
    RecordingIncomplete,
    RecordingReader,
    ReplayMismatch,
    ReplayTransport,
)
from backend.evaluation.recorder import RecordingWriter
from backend.evaluation.runner import EvaluationRunner

from eval_helpers import make_case, write_jsonl

STUB_BASE = "http://stub.local/api/paas/v4"
PRICES = {"generation": 0.5, "translate": 0.05, "poll": 0.0}


def tiny_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(output, "PNG")
    return output.getvalue()


class StubUpstream(httpx.AsyncBaseTransport):
    """zhipu 兼容协议形状的沙盒上游:翻译 + 文生图(b64 返回,无独立下载)。"""

    def __init__(self, png: bytes | None = None, translate="a white cat", fail_status=None):
        self.png = png or tiny_png()
        self.translate = translate
        self.fail_status = fail_status
        self.requests = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/chat/completions"):
            if self.fail_status:
                return httpx.Response(self.fail_status, json={"error": {"message": "rate limited"}})
            return httpx.Response(
                200, json={"choices": [{"message": {"content": self.translate}}]}
            )
        if path.endswith("/images/generations"):
            if self.fail_status:
                return httpx.Response(self.fail_status, json={"error": {"message": "rate limited"}})
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(self.png).decode()}]}
            )
        return httpx.Response(404, json={"error": "unknown path"})


@pytest.fixture
def cloud_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMUI_IMAGE_API_KEY", "test-key")
    instance = EvaluationRunner(tmp_path, store=tmp_path / "store")
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case("t2i-cloud-01", prompt="一只白猫")])
    instance.freeze_dataset(source, "ds-zh")
    return instance


def create_live(runner, run_id, **overrides):
    budget = {
        "currency": "CNY",
        "maxCost": overrides.pop("max_cost", 5.0),
        "prices": dict(PRICES),
    }
    budget.update(overrides.pop("budget_extra", {}))
    return runner.create_run(
        run_id,
        "ds-zh",
        mode="live",
        provider="cloud",
        purpose="P1 录制切片",
        budget=budget,
        sandbox_config={
            "cloudVendor": "zhipu",
            "cloudBaseUrl": STUB_BASE,
            "cloudModel": "test-image",
            "cloudTextModel": "test-text",
        },
        **overrides,
    )


def read_state(runner, run_id):
    return json.loads((runner.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))


def test_live_run_records_budgets_and_seals(tmp_path, cloud_runner):
    upstream = StubUpstream()
    manifest = create_live(cloud_runner, "run-live")
    assert manifest.recordingId == "rec-run-live"
    state = cloud_runner.execute_run("run-live", transport=upstream)
    assert state.status == "completed"
    assert all(trial.status == "completed" for trial in state.trials)

    # 账本:翻译与生成各自预留并按估算结算
    state_file = read_state(cloud_runner, "run-live")
    assert state_file["budget"]["totalCalls"] == 2  # translate + generation
    assert state_file["budget"]["settled"] == pytest.approx(0.55)
    assert state_file["recordingSealed"] is True

    # 录制:封存、host 清单、敏感头脱敏
    recording_dir = tmp_path / "store" / "recordings" / "rec-run-live"
    meta = json.loads((recording_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["sealed"] is True and meta["interactionCount"] == 2
    assert meta["hosts"] == ["stub.local"]
    interactions = [
        json.loads(line)
        for line in (recording_dir / "interactions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    call_sites = {item["callSite"] for item in interactions}
    assert call_sites == {"translate", "generation"}
    for item in interactions:
        assert item["request"]["headers"]["authorization"] == "[REDACTED]"
    # 翻译请求保留原始提示词,生成请求使用翻译后文本(供排查对照)
    translate_body = json.loads(interactions[0]["request"]["body"])
    assert translate_body["messages"][1]["content"] == "一只白猫"
    generation_body = json.loads(interactions[1]["request"]["body"])
    assert generation_body["prompt"] == "a white cat"


def test_replay01_twice_identical_projection_zero_new_calls(tmp_path, cloud_runner):
    create_live(cloud_runner, "run-live")
    cloud_runner.execute_run("run-live", transport=StubUpstream())
    live_state = cloud_runner.load_run("run-live")

    def replay(run_id):
        cloud_runner.create_run(
            run_id, "ds-zh", mode="replay", source_run_id="run-live", recording_id="rec-run-live"
        )
        state = cloud_runner.execute_run(run_id)  # 不提供任何网络 transport
        meta = read_state(cloud_runner, run_id)["replay"]
        assert meta["newExternalCalls"] == 0  # 实际观测,不是硬编码断言对象
        assert meta["unconsumedInPlan"] == []
        assert meta["unconsumedOutOfPlan"] == []
        assert meta["verification"] == "passed"
        return state

    first = replay("replay-a")
    second = replay("replay-b")

    def projection(state):
        return [
            (t.caseId, t.status, t.resolvedSeed, t.qualityVerdict, t.artifactIds[0])
            for t in state.trials
        ]

    assert projection(first) == projection(second)
    assert projection(first) == projection(live_state)
    assert first.trials[0].status == "completed"
    assert first.trials[0].qualityVerdict == "pass"


def test_replay02_prompt_change_fails_at_boundary_with_diff(tmp_path, cloud_runner):
    create_live(cloud_runner, "run-live")
    cloud_runner.execute_run("run-live", transport=StubUpstream())
    cloud_runner.create_run(
        "replay-edit", "ds-zh", mode="replay", source_run_id="run-live", recording_id="rec-run-live"
    )
    # 改了提示词但复用旧录制:严格回放在实际调用边界失败并显示差异(§7.4);
    # 差异同时升级为 run 级完整性失败(影响 CLI 退出码与门禁,不只进日志)。
    cases_file = cloud_runner.run_dir("replay-edit") / "dataset" / "cases.jsonl"
    cases_file.write_text(
        cases_file.read_text(encoding="utf-8").replace("一只白猫", "两只黑狗"), encoding="utf-8"
    )
    from backend.evaluation.runner import ReplayIntegrityFailed

    with pytest.raises(ReplayIntegrityFailed, match="未消费交互"):
        cloud_runner.execute_run("replay-edit")
    state = cloud_runner.load_run("replay-edit")
    assert state.status == "failed"
    trial = state.trials[0]
    assert trial.status == "failed"
    assert "REPLAY_REQUEST_MISMATCH" in (trial.error or "")
    assert "字段" in (trial.error or "")  # 字段级 diff
    assert "两只黑狗" in (trial.error or "")
    meta = read_state(cloud_runner, "replay-edit")["replay"]
    assert meta["verification"] == "failed"
    assert meta["unconsumedInPlan"]  # 未消费交互如实记录在 state 证据里


def test_replay05_recorded_faults_replay_as_faults(tmp_path):
    """超时/断连等故障夹具按错误路径重放;429 按原状态返回,不冒充成功。"""
    store = tmp_path / "store"
    writer = RecordingWriter(store, "rec-faults", source_run_id="src")
    request = httpx.Request("POST", "http://stub.local/v1/chat", content=b"1")
    error_entry = writer.append_interaction(request, None, "ConnectTimeout: boom", None)
    assert error_entry["error"]["message"]
    failed = writer.append_interaction(
        request, httpx.Response(429, json={"error": "rate limited"}), None, None
    )
    ok = writer.append_interaction(request, httpx.Response(200, json={"ok": True}), None, None)
    writer.seal()

    recording = RecordingReader(store, "rec-faults")
    transport = ReplayTransport(recording)

    async def send():
        # 按录制顺序消费:故障 → 429 → 成功(同一键按逻辑序列 FIFO)
        with pytest.raises(RuntimeError, match="录制中的故障被重放"):
            await transport.handle_async_request(
                httpx.Request("POST", "http://stub.local/v1/chat", content=b"1")
            )
        response = await transport.handle_async_request(
            httpx.Request("POST", "http://stub.local/v1/chat", content=b"1")
        )
        assert response.status_code == 429  # 不冒充成功
        response = await transport.handle_async_request(
            httpx.Request("POST", "http://stub.local/v1/chat", content=b"1")
        )
        assert response.status_code == 200
        assert json.loads(response.content) == {"ok": True}

    asyncio.run(send())
    assert [item.index for item in transport.matched] == [error_entry["index"], failed["index"], ok["index"]]
    assert transport.unconsumed() == []


def test_replay04_extra_poll_is_a_mismatch(tmp_path):
    store = tmp_path / "store"
    writer = RecordingWriter(store, "rec-poll", source_run_id="src")
    for status_body in ({"s": "queued"}, {"s": "running"}, {"s": "completed"}):
        writer.append_interaction(
            httpx.Request("GET", "http://stub.local/v1/tasks/t-1"),
            httpx.Response(200, json=status_body),
            None,
            None,
        )
    writer.seal()
    transport = ReplayTransport(RecordingReader(store, "rec-poll"))

    async def poll_too_many():
        for _ in range(3):
            await transport.handle_async_request(httpx.Request("GET", "http://stub.local/v1/tasks/t-1"))
        with pytest.raises(ReplayMismatch):  # 过多轮询:录制已耗尽
            await transport.handle_async_request(httpx.Request("GET", "http://stub.local/v1/tasks/t-1"))

    asyncio.run(poll_too_many())
    assert len(transport.unconsumed()) == 0


def test_replay06_side_channel_host_is_forbidden(tmp_path):
    store = tmp_path / "store"
    writer = RecordingWriter(store, "rec-guard", source_run_id="src")
    writer.append_interaction(
        httpx.Request("POST", "http://stub.local/v1/images/generations"),
        httpx.Response(200, json={}),
        None,
        None,
    )
    writer.seal()
    transport = ReplayTransport(RecordingReader(store, "rec-guard"))
    with pytest.raises(NetworkForbidden, match="NETWORK_FORBIDDEN"):
        asyncio.run(
            transport.handle_async_request(
                httpx.Request("GET", "http://telemetry.local/v1/batch")  # 旁路遥测尝试
            )
        )


def test_replay08_matching_is_by_key_not_arrival_order(tmp_path):
    store = tmp_path / "store"
    writer = RecordingWriter(store, "rec-conc", source_run_id="src")
    writer.append_interaction(
        httpx.Request("POST", "http://stub.local/v1/a", content=b'{"who":"A"}'),
        httpx.Response(200, json={"answer": "A"}),
        None,
        None,
    )
    writer.append_interaction(
        httpx.Request("POST", "http://stub.local/v1/b", content=b'{"who":"B"}'),
        httpx.Response(200, json={"answer": "B"}),
        None,
        None,
    )
    writer.seal()
    transport = ReplayTransport(RecordingReader(store, "rec-conc"))

    async def send_reversed():
        second = await transport.handle_async_request(
            httpx.Request("POST", "http://stub.local/v1/b", content=b'{"who":"B"}')
        )
        first = await transport.handle_async_request(
            httpx.Request("POST", "http://stub.local/v1/a", content=b'{"who":"A"}')
        )
        return second, first

    second, first = asyncio.run(send_reversed())
    assert json.loads(second.content)["answer"] == "B"
    assert json.loads(first.content)["answer"] == "A"


def test_replay07_unsealed_or_tampered_recording_rejected(tmp_path):
    store = tmp_path / "store"
    writer = RecordingWriter(store, "rec-partial", source_run_id="src")
    writer.append_interaction(
        httpx.Request("POST", "http://stub.local/v1/x"),
        httpx.Response(200, json={}),
        None,
        None,
    )
    # 未封存:拒绝严格回放
    with pytest.raises(RecordingIncomplete, match="未封存"):
        RecordingReader(store, "rec-partial")
    writer.seal()
    # 账本被篡改:哈希校验失败
    journal = store / "recordings" / "rec-partial" / "interactions.jsonl"
    journal.write_text(journal.read_text(encoding="utf-8").replace("stub.local", "evil.local"), encoding="utf-8")
    with pytest.raises(RecordingIncomplete, match="哈希不一致"):
        RecordingReader(store, "rec-partial")


def test_replay07b_missing_media_reference_rejected(tmp_path):
    store = tmp_path / "store"
    objects = ObjectStore(store, project="frayune")
    writer = RecordingWriter(store, "rec-media", source_run_id="src")
    png = tiny_png()
    artifact = objects.put(png, "png")
    writer.append_interaction(
        httpx.Request("GET", "http://cdn.local/img/1.png"),
        httpx.Response(200, content=png, headers={"content-type": "image/png"}),
        None,
        objects,
    )
    writer.seal()
    reader = RecordingReader(store, "rec-media", objects=objects)
    assert reader.interactions[0].response_body == png  # 媒体从内容寻址库复原
    # 素材被删除后:预检失败,完整性状态准确
    (store / "objects" / "frayune" / artifact.sha256).unlink()
    with pytest.raises(RecordingIncomplete, match="媒体缺失"):
        RecordingReader(store, "rec-media", objects=objects)
