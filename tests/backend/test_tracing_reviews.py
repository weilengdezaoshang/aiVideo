"""P2 验收:步骤追踪(TRACE-01/02/03)、人工审核与问题转用例(REVIEW-01/02)。"""

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from backend.app import create_app
from backend.config import Config
from backend.evaluation.outbox import Outbox
from backend.evaluation.reviews import (
    ReviewConflict,
    ReviewStore,
    publish_case_draft,
)
from backend.evaluation.tracing import TraceRecorder
from backend.jobs import Jobs
from backend.traces import Traces

from eval_helpers import make_case


def tiny_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(output, "PNG")
    return output.getvalue()


class FailingTranslateTransport(httpx.AsyncBaseTransport):
    """翻译端点断连(TRACE-01:建任务前失败并降级),生成端点正常。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            raise httpx.ConnectError("connection refused")
        if request.url.path.endswith("/images/generations"):
            b64 = base64.b64encode(tiny_png()).decode()
            return httpx.Response(200, json={"data": [{"b64_json": b64}]})
        return httpx.Response(404, json={"error": "unknown"})


def test_trace01_translation_failure_visible_at_entry(tmp_path):
    app = create_app(
        root=tmp_path,
        data_dir=tmp_path / "data",
        transport=FailingTranslateTransport(),
        config=Config(
            provider="cloud",
            cloudVendor="zhipu",
            cloudBaseUrl="http://stub.local/api/paas/v4",
            cloudModel="test-image",
            cloudTextModel="test-text",
            imageApiKey="k",
        ),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.post(
            "/api/generate",
            json={"prompt": "三只白猫", "model": "test-image", "seed": 1, "width": 64, "height": 64},
        )
        assert response.status_code == 202
        job_id = response.json()["jobId"]

        recorder = TraceRecorder(tmp_path / "data" / "trace-steps.jsonl")
        records = recorder.read_all()
        trace_id = next(
            item["traceId"] for item in records if item["kind"] == "trace-started"
        )
        translate = [item for item in records if item.get("step") == "prompt.translate"]
        # 入口可查:翻译失败降级,记录了错误与原始/实际提示词指纹
        assert any(item["status"] == "failed_fallback" for item in translate)
        fallback = next(item for item in translate if item["status"] == "failed_fallback")
        assert fallback["error"]
        assert fallback["promptChars"] == len("三只白猫")
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        assert job["traceId"] == trace_id
        # 生成继续走完:provider/artifact 步骤可见
        while client.get(f"/api/jobs/{job_id}").json()["job"]["status"] not in {"completed", "failed"}:
            pass
        steps = {item["step"] for item in recorder.read_all(trace_id) if item["kind"] == "step"}
        assert "artifact.persist" in steps


def test_trace02_restart_recovery_links_traces(tmp_path):
    from backend.common import now, write_json
    from backend.models import GenParams

    params = GenParams(prompt="测试", model="mock-diffusion-xl", kind="video", seed=1)
    job = dict(
        id="job-1",
        status="running",
        params=params.model_dump(exclude_none=True),
        batchCount=1,
        progress=0.5,
        message="生成中",
        images=[],
        createdAt=now(),
        traceId="trace-old-1",
        externalTaskId="upstream-9",
    )
    write_json(tmp_path / "jobs.json", {"jobs": [job]})
    recorder = TraceRecorder(tmp_path / "trace-steps.jsonl")

    jobs = Jobs(None, None, tmp_path / "jobs.json", Traces(tmp_path / "traces.jsonl"), step_tracer=recorder)
    restored = jobs.jobs["job-1"]
    # 视频已派发任务:保持 unknown 语义不变(§5.4),但 trace 已重建并链接
    assert restored["status"] == "unknown"
    assert restored["traceLinks"] == ["trace-old-1"]
    assert restored["traceId"] != "trace-old-1"
    recoveries = [item for item in recorder.read_all() if item["kind"] == "trace-recovery"]
    assert recoveries and recoveries[0]["traceLinks"] == ["trace-old-1"]
    assert recoveries[0]["jobId"] == "job-1"


def test_trace03_outbox_never_blocks_and_retries_bounded(tmp_path):
    outbox = Outbox(tmp_path / "outbox.jsonl")
    outbox.push({"type": "grade.created", "trialId": "t1"})
    outbox.push({"type": "run.finished", "runId": "r1"})

    calls = []

    def flaky_exporter(events):
        calls.append(len(events))
        if len(calls) < 3:
            raise ConnectionError("第三方观测端不可用")

    result = outbox.flush(flaky_exporter, max_attempts=3)
    assert result["flushed"] == 2 and result["remaining"] == 0

    def dead_exporter(events):
        raise ConnectionError("仍然不可用")

    outbox.push({"type": "x"})
    result = outbox.flush(dead_exporter, max_attempts=2)
    assert result["flushed"] == 0
    assert result["remaining"] == 1
    assert "ConnectionError" in result["lastError"]
    assert len(outbox.pending()) == 1  # 缓冲保留,不丢失


# ---------- REVIEW-01 / REVIEW-02 ----------


@pytest.fixture
def reviews(tmp_path):
    return ReviewStore(tmp_path / "store")


def make_task(reviews):
    return reviews.create(
        "run-1",
        "trial-1",
        "t2i-count-01",
        artifact_ids=["art-1"],
        grade_refs=[{"gradeId": "trial-1:cat_count", "evaluator": "typed-rules", "version": "1"}],
        priority="required",
    )


def test_review01_lease_conflict_and_stale_revision(reviews):
    task = make_task(reviews)
    claimed = reviews.claim(task.reviewTaskId, "alice", expected_revision=task.revision)
    # bob 用过期 revision 抢占 → 409 语义
    with pytest.raises(ReviewConflict):
        reviews.claim(task.reviewTaskId, "bob", expected_revision=task.revision)
    # bob 用新 revision 但 alice 租约未过期 → 409 语义
    with pytest.raises(ReviewConflict, match="租用"):
        reviews.claim(task.reviewTaskId, "bob", expected_revision=claimed.revision)
    # alice 提交独立意见
    opinion = reviews.submit_opinion(
        task.reviewTaskId,
        "alice",
        verdicts=[{"checkId": "cat_count", "verdict": "fail", "note": "只有两只"}],
        expected_revision=claimed.revision,
        agree_with_auto=False,
        category="count",
    )
    # 迟到的旧 revision 提交不得覆盖已提交意见
    with pytest.raises(ReviewConflict, match="revision 过期"):
        reviews.submit_opinion(
            task.reviewTaskId,
            "alice",
            verdicts=[{"checkId": "cat_count", "verdict": "pass"}],
            expected_revision=claimed.revision,
        )
    assert len(reviews._opinions(task.reviewTaskId)) == 1
    assert opinion.verdicts[0].verdict == "fail"


def test_review02_revision_history_fully_retained(reviews):
    task = make_task(reviews)
    claimed = reviews.claim(task.reviewTaskId, "alice", expected_revision=task.revision)
    first = reviews.submit_opinion(
        task.reviewTaskId,
        "alice",
        verdicts=[{"checkId": "cat_count", "verdict": "pass"}],
        expected_revision=claimed.revision,
    )
    replacement = reviews.supersede(
        task.reviewTaskId,
        first.opinionId,
        "alice",
        verdicts=[{"checkId": "cat_count", "verdict": "fail", "note": "复核发现只有两只"}],
    )
    # 原意见保留且指向取代版本;取代版可追溯
    raw_lines = [
        json.loads(line)
        for line in (
            reviews._dir(task.reviewTaskId) / "opinions.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sum(1 for item in raw_lines if item.get("opinionId") == first.opinionId) == 2  # 原文 + 取代标记
    assert raw_lines[0]["verdicts"][0]["verdict"] == "pass"  # 原文未被改写
    assert replacement.opinionId and replacement.verdicts[0].verdict == "fail"

    reviews.dispute(task.reviewTaskId, by="bob", reason="与另一审核结论冲突")
    adjudication = reviews.adjudicate(
        task.reviewTaskId,
        bound_opinion_ids=[first.opinionId, replacement.opinionId],
        final_verdict="fail",
        reason="数量缺陷成立",
        by="lead",
    )
    assert adjudication.boundOpinionIds == [first.opinionId, replacement.opinionId]
    assert len(reviews.adjudications(task.reviewTaskId)) == 1
    assert reviews.get(task.reviewTaskId).status == "adjudicated"
    # 再次裁决产生新记录,不改写旧裁决
    reviews.adjudicate(
        task.reviewTaskId,
        bound_opinion_ids=[replacement.opinionId],
        final_verdict="fail",
        reason="维持",
        by="lead2",
    )
    assert len(reviews.adjudications(task.reviewTaskId)) == 2


def test_review_confirmed_issue_becomes_regression_case(reviews, tmp_path):
    """线上问题 → 草稿 → 冻结进回归数据集(§9.4)。"""
    task = make_task(reviews)
    claimed = reviews.claim(task.reviewTaskId, "alice", expected_revision=task.revision)
    reviews.submit_opinion(
        task.reviewTaskId,
        "alice",
        verdicts=[{"checkId": "cat_count", "verdict": "fail"}],
        expected_revision=claimed.revision,
    )
    case = make_case("reg-count-001", prompt="三只白猫")
    case["provenance"] = {"source": "real", "note": "来自 run-1/trial-1 数量缺陷"}
    draft = reviews.create_case_draft(task.reviewTaskId, case)
    draft_data = json.loads(draft.read_text(encoding="utf-8"))
    manifest = publish_case_draft(
        tmp_path / "store", draft_data["draftId"], "reg-zh"
    )
    assert manifest["split"] == "regression"

    from backend.evaluation.datasets import DatasetStore

    frozen = DatasetStore(tmp_path / "store").load("reg-zh")
    assert frozen.cases[0].caseId == "reg-count-001"
    assert frozen.cases[0].provenance.source == "real"

    # 非法草稿(依赖成环)在发布时被 DATA-02 校验拒绝
    bad = make_case("reg-count-002", checks=[
        {"id": "chk-a", "kind": "boolean", "question": "a?", "expected": True, "dependsOn": ["chk-b"]},
        {"id": "chk-b", "kind": "boolean", "question": "b?", "expected": True, "dependsOn": ["chk-a"]},
    ])
    draft2 = reviews.create_case_draft(task.reviewTaskId, bad)
    draft2_data = json.loads(draft2.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="依赖成环"):
        publish_case_draft(tmp_path / "store", draft2_data["draftId"], "reg-zh")
