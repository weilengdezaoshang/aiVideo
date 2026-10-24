"""Session merge, fencing, interrupt and correction rules without PostgreSQL."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.core.sessions.contracts import SessionSettings, merge_deadline
from backend.core.sessions.correction import detect_correction
from backend.core.sessions.fencing import ever_submitted, revision_blocks_submit
from backend.core.sessions.intake import request_identity, semantic_hash, validate_content
from backend.core.sessions.interrupt import is_replan, invocation_obsolete
from backend.core.sessions.revisions import effective_request
from backend.core.supervisor.planners import FakePlanner
from backend.core.supervisor.reviewer import _sources
import asyncio
import pytest


def test_quiet_window_caps_at_max_wait():
    settings = SessionSettings(quiet_window_seconds=1.2, max_merge_wait_seconds=4)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = now - timedelta(seconds=3.5)
    deadline = merge_deadline(now, first, settings)
    assert deadline == first + timedelta(seconds=4)
    early = merge_deadline(now, now, settings)
    assert early == now + timedelta(seconds=1.2)


def test_three_turns_keep_ordered_text():
    rows = [
        SimpleNamespace(content={"text": "狗"}),
        SimpleNamespace(content={"text": "不对兔子"}),
        SimpleNamespace(content={"text": "再暗一点"}),
    ]
    request = effective_request(rows)
    assert request["messages"] == ["狗", "不对兔子", "再暗一点"]
    assert request["message"] == "狗 不对兔子 再暗一点"


def test_client_request_keys_are_namespaced():
    left = request_identity("device-a", "m1")
    right = request_identity("device-b", "m1")
    assert left != right
    assert semantic_hash({"text": "狗"}, intent="draw") != semantic_hash({"text": "狗"}, intent="edit")


def test_validate_rejects_empty_and_bad_mode():
    settings = SessionSettings()
    with pytest.raises(ValueError, match="empty_message"):
        validate_content({"text": "   "}, settings)
    with pytest.raises(ValueError, match="invalid_mode"):
        validate_content({"text": "兔子", "mode": "chaos"}, settings)
    assert validate_content({"text": "  兔子  "}, settings)["text"] == "兔子"


def test_intake_fence_blocks_submit_before_quiet_dispatch():
    run = SimpleNamespace(input_revision=1, submit_fence=2)
    old = SimpleNamespace(input_revision=1)
    assert revision_blocks_submit(run, old) is True
    run.submit_fence = 1
    assert revision_blocks_submit(run, old) is False


def test_worker_first_submit_then_interrupt_keeps_latch():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run = SimpleNamespace(input_revision=1, submit_fence=1, first_submitted_at=now, first_submitted_task_id="t1")
    assert ever_submitted(run) is True
    run.submit_fence = 2
    run.input_revision = 2
    stale = SimpleNamespace(input_revision=1)
    assert revision_blocks_submit(run, stale) is True


def test_interrupt_replan_phase_is_not_reapplied():
    assert is_replan(SimpleNamespace(context={"phase": "replan"})) is True
    assert is_replan(SimpleNamespace(context={})) is False
    run = SimpleNamespace(input_revision=2)
    assert invocation_obsolete(SimpleNamespace(context={"input_revision": 1}), run) is True


def test_correction_phrases_and_reviewer_sources():
    assert detect_correction("不对兔子") is True
    assert detect_correction("生成一只狗") is False
    run = SimpleNamespace(id="run-1", snapshot={"request": {"message": "狗"}})
    messages = [
        {"role": "user", "run_id": "run-1", "content": {"text": "狗"}},
        {"role": "user", "run_id": "run-1", "content": {"text": "不对兔子", "correction": True, "feedback": True}},
        {"role": "user", "run_id": "run-1", "content": {"text": "再暗一点"}},
    ]
    texts, _fields = _sources(run, messages)
    assert texts == ["狗", "不对兔子", "再暗一点"]


def test_fake_planner_sees_full_correction_chain():
    async def scenario():
        planner = FakePlanner([{
            "plan": {"goal": "dark rabbit", "steps": [
                {"id": "t1", "kind": "call_tool", "tool": "edit", "parameters": {"prompt": "dark rabbit"}},
                {"id": "t2", "kind": "call_tool", "tool": "edit", "parameters": {"prompt": "followup"}, "depends_on": ["t1"]},
            ], "writer": "rule", "result_type": "generate"},
            "confidence": 1, "raw_output": {"obsolete": False}, "tokens": 1,
        }])
        context = {"attempt": 0, "invocation_key": "k", "request": {"message": "狗 不对兔子 再暗一点",
            "messages": ["狗", "不对兔子", "再暗一点"]}}
        result = await planner.plan(context)
        assert "rabbit" in result.plan.steps[0].parameters["prompt"]
        stale = FakePlanner([{
            "plan": {"goal": "dog", "steps": [{"id": "t1", "kind": "ask_user"}], "writer": "rule", "result_type": "clarify"},
            "confidence": 1, "raw_output": {"stale": True}, "tokens": 4,
        }])
        leftover = await stale.plan({"attempt": 0, "invocation_key": "old"})
        assert leftover.raw_output["stale"] is True
        assert invocation_obsolete(SimpleNamespace(context={"input_revision": 1}), SimpleNamespace(input_revision=2))
    asyncio.run(scenario())


def test_selected_image_modifier_prefers_edit_tool_without_inventing_mask():
    async def scenario():
        from backend.core.supervisor.planners import RulePlanner
        context = {"request": {"message": "换成蓝色", "selected_assets": ["selected-image"]},
            "invariants": {}, "tools": [{"name": "image_edit_preview", "available": True,
                "parameters": {"properties": {"prompt": {"type": "string"}, "image": {},
                    "count": {"type": "integer"}, "width": {"enum": [512]}, "height": {"enum": [512]}}}}]}
        result = await RulePlanner().plan(context)
        assert result.confidence == 1
        assert result.plan.steps[0].tool == "image_edit_preview"
        assert result.plan.steps[0].parameters["image"]["asset_id"] == "selected-image"
        assert "mask" not in result.plan.steps[0].parameters
    asyncio.run(scenario())
