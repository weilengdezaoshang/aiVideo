"""指标口径验收(§21 METRIC-01/METRIC-03)与旧数据导入验收(§19.2/COMPAT)。"""

import json

from backend.evaluation.legacy import LegacyImporter
from backend.evaluation.models import (
    Attempt,
    CaseVersion,
    Grade,
    RunManifest,
    RunState,
    Trial,
    TrialPlanLine,
    trial_identity,
)
from backend.evaluation.reports import ReportWriter, build_report, render_markdown

from eval_helpers import make_case


def _case_for(case_id):
    return CaseVersion.model_validate(make_case(case_id))


def build_state(run_id, total, executed, judged_pass=0, judged_fail=0):
    plan = [
        TrialPlanLine(
            caseId=f"case-{i:03d}",
            caseVersion=1,
            caseContentHash=f"hash-{i}",
            repetitionIndex=0,
            requestedSeed=i,
        )
        for i in range(total)
    ]
    manifest = RunManifest(
        runId=run_id,
        createdAt="2026-09-11T00:00:00.000Z",
        mode="mock",
        provider="mock",
        dataset={"datasetId": "ds", "version": 1, "contentHash": "x", "frozenAt": "t"},
        plan=plan,
    )
    trials = []
    grades = []
    attempts = []
    for index, line in enumerate(plan):
        trial = Trial(
            trialId=trial_identity(run_id, line.caseId, 1, 0),
            runId=run_id,
            caseId=line.caseId,
            caseVersion=1,
            repetitionIndex=0,
            requestedSeed=line.requestedSeed,
        )
        if index < executed:
            trial.status = "completed"
            trial.artifactIds = [f"art-{index:016d}"]
            attempt = Attempt(
                attemptId=f"{trial.trialId}-gen-0",
                trialId=trial.trialId,
                callSite="generation",
                status="succeeded",
            )
            attempts.append(attempt)
            if index < judged_pass + judged_fail:
                verdict_pass = index < judged_pass
                trial.qualityVerdict = "pass" if verdict_pass else "fail"
                trial.weightedScore = 1.0 if verdict_pass else 0.0
                grades.append(
                    Grade(
                        gradeId=f"{trial.trialId}:c1",
                        trialId=trial.trialId,
                        checkId="c1",
                        evaluator={"id": "typed-rules", "version": "1", "rubricVersion": "1"},
                        status="pass" if verdict_pass else "fail",
                        observed=True,
                        expected=True,
                        score=1.0 if verdict_pass else 0.0,
                        source="rules",
                    )
                )
        trials.append(trial)
    return RunState(manifest=manifest, trials=trials, grades=grades, attempts=attempts)


def test_metric01_partial_judging_shows_both_rates():
    """10/100 判定且全通过:计划合格完成率 10% 与条件质量通过率 100% 同时呈现。"""
    state = build_state("metric01", total=100, executed=10, judged_pass=10)
    cases = [_case_for(f"case-{i:03d}") for i in range(100)]
    report = build_report(state, cases)
    assert report["denominators"] == {
        "plan": 100, "attempted": 10, "generated": 10, "determined": 10, "pass": 10,
    }
    assert report["metrics"]["conditionalQualityPassRate"] == 1.0
    assert report["metrics"]["planCompletionRate"] == 0.1
    assert report["metrics"]["executionCoverage"] == 0.1
    markdown = render_markdown(report)
    assert "10.0%" in markdown and "100.0%" in markdown
    assert len(report["coverageGaps"]) == 90  # 未执行不隐藏


def test_metric03_empty_and_unknown_states_render_na():
    state = build_state("metric03", total=10, executed=0)
    cases = [_case_for(f"case-{i:03d}") for i in range(10)]
    report = build_report(state, cases)
    for metric in report["metrics"].values():
        assert metric is None or metric == 0.0
    assert report["metrics"]["conditionalQualityPassRate"] is None
    assert report["metrics"]["costPerQualifiedArtifact"] is None
    markdown = render_markdown(report)
    assert "N/A" in markdown
    # 零合格素材:成本显示不可计算,不显示 0
    assert "不可计算" in markdown or "N/A(mock" in markdown


def test_report_versions_are_immutable(tmp_path):
    state = build_state("metric-ver", total=4, executed=4, judged_pass=2, judged_fail=2)
    cases = [_case_for(f"case-{i:03d}") for i in range(4)]
    report = build_report(state, cases)
    writer = ReportWriter(tmp_path / "store")
    first_json, first_md = writer.write(dict(report))
    second_json, _ = writer.write(dict(report))
    assert first_json.parent != second_json.parent
    first = json.loads(first_json.read_text(encoding="utf-8"))
    assert first["reportVersion"] == 1
    assert json.loads(second_json.read_text(encoding="utf-8"))["reportVersion"] == 2
    assert first_md.is_file()


def test_legacy_import_preserves_old_evidence_without_faking_verdicts(tmp_path):
    old_dir = tmp_path / "old-run"
    old_dir.mkdir()
    artifacts = old_dir / "artifacts"
    artifacts.mkdir()
    (artifacts / "old-1.png").write_bytes(b"\x89PNG-fake-bytes")
    (old_dir / "run.json").write_text(
        json.dumps(
            {
                "runId": "old-1",
                "createdAt": "2026-08-01T00:00:00.000Z",
                "provider": "mock",
                "goldenSet": "evals/golden-set.jsonl",
                "entries": [
                    {
                        "id": "t2i-subject-01",
                        "bucket": "t2i.subject",
                        "status": "completed",
                        "artifact": "artifacts/old-1.png",
                        "seed": 42,
                    },
                    {"id": "t2i-subject-02", "bucket": "t2i.subject", "status": "failed", "error": "生成失败"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (old_dir / "scores.json").write_text(
        json.dumps(
            {
                "judge": "stub",
                "entries": [
                    {
                        "id": "t2i-subject-01",
                        "score": 1.0,
                        "questions": [
                            {"q": "图中主体?", "expect": ["猫"], "answer": "(stub 判分,仅验证管线)", "score": 1.0, "weight": 1}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = tmp_path / "store"
    result = LegacyImporter(store).import_run(old_dir)
    assert result["runId"] == "legacy-old-1"
    assert result["trials"] == 2
    assert result["grades"] == 1

    # 原目录保持只读完整
    assert json.loads((old_dir / "run.json").read_text(encoding="utf-8"))["runId"] == "old-1"

    from backend.evaluation.runner import EvaluationRunner

    runner = EvaluationRunner(tmp_path, store=store)
    state = runner.load_run("legacy-old-1")
    assert state.manifest.legacy is True
    assert state.manifest.mode == "mock"
    # 旧素材入库且可校验
    completed = next(t for t in state.trials if t.caseId == "t2i-subject-01")
    assert completed.artifactIds
    assert runner.objects.verify_all(completed.artifactIds) == []
    failed = next(t for t in state.trials if t.caseId == "t2i-subject-02")
    assert failed.status == "failed"
    # legacy 评分不产生新的质量结论(不可复现),不冒充 determined
    assert completed.qualityVerdict is None
    grades = [g for g in state.grades if g.trialId == completed.trialId]
    assert grades and grades[0].source == "legacy"
    assert grades[0].evaluator["version"] == "unknown"
    # completeness 如实列出缺失:不可复现与不可回放
    completeness = json.loads((runner.run_dir("legacy-old-1") / "completeness.json").read_text(encoding="utf-8"))
    text = "".join(completeness["gaps"])
    assert "不可复现" in text
    assert "不能升级为严格回放" in text

    # legacy run 的报告:分母如实、结论不可判定
    report = build_report(state, [])
    assert report["denominators"]["plan"] == 2
    assert report["metrics"]["conditionalQualityPassRate"] is None
    assert any("legacy" in note for note in report["annotations"])
