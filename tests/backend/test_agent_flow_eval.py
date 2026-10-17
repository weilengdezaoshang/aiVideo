"""agent_flow 套件选择、非法用例、负样本与文生图回归。"""

import json
from pathlib import Path

import pytest

from backend.evaluation.agent_flow import filter_cases_by_suite, load_flow_sources, select_flow_suite
from backend.evaluation.datasets import read_jsonl
from backend.evaluation.models import CaseVersion
from backend.evaluation.runner import EvaluationRunner

ROOT = Path(__file__).resolve().parents[2]
LAYER_COUNTS = {"L0": 4, "L1": 3, "L2": 3, "L3": 4, "L4": 4, "L5": 8, "L7": 4}


def _cases():
    return [case for _, case in load_flow_sources(ROOT)]


def test_select_each_layer_and_full_package():
    cases = _cases()
    assert len(cases) == sum(LAYER_COUNTS.values())
    for layer, count in LAYER_COUNTS.items():
        selected = select_flow_suite(cases, layer)
        assert len(selected) == count
        assert {layer, "agent_flow"} <= set(selected[0].suites)
    full = select_flow_suite(cases, "agent_flow")
    assert {case.caseId for case in full} == {case.caseId for case in cases}


def test_unsupported_suite_fails_clearly():
    with pytest.raises(ValueError, match="不支持的 suite"):
        select_flow_suite(_cases(), "not-a-suite")


def test_known_suite_without_cases_fails_clearly():
    with pytest.raises(ValueError, match="没有用例"):
        filter_cases_by_suite(_cases(), "judge_calibration")


def test_illegal_action_rejected():
    with pytest.raises(ValueError, match="不支持的 action"):
        CaseVersion.model_validate(_flow_case(steps=[{"action": "teleport"}], expect={"route": "direct"}))


def test_paid_provider_rejected():
    with pytest.raises(ValueError, match="stub"):
        CaseVersion.model_validate(
            _flow_case(
                steps=[{"action": "send", "prompt": "生成一张咖啡海报"}],
                expect={"route": "direct"},
                mocks={"imageProvider": "cloud"},
            )
        )


def test_t2i_cases_roundtrip_without_flow_fields():
    items = read_jsonl(ROOT / "evals" / "datasets" / "core-zh.jsonl")
    assert len(items) >= 1
    for item in items:
        case = CaseVersion.model_validate(item)
        assert case.taskType == "text_to_image"
        assert "steps" not in case.input.params
        again = CaseVersion.model_validate(json.loads(case.model_dump_json()))
        assert again.contentHash == case.contentHash
        assert again.input.prompt == case.input.prompt


def test_negative_route_sample_fails_with_real_trace(tmp_path):
    """复杂广告被标成 direct：执行成功，规则失败，Trace 里仍有真实 handoff。"""
    source = tmp_path / "neg.jsonl"
    source.write_text(
        json.dumps(
            _flow_case(
                case_id="neg-complex-as-direct",
                steps=[{"action": "send", "prompt": "做一条30秒咖啡广告，3个镜头"}],
                expect={"route": "direct"},
                suites=["agent_flow", "L1", "routing"],
            ),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    runner = EvaluationRunner(ROOT, store=tmp_path / "store")
    runner.freeze_dataset(source, "neg-flow")
    runner.create_run("neg-route", "neg-flow")
    state = runner.execute_run("neg-route")
    trial = state.trials[0]
    assert trial.status == "completed"
    assert trial.qualityVerdict == "fail"
    failed = [
        grade.checkId
        for grade in state.grades
        if grade.trialId == trial.trialId and grade.status == "fail"
    ]
    assert failed == ["route"]
    assert trial.tracePath
    document = json.loads(Path(trial.tracePath).read_text(encoding="utf-8"))
    assert document["version"] == "1"
    assert "handoff_to_planner" in json.dumps(document, ensure_ascii=False)


def _flow_case(steps, expect, case_id="bad-flow-xx", suites=None, mocks=None):
    return {
        "schemaVersion": 2,
        "caseId": case_id,
        "version": 1,
        "language": "zh-CN",
        "taskType": "agent_flow",
        "tags": ["negative"],
        "suites": suites or ["agent_flow", "L0"],
        "title": "流程用例",
        "steps": steps,
        "expect": expect,
        "mocks": mocks or {"imageProvider": "stub"},
        "provenance": {"source": "synthetic"},
    }
