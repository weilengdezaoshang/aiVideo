"""Engine failure protocol helpers. No database and no paid model."""
from types import SimpleNamespace

import pytest

from backend.core.supervisor.contracts import PlannerResult
from backend.core.supervisor.engine import account_planner_usage
from backend.core.supervisor.reviewer import conversion_rejection
from tests.backend.test_planner_tool_calls import _catalog, _convert, _filled, _image, _tool
from backend.core.tools.contracts import Port


def test_non_finite_tool_arguments_and_form_numbers_are_rejected():
    from backend.core.supervisor.service import _check_form_fields
    from backend.core.supervisor.tool_calls import ConversionError, _parse_call
    with pytest.raises(ConversionError):
        _parse_call({"id": "a", "name": "edit", "arguments": '{"prompt": NaN}'}, 0)
    with pytest.raises(ConversionError):
        _parse_call({"id": "a", "name": "edit", "arguments": {"prompt": "x", "duration_seconds": float("inf")}}, 0)
    form = {"fields": [{"id": "duration_seconds", "type": "number", "required": True}]}
    with pytest.raises(ValueError):
        _check_form_fields(form, {"duration_seconds": float("nan")})
    assert _check_form_fields(form, {"duration_seconds": 8})["duration_seconds"] == 8


def test_conversion_rejection_uses_redacted_codes():
    verdict = conversion_rejection({
        "rejected_planning_result": True,
        "conversion_errors": [{"code": "unknown_tool", "detail": "The model named a tool that was not offered."},
                              {"code": "parameter_schema_invalid"}],
    })
    assert verdict["verdict"] == "reject"
    assert [item["code"] for item in verdict["reasons"]] == ["unknown_tool", "parameter_schema_invalid"]
    assert conversion_rejection({"writer": "llm"}) is None


def test_review_fallback_form_is_rule_and_follows_session_language():
    from backend.core.supervisor.engine import _clarification_result
    from backend.core.supervisor.replies import plan_confirmation, review_exhausted
    run = SimpleNamespace(snapshot={"request": {"message": "make a poster"}}, invariants={"deliverable": {}})
    english = _clarification_result(run, "en-US")
    assert english.plan.writer == "rule"
    assert english.raw_output["writer"] == "rule"
    assert english.raw_output["clarification"] is True
    assert english.plan.result_type == "clarify"
    label = english.plan.steps[0].parameters["form"]["fields"][0]["label"]
    assert label == "Add the requirement that cannot be determined from the original request"
    assert "language" not in run.snapshot
    chinese = _clarification_result(run, None)
    assert chinese.plan.steps[0].parameters["form"]["fields"][0]["label"] == "请补充无法从原话确定的要求"
    question, options = plan_confirmation("en")
    assert question == "Run this plan?"
    assert [item["label"] for item in options] == ["Confirm", "Cancel"]
    assert review_exhausted("en-GB").startswith("The plan could not pass review")
    assert plan_confirmation("zh-CN")[1][0]["label"] == "确认执行"


def test_english_video_duration_form_from_rule_and_tool_calls():
    import asyncio
    from backend.core.supervisor.planners import RulePlanner
    from backend.core.supervisor.tool_calls import _duration_form
    ruled = asyncio.run(RulePlanner().plan({
        "request": {"message": "生成视频", "selected_assets": []},
        "language": "en-US", "invariants": {}, "tools": [],
    }))
    field = ruled.plan.steps[0].parameters["form"]["fields"][0]
    assert ruled.plan.steps[0].parameters["form"]["id"] == "video_duration"
    assert field["label"] == "Video duration (seconds)"
    assert ruled.plan.writer == "rule"
    catalog = _catalog([_tool("video", "v", {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "duration_seconds": {"type": "integer"},
                       "fps": {"type": "integer"}},
        "required": ["prompt", "duration_seconds", "fps"],
        "additionalProperties": False,
    }, outputs=[Port(path="output", media_type="video")])])
    form = _duration_form(
        [{"name": "video", "arguments": {"prompt": "clip"}}], catalog, catalog.lock,
        {"language": "en", "request": {"message": "make a video"}, "snapshot": {}, "invariants": {}})
    labels = {item.id: item.label for item in form.fields}
    assert labels["duration_seconds"] == "Video duration (seconds)"
    assert labels["fps"] == "Frame rate"
    zh = asyncio.run(RulePlanner().plan({
        "request": {"message": "生成视频", "selected_assets": []}, "invariants": {}, "tools": [],
    }))
    assert zh.plan.steps[0].parameters["form"]["fields"][0]["label"] == "视频时长（秒）"


def test_guessed_video_duration_becomes_form():
    catalog = _catalog([_tool("video", "v", {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "image": {"type": "object"},
                       "duration_seconds": {"type": "integer", "minimum": 1}},
        "required": ["prompt", "image", "duration_seconds"],
        "additionalProperties": False,
    }, outputs=[Port(path="output", media_type="video")])])
    video = catalog.get_tool("video", catalog.lock.version)
    guessed = _convert(catalog, [{"id": "v", "name": "video", "arguments": _filled(
        video, image=_image(), duration_seconds=5)}],
        context={"request": {"message": "换背景再做成视频"},
                 "scope": {"readable_assets": ["asset-a"]}, "invariants": {},
                 "snapshot": {"request": {"message": "换背景再做成视频"}, "objects": {"n": {"assetId": "asset-a"}}}})
    assert guessed.plan.result_type == "clarify"
    assert guessed.plan.steps[0].parameters["form"]["id"] == "video_duration"
    assert guessed.plan.steps[0].parameters["form"]["fields"][0].get("default") is None
    proven = _convert(catalog, [{"id": "v", "name": "video", "arguments": _filled(
        video, image=_image(), duration_seconds=8)}],
        context={"request": {"message": "换背景再做成8秒视频"},
                 "scope": {"readable_assets": ["asset-a"]}, "invariants": {},
                 "snapshot": {"request": {"message": "换背景再做成8秒视频"}, "objects": {"n": {"assetId": "asset-a"}}}})
    assert proven.plan.steps[0].tool == "video"
    assert proven.plan.steps[0].parameters["duration_seconds"] == 8
    concat = _catalog([_tool("concat", "c", {
        "type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"],
        "additionalProperties": False,
    }, outputs=[Port(path="output", media_type="video")])])
    kept = _convert(concat, [{"id": "c", "name": "concat", "arguments": {"prompt": "join"}}],
                    context={"request": {"message": "拼接"}, "scope": {"tools": ["concat"]},
                             "invariants": {}, "snapshot": {}})
    assert kept.plan.steps[0].tool == "concat"


def test_usage_accounted_once_and_unknown_is_flagged():
    run = SimpleNamespace(tokens_limit=10, tokens_used=0, error=None)
    invocation = SimpleNamespace(context={})
    result = PlannerResult(plan={"goal": "g", "steps": [], "result_type": "answer_only", "writer": "llm", "answer": "x"},
                           confidence=0, tokens=4, raw_output={"usage": {"total_tokens": 4}})
    invocation.id = "inv-1"
    assert account_planner_usage(run, invocation, result) is False
    assert run.tokens_used == 4
    assert run.error["planner_usage"]["known_tokens"] == 4
    assert run.error["planner_usage"]["unknown_calls"] == 0
    assert account_planner_usage(run, invocation, result) is False
    assert run.tokens_used == 4
    unknown = PlannerResult(plan=result.plan, confidence=0, tokens=0, raw_output={"usage": {"unknown": True}})
    other = SimpleNamespace(id="inv-2", context={})
    assert account_planner_usage(run, other, unknown) is False
    assert run.tokens_used == 4
    assert run.error["planner_usage"]["unknown_calls"] == 1
    over_run = SimpleNamespace(tokens_limit=5, tokens_used=0, error=None)
    big = PlannerResult(plan=result.plan, confidence=0, tokens=9, raw_output={"usage": {"total_tokens": 9}})
    assert account_planner_usage(over_run, SimpleNamespace(id="inv-3", context={}), big) is True
    assert over_run.tokens_used == 5
    assert over_run.error["planner_usage"]["known_tokens"] == 9
