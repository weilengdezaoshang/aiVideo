"""成本与压缩配对硬校验(§12.3/§13.5):非法配对 4xx + 中文 reasons,合法配对出真实数字。

全部使用隔离目录与 mock 运行;费用通过预算账本注入、token 通过改写 attempts 的
provider_reported usage 注入,不调用真实付费服务。
"""
import base64
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.evaluation.api import build_eval_router
from backend.evaluation.budgets import BudgetLedger, BudgetPolicy
from backend.evaluation.runner import EvaluationRunner
from eval_helpers import make_case, write_jsonl


def tiny_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "green").save(output, "PNG")
    return output.getvalue()


class StubUpstream(httpx.AsyncBaseTransport):
    """live 运行的桩上游:生成端点返回小图,不产生真实费用。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            b64 = base64.b64encode(tiny_png()).decode()
            return httpx.Response(200, json={"data": [{"b64_json": b64}]})
        return httpx.Response(404, json={"error": "unknown"})


def settle_cost(store, run_id: str, amount: float) -> None:
    """给 run 写入一笔已结算费用(估算口径),模拟 live 记账。"""
    ledger = BudgetLedger.open(
        store, run_id, policy=BudgetPolicy(maxCost=100.0, prices={"generation": amount})
    )
    reservation = ledger.reserve(run_id, "generation", amount)
    ledger.mark_sent(reservation.reservationId)
    ledger.settle(reservation.reservationId, amount, basis="estimated")


def add_judge_tokens(runner: EvaluationRunner, run_id: str, tokens: int) -> None:
    """给 run 的 judge 调用补上 provider_reported token(模拟真实 VLM 裁判 usage)。"""
    for trial in runner.load_run(run_id).trials:
        path = runner.run_dir(run_id) / "trials" / trial.trialId / "attempts.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for attempt in data["attempts"]:
            if attempt.get("callSite") == "judge":
                attempt["usage"] = {"source": "provider_reported", "textTokens": tokens}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_recorded_run(runner, run_id: str, dataset_id: str, *, compression: bool, cost: float, tokens: int, version: int | None = None):
    runner.create_run(run_id, dataset_id, version=version, compression={"enabled": compression})
    runner.execute_run(run_id)
    if cost is not None:
        settle_cost(runner.store, run_id, cost)
    if tokens is not None:
        add_judge_tokens(runner, run_id, tokens)
    return runner.load_run(run_id)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMUI_IMAGE_API_KEY", "test-key")
    runner = EvaluationRunner(tmp_path, store=tmp_path / "data" / "evaluation")
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case("t2i-pair-01")])
    runner.freeze_dataset(source, "ds-zh")
    return tmp_path, runner


@pytest.fixture
def api(env):
    tmp_path, runner = env
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(build_eval_router(tmp_path / "data"))
    with TestClient(app) as client:
        yield client, runner, tmp_path


def codes_of(response) -> set[str]:
    return {item["code"] for item in response.json()["reasons"]}


# ---------- 硬条件:缺任一条 → 4xx + 中文 reasons,不生成全未知报告 ----------


def test_mock_uncompressed_pair_is_rejected_with_chinese_reasons(api):
    client, runner, _ = api
    runner.create_run("base-mock", "ds-zh")
    runner.execute_run("base-mock")
    runner.create_run("other-mock", "ds-zh")
    runner.execute_run("other-mock")
    response = client.post(
        "/api/evals/v1/validate-pair",
        json={"baselineRunId": "base-mock", "candidateRunId": "other-mock"},
    )
    assert response.status_code == 422
    codes = codes_of(response)
    assert "not_compression_pair" in codes
    assert "missing_cost" in codes
    assert "missing_tokens" in codes
    for item in response.json()["reasons"]:
        assert item["message"].strip()
    # 报告端点同样拒绝:不存在「全未知成功态」
    report = client.post(
        "/api/evals/v1/compression-report",
        json={"baselineRunId": "base-mock", "compressedRunId": "other-mock"},
    )
    assert report.status_code == 422
    assert "无法" in report.json()["error"]


def test_snapshot_mismatch_blocks_total_cost_comparison(api):
    client, runner, tmp_path = api
    make_recorded_run(runner, "snap-base", "ds-zh", compression=False, cost=1.0, tokens=100)
    # 修改源后冻结 v2:快照 hash 不同,即使其余条件全满足也禁止总费用对比
    modified = write_jsonl(tmp_path / "ds2.jsonl", [make_case("t2i-pair-01", prompt="两只黑狗")])
    runner.freeze_dataset(modified, "ds-zh")
    make_recorded_run(runner, "snap-comp", "ds-zh", compression=True, cost=0.5, tokens=80, version=2)
    response = client.post(
        "/api/evals/v1/validate-pair",
        json={"baselineRunId": "snap-base", "candidateRunId": "snap-comp"},
    )
    assert response.status_code == 422
    assert "snapshot_mismatch" in codes_of(response)
    assert any("快照不一致" in item["message"] for item in response.json()["reasons"])
    assert client.post(
        "/api/evals/v1/compression-report",
        json={"baselineRunId": "snap-base", "compressedRunId": "snap-comp"},
    ).status_code == 422


def test_direction_and_dataset_rules(api):
    client, runner, tmp_path = api
    base = make_recorded_run(runner, "dir-base", "ds-zh", compression=False, cost=1.0, tokens=100)
    comp = make_recorded_run(runner, "dir-comp", "ds-zh", compression=True, cost=0.5, tokens=80)
    # 方向颠倒(基线传成了压缩侧)→ not_compression_pair
    reversed_pair = client.post(
        "/api/evals/v1/validate-pair",
        json={"baselineRunId": comp.manifest.runId, "candidateRunId": base.manifest.runId},
    )
    assert reversed_pair.status_code == 422
    assert "not_compression_pair" in codes_of(reversed_pair)
    # 同一 run → same_run
    same = client.post(
        "/api/evals/v1/validate-pair",
        json={"baselineRunId": "dir-base", "candidateRunId": "dir-base"},
    )
    assert same.status_code == 422
    assert "same_run" in codes_of(same)
    # 不同数据集 → dataset_mismatch
    other = write_jsonl(tmp_path / "other.jsonl", [make_case("other-case")])
    runner.freeze_dataset(other, "other-ds")
    make_recorded_run(runner, "other-comp", "other-ds", compression=True, cost=0.5, tokens=80)
    mismatch = client.post(
        "/api/evals/v1/validate-pair",
        json={"baselineRunId": "dir-base", "candidateRunId": "other-comp"},
    )
    assert mismatch.status_code == 422
    assert "dataset_mismatch" in codes_of(mismatch)


def test_missing_tokens_or_cost_each_block_the_pair(api):
    client, runner, _ = api
    make_recorded_run(runner, "half-base", "ds-zh", compression=False, cost=1.0, tokens=100)
    make_recorded_run(runner, "half-comp", "ds-zh", compression=True, cost=None, tokens=None)
    response = client.post(
        "/api/evals/v1/validate-pair",
        json={"baselineRunId": "half-base", "candidateRunId": "half-comp"},
    )
    assert response.status_code == 422
    assert {"missing_cost", "missing_tokens"} <= codes_of(response)


# ---------- 合法配对:报告必须有真实数字 ----------


def test_valid_pair_returns_real_numbers_and_coverage(api):
    client, runner, _ = api
    make_recorded_run(runner, "ok-base", "ds-zh", compression=False, cost=1.0, tokens=1000)
    make_recorded_run(runner, "ok-comp", "ds-zh", compression=True, cost=0.6, tokens=700)
    validated = client.post(
        "/api/evals/v1/validate-pair",
        json={"baselineRunId": "ok-base", "candidateRunId": "ok-comp"},
    )
    assert validated.status_code == 200, validated.text
    payload = validated.json()
    assert payload["valid"] is True
    assert payload["coverage"]["alignedCases"] == 1
    report = client.post(
        "/api/evals/v1/compression-report",
        json={"baselineRunId": "ok-base", "compressedRunId": "ok-comp"},
    )
    assert report.status_code == 200, report.text
    summary = report.json()
    assert summary["cost"]["baselineSettled"] == pytest.approx(1.0)
    assert summary["cost"]["compressedSettled"] == pytest.approx(0.6)
    assert summary["cost"]["delta"] == pytest.approx(-0.4)
    assert summary["tokens"]["baseline"]["providerReportedTokens"] == 1000
    assert summary["tokens"]["compressed"]["providerReportedTokens"] == 700
    assert summary["compressionEnabled"] is True


def test_failed_run_cannot_pair_even_with_ledger(api):
    client, runner, _ = api
    make_recorded_run(runner, "fail-base", "ds-zh", compression=False, cost=1.0, tokens=100)
    runner.create_run("fail-comp", "ds-zh", compression={"enabled": True})
    runner.execute_run("fail-comp")
    # 人为把 run 置为 failed 终态
    runner._write_state("fail-comp", status="failed", error="注入失败")
    settle_cost(runner.store, "fail-comp", 0.5)
    add_judge_tokens(runner, "fail-comp", 80)
    response = client.post(
        "/api/evals/v1/validate-pair",
        json={"baselineRunId": "fail-base", "candidateRunId": "fail-comp"},
    )
    assert response.status_code == 422
    assert "run_not_success" in codes_of(response)


# ---------- suggest-pair:找得到配对,或给重跑引导 ----------


def test_suggest_pair_finds_valid_counterpart(api):
    client, runner, _ = api
    make_recorded_run(runner, "pair-base", "ds-zh", compression=False, cost=1.0, tokens=1000)
    make_recorded_run(runner, "pair-comp", "ds-zh", compression=True, cost=0.6, tokens=700)
    suggestion = client.get("/api/evals/v1/suggest-pair?run_id=pair-comp").json()
    assert suggestion["currentRole"] == "compressed"
    assert suggestion["pairRunId"] == "pair-base"
    assert suggestion["coverage"]["alignedCases"] == 1
    assert suggestion["guidance"] is None
    # 未压缩侧视角同样能找到
    reverse = client.get("/api/evals/v1/suggest-pair?run_id=pair-base").json()
    assert reverse["currentRole"] == "baseline"
    assert reverse["pairRunId"] == "pair-comp"
    # 不传 run_id:默认推荐最新可配对运行
    default = client.get("/api/evals/v1/suggest-pair").json()
    assert default["pairRunId"]


def test_suggest_pair_guides_counterpart_rerun(api):
    client, runner, _ = api
    # live 基线(带预算记账 + 注入 token):找不到压缩对手,引导开压缩重跑并继承配置
    runner.create_run(
        "lone-base",
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
    runner.execute_run("lone-base", transport=StubUpstream())
    add_judge_tokens(runner, "lone-base", 1000)
    suggestion = client.get("/api/evals/v1/suggest-pair?run_id=lone-base").json()
    assert suggestion["pairRunId"] is None
    assert suggestion["currentRole"] == "baseline"
    assert any(item["code"] == "no_counterpart" for item in suggestion["reasons"])
    guidance = suggestion["guidance"]
    assert guidance["action"] == "create_counterpart"
    assert "开启压缩" in guidance["message"]
    prefill = guidance["prefill"]
    assert prefill["datasetId"] == "ds-zh"
    assert prefill["compression"]["enabled"] is True
    assert prefill["mode"] == "live"
    assert prefill["budget"]["prices"]["generation"] == pytest.approx(0.5)  # 继承预算单价
    assert prefill["provider"] == "cloud"


def test_suggest_pair_guides_mock_current_to_live_rerun(api):
    client, runner, _ = api
    runner.create_run("mock-comp", "ds-zh", compression={"enabled": True})
    runner.execute_run("mock-comp")
    suggestion = client.get("/api/evals/v1/suggest-pair?run_id=mock-comp").json()
    assert suggestion["pairRunId"] is None
    assert {item["code"] for item in suggestion["reasons"]} == {"missing_cost", "missing_tokens"}
    guidance = suggestion["guidance"]
    assert guidance["action"] == "rerun_current_live"
    assert guidance["prefill"]["compression"]["enabled"] is True  # 保持同一压缩开关
    assert guidance["prefill"]["mode"] == "live"


def test_suggest_pair_reports_near_miss_candidates(api):
    client, runner, tmp_path = api
    make_recorded_run(runner, "near-base", "ds-zh", compression=False, cost=1.0, tokens=1000)
    # 反向压缩运行存在,但快照不同 → 候选不合法,reasons 指名候选与原因
    modified = write_jsonl(tmp_path / "ds2.jsonl", [make_case("t2i-pair-01", prompt="两只黑狗")])
    runner.freeze_dataset(modified, "ds-zh")
    make_recorded_run(runner, "near-comp", "ds-zh", compression=True, cost=0.5, tokens=80, version=2)
    suggestion = client.get("/api/evals/v1/suggest-pair?run_id=near-base").json()
    assert suggestion["pairRunId"] is None
    assert suggestion["candidates"][0]["runId"] == "near-comp"
    assert any(
        item["code"] == "snapshot_mismatch" and "near-comp" in item["message"]
        for item in suggestion["reasons"]
    )
    assert suggestion["guidance"]["action"] == "create_counterpart"
