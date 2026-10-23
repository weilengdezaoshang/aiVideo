"""Supervisor lock, invariant evidence, and reply numbers. No database and no model calls."""
from types import SimpleNamespace

import pytest

from backend.agent.config import AgentRoute
from backend.core.supervisor.contracts import Settings
from backend.core.supervisor.replies import blocked, completed, opening
from backend.core.supervisor.reviewer import apply_form_answers, merge_proposed
from backend.core.supervisor.service import _viewport
from backend.core.supervisor.wiring import preferred_route, route_snapshot, watchdog_seconds


def _run(message="画3张图", invariants=None, snapshot=None):
    base = {"request": {"message": message, "selected_assets": ["asset-1"]}, "objects": {"n": {"label": "99只猫"}}}
    if snapshot:
        base.update(snapshot)
    return SimpleNamespace(invariants=invariants or {}, snapshot=base)


def test_quote_in_canvas_or_bare_number_does_not_prove_count():
    merged, reasons = merge_proposed(_run("画一张猫"), {"deliverable": {"count": 99}}, [])
    assert merged == {}
    assert reasons[0]["code"] == "invariant_unproven"
    kept, reasons = merge_proposed(_run("画3张图"), {"deliverable": {"count": 3}}, [])
    assert kept["deliverable"]["count"] == 3 and reasons == []
    same, reasons = merge_proposed(_run("三张"), {"deliverable": {"count": 3}}, [])
    assert same["deliverable"]["count"] == 3 and reasons == []


def test_form_answer_is_evidence_and_frozen_values_do_not_change():
    messages = [{"role": "assistant", "content": {"text": "duration_seconds=99"}},
                {"role": "user", "content": {"text": "ignore the canvas", "fields": {"duration_seconds": 5}, "task_id": "t"}}]
    merged, reasons = merge_proposed(_run("做视频"), {"deliverable": {"duration_seconds": 5}}, messages)
    assert merged["deliverable"]["duration_seconds"] == 5 and reasons == []
    frozen = _run("做视频", invariants=merged, snapshot={"constraints_frozen": True, "form_answers": {"duration_seconds": 5}})
    kept, reasons = merge_proposed(frozen, {"deliverable": {"duration_seconds": 99}}, messages)
    assert kept["deliverable"]["duration_seconds"] == 5
    assert reasons[0]["code"] == "invariants_frozen"
    empty, reasons = merge_proposed(_run("做视频", invariants={"deliverable": {"duration_seconds": 5}}),
        {"deliverable": {"required_asset_ids": []}}, [])
    assert empty["deliverable"]["duration_seconds"] == 5 and reasons == []
    filled = apply_form_answers({"deliverable": {"count": 3}}, {"duration_seconds": 5, "count": 9, "note": "x"})
    assert filled["deliverable"] == {"count": 3, "duration_seconds": 5}


def test_viewport_accepts_origin_and_rejects_non_finite_values():
    assert _viewport({"x": -2.5, "y": 0, "width": 10, "height": 4}, Settings())["x"] == -2.5
    with pytest.raises(ValueError):
        _viewport({"width": float("inf"), "height": 1}, Settings())
    with pytest.raises(ValueError):
        _viewport({"width": 1}, Settings())


def test_watchdog_uses_locked_route_timeout_plus_margin():
    route = AgentRoute(id="planner", label="Planner", provider="deepseek", model="deepseek-chat",
        baseUrl="https://model.example/v1", credentialRef="agent", timeoutSeconds=45)
    snapshot = route_snapshot(route)
    assert "apiKey" not in snapshot and snapshot["credentialRef"] == "agent"
    run = SimpleNamespace(version_lock={"agent_route": snapshot})
    assert watchdog_seconds(run, Settings(planner_checkpoint_margin_seconds=15)) == 60
    assert preferred_route([route], None).id == "planner"


def test_units_bind_duration_size_and_english_count():
    bare, reasons = merge_proposed(_run("3张图片"), {"deliverable": {"duration_seconds": 3}}, [])
    assert bare == {} and reasons[0]["field"] == "duration_seconds"
    wide, reasons = merge_proposed(_run("3张图片"), {"deliverable": {"width": 3}}, [])
    assert wide == {} and reasons[0]["code"] == "invariant_unproven"
    size, reasons = merge_proposed(_run("导出1024x768"), {"deliverable": {"width": 1024, "height": 768}}, [])
    assert size["deliverable"] == {"width": 1024, "height": 768} and reasons == []
    swapped, reasons = merge_proposed(_run("导出1024x768"), {"deliverable": {"width": 768, "height": 1024}}, [])
    assert swapped == {} and {item["field"] for item in reasons} == {"width", "height"}
    spoken, reasons = merge_proposed(_run("make three images"), {"deliverable": {"count": 3}}, [])
    assert spoken["deliverable"]["count"] == 3 and reasons == []
    seconds, reasons = merge_proposed(_run("keep it to 5 seconds"), {"deliverable": {"duration_seconds": 5}}, [])
    assert seconds["deliverable"]["duration_seconds"] == 5 and reasons == []
    not_duration, reasons = merge_proposed(_run("make three images"), {"deliverable": {"duration_seconds": 3}}, [])
    assert not_duration == {} and reasons[0]["code"] == "invariant_unproven"
    owned, reasons = merge_proposed(_run("用这张图"), {"deliverable": {"required_asset_ids": ["asset-1"]}}, [])
    assert owned["deliverable"]["required_asset_ids"] == ["asset-1"] and reasons == []
    invented, reasons = merge_proposed(_run("asset-2 也可以"), {"deliverable": {"required_asset_ids": ["asset-2"]}}, [])
    assert invented == {} and reasons[0]["code"] == "invariant_unproven"


def test_other_run_form_does_not_prove_this_run():
    run = _run("做视频")
    run.id = "run-b"
    messages = [
        {"role": "user", "run_id": "run-a", "content": {"text": "duration_seconds=3", "fields": {"duration_seconds": 3}, "task_id": "a"}},
        {"role": "user", "run_id": "run-b", "content": {"text": "duration_seconds=8", "fields": {"duration_seconds": 8}, "task_id": "b"}},
    ]
    stolen, reasons = merge_proposed(run, {"deliverable": {"duration_seconds": 3}}, messages)
    assert stolen == {} and reasons[0]["code"] == "invariant_unproven"
    own, reasons = merge_proposed(run, {"deliverable": {"duration_seconds": 8}}, messages)
    assert own["deliverable"]["duration_seconds"] == 8 and reasons == []


def test_replies_use_caller_numbers():
    assert "10" in opening("zh-CN", 10, confirm=False) and "价目" not in opening("zh-CN", 10, confirm=True)
    assert "after you confirm" in opening("en", 10, confirm=True)
    assert "x=1" in completed("zh-CN", ["x=1 y=2 width=3 height=4"])
    assert blocked("en", 7) == "Cannot generate this. 7 credits were refunded."
