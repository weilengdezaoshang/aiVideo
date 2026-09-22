"""P3 验收:发布门禁(GATE-01)、批次比较(METRIC-02)、/api/evals/v1 契约、第三方适配。"""

import base64
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import create_app
from backend.config import Config
from backend.evaluation.budgets import BudgetLedger, BudgetPolicy
from backend.evaluation.datasets import DatasetStore
from backend.evaluation.gates import GateStore, evaluate_gate
from backend.evaluation.integrations import (
    LangfuseExporter,
    build_promptfoo_tests,
)
from backend.evaluation.outbox import Outbox
from backend.evaluation.runner import EvaluationRunner

from eval_helpers import make_case, write_jsonl


def tiny_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "green").save(output, "PNG")
    return output.getvalue()


class StubUpstream(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "a white cat"}}]})
        if request.url.path.endswith("/images/generations"):
            b64 = base64.b64encode(tiny_png()).decode()
            return httpx.Response(200, json={"data": [{"b64_json": b64}]})
        return httpx.Response(404, json={"error": "unknown"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMUI_IMAGE_API_KEY", "test-key")
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case("t2i-gate-01")])
    runner.freeze_dataset(source, "ds-zh")
    return tmp_path, runner


def test_gate01_mock_evidence_is_inconclusive_not_pass(env):
    """mock/stub 证据不能通过质量门禁;也不同于 block(区别展示)。"""
    tmp_path, runner = env
    runner.create_run("run-mock", "ds-zh")
    state = runner.execute_run("run-mock")
    cases = DatasetStore.load_run_snapshot(runner.run_dir("run-mock"))
    decision = evaluate_gate(state, cases)
    assert decision.verdict == "inconclusive"
    assert any("mock" in reason for reason in decision.reasons)
    saved = GateStore(tmp_path / "store").save(decision)
    assert saved.is_file()
    # 判定不可变:同 run 再次保存是新增记录
    assert len(GateStore(tmp_path / "store").list_for_run("run-mock")) == 1


def test_gate01b_live_full_pass_can_pass_gate(env):
    """live+全通过:在显式放宽 stub/成本限制的策略下可 pass;默认策略仍 inconclusive。"""
    tmp_path, runner = env
    runner.create_run(
        "run-live",
        "ds-zh",
        mode="live",
        provider="cloud",
        budget={"currency": "CNY", "maxCost": 5.0, "prices": {"generation": 0.5, "translate": 0.05, "poll": 0.0}},
        sandbox_config={
            "cloudVendor": "zhipu",
            "cloudBaseUrl": "http://stub.local/api/paas/v4",
            "cloudModel": "test-image",
            "cloudTextModel": "test-text",
        },
    )
    state = runner.execute_run("run-live", transport=StubUpstream())
    cases = DatasetStore.load_run_snapshot(runner.run_dir("run-live"))
    # 默认策略:stub 判分不能支撑质量结论(P1 判分仍为 stub,P4 接入真实裁判校准)
    default_decision = evaluate_gate(state, cases, store=tmp_path / "store")
    assert default_decision.verdict == "inconclusive"
    # 显式放宽的流程验证策略:验证 pass 路径本身
    relaxed = {
        "policyId": "flow-verification",
        "version": 1,
        "integrity": {
            "requireFrozenDataset": True,
            "requireCompleteCoreEvidence": True,
            "allowMockQualityEvidence": True,
            "allowStubJudgeEvidence": True,
        },
        "execution": {"maxCriticalFlowFailures": 0},
        "human": {},
        "cost": {"requireKnownOrBoundedCost": False},
    }
    decision = evaluate_gate(state, cases, store=tmp_path / "store", policy=relaxed)
    assert decision.verdict == "pass", decision.reasons


def test_metric02_comparison_intersection_and_refusal(env):
    tmp_path, runner = env
    # 两次 mock run(同快照)→ 配对呈现;但 mock 不是独立样本,不给总体差值(§12.1)
    runner.create_run("base", "ds-zh")
    runner.execute_run("base")
    runner.create_run("cand", "ds-zh")
    state_cand = runner.execute_run("cand")
    state_base = runner.load_run("base")
    cases = DatasetStore.load_run_snapshot(runner.run_dir("base"))
    comparison = evaluate_and_compare(runner, tmp_path, state_base, state_cand, cases)
    assert comparison["comparability"] == "compatible"
    assert comparison["independentSamples"] is False  # mock 不构成独立统计样本
    assert comparison["summary"]["overallDelta"] is None
    # 候选数据集快照不同(修改源后冻结出 v2)→ 拒绝总体比较,仅交集
    modified = write_jsonl(tmp_path / "ds2.jsonl", [make_case("t2i-gate-01", prompt="两只黑狗")])
    runner.freeze_dataset(modified, "ds-zh")
    runner.create_run("cand2", "ds-zh", version=2)
    state_cand2 = runner.execute_run("cand2")
    comparison2 = evaluate_and_compare(runner, tmp_path, state_base, state_cand2, cases)
    assert comparison2["comparability"] == "intersection_only"
    assert comparison2["summary"]["overallDelta"] is None
    assert "公共用例交集" in comparison2["note"]


def evaluate_and_compare(runner, tmp_path, base, cand, cases):
    from backend.evaluation.compare import build_comparison

    return build_comparison(base, cand, baseline_cases=cases, candidate_cases=cases)


# ---------- /api/evals/v1 契约 ----------


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMUI_IMAGE_API_KEY", "test-key")
    runner = EvaluationRunner(tmp_path, store=tmp_path / "data" / "evaluation")
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case("t2i-api-01")])
    runner.freeze_dataset(source, "ds-zh")
    app = create_app(root=tmp_path, data_dir=tmp_path / "data", config=Config(provider="mock"))
    with TestClient(app) as client:
        yield client, runner, tmp_path


def wait_terminal(client, run_id: str, timeout: float = 30.0) -> dict:
    """创建接口现为异步(202 后台执行):契约测试轮询详情直至终态。"""
    import time

    deadline = time.monotonic() + timeout
    detail: dict = {}
    while time.monotonic() < deadline:
        detail = client.get(f"/api/evals/v1/runs/{run_id}").json()
        if detail["status"] in {"completed", "failed", "cancelled", "interrupted", "budget_exhausted"}:
            return detail
        time.sleep(0.05)
    return detail


def test_api_run_lifecycle(api_client):
    client, runner, tmp_path = api_client
    # 数据集列表
    payload = client.get("/api/evals/v1/datasets").json()
    assert payload["datasets"][0]["datasetId"] == "ds-zh"
    # 创建即返回 202,由后台执行器接管;页面轮询详情直至终态
    created = client.post(
        "/api/evals/v1/runs",
        json={"runId": "api-run-1", "datasetId": "ds-zh", "purpose": "API 契约"},
    )
    assert created.status_code == 202, created.text
    assert created.json()["planCount"] == 1
    assert created.json()["status"] in {"queued", "running"}
    # runId 幂等护栏 → 409
    again = client.post("/api/evals/v1/runs", json={"runId": "api-run-1", "datasetId": "ds-zh"})
    assert again.status_code == 409
    # 详情/试算/trials/events
    detail = wait_terminal(client, "api-run-1")
    assert detail["status"] == "completed", detail.get("extra")
    assert detail["denominators"]["plan"] == 1
    assert detail["progress"]["completed"] == 1
    trials = client.get("/api/evals/v1/runs/api-run-1/trials").json()["trials"]
    assert trials[0]["status"] == "completed"
    events = client.get("/api/evals/v1/runs/api-run-1/events").json()["events"]
    assert any(event["type"] == "run.finished" for event in events)
    # 报告 + 门禁
    report = client.post("/api/evals/v1/runs/api-run-1/report").json()
    assert report["reportVersion"] == 1
    gate = client.post("/api/evals/v1/runs/api-run-1/gate").json()
    assert gate["verdict"] == "inconclusive"  # mock 证据
    assert client.get("/api/evals/v1/runs/api-run-1/gates").json()["gates"]
    # trial 详情(步骤树)
    trial_id = trials[0]["trialId"]
    detail_trial = client.get(f"/api/evals/v1/trials/{trial_id}").json()
    assert detail_trial["runId"] == "api-run-1"
    assert any(step["type"] == "artifact.created" for step in detail_trial["steps"])
    # 不存在 → 404
    assert client.get("/api/evals/v1/runs/missing").status_code == 404


def test_api_review_flow_with_conflict(api_client):
    client, runner, tmp_path = api_client
    runner.create_run("api-run-2", "ds-zh")
    state = runner.execute_run("api-run-2")
    trial_id = state.trials[0].trialId
    created = client.post(
        "/api/evals/v1/review-tasks",
        json={"runId": "api-run-2", "trialId": trial_id, "caseId": "t2i-api-01", "priority": "required"},
    )
    assert created.status_code == 201
    task = created.json()["task"]
    # 领取
    claimed = client.post(
        f"/api/evals/v1/review-tasks/{task['reviewTaskId']}/claim",
        json={"reviewer": "alice", "expectedRevision": task["revision"]},
    )
    assert claimed.status_code == 200
    # 过期 revision 提交 → 409
    stale = client.post(
        f"/api/evals/v1/review-tasks/{task['reviewTaskId']}/reviews",
        json={"reviewer": "alice", "expectedRevision": task["revision"], "verdicts": []},
    )
    assert stale.status_code == 409
    # 正确 revision 提交 → 201
    ok = client.post(
        f"/api/evals/v1/review-tasks/{task['reviewTaskId']}/reviews",
        json={
            "reviewer": "alice",
            "expectedRevision": claimed.json()["task"]["revision"],
            "verdicts": [{"checkId": "has_cat", "verdict": "pass"}],
            "agreeWithAuto": True,
        },
    )
    assert ok.status_code == 201
    # 裁决
    adjudication = client.post(
        f"/api/evals/v1/review-tasks/{task['reviewTaskId']}/adjudications",
        json={
            "boundOpinionIds": [ok.json()["opinion"]["opinionId"]],
            "finalVerdict": "pass",
            "reason": "人工确认",
            "decidedBy": "lead",
        },
    )
    assert adjudication.status_code == 201


# ---------- 第三方适配 ----------


def test_promptfoo_provider_is_read_only(env):
    tmp_path, runner = env
    runner.create_run("pf-run", "ds-zh")
    runner.execute_run("pf-run")
    view = build_promptfoo_tests(tmp_path / "store", "pf-run")
    assert view["provider"] == "frayune-evidence(只读)"
    assert len(view["results"]) == 1
    assert view["results"][0]["checks"][0]["gradeRef"].startswith("trial-")
    assert "不" in view["note"] and "重新" in view["note"]  # 明示不重复执行/判分


def test_langfuse_export_contract(tmp_path):
    outbox = Outbox(tmp_path / "outbox.jsonl")
    outbox.push({"eventId": "ev-1", "type": "run.finished", "runId": "r", "timestamp": "2026-09-11T00:00:00Z"})
    # 未配置凭据:明确报错,不静默假装已导出
    exporter = LangfuseExporter()
    with pytest.raises(ValueError, match="Langfuse 未配置"):
        exporter.export(outbox.pending())
    # 配置 + mock transport:ingestion payload 形状正确,导出成功
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"statuses": []})

    exporter = LangfuseExporter(
        public_key="pk", secret_key="sk", host="http://langfuse.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    count = exporter.export(outbox.pending())
    assert count == 1
    assert seen["path"] == "/api/public/ingestion"
    assert seen["auth"].startswith("Basic ")
    assert seen["body"]["batch"][0]["metadata"]["origin"] == "evaluation"


def test_budget_endpoint_reflects_ledger(api_client):
    client, runner, tmp_path = api_client
    runner.create_run("api-budget", "ds-zh")
    runner.execute_run("api-budget")
    # mock 运行没有经过预算网关;端点返回零账本(不冒充有费用)
    summary = client.get("/api/evals/v1/runs/api-budget/budget").json()
    assert summary["totalCalls"] == 0 and summary["settled"] == 0.0
    # 直接记账后端点如实反映
    ledger = BudgetLedger(tmp_path / "data" / "evaluation", "api-budget", BudgetPolicy(maxCost=1.0, prices={"generation": 0.5}))
    reservation = ledger.reserve("api-budget", "generation", 0.5)
    ledger.settle(reservation.reservationId, 0.5)
    summary = client.get("/api/evals/v1/runs/api-budget/budget").json()
    assert summary["totalCalls"] == 1 and summary["settled"] == 0.5


def test_trial_detail_has_attempts_and_artifact_content(api_client):
    """trial 详情返回调用记录;素材内容端点按内容寻址只读输出。"""
    client, runner, tmp_path = api_client
    runner.create_run("api-attempts", "ds-zh")
    state = runner.execute_run("api-attempts")
    trial = state.trials[0]
    detail = client.get(f"/api/evals/v1/trials/{trial.trialId}").json()
    assert detail["attempts"], "trial 应至少有一条生成调用记录"
    attempt = detail["attempts"][0]
    assert attempt["callSite"] == "generation"
    assert attempt["status"] == "succeeded"
    # 素材内容端点:哈希一致、MIME 正确
    artifact_id = trial.artifactIds[0]
    content = client.get(f"/api/evals/v1/artifacts/{artifact_id}/content")
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("image/")
    record, data = runner.objects.get(artifact_id)
    assert content.content == data  # 字节级一致
    # 不存在 → 404;缺失引用不出现在目录列举(端点仅按 ID 直读)
    assert client.get("/api/evals/v1/artifacts/art-missing/content").status_code == 404


def test_review_task_autobinds_artifacts_and_grades(api_client):
    """手动创建审核任务自动补全素材与评分引用(审核页素材查看闭环,§11)。"""
    client, runner, tmp_path = api_client
    runner.create_run("api-autobind", "ds-zh")
    state = runner.execute_run("api-autobind")
    trial = state.trials[0]
    created = client.post(
        "/api/evals/v1/review-tasks",
        json={"runId": "api-autobind", "trialId": trial.trialId, "caseId": trial.caseId},
    )
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["artifactIds"] == trial.artifactIds  # 自动绑定产物
    assert task["gradeRefs"], "评分引用自动补全"
    assert task["gradeRefs"][0]["gradeId"].startswith("trial-")
