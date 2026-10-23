"""Hand-written planner observations. These plans are not copied from expected."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.core.supervisor.contracts import PlannerResult
from backend.evaluation.planner_cases import ExpectedPlan, PlannerCase, load_cases
from backend.evaluation.planner_cli import main
from backend.evaluation.planner_evaluation import (
    RULE_BASELINE_MODE,
    aggregate,
    evaluate_cases,
    score,
)


def _expected(**overrides) -> ExpectedPlan:
    payload = {
        "tool_names": ["image_edit_preview", "image_to_video_preview"],
        "dependencies": {"t2": ["t1"]},
        "invariants": {"deliverable": {"modality": "video", "duration_seconds": 5, "count": 1}},
        "result_type": "generate",
        "must_ask": [],
        "bindings": {},
    }
    payload.update(overrides)
    return ExpectedPlan.model_validate(payload)


def _plan(steps, result_type=None):
    plan = {"goal": "hand written", "steps": steps}
    if result_type is not None:
        plan["result_type"] = result_type
    return plan


def _edit(step_id="t1", depends=None):
    return {
        "id": step_id,
        "kind": "call_tool",
        "tool": "image_edit_preview",
        "parameters": {"prompt": "edit"},
        "depends_on": depends or [],
    }


def _video(step_id="t2", depends=None):
    return {
        "id": step_id,
        "kind": "call_tool",
        "tool": "image_to_video_preview",
        "parameters": {"prompt": "video"},
        "depends_on": depends or [],
    }


def _invariants(**deliverable):
    return {"deliverable": deliverable or None}


PASS = {"verdict": "pass", "reasons": []}
REJECT = {"verdict": "reject", "reasons": [{"gate": "intent", "code": "hand_written"}]}


def test_wrong_tool_is_not_a_match():
    expected = _expected()
    plan = _plan([
        {**_edit(), "tool": "text_to_image_preview"},
        _video(depends=["t1"]),
    ])
    result = score(expected, plan, _invariants(modality="video", duration_seconds=5, count=1), PASS)
    assert result.tool_match is False


def test_reversed_steps_fail_order_even_with_a_dependency_edge():
    expected = _expected()
    # Video is written first. The edge points at a later step, so list order is not enough.
    plan = _plan([
        _video("t1", depends=["t2"]),
        _edit("t2"),
    ])
    result = score(expected, plan, _invariants(modality="video", duration_seconds=5, count=1), PASS)
    assert result.order_required is True
    assert result.order_match is False
    assert result.tool_match is False


def test_correct_list_order_without_dependency_fails_order():
    expected = _expected()
    plan = _plan([_edit("t1"), _video("t2")])
    result = score(expected, plan, _invariants(modality="video", duration_seconds=5, count=1), PASS)
    assert result.tool_match is True
    assert result.order_match is False


def test_wrong_mask_binding_fails():
    expected = _expected(
        tool_names=["detect_subject", "cutout"],
        dependencies={"t2": ["t1"]},
        bindings={"t2.mask": "$t1.output"},
        invariants={"deliverable": {}},
    )
    plan = _plan([
        {
            "id": "t1",
            "tool": "detect_subject",
            "parameters": {"image": {"asset_id": "a", "media_type": "image"}},
            "depends_on": [],
        },
        {
            "id": "t2",
            "tool": "cutout",
            "parameters": {"mask": "$t9.output"},
            "depends_on": ["t1"],
        },
    ])
    result = score(expected, plan, {"deliverable": None}, PASS)
    assert result.tool_match is True
    assert result.bindings_match is False


def test_parent_id_order_does_not_change_dependency_match():
    expected = _expected(dependencies={"t3": ["t1", "t2"]})
    plan = _plan([
        _edit("t1"),
        _edit("t2"),
        {**_video("t3"), "depends_on": ["t2", "t1"]},
    ])
    assert score(expected, plan, _invariants(modality="video", duration_seconds=5, count=1), PASS).order_match is True


def test_invariants_normalize_empty_assets_and_reject_illegal_shapes():
    expected = _expected(invariants={"deliverable": {"count": 1}})
    plan = _plan([_edit(), _video(depends=["t1"])])
    omitted = score(expected, plan, {"deliverable": {"count": 1}}, PASS)
    explicit_empty = score(expected, plan, {"deliverable": {"count": 1, "required_asset_ids": []}}, PASS)
    explicit_null = score(expected, plan, {"deliverable": {"count": 1, "required_asset_ids": None}}, PASS)
    reordered_keys = score(
        expected,
        plan,
        {"deliverable": {"required_asset_ids": [], "count": 1}},
        PASS,
    )
    assert omitted.invariants_match is True
    assert explicit_empty.invariants_match is True
    assert explicit_null.invariants_match is True
    assert reordered_keys.invariants_match is True
    assert score(expected, plan, {"deliverable": {"count": True}}, PASS).invariants_match is False
    assert score(expected, plan, {"deliverable": {"count": 1}, "extra": 1}, PASS).invariants_match is False
    assert score(expected, plan, {"deliverable": {"count": 1, "unknown": 1}}, PASS).invariants_match is False
    assert score(expected, plan, {"deliverable": {"count": 2}}, PASS).invariants_match is False
    assert score(
        expected,
        plan,
        {"deliverable": {"count": 1, "required_asset_ids": ["asset-extra"]}},
        PASS,
    ).invariants_match is False
    labeled = _expected(invariants={"deliverable": {"required_asset_ids": ["a", "b"]}})
    assert score(
        labeled,
        plan,
        {"deliverable": {"required_asset_ids": ["b", "a"]}},
        PASS,
    ).invariants_match is True
    assert score(labeled, plan, {"deliverable": {}}, PASS).invariants_match is False
    assert score(
        labeled,
        plan,
        {"deliverable": {"required_asset_ids": ["a"]}},
        PASS,
    ).invariants_match is False
    assert score(
        labeled,
        plan,
        {"deliverable": {"required_asset_ids": ["a", "c"]}},
        PASS,
    ).invariants_match is False


def test_missing_and_extra_invariant_fields_fail():
    expected = _expected()
    missing = score(
        expected,
        _plan([_edit(), _video(depends=["t1"])]),
        _invariants(modality="video", duration_seconds=5),
        PASS,
    )
    extra = score(
        expected,
        _plan([_edit(), _video(depends=["t1"])]),
        _invariants(modality="video", duration_seconds=5, count=1, aspect_ratio="16:9"),
        PASS,
    )
    assert missing.invariants_match is False
    assert extra.invariants_match is False


def test_review_reject_is_not_a_first_pass_and_null_review_stays_null():
    expected = _expected(dependencies={}, tool_names=["image_edit_preview"])
    plan = _plan([_edit()])
    rejected = score(expected, plan, _invariants(modality="video", duration_seconds=5, count=1), REJECT)
    unknown = score(expected, plan, _invariants(modality="video", duration_seconds=5, count=1), None)
    assert rejected.first_review_pass is False
    assert unknown.first_review_pass is None
    assert unknown.review_unavailable_reason == "reviewer_returned_null"


def test_set_invariants_is_not_a_tool_and_answer_only_reads_result_type():
    expected = _expected(
        tool_names=["answer_only"],
        dependencies={},
        result_type="answer_only",
        invariants={"deliverable": {}},
    )
    plan = _plan(
        [{"id": "t1", "kind": "set_invariants", "tool": "set_invariants", "parameters": {}}],
        result_type="answer_only",
    )
    result = score(expected, plan, {"deliverable": None}, PASS)
    assert result.tool_match is True
    assert result.order_match is None


def test_ask_user_maps_to_ask_user_form_and_must_ask_is_required():
    expected = _expected(
        tool_names=["ask_user_form"],
        dependencies={},
        result_type="clarify",
        must_ask=["duration_seconds"],
        invariants={"deliverable": {}},
    )
    silent = _plan([{
        "id": "t1",
        "kind": "ask_user",
        "parameters": {"question": "请补充要求", "fields": []},
    }])
    asked = _plan([{
        "id": "t1",
        "kind": "ask_user",
        "parameters": {"question": "时长？", "fields": ["duration_seconds"]},
    }])
    assert score(expected, silent, {"deliverable": None}, PASS).must_ask_match is False
    assert score(expected, asked, {"deliverable": None}, PASS).tool_match is True
    assert score(expected, asked, {"deliverable": None}, PASS).must_ask_match is True


def test_order_rate_ignores_unordered_cases_and_zero_denominator_is_null():
    unordered = score(
        _expected(dependencies={}, tool_names=["image_edit_preview"]),
        _plan([_edit()]),
        _invariants(modality="video", duration_seconds=5, count=1),
        {"verdict": None, "unavailable_reason": "not_run"},
    )
    records = []
    from backend.evaluation.planner_evaluation import CaseEvaluation

    records.append(CaseEvaluation(
        case_id="only-unordered",
        category="single",
        language="zh",
        mode=RULE_BASELINE_MODE,
        input={"message": "x"},
        usage={"tokens": 0},
        latency_ms=1.5,
        score=unordered,
    ))
    summary = aggregate(records, mode=RULE_BASELINE_MODE)
    assert summary.order_match_denominator == 0
    assert summary.order_match_rate is None
    assert summary.first_review_pass_rate is None
    assert summary.llm_tested is False
    assert summary.tokens["distribution"] == {"0": 1}
    assert summary.mode == RULE_BASELINE_MODE


class _ScriptPlanner:
    def __init__(self, result: PlannerResult):
        self.result = result
        self.seen = []

    async def plan(self, context):
        self.seen.append(context["request"]["message"])
        return self.result


def _one_case() -> PlannerCase:
    rows = load_cases()
    return rows[0]


def test_evaluate_cases_uses_planner_return_not_expected():
    case = _one_case()
    plan = {
        "goal": case.context.request.message,
        "steps": [{"id": "t1", "kind": "ask_user", "parameters": {"question": "手写", "fields": []}}],
    }
    planner = _ScriptPlanner(PlannerResult(
        plan=plan,
        invariants={"deliverable": None},
        confidence=1,
        raw_output={"writer": "test"},
        tokens=0,
    ))

    def context_builder(row: PlannerCase) -> dict:
        return {"request": row.context.request.model_dump()}

    def reviewer(result, context):
        assert result.plan.steps[0].kind == "ask_user"
        assert context["request"]["message"] == case.context.request.message
        return {"verdict": "reject", "reasons": [{"gate": "intent", "code": "observed"}]}

    records, summary = asyncio.run(evaluate_cases(
        [case], planner, context_builder, reviewer, concurrency=1, mode=RULE_BASELINE_MODE,
    ))
    assert planner.seen == [case.context.request.message]
    assert records[0].score.tool_match is False
    assert records[0].score.first_review_pass is False
    assert records[0].usage["tokens"] == 0
    assert records[0].latency_ms is not None
    assert records[0].latency_ms >= 0
    assert records[0].mode == RULE_BASELINE_MODE
    assert summary.llm_tested is False
    assert "未调用 LLM" in summary.llm_untested_reason
    assert records[0].input["untrusted_text_omitted"] is True
    assert "summary" not in records[0].input


def test_schema_failure_is_not_a_silent_pass(tmp_path: Path):
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"case_id": "x"}\n', encoding="utf-8")
    code = main(["--cases", str(broken), "--output", str(tmp_path / "out")])
    assert code == 2
    assert not (tmp_path / "out" / "summary.json").exists()
    with pytest.raises((ValidationError, ValueError, json.JSONDecodeError)):
        load_cases(broken)


def test_non_case_rows_are_rejected():
    with pytest.raises(TypeError):
        asyncio.run(evaluate_cases(
            [{"case_id": "raw"}], _ScriptPlanner(PlannerResult(
                plan={"goal": "n", "steps": [{"id": "t1", "kind": "ask_user"}]},
                confidence=1,
                raw_output={},
            )),
            lambda case: {},
            lambda result, context: {"verdict": "pass"},
        ))


def test_upload_injection_fixtures_reach_only_the_data_tag():
    from backend.core.supervisor.context import assemble_messages
    from backend.evaluation.planner_cases import FIXTURE_PATH, load_cases
    from backend.evaluation.planner_evaluation import rule_context
    exercised = 0
    for case in load_cases(FIXTURE_PATH):
        note = case.context.upload_note
        if not note:
            continue
        context = rule_context(case)
        messages = assemble_messages(context)
        data = messages[0]["content"].split("<upload>", 1)[1].split("</upload>", 1)[0]
        escaped = note.replace("&", r"\u0026").replace("<", r"\u003c").replace(">", r"\u003e")
        assert escaped in data
        assert messages[1]["content"] == case.context.request.message
        assert context["language"] == case.language
        exercised += 1
    assert exercised == 4
