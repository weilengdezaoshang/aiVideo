"""Rule/fake planners and program-owned subject policy: no model or database."""
import asyncio
import pytest
from backend.core.supervisor.planners import RulePlanner, FakePlanner, cutout_target
from backend.core.supervisor.subjects import decision


@pytest.mark.parametrize("text,target", [("抠图", ""), ("抠出主体", ""), ("去背景", ""), ("把杯子抠出来", "杯子")])
def test_cutout_rule_has_two_steps(text, target):
    result = asyncio.run(RulePlanner().plan({"request": {"message": text, "selected_assets": ["image"]}, "invariants": {}}))
    assert result.plan.steps[0].parameters["target_description"] == target
    assert result.plan.steps[1].parameters["mask"] == "$t1.output"
    assert result.plan.steps[1].depends_on == ["t1"]
    assert result.tokens == 0


@pytest.mark.parametrize("labels,confidence,target,mode,expected", [
    (["杯子"], .9, "", "auto", "auto"), (["杯子", "盘子"], .9, "杯子", "auto", "auto"),
    (["杯子", "盘子"], .9, "", "auto", "ask"), (["杯子"], .1, "", "auto", "ask"),
    (["杯子"], None, "", "auto", "ask"), (["杯子"], .9, "", "plan", "ask"),
    ([], .9, "", "auto", "none"), (["杯子"], .9, "盘子", "auto", "ask")])
def test_subject_policy(labels, confidence, target, mode, expected):
    result = {"candidates": [{"label": label, "confidence": confidence} for label in labels]}
    assert decision(result, target, mode, .8)[0] == expected


def test_fake_cursor_survives_recreation():
    result = {"plan": {"goal": "test", "steps": [{"id": "t1", "kind": "ask_user"}]},
        "confidence": 1, "raw_output": {"test": True}}
    planner = FakePlanner([result])
    assert asyncio.run(planner.plan({"attempt": 2, "invocation_key": "persisted"})).raw_output == {"test": True}
    assert cutout_target("生成视频") is None


def test_explicit_single_image_rule_and_uncertain_video():
    context = {"invariants": {}, "request": {"message": "生成一张红色图片", "selected_assets": []},
        "tools": [{"name": "text_to_image_preview", "available": True,
            "parameters": {"properties": {"width": {"enum": [512]}, "height": {"enum": [512]}}}}]}
    result = asyncio.run(RulePlanner().plan(context))
    assert result.plan.steps[0].tool == "text_to_image_preview"
    assert result.plan.steps[0].parameters["width"] == 512
    context["request"]["message"] = "生成视频"
    assert asyncio.run(RulePlanner().plan(context)).plan.steps[0].kind == "ask_user"
