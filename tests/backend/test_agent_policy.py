"""Duplicate suppression, circuit breaking and next-step filtering are pure functions of rows."""
from pathlib import Path

from backend.agent import evidence as ev
from backend.agent import policy as pol
from backend.agent.tools import GATEWAY, execute_envelope, project_snapshot

POLICY = pol.Policy(GATEWAY)


def snapshot(count=60):
    objects = {f"o{i}": {"id": f"o{i}", "kind": "image", "x": 0, "y": 0, "width": 10, "height": 10}
               for i in range(count)}
    return project_snapshot({"id": "d", "name": "n", "revision": 1, "order": list(objects), "objects": objects}, [])


def row(ordinal, name, args, status="completed", output=None, kind="read"):
    return {"ordinal": ordinal, "name": name, "arguments": args, "status": status, "kind": kind, "output": output}


def executed(ordinal, name, args, snap):
    envelope = execute_envelope(name, args, snap, {}, Path("."), max_output_bytes=16384,
                                ordinal=ordinal, call_id=f"c{ordinal}")
    return row(ordinal, name, args, "failed" if envelope["status"] == "error" else "completed", envelope)


def test_key_ignores_argument_order_and_filled_defaults():
    assert POLICY.key("list_canvas_elements", {}) == POLICY.key("list_canvas_elements", {"offset": 0, "limit": 50})
    assert POLICY.key("get_canvas_elements", {"objectIds": ["a"]}) != POLICY.key("get_canvas_elements", {"objectIds": ["b"]})
    assert POLICY.key("list_canvas_elements", {"kind": "image", "limit": 5}) == \
        POLICY.key("list_canvas_elements", {"limit": 5, "kind": "image"})
    assert POLICY.key("read_canvas", {}) != POLICY.key("list_canvas_elements", {})
    assert POLICY.key("nope", {"x": 1}) == POLICY.key("nope", {"x": 1})        # unknown tools still hash


def test_repeating_a_successful_call_is_a_duplicate_of_the_original_evidence():
    snap, rows = snapshot(), []
    rows.append(executed(1, "list_canvas_elements", {}, snap))
    decision = POLICY.admit_read(rows, "list_canvas_elements", {"offset": 0, "limit": 50}, {})
    assert isinstance(decision, pol.Duplicate) and decision.of == "E1"
    assert any(item["priority"] == "required" for item in decision.next)        # unfinished work is carried over
    assert isinstance(POLICY.admit_read(rows, "list_canvas_elements", {"offset": 50}, {}), pol.Run)


def test_duplicate_next_drops_suggestions_that_have_since_been_executed():
    snap = snapshot()
    rows = [executed(1, "list_canvas_elements", {}, snap)]
    rows.append(executed(2, "list_canvas_elements", {"offset": 50, "limit": 50}, snap))
    decision = POLICY.admit_read(rows, "list_canvas_elements", {}, {})
    assert isinstance(decision, pol.Duplicate) and not any(
        item["tool"] == "list_canvas_elements" and item["priority"] == "required" for item in decision.next)


def test_a_repeat_inside_the_same_batch_points_at_the_first_ordinal():
    decision = POLICY.admit_read([], "read_canvas", {}, {POLICY.key("read_canvas", {}): 4})
    assert decision == pol.Duplicate("E4")


def test_a_failed_call_may_be_retried_until_the_circuit_opens():
    snap, rows = snapshot(), []
    for ordinal, asset in enumerate(["a", "b"], start=1):
        assert isinstance(POLICY.admit_read(rows, "inspect_image", {"assetId": asset}, {}), pol.Run)
        rows.append(executed(ordinal, "inspect_image", {"assetId": asset}, snap))
    refused = POLICY.admit_read(rows, "inspect_image", {"assetId": "c"}, {})
    assert isinstance(refused, pol.Reject) and refused.code == "CIRCUIT_OPEN"
    rows.append(row(3, "inspect_image", {"assetId": "c"}, "failed"))
    assert isinstance(POLICY.admit_read(rows, "inspect_image", {"assetId": "d"}, {}), pol.Fail)


def test_another_tool_or_a_success_resets_the_failure_streak():
    snap = snapshot()
    rows = [executed(1, "inspect_image", {"assetId": "a"}, snap), executed(2, "inspect_image", {"assetId": "b"}, snap),
            executed(3, "read_canvas", {}, snap)]
    assert isinstance(POLICY.admit_read(rows, "inspect_image", {"assetId": "c"}, {}), pol.Run)


def test_filter_next_removes_only_executed_suggestions():
    snap = snapshot()
    first = executed(1, "list_canvas_elements", {}, snap)
    followup = {"offset": 50, "limit": 50}
    again = execute_envelope("list_canvas_elements", {"offset": 0, "limit": 50, "kind": "image"}, snap, {}, Path("."),
                             max_output_bytes=16384, ordinal=2, call_id="c2")
    assert again["status"] == "ok"
    before = [item["arguments"] for item in first["output"]["result"]["next"]]
    assert followup in [{k: v for k, v in args.items() if k != "kind"} for args in before if args]
    rows = [row(0, "list_canvas_elements", followup, "completed", {"status": "ok"})]
    filtered = POLICY.filter_next(first["output"], rows)
    assert not any(item["arguments"] == followup for item in filtered["result"]["next"])
    assert any(item["tool"] == "get_canvas_elements" for item in filtered["result"]["next"])


def test_error_envelope_next_is_filtered_too():
    snap = snapshot()
    error = ev.error_envelope(tool="get_canvas_elements", ordinal=1, call_id="c", step_key=None, snapshot=snap,
                              code="OBJECT_OUT_OF_SCOPE", message="x")
    rows = [row(0, "list_canvas_elements", {}, "completed", {"status": "ok"})]
    assert POLICY.filter_next(error, rows)["error"]["next"] == []
