"""自动送审使用隔离运行和模拟 judge；不调用真实生成服务。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.evaluation.runner import EvaluationRunner
from backend.evaluation.reviews import ReviewSamplingPolicy, ReviewStore, plan_review_tasks
from backend.evaluation.datasets import DatasetStore
from backend.evaluation.graders.judge import JudgeResult
from backend.evaluation.api import build_eval_router
from eval_helpers import make_case, write_jsonl


@pytest.fixture
def completed(tmp_path):
    runner = EvaluationRunner(tmp_path, store=tmp_path / "evaluation")
    runner.freeze_dataset(write_jsonl(tmp_path / "source.jsonl", [make_case("quality")]), "quality")
    runner.create_run("quality-run", "quality")
    runner.execute_run("quality-run")
    state = runner.load_run("quality-run")
    cases = DatasetStore.load_run_snapshot(runner.run_dir("quality-run"))
    return runner, state, cases


def test_default_excludes_mock_and_agent_fail_even_with_judge_error(completed):
    runner, state, cases = completed
    state.grades[0].status = "error"
    state.trials[0].qualityVerdict = "undetermined"
    assert plan_review_tasks(state, ReviewSamplingPolicy(randomRate=1), cases) == []
    state.trials[0].flowLayer = "L4"
    state.trials[0].qualityVerdict = "fail"
    assert plan_review_tasks(state, ReviewSamplingPolicy(), cases) == []
    assert plan_review_tasks(state, ReviewSamplingPolicy(reviewAgentFlowFails=True), cases)[0]["rule"] == "agent_flow_failure"
    state.trials[0].qualityVerdict = "pass"
    assert plan_review_tasks(state, ReviewSamplingPolicy(reviewAgentFlowFails=True, randomRate=1), cases) == []
    assert runner.read_state("quality-run")["reviewSummary"]["count"] == 0


@pytest.mark.parametrize("score,expected", [(0.4, "score_gray"), (0.6, "score_gray"), (0.7, "score_gray"), (0.8, "score_ambiguous"), (1, "random")])
def test_gray_ambiguous_and_pass_random_are_distinct(completed, score, expected):
    _, state, cases = completed
    state.manifest.mode, state.manifest.provider = "live", "cloud"
    state.manifest.grading["judge"] = "vlm"
    state.trials[0].weightedScore = score
    assert plan_review_tasks(state, ReviewSamplingPolicy(randomRate=1), cases)[0]["rule"] == expected
    if expected == "random":
        assert plan_review_tasks(state, ReviewSamplingPolicy(randomRate=0), cases) == []


def test_flags_conflict_disagreement_and_switches(completed):
    _, state, cases = completed
    policy = ReviewSamplingPolicy(includeSynthetic=True, randomRate=0)
    trial = state.trials[0]
    trial.weightedScore = 0.2
    trial.qualityVerdict = "fail"
    assert plan_review_tasks(state, policy, cases) == []  # 普通画质 FAIL 不必送
    cases[0].needsHumanReview = True
    assert plan_review_tasks(state, policy, cases)[0]["rule"] == "flagged_case"
    assert plan_review_tasks(state, policy.model_copy(update={"enabled": False}), cases) == []
    cases[0].needsHumanReview = False
    trial.qualityVerdict = "pass"
    assert plan_review_tasks(state, policy.model_copy(update={"gateScoreThreshold": 0.8}), cases)[0]["rule"] == "score_gate_conflict"
    trial.weightedScore = 1
    state.grades.append(state.grades[0].model_copy(update={"gradeId": "other", "evaluator": {"name": "other"}, "status": "fail"}))
    assert plan_review_tasks(state, policy, cases)[0]["rule"] == "judge_disagreement"
    assert plan_review_tasks(state, policy.model_copy(update={"riskRate": 0}), cases) == []
    with pytest.raises(ValueError):
        ReviewSamplingPolicy(grayMin=0.8, grayMax=0.4)
    assert policy.version() != policy.model_copy(update={"grayMax": 0.6}).version()


@pytest.mark.parametrize("error", ["judge timeout", "unparseable JSON"])
def test_runner_completion_automatically_creates_tasks_visible_via_api(tmp_path, monkeypatch, error):
    from backend.evaluation import runner as runner_module
    from backend.evaluation import api as api_module
    class BrokenJudge:
        def grade_artifact(self, *_):
            return JudgeResult(answers={}, error=error)
    class Scheduler:
        def shutdown(self, wait=False):
            pass
    monkeypatch.setattr(runner_module, "make_judge", lambda *_args, **_kwargs: BrokenJudge())
    monkeypatch.setattr(api_module, "_scheduler", lambda _: Scheduler())
    data = tmp_path / "data"
    runner = api_module._runner(data)
    runner.freeze_dataset(write_jsonl(tmp_path / "source.jsonl", [make_case("quality-error")]), "quality")
    runner.create_run("judge-error", "quality")
    path = runner.run_dir("judge-error") / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["reviewSampling"] = ReviewSamplingPolicy(includeSynthetic=True, randomRate=0).model_dump()
    path.write_text(json.dumps(manifest))
    runner.execute_run("judge-error")
    summary = runner.read_state("judge-error")["reviewSummary"]
    assert summary["status"] == "completed" and summary["required"] == 1
    app = FastAPI()
    app.include_router(build_eval_router(data))
    with TestClient(app) as client:
        tasks = client.get("/api/evals/v1/review-tasks?run_id=judge-error").json()["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["sampling"]["rule"] == "judge_error"
        assert tasks[0]["artifactIds"]
        assert tasks[0]["gradeRefs"]
    runner._write_state("judge-error", status="completed")
    assert len(ReviewStore(runner.store).list()) == 1


@pytest.mark.parametrize("verdict,score", [("pass", 1.0), ("fail", 0.6), ("undetermined", 0.8)])
def test_program_rules_are_not_quality_review_candidates(completed, verdict, score):
    _, state, cases = completed
    for grade in state.grades:
        grade.source = "rules"
    state.trials[0].qualityVerdict = verdict
    state.trials[0].weightedScore = score
    policy = ReviewSamplingPolicy(includeSynthetic=True, randomRate=1, required="all")
    assert plan_review_tasks(state, policy, cases) == []
    cases[0].needsHumanReview = True
    assert plan_review_tasks(state, policy, cases)[0]["rule"] == "flagged_case"


@pytest.mark.parametrize("flow", [True, False])
def test_manual_review_rejects_program_rules_even_with_explicit_bindings(completed, monkeypatch, flow):
    from backend.evaluation import api as api_module
    runner, state, _ = completed
    if flow:
        state.trials[0].flowLayer = "L1"
    else:
        for grade in state.grades:
            grade.source = "rules"
    monkeypatch.setattr(api_module, "_state_or_404", lambda *_: state)
    app = FastAPI()
    app.include_router(build_eval_router(runner.store.parent))
    with TestClient(app) as client:
        response = client.post("/api/evals/v1/review-tasks", json={
            "runId": state.manifest.runId,
            "trialId": state.trials[0].trialId,
            "caseId": state.trials[0].caseId,
            "artifactIds": ["explicit"], "gradeRefs": [{"gradeId": "explicit"}],
        })
        assert response.status_code == 422
        assert "无需人工重判" in response.json()["detail"]
        assert client.get("/api/evals/v1/review-tasks").json()["tasks"] == []
