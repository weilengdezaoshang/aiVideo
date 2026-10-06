"""The loop policy decides what a model answer means; nothing here touches a database."""
from pathlib import Path
from types import SimpleNamespace

from backend.agent.kernel import Limits
from backend.agent.legacy_loop import LegacyLoop, REPAIR_NOTICE
from backend.agent.tools import execute_envelope, project_snapshot

LIMITS = Limits(max_model_calls=8, max_tool_calls=12, tool_timeout_s=15, tool_heartbeat_s=5)
LOOP = LegacyLoop()


def canvas():
    objects = {"a": {"id": "a", "kind": "image", "x": 0, "y": 0, "width": 40, "height": 30}}
    return project_snapshot({"id": "d", "name": "n", "revision": 3, "order": ["a"], "objects": objects}, [])


def run(counters=None, tool_call_count=0):
    return SimpleNamespace(counters=counters or {}, tool_call_count=tool_call_count, snapshot=canvas())


def model(calls=(), finish="tool_calls", content=None):
    return SimpleNamespace(tool_calls=list(calls), finish_reason=finish, content=content)


def request(name, arguments, ident="c1"):
    return {"id": ident, "name": name, "arguments": arguments, "raw_arguments": "{}"}


def finish(answer="好", claims=(), ident="f"):
    return request("finish", {"answer": answer, "claims": list(claims)}, ident)


def after(r, response, rows=()):
    return LOOP.after_model(r, SimpleNamespace(ordinal=1), response, LIMITS, list(rows))


def ok_row(ordinal, name, arguments, snapshot):
    envelope = execute_envelope(name, arguments, snapshot, {}, Path("."), max_output_bytes=16384,
                                ordinal=ordinal, call_id=f"c{ordinal}")
    return {"ordinal": ordinal, "name": name, "arguments": arguments, "kind": "read", "status": "completed",
            "output": envelope}


def test_a_tool_free_answer_may_finish_without_claims():
    outcome = after(run(), model([finish("你好")]))
    assert (outcome.kind, outcome.status, outcome.reply) == ("end", "completed", "你好")
    assert outcome.plans[0].kind == "control" and outcome.plans[0].output["status"] == "accepted"
    assert outcome.claims["verification"]["passed"] is True


def test_read_calls_become_pending_rows_and_finish_is_never_queued():
    outcome = after(run(), model([request("read_canvas", {}), request("list_canvas_elements", {}, "c2")]))
    assert outcome.kind == "tools" and [p.status for p in outcome.plans] == ["pending", "pending"]
    assert all(p.kind == "read" for p in outcome.plans)


def test_plain_text_is_repaired_twice_then_fails_without_replaying_the_text():
    r = run()
    first = after(r, model(finish="stop", content="直接回答"))
    assert first.kind == "continue" and first.counters == {"formatRepairs": 1, "repairAfter": [1]}
    second = after(run(first.counters), model(finish="stop", content="还是直接回答"))
    assert second.counters["formatRepairs"] == 2
    third = after(run(second.counters), model(finish="stop", content="仍然"))
    assert (third.kind, third.status, third.error["code"]) == ("end", "failed", "FORMAT_REPAIR_EXHAUSTED")
    assert "finish" in REPAIR_NOTICE


def test_truncated_or_rejected_output_is_not_a_success():
    assert after(run(), model(finish="length", content="半截")).status == "limit_reached"
    assert after(run(), model(finish="content_filter", content="x")).error["code"] == "MODEL_RESPONSE_REJECTED"


def test_finish_must_stand_alone_and_invalid_arguments_are_actionable():
    mixed = after(run(), model([request("read_canvas", {}), finish()]))
    assert mixed.kind == "tools" and [p.status for p in mixed.plans] == ["pending", "failed"]
    assert mixed.plans[1].output["error"]["code"] == "FORMAT_ERROR" and mixed.counters["formatRepairs"] == 1
    bad = after(run(), model([request("finish", {"answer": "", "claims": "x", "extra": 1})]))
    error = bad.plans[0].output["error"]
    assert error["code"] == "INVALID_TOOL_ARGUMENTS" and error["retryable"] is True
    paths = {field["path"] for field in error["details"]["fields"]}
    assert {"answer", "claims", "extra"} <= paths


def test_unverified_answers_are_rejected_with_the_failing_checks_and_end_after_two_revisions():
    snapshot = canvas()
    rows = [ok_row(1, "read_canvas", {}, snapshot)]
    bad = finish("共 9 个", [{"text": "共 9 个对象", "evidenceRefs": ["E1.f2"], "scope": "complete"}])
    counters: dict = {}
    for revision in (1, 2):
        outcome = after(run(counters, 1), model([bad]), rows)
        assert outcome.kind == "tools" and outcome.plans[0].status == "failed"
        receipt = outcome.plans[0].output
        assert receipt["error"]["code"] == "CLAIMS_INVALID" and "H4" in receipt["error"]["message"]
        assert outcome.claims["verification"]["passed"] is False
        counters = outcome.counters
        assert counters["answerRevisions"] == revision
    last = after(run(counters, 1), model([bad]), rows)
    assert (last.kind, last.status, last.error["code"]) == ("end", "limit_reached", "ANSWER_UNVERIFIED")
    assert last.reply == "共 9 个" and last.claims["verification"]["passed"] is False


def test_rejected_completeness_claim_carries_the_required_follow_up():
    objects = {f"o{i}": {"id": f"o{i}", "kind": "image", "x": 0, "y": 0, "width": 1, "height": 1} for i in range(60)}
    snapshot = project_snapshot({"id": "d", "name": "n", "revision": 3, "order": list(objects), "objects": objects}, [])
    rows = [ok_row(1, "list_canvas_elements", {"limit": 50}, snapshot)]
    claim = {"text": "共 60 个对象", "evidenceRefs": ["E1"], "scope": "complete"}
    outcome = LOOP.after_model(SimpleNamespace(counters={}, tool_call_count=1, snapshot=snapshot),
                               SimpleNamespace(ordinal=2), model([finish("共 60 个", [claim])]), LIMITS, rows)
    nxt = outcome.plans[0].output["error"]["next"]
    assert nxt and nxt[0]["tool"] == "list_canvas_elements" and nxt[0]["arguments"]["offset"] == 50


def test_read_evidence_replays_an_earlier_envelope_without_creating_evidence():
    snapshot = canvas()
    rows = [ok_row(1, "read_canvas", {}, snapshot)]
    hit = after(run(counters={}, tool_call_count=1), model([request("read_evidence", {"evidenceId": "E1"})]), rows)
    assert hit.plans[0].output["effect"]["envelope"]["evidenceId"] == "E1" and hit.plans[0].status == "completed"
    miss = after(run(counters={}, tool_call_count=1), model([request("read_evidence", {"evidenceId": "E7"})]), rows)
    assert miss.plans[0].output["error"]["code"] == "EVIDENCE_NOT_FOUND"
    bad = after(run(), model([request("read_evidence", {"evidenceId": "7"})]))
    assert bad.plans[0].output["error"]["code"] == "INVALID_TOOL_ARGUMENTS"


def test_batch_overflow_is_rejected_whole():
    outcome = after(run(tool_call_count=11), model([request("read_canvas", {}), request("read_canvas", {}, "c2")]))
    assert (outcome.kind, outcome.error["code"]) == ("end", "TOOL_CALL_LIMIT") and not outcome.plans
