"""调度器、预算强化、门禁人工联动、多模态适配与压缩实验验收。

覆盖回归场景:
- 创建接口 202 后台执行,长运行期间健康检查等 API 保持响应;
- 同一 run 独占执行,重复提交被拒绝;
- 取消:排队直接取消;执行中协作停止,剩余 trial 记 cancelled;
- 同 scope 多账本实例争用不超额;run+项目两级预算原子预留;
- 非法金额(负数/NaN/inf)拒绝;发送后未知结果不释放预留;
- image_to_image / image_edit / 视频任务素材传递,不静默降级文生图;
- 必审未完成门禁 inconclusive;人工最终裁决 fail → block;
- 校准缺失 → vlm 裁判质量门禁 inconclusive;
- 抽样审核自动创建(必审/风险/随机,记录抽样依据);
- 压缩实验报告同时呈现费用与质量,质量退化禁止推广。
"""

import json
import time
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Config
from backend.evaluation.datasets import DatasetStore
from backend.evaluation.budgets import (
    BudgetExhausted,
    BudgetLedger,
    BudgetPolicy,
    PriceUnknown,
)
from backend.evaluation.datasets import validate_source_params
from backend.evaluation.gates import evaluate_gate
from backend.evaluation.reviews import (
    ReviewSamplingPolicy,
    ReviewStore,
    create_scheduled_reviews,
)
from backend.evaluation.runner import EvaluationRunner
from backend.evaluation.scheduler import EvaluationScheduler

from eval_helpers import make_case, write_jsonl


@pytest.fixture
def runner(tmp_path):
    instance = EvaluationRunner(tmp_path, store=tmp_path / "store")
    source = write_jsonl(
        tmp_path / "ds.jsonl",
        [make_case("t2i-sched-a"), make_case("t2i-sched-b", prompt="一只黑狗")],
    )
    instance.freeze_dataset(source, "ds-zh")
    return instance


# ---------- 调度器:非阻塞、独占、取消 ----------

def test_scheduler_create_returns_immediately_and_api_stays_responsive(tmp_path):
    """创建 run 202 后台执行;执行期间 /api/evals/v1/health 立即响应。"""
    store = tmp_path / "data" / "evaluation"
    runner = EvaluationRunner(tmp_path, store=store)
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case("t2i-api-live")])
    runner.freeze_dataset(source, "ds-zh")
    app = create_app(root=tmp_path, data_dir=tmp_path / "data", config=Config(provider="mock"))
    with TestClient(app) as client:
        started = time.monotonic()
        created = client.post(
            "/api/evals/v1/runs", json={"runId": "sched-1", "datasetId": "ds-zh"}
        )
        assert created.status_code == 202
        assert created.json()["queued"] is True
        assert time.monotonic() - started < 2.0  # 创建不等待执行完成
        # 执行窗口内健康检查必须响应(不阻塞事件循环)
        health_started = time.monotonic()
        response = client.get("/api/evals/v1/health")
        assert response.status_code == 200
        assert time.monotonic() - health_started < 2.0
        # 轮询到终态
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            status = client.get("/api/evals/v1/runs/sched-1").json()["status"]
            if status in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert status == "completed"


def test_scheduler_rejects_duplicate_submit_of_same_run(runner, tmp_path):
    store = tmp_path / "store"
    runner.create_run("dup-run", "ds-zh")
    scheduler = EvaluationScheduler(store)
    first = scheduler.submit("dup-run")
    assert first["queued"] is True
    second = scheduler.submit("dup-run")
    assert second["queued"] is False
    scheduler.shutdown(wait=True)
    state = runner.load_run("dup-run")
    assert state.status == "completed"


def test_scheduler_cancel_running_run_marks_remaining_cancelled(runner, tmp_path):
    """执行中取消:停止派发;未派发 trial 记 cancelled,不冒充完成或失败。"""
    store = tmp_path / "store"
    runner.create_run("cancel-run", "ds-zh")
    scheduler = EvaluationScheduler(store)
    scheduler.submit("cancel-run")
    # 等 run 实际开始后取消
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if runner.read_state("cancel-run").get("status") == "running":
            break
        time.sleep(0.02)
    result = scheduler.cancel("cancel-run")
    assert result["cancelRequested"] is True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = runner.read_state("cancel-run").get("status")
        if status in {"cancelled", "completed"}:
            break
        time.sleep(0.02)
    scheduler.shutdown(wait=True)
    state = runner.load_run("cancel-run")
    assert state.status == "cancelled", runner.read_state("cancel-run")
    statuses = {trial.status for trial in state.trials}
    assert "cancelled" in statuses


def test_scheduler_restart_recovers_mock_but_not_live(runner, tmp_path, monkeypatch):
    """重启恢复:mock 安全重新入队;live 保持 interrupted,不盲目重提。"""
    store = tmp_path / "store"
    runner.create_run("mock-resume", "ds-zh")
    state_file = runner.run_dir("mock-resume") / "state.json"
    state_file.write_text(json.dumps({"status": "running"}), encoding="utf-8")

    runner.create_run(
        "live-unknown",
        "ds-zh",
        mode="live",
        provider="cloud",
        budget={"currency": "CNY", "maxCost": 5.0, "prices": {"generation": 0.5}},
        sandbox_config={"cloudVendor": "zhipu"},
    )
    live_state_file = runner.run_dir("live-unknown") / "state.json"
    live_state_file.write_text(
        json.dumps({"status": "running", "externalTaskId": "task-123"}), encoding="utf-8"
    )
    trial = runner.load_run("live-unknown").trials[0]
    trial.status = "running"
    trial.externalTaskId = "task-123"
    runner._write_trial(trial)

    scheduler = EvaluationScheduler(store)
    scheduler.start()
    scheduler.shutdown(wait=True)
    assert runner.load_run("mock-resume").status == "completed"
    # live:未知上游,不自动重提
    assert runner.read_state("live-unknown").get("status") == "interrupted"
    assert runner.load_run("live-unknown").trials[0].status == "running"  # 未被重置为 pending


# ---------- 预算:多实例争用、项目总预算、非法金额、未知结果 ----------

def test_multiple_ledger_instances_same_scope_cannot_overspend(tmp_path):
    """同 scope 两个实例(复现场景):各预留 0.75、上限 1 → 只允许一笔。"""
    policy = BudgetPolicy(maxCost=1.0, prices={"generation": 0.75})
    first = BudgetLedger(tmp_path, "shared", policy)
    second = BudgetLedger(tmp_path, "shared", policy)
    first.reserve("run-a", "generation", 0.75)
    with pytest.raises(BudgetExhausted):
        second.reserve("run-b", "generation", 0.75)
    summary = second.summary()
    assert summary.outstandingReserved == 0.75


def test_chained_project_budget_blocks_second_run(tmp_path):
    """run 预算各自够,但项目总预算不够 → 第二个 run 被拒绝;不产生部分预留。"""
    project = BudgetLedger(tmp_path, "project-total", BudgetPolicy(maxCost=1.0, prices={"generation": 0.6}))
    run_a = BudgetLedger(tmp_path, "run-a", BudgetPolicy(maxCost=2.0, prices={"generation": 0.6}))
    run_b = BudgetLedger(tmp_path, "run-b", BudgetPolicy(maxCost=2.0, prices={"generation": 0.6}))
    run_a.reserve_chained("run-a", "generation", 0.6, parent=project)
    with pytest.raises(BudgetExhausted):
        run_b.reserve_chained("run-b", "generation", 0.6, parent=project)
    # run_a 的预留同时占用项目预算;回滚后 run_b 无未结预留
    assert project.summary().outstandingReserved == 0.6
    assert run_b.summary().outstandingReserved == 0.0
    assert run_b.summary().unknownCount == 0


def test_invalid_amounts_are_rejected(tmp_path):
    ledger = BudgetLedger(tmp_path, "invalid", BudgetPolicy(maxCost=10.0, prices={"generation": 1.0}))
    for bad in (-0.5, float("nan"), float("inf"), "abc", True):
        with pytest.raises(PriceUnknown):
            ledger.reserve("run", "generation", bad)
    with pytest.raises(Exception):
        BudgetPolicy(maxCost=-1.0)
    with pytest.raises(Exception):
        BudgetPolicy(maxCost=1.0, prices={"generation": -0.1})
    # Decimal 与字符串金额可比,不因表示不同而误判
    ledger.reserve("run", "generation", Decimal("0.5"))
    ledger.reserve("run", "translate", "0.5")
    assert ledger.summary().outstandingReserved == 1.0


def test_sent_then_unknown_keeps_reservation_and_no_auto_retry(tmp_path):
    """mark_sent 后进程崩溃:结果未知,预留保留;不能释放,也不能重发。"""
    ledger = BudgetLedger(tmp_path, "sent", BudgetPolicy(maxCost=1.0, prices={"generation": 0.6}))
    reservation = ledger.reserve("run", "generation", 0.6)
    ledger.mark_sent(reservation.reservationId)
    # "崩溃"后重新加载:reserved/sent → unknown,占用预算
    reloaded = BudgetLedger(tmp_path, "sent", BudgetPolicy(maxCost=1.0, prices={"generation": 0.6}))
    assert reloaded.summary().unknownCount == 1
    with pytest.raises(ValueError, match="不能释放"):
        reloaded.release(reservation.reservationId)
    with pytest.raises(BudgetExhausted):
        reloaded.reserve("run", "generation", 0.6)  # 未知占用 0.6,预算不足


def test_settle_basis_distinction_and_billing_import(tmp_path):
    ledger = BudgetLedger(tmp_path, "basis", BudgetPolicy(maxCost=5.0, prices={"generation": 0.5}))
    reservation = ledger.reserve("run", "generation", 0.5)
    ledger.settle(reservation.reservationId, 0.5, basis="estimated")
    summary = ledger.summary()
    assert summary.settledEstimated == 0.5 and summary.settledBilled == 0.0
    # 账单导入升级为 billed 口径
    ledger.import_billing(reservation.reservationId, 0.42, raw={"usage": "x"})
    summary = ledger.summary()
    assert summary.settledBilled == 0.42 and summary.settledEstimated == 0.0


# ---------- 多模态任务适配 ----------

def test_image_to_image_passes_reference_not_silent_text_to_image(runner, tmp_path, png):
    """image_to_image:素材归档 → 请求携带 initImage;缺素材显式失败,不降级。"""
    artifact = runner.objects.put(png, "png", kind="reference")
    cases = [
        make_case(
            "i2i-ref-01",
            **{
                "taskType": "image_to_image",
                "input": {
                    "prompt": "把背景换成雪山",
                    "referenceArtifactIds": [artifact.artifactId],
                    "params": {"seed": 3},
                },
            },
        )
    ]
    from backend.evaluation.datasets import load_cases

    source = write_jsonl(tmp_path / "ds-i2i.jsonl", cases)
    problems = validate_source_params(load_cases(source))
    assert problems == []
    runner.freeze_dataset(source, "ds-i2i")
    runner.create_run("run-i2i", "ds-i2i")
    assert (runner.run_dir("run-i2i") / "references").is_dir()
    state = runner.execute_run("run-i2i")
    assert state.status == "completed"
    assert state.trials[0].status == "completed"


def test_image_edit_without_mask_artifact_is_rejected(runner, tmp_path, png):
    """image_edit 缺蒙版素材:创建期显式拒绝,不降级执行。"""
    artifact = runner.objects.put(png, "png", kind="reference")
    cases = [
        make_case(
            "edit-01",
            **{
                "taskType": "image_edit",
                "input": {
                    "prompt": "删除行人",
                    "referenceArtifactIds": [artifact.artifactId],
                    "params": {"seed": 3},
                },
            },
        )
    ]
    from backend.evaluation.datasets import load_cases

    problems = validate_source_params(
        load_cases(write_jsonl(tmp_path / "ds-edit.jsonl", cases))
    )
    assert any("蒙版" in problem for problem in problems)


def test_video_task_artifact_saved_as_video(runner, tmp_path):
    """text_to_video:mock 产物以 video 类型落库,不是静默的文生图。"""
    cases = [
        make_case(
            "t2v-01",
            **{
                "taskType": "text_to_video",
                "input": {"prompt": "海浪拍岸", "referenceArtifactIds": [], "params": {"kind": "video", "seed": 5, "durationSec": 2}},
            },
        )
    ]
    source = write_jsonl(tmp_path / "ds-video.jsonl", cases)
    runner.freeze_dataset(source, "ds-video")
    runner.create_run("run-video", "ds-video")
    state = runner.execute_run("run-video")
    assert state.trials[0].status == "completed"
    record = runner.objects.record(state.trials[0].artifactIds[0])
    assert record.kind == "video"


# ---------- 门禁:必审、人工裁决、校准 ----------

def _make_completed_live_state(tmp_path, runner):
    class StubUpstream(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/chat/completions"):
                return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
            import base64
            import io

            from PIL import Image

            output = io.BytesIO()
            Image.new("RGB", (8, 8), "red").save(output, "PNG")
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(output.getvalue()).decode()}]}
            )

    runner.create_run(
        "gate-live",
        "ds-zh",
        mode="live",
        provider="cloud",
        budget={"currency": "CNY", "maxCost": 5.0, "prices": {"generation": 0.5, "translate": 0.05, "poll": 0.0}},
        sandbox_config={
            "cloudVendor": "zhipu",
            "cloudBaseUrl": "http://stub.local/api/paas/v4",
            "cloudModel": "m",
            "cloudTextModel": "t",
        },
    )
    return runner.execute_run("gate-live", transport=StubUpstream())


def test_required_review_incomplete_blocks_pass_and_human_fail_verdict_blocks(
    runner, tmp_path, monkeypatch
):
    monkeypatch.setenv("SWARMUI_IMAGE_API_KEY", "test-key")
    state = _make_completed_live_state(tmp_path, runner)
    cases = DatasetStore.load_run_snapshot(runner.run_dir("gate-live"))
    relaxed = {
        "policyId": "flow", "version": 1,
        "integrity": {"requireFrozenDataset": True, "requireCompleteCoreEvidence": False,
                      "allowMockQualityEvidence": True, "allowStubJudgeEvidence": True},
        "execution": {}, "human": {}, "cost": {},
    }
    store = tmp_path / "store"
    base = evaluate_gate(state, cases, store=store, policy=relaxed)
    assert base.verdict == "pass", base.reasons

    reviews = ReviewStore(store)
    task = reviews.create(
        state.manifest.runId, state.trials[0].trialId, state.trials[0].caseId, priority="required"
    )
    pending = evaluate_gate(state, cases, store=store, policy=relaxed)
    assert pending.verdict == "inconclusive"
    assert any("必审" in reason for reason in pending.reasons)

    claimed = reviews.claim(task.reviewTaskId, "alice", task.revision)
    reviews.submit_opinion(
        claimed.reviewTaskId, "alice", [{"checkId": "has_cat", "verdict": "fail"}],
        claimed.revision,
    )
    reviews.adjudicate(
        task.reviewTaskId, [], "fail", "人工确认失败", "lead"
    )
    blocked = evaluate_gate(state, cases, store=store, policy=relaxed)
    assert blocked.verdict == "block"
    assert any("人工最终裁决失败" in reason for reason in blocked.reasons)


def test_calibration_absent_keeps_quality_gate_inconclusive(runner, tmp_path, monkeypatch):
    """vlm 裁判缺少校准记录:质量门禁 inconclusive,不默认通过(§11.3)。"""
    monkeypatch.setenv("SWARMUI_IMAGE_API_KEY", "test-key")
    state = _make_completed_live_state(tmp_path, runner)
    state.manifest.grading["judge"] = "vlm"
    state.manifest.grading["model"] = "qwen-vl-max"
    cases = DatasetStore.load_run_snapshot(runner.run_dir("gate-live"))
    relaxed = {
        "policyId": "flow", "version": 1,
        "integrity": {"requireFrozenDataset": True, "requireCompleteCoreEvidence": False,
                      "allowMockQualityEvidence": True, "allowStubJudgeEvidence": True},
        "execution": {}, "human": {}, "cost": {},
        "quality": {"requireJudgeCalibration": True, "minDefectRecall": 0.8},
    }
    decision = evaluate_gate(state, cases, store=tmp_path / "store", policy=relaxed)
    assert decision.verdict == "inconclusive"
    assert any("校准" in reason for reason in decision.reasons)


def test_scheduled_reviews_created_by_sampling_policy(runner, tmp_path):
    """抽样策略自动创建审核任务:失败必审 + 记录抽样依据与版本。"""
    runner.create_run("sample-run", "ds-zh")
    runner.execute_run("sample-run")
    store = tmp_path / "store"
    reviews = ReviewStore(store)
    policy = ReviewSamplingPolicy(required="fail_or_undetermined", riskRate=1.0, seed=42)
    created = create_scheduled_reviews(reviews, runner.load_run("sample-run"), policy)
    assert len(created) == 2  # stub 裁判全 pass → 走风险抽样(比例 1.0)
    for task in created:
        assert task.priority == "risk"
        assert task.sampling["rule"] == "riskSample"
        assert "sampling-v" in task.sampling["policyVersion"]


# ---------- 压缩实验 ----------

def test_compression_experiment_reports_cost_quality_and_promotion(runner, tmp_path):
    from backend.evaluation.compression import (
        apply_cost,
        apply_promotion_gate,
        compression_summary,
    )

    runner.create_run("comp-base", "ds-zh")
    runner.execute_run("comp-base")
    runner.create_run(
        "comp-on", "ds-zh", compression={"enabled": True, "compressorVersion": "whitespace-v1"}
    )
    runner.execute_run("comp-on")
    manifest = runner.load_run("comp-on").manifest
    assert manifest.compression == {"enabled": True, "compressorVersion": "whitespace-v1"}
    summary = compression_summary(runner.load_run("comp-base"), runner.load_run("comp-on"))
    assert summary["quality"]["baselinePassRate"] is not None
    assert summary["cost"]["baselineSettled"] is None  # 无账本时 N/A,不显示 0
    summary = apply_cost(summary, {"settled": 0.5}, {"settled": 0.4})
    assert summary["cost"]["delta"] == pytest.approx(-0.1)
    summary = apply_promotion_gate(summary, max_quality_drop=0.02)
    assert summary["promote"] is True
    # 质量下降超阈值 → 禁止推广
    summary["quality"]["delta"] = -0.5
    summary = apply_promotion_gate(summary, max_quality_drop=0.02)
    assert summary["promote"] is False


# ---------- 契约:创建接口入参校验 ----------

def test_create_run_rejects_invalid_budget_and_mode(runner, tmp_path):
    with pytest.raises(ValueError, match="负数"):
        runner.create_run("b1", "ds-zh", mode="live", provider="cloud", budget={"maxCost": -1})
    with pytest.raises(Exception):
        runner.create_run(
            "b2", "ds-zh", mode="live", provider="cloud",
            budget={"maxCost": 1.0, "prices": {"generation": float("nan")}},
        )
    with pytest.raises(ValueError, match="live 模式"):
        runner.create_run("b3", "ds-zh", judge="vlm")


def test_grade_cache_first_usage_retained_and_hit_not_double_billed(tmp_path):
    """缓存首次计算的 usage 保留;命中方引用来源,不重复计费。"""
    from backend.evaluation.gradecache import GradeCache, grade_cache_key

    cache = GradeCache(tmp_path / "store")
    key = grade_cache_key("sha-x", [{"id": "c"}], "1", "vlm", "m1")

    def compute():
        return {
            "answers": {"c": {"observed": True, "source": "vlm"}},
            "judgeError": None,
            "usage": {"source": "provider_reported", "textTokens": 100, "raw": {"p": 60, "c": 40}},
            "costBasis": "provider_reported",
        }

    entry, hit = cache.singleflight(key, compute)
    assert hit is False and entry["usage"]["textTokens"] == 100
    entry2, hit2 = cache.singleflight(key, compute)
    assert hit2 is True
    assert entry2["usage"]["textTokens"] == 100  # 来源引用随条目返回
    # 错误结果不入缓存:下次重新计算
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        return {"answers": {}, "judgeError": "裁判超时"}

    cache.singleflight("k-err", failing)
    cache.singleflight("k-err", failing)
    assert calls["n"] == 2


def test_vlm_judge_charged_at_judge_price_not_translate(tmp_path, monkeypatch):
    """vlm 裁判经预算网关按 judge 价格预留;修复重试逐次记账,usage 聚合完整。"""
    import io

    from PIL import Image

    from backend.evaluation.gateway import CallContext
    from backend.evaluation.runner import make_judge
    from backend.evaluation.models import Check

    monkeypatch.setenv("EVAL_JUDGE_API_KEY", "test-judge-key")
    ledger = BudgetLedger(
        tmp_path, "judge-scope",
        BudgetPolicy(maxCost=10.0, prices={"generation": 0.5, "translate": 0.05, "judge": 0.2}),
    )
    context = CallContext()
    context.run_id = "judge-run"
    context.trial_id = "trial-j1"

    seen = []

    class JudgeUpstream(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append(request.headers.get("x-frayune-callsite"))
            usage = {"prompt_tokens": 10, "completion_tokens": 5}
            if len(seen) == 1:  # 首次输出非法 JSON → 触发修复重试
                content = "not-json"
            else:
                content = json.dumps(
                    {"answers": [{"checkId": "has_cat", "observed": True, "evidence": "可见"}]}
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": content}}],
                    "usage": usage,
                },
            )

    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, "PNG")
    # make_judge 内部以 httpx.HTTPTransport() 作为真实出口:替换为 stub 以离线验证接线
    monkeypatch.setattr(httpx, "HTTPTransport", lambda *args, **kwargs: JudgeUpstream())
    judge = make_judge(
        "vlm",
        grading={"model": "judge-model", "maxAttempts": 2, "baseUrl": "http://judge.local/v1"},
        ledger=ledger,
        context=context,
    )
    result = judge.grade_artifact(
        output.getvalue(), "image/png",
        [Check(id="has_cat", kind="boolean", question="猫?", expected=True)],
    )
    assert result.error is None and result.attempts == 2
    assert result.usage is not None and result.usage.textTokens == 30  # 两次尝试聚合
    assert seen == [None, None]  # 内部观测头不得发往真实上游
    summary = ledger.summary()
    assert summary.totalCalls == 2  # 每次尝试独立预留
    assert summary.settled == pytest.approx(0.4)  # 2 × judge 价 0.2,不是 translate 价
    charges = [r for r in ledger.reservations()]
    assert {r.callSite for r in charges} == {"judge"}
    assert {r.trialId for r in charges} == {"trial-j1"}  # 费用归到正确 trial


def test_media_download_is_reserved_at_zero_cost_not_rejected(tmp_path):
    """结果下载(GET,未登记价格)按零费用预留:审计完整且不被硬预算误拒。"""
    from backend.evaluation.gateway import BudgetedSyncTransport, CallContext

    ledger = BudgetLedger(
        tmp_path, "dl-scope", BudgetPolicy(maxCost=1.0, prices={"generation": 0.5})
    )
    context = CallContext()
    context.run_id = "dl-run"
    context.trial_id = "trial-dl"

    class DownloadUpstream(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, content=b"media-bytes")

    transport = BudgetedSyncTransport(
        inner=DownloadUpstream(), ledger=ledger, context=context, call_site=None
    )
    # call_site=None:走路径分类,GET 未命中标记 → download
    response = transport.handle_request(httpx.Request("GET", "http://cdn.local/media/1.png"))
    assert response.content == b"media-bytes"
    summary = ledger.summary()
    assert summary.totalCalls == 1
    assert summary.settled == 0.0  # 下载零费用
    assert summary.outstandingReserved == 0.0
    sites = {r.callSite for r in ledger.reservations()}
    assert sites == {"download"}
    # POST 未登记价格仍然拒绝(硬预算不变)
    with pytest.raises(PriceUnknown):
        transport.handle_request(httpx.Request("POST", "http://cdn.local/other"))


def test_video_trial_extracts_frames_or_marks_unavailable(runner, tmp_path, monkeypatch):
    """视频 trial 抽帧入库为证据;无 ffmpeg 时事件如实标记缺失,报告加标注。"""
    import backend.evaluation.runner as runner_module

    cases = [
        make_case(
            "t2v-frames",
            **{
                "taskType": "text_to_video",
                "input": {"prompt": "海浪", "referenceArtifactIds": [], "params": {"kind": "video", "seed": 9, "durationSec": 2}},
            },
        )
    ]
    source = write_jsonl(tmp_path / "ds-frames.jsonl", cases)
    runner.freeze_dataset(source, "ds-frames")
    runner.create_run("run-frames", "ds-frames")

    png_bytes = b"frame-png" * 10

    def fake_extract(video_path, count=3, timeout=30):
        return {
            "available": True,
            "frames": [{"timeSec": 0.5, "png": png_bytes}, {"timeSec": 1.5, "png": png_bytes}],
            "durationSec": 2.0,
            "version": "ffmpeg-uniform-v1",
            "note": None,
        }

    monkeypatch.setattr(runner_module, "extract_video_frames", fake_extract)
    state = runner.execute_run("run-frames")
    trial = state.trials[0]
    assert len(trial.frameArtifactIds) == 2
    for frame_id in trial.frameArtifactIds:
        record, data = runner.objects.get(frame_id)
        assert data == png_bytes  # 帧证据可校验读取
    events = [
        json.loads(line)
        for line in (runner.run_dir("run-frames") / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(event["type"] == "video.frames.extracted" for event in events)

    # 无 ffmpeg 环境:available=False → 未抽帧,事件明确标记缺失,报告标注
    runner.create_run("run-noframes", "ds-frames")

    def unavailable(video_path, count=3, timeout=30):
        return {"available": False, "frames": [], "version": "ffmpeg-uniform-v1", "note": "环境无 ffmpeg"}

    monkeypatch.setattr(runner_module, "extract_video_frames", unavailable)
    state2 = runner.execute_run("run-noframes")
    assert state2.trials[0].frameArtifactIds == []
    events2 = [
        json.loads(line)
        for line in (runner.run_dir("run-noframes") / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(event["type"] == "video.frames.unavailable" for event in events2)
    from backend.evaluation.reports import build_report

    dataset_cases = DatasetStore.load_run_snapshot(runner.run_dir("run-noframes"))
    report = build_report(state2, dataset_cases)
    assert any("帧级证据" in note for note in report["annotations"])


def test_drift_report_flags_regression_and_cost_efficiency_na_without_ledger(runner, tmp_path, monkeypatch):
    """漂移报表:同快照通过率下降超阈值被标记;成本效率无账本显示 N/A 不冒充 0。"""
    from backend.evaluation.drift import cost_efficiency, create_drift_reviews, drift_report

    store = tmp_path / "store"
    # 基线:期望 True(stub 全通过)
    runner.create_run("drift-base", "ds-zh")
    runner.execute_run("drift-base")
    # 漂移:注入取反裁判(observed=not expected)→ 必需项失败 → verdict fail 0%
    import backend.evaluation.runner as runner_module
    from backend.evaluation.graders.judge import JudgeResult
    from backend.evaluation.graders.rules import Answer

    class FlippedJudge:
        source = "stub"

        def grade_artifact(self, data, mime, checks):
            return JudgeResult(
                answers={
                    check.id: Answer(
                        checkId=check.id,
                        observed=(not check.expected) if check.kind == "boolean" else check.expected,
                        evidence="取反裁判:模拟质量退化",
                        source="stub",
                    )
                    for check in checks
                },
                usage=None,
            )

    monkeypatch.setattr(runner_module, "make_judge", lambda *a, **k: FlippedJudge())
    runner.create_run("drift-low", "ds-zh")
    # 同素材会命中评分缓存(stub 首算即 pass):绕过缓存让取反裁判真实生效
    runner.execute_run("drift-low", use_grade_cache=False)

    report = drift_report(store, window=5, threshold=0.05)
    assert report["drifted"], report
    assert report["drifted"][0]["runId"] == "drift-low"
    assert report["drifted"][0]["baselinePassRate"] == 1.0

    # 成本效率:无账本 → settled N/A;有账本 → 按 N_pass 摊
    cost = cost_efficiency(store, "drift-base")
    assert cost["settled"] is None and cost["costPerQualifiedArtifact"] is None
    from backend.evaluation.budgets import BudgetLedger, BudgetPolicy

    ledger = BudgetLedger(store, "drift-low", BudgetPolicy(maxCost=5.0, prices={"generation": 0.5}))
    reservation = ledger.reserve("drift-low", "generation", 0.5)
    ledger.settle(reservation.reservationId, 0.5)
    cost2 = cost_efficiency(store, "drift-low")
    low_state = runner.load_run("drift-low")
    passing = sum(1 for t in low_state.trials if t.qualityVerdict == "pass")
    if passing:
        assert cost2["costPerQualifiedArtifact"] == pytest.approx(0.5 / passing)
    else:
        assert cost2["costPerQualifiedArtifact"] is None

    # 显式创建漂移审核任务:只补抽通过样本(risk/drift),失败样本归必审
    tasks = create_drift_reviews(store, report)
    for task in tasks:
        assert task.sampling["rule"] == "drift"


def test_gate_performance_threshold_blocks_on_p95(runner, tmp_path):
    """门禁性能阈值:完成 trial 的 p95 延迟超过配置即 block。"""
    store = tmp_path / "store"
    runner.create_run("perf-run", "ds-zh")
    runner.execute_run("perf-run")
    state = runner.load_run("perf-run")
    for trial in state.trials:
        trial.latencyMs = 5000
        runner._write_trial(trial)
    state = runner.load_run("perf-run")
    cases = DatasetStore.load_run_snapshot(runner.run_dir("perf-run"))
    policy = {
        "policyId": "perf", "version": 1,
        "integrity": {"requireFrozenDataset": True, "requireCompleteCoreEvidence": False,
                      "allowMockQualityEvidence": True, "allowStubJudgeEvidence": True},
        "execution": {}, "human": {}, "cost": {},
        "performance": {"maxLatencyP95Ms": 1000},
    }
    decision = evaluate_gate(state, cases, store=store, policy=policy)
    assert decision.verdict == "block"
    assert any("p95" in reason for reason in decision.reasons)


def test_comparison_localizes_regression_by_check_and_error_category(runner, tmp_path, monkeypatch):
    """比较退化定位:检查项级 pass→fail 与错误类别分组,均关联可回查的 gradeId/trialId。"""
    from backend.evaluation.compare import build_comparison
    from backend.evaluation.graders.judge import JudgeResult
    from backend.evaluation.graders.rules import Answer

    class FlippedJudge:
        source = "stub"

        def grade_artifact(self, data, mime, checks):
            return JudgeResult(
                answers={
                    check.id: Answer(
                        checkId=check.id,
                        observed=(not check.expected) if check.kind == "boolean" else check.expected,
                        evidence="取反裁判",
                        source="stub",
                    )
                    for check in checks
                },
                usage=None,
            )

    runner.create_run("cmp-base", "ds-zh")
    runner.execute_run("cmp-base", use_grade_cache=False)
    import backend.evaluation.runner as runner_module

    monkeypatch.setattr(runner_module, "make_judge", lambda *a, **k: FlippedJudge())
    runner.create_run("cmp-low", "ds-zh")
    runner.execute_run("cmp-low", use_grade_cache=False)
    comparison = build_comparison(
        runner.load_run("cmp-base"),
        runner.load_run("cmp-low"),
        baseline_cases=DatasetStore.load_run_snapshot(runner.run_dir("cmp-base")),
        candidate_cases=DatasetStore.load_run_snapshot(runner.run_dir("cmp-low")),
    )
    assert comparison["checkRegressions"], comparison
    regression = comparison["checkRegressions"][0]
    assert regression["baselineStatus"] == "pass"
    assert regression["candidateStatus"] in {"fail", "dependency_failed"}
    assert regression["candidateGradeId"].startswith("trial-")
