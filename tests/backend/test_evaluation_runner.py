"""评测执行器验收:完整 run、执行失败与质量失败分离、崩溃恢复、run 不可变(§6/§7/§21)。"""

import json

import pytest

from backend.evaluation.datasets import DatasetStore
from backend.evaluation.models import RunState
from backend.evaluation.reports import ReportWriter, build_report, render_markdown
from backend.evaluation.runner import EvaluationRunner
from backend.providers.mock import MockProvider

from eval_helpers import make_case, write_jsonl


@pytest.fixture
def runner(tmp_path):
    instance = EvaluationRunner(tmp_path, store=tmp_path / "store")
    source = write_jsonl(
        tmp_path / "ds.jsonl",
        [
            make_case("t2i-demo-a"),
            make_case("t2i-demo-b", prompt="一只黑狗"),
        ],
    )
    instance.freeze_dataset(source, "ds-zh")
    return instance


def test_full_mock_run_produces_trials_grades_events_and_report(runner):
    runner.create_run("run-full", "ds-zh", purpose="冒烟验收")
    state = runner.execute_run("run-full")
    assert state.status == "completed"
    assert len(state.trials) == 2
    for trial in state.trials:
        assert trial.status == "completed"
        assert trial.qualityVerdict == "pass"
        assert trial.weightedScore == 1.0
        assert trial.resolvedSeed == trial.requestedSeed  # mock 固定 seed 逐字一致
        assert trial.artifactIds
    # 素材可校验:哈希一致、可读
    assert runner.objects.verify_all([t.artifactIds[0] for t in state.trials]) == []

    # 事件账本:sequence 单调、eventId 唯一、类型齐备
    events = [
        json.loads(line)
        for line in (runner.run_dir("run-full") / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len({event["eventId"] for event in events}) == len(events)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    types = {event["type"] for event in events}
    assert {
        "run.created", "run.started", "trial.started", "step.started",
        "artifact.created", "grade.created", "trial.finished", "run.finished",
    } <= types

    # 报告:完整分母 + mock/stub 强制标注
    cases = DatasetStore.load_run_snapshot(runner.run_dir("run-full"))
    report = build_report(state, cases)
    assert report["denominators"] == {"plan": 2, "attempted": 2, "generated": 2, "determined": 2, "pass": 2}
    assert any("mock" in note for note in report["annotations"])
    assert any("stub" in note for note in report["annotations"])
    _, markdown = ReportWriter(runner.store).write(report)
    assert "mock 生成" in markdown.read_text(encoding="utf-8")


def test_execution_failure_is_separate_from_quality_failure(runner, monkeypatch):
    """执行失败既不是质量不通过,也不是通过:进入覆盖缺口,分母如实呈现。"""
    def broken_render(p, seed, ref, mask):
        raise RuntimeError("模拟渲染失败:连接超时")

    monkeypatch.setattr(MockProvider, "render", staticmethod(broken_render))
    runner.create_run("run-fail", "ds-zh")
    state = runner.execute_run("run-fail")
    assert state.status == "completed"  # 执行器完成;失败记录在单条 trial
    assert all(trial.status == "failed" for trial in state.trials)
    assert all(trial.qualityVerdict is None for trial in state.trials)
    assert all(trial.errorCategory == "timeout" for trial in state.trials)

    from backend.evaluation.reports import build_report

    cases = DatasetStore.load_run_snapshot(runner.run_dir("run-fail"))
    report = build_report(state, cases)
    assert report["denominators"] == {"plan": 2, "attempted": 2, "generated": 0, "determined": 0, "pass": 0}
    assert report["metrics"]["generationSuccessRate"] == 0.0
    assert report["metrics"]["conditionalQualityPassRate"] is None  # N/A,不是 0 也不是 100%
    assert report["metrics"]["planCompletionRate"] == 0.0
    assert len(report["coverageGaps"]) == 2

    markdown = render_markdown(report)
    assert "N/A" in markdown and "覆盖缺口" in markdown


def test_run_id_is_immutable_and_rerun_creates_new_run(runner):
    runner.create_run("run-x", "ds-zh")
    with pytest.raises(ValueError, match="runId 已存在"):
        runner.create_run("run-x", "ds-zh")
    runner.execute_run("run-x")
    with pytest.raises(ValueError, match="不能执行"):
        runner.execute_run("run-x")
    # 重跑 = 新 runId
    runner.create_run("run-y", "ds-zh")
    assert runner.execute_run("run-y").status == "completed"


def test_crash_recovery_in_mock_mode_resumes_pending_and_stale_trials(runner, tmp_path):
    runner.create_run("run-crash", "ds-zh")
    # 模拟崩溃:状态停留在 running,一条 trial 中断、一条未开始
    state_file = runner.run_dir("run-crash") / "state.json"
    state_file.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    state = runner.load_run("run-crash")
    assert state.status == "running"
    stale, fresh = state.trials[0], state.trials[1]
    stale.status = "running"
    runner._write_trial(stale)
    assert fresh.status == "pending"

    resumed = runner.execute_run("run-crash")
    assert resumed.status == "completed"
    assert all(trial.status == "completed" for trial in resumed.trials)
    events = [
        json.loads(line)
        for line in (runner.run_dir("run-crash") / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(event["type"] == "trial.reset" for event in events)


def test_live_requires_explicit_budget_and_replay_requires_source(runner):
    """live 默认预算为 0:没有显式正预算不能发起真实调用;回放必须指明源与录制。"""
    with pytest.raises(ValueError, match="显式配置正预算"):
        runner.create_run("run-live", "ds-zh", mode="live", provider="cloud")
    with pytest.raises(ValueError, match="不能使用 mock Provider"):
        runner.create_run("run-live", "ds-zh", mode="live", provider="mock", budget={"maxCost": 5.0})
    with pytest.raises(ValueError, match="source_run_id"):
        runner.create_run("run-replay", "ds-zh", mode="replay")


def test_pending_trials_do_not_inflate_pass_rate(runner):
    """计划冻结后未执行的 trial 保留在分母里(METRIC-01 的执行器侧验证)。"""
    manifest = runner.create_run("run-plan", "ds-zh")
    state = runner.load_run("run-plan")
    assert len(manifest.plan) == 2
    assert all(trial.status == "pending" for trial in state.trials)
    cases = DatasetStore.load_run_snapshot(runner.run_dir("run-plan"))
    report = build_report(RunState(manifest=state.manifest, trials=state.trials), cases)
    assert report["denominators"]["plan"] == 2
    assert report["denominators"]["attempted"] == 0
    assert report["metrics"]["planCompletionRate"] == 0.0
    assert report["metrics"]["conditionalQualityPassRate"] is None
