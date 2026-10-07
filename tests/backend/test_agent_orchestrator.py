"""Stage transitions of the harness as pure functions: every row of the state table, and every limit."""
from pathlib import Path

import pytest

from backend.agent import orchestrator as orch
from backend.agent.kernel import Limits
from backend.agent.state import State, StepState
from backend.agent.tools import execute_envelope, project_snapshot

LIMITS = Limits(max_model_calls=20, max_tool_calls=24, tool_timeout_s=15, tool_heartbeat_s=5)


def canvas(count=3):
    objects = {f"o{i}": {"id": f"o{i}", "kind": "image", "x": i, "y": 0, "width": 100 + i, "height": 50}
               for i in range(count)}
    return project_snapshot({"id": "d", "name": "n", "revision": 7, "order": list(objects), "objects": objects}, [])


def step(key="s1", status="running", tools=("read_canvas",), ordinal=1, retries=0, depends=(), summary=None, refs=()):
    return StepState(f"id-{key}", key, ordinal, f"步骤{key}", "读取画布", tuple(tools), tuple(depends),
                     "已取得对象数量", status, summary, tuple(refs), retries)


def make(stage="plan", steps=(), counters=None, rows=(), draft=None, count=3, tool_calls=0, **kw):
    current = next((s.key for s in steps if s.status == "running"), None)
    plan = {"goal": "了解画布", "version": 1, "current": current} if steps else None
    return State(stage=stage, snapshot=canvas(count), message="画布上有什么", counters=counters or {},
                 rows=tuple(rows), tool_call_count=tool_calls, plan=plan, steps=tuple(steps), draft=draft, **kw)


def req(name, args, ident="c1"):
    return {"id": ident, "name": name, "arguments": args, "raw_arguments": "{}"}


def on(state, *calls, finish="tool_calls", content=None, ordinal=1):
    return orch.on_model(state, list(calls), finish, content, ordinal, LIMITS)


def read_row(number, name, args, step_id=None, count=3, status="completed"):
    envelope = execute_envelope(name, args, canvas(count), {}, Path("."), max_output_bytes=16384,
                                ordinal=number, call_id=f"r{number}")
    return {"name": name, "arguments": args, "kind": "read", "status": "failed" if envelope["status"] == "error" else status,
            "output": envelope, "step_id": step_id}


def plan_args(*steps, goal="了解画布"):
    return {"goal": goal, "steps": list(steps)}


def step_args(key="s1", tools=("read_canvas",), deps=(), done="已取得对象数量"):
    return {"key": key, "title": f"步骤{key}", "intent": "读取画布", "tools": list(tools), "dependsOn": list(deps),
            "doneWhen": done}


def receipt(outcome, index=0):
    return outcome.plans[index].output


def finish_args(answer="共 3 个[E1.f2]", claims=None):
    claims = claims if claims is not None else [{"text": "对象总数为 3", "evidenceRefs": ["E1.f2"], "scope": "complete"}]
    return {"answer": answer, "claims": claims}


def draft(verified=True):
    return {"answer": "共 3 个", "claims": [{"text": "对象总数为 3", "evidenceRefs": ["E1.f2"], "scope": "complete"}],
            "verification": {"passed": verified, "checks": []}}


# ---- plan -------------------------------------------------------------------------------------
def test_a_valid_plan_creates_ordered_steps_and_enters_execute():
    out = on(make("plan"), req("submit_plan", plan_args(step_args("b", deps=["a"]), step_args("a"))))
    assert out.kind == "tools" and receipt(out)["status"] == "accepted"
    assert out.run["stage"] == "execute" and out.run["plan"] == {"goal": "了解画布", "version": 1, "current": "a"}
    created = out.steps[0]
    assert created["op"] == "create" and [(s["key"], s["status"], s["ordinal"]) for s in created["steps"]] == [
        ("a", "running", 1), ("b", "pending", 2)]
    assert out.events == ["plan.submitted", "stage.entered"] and out.run["counters"]["formatRepairs"] == 0


def test_replanning_obsoletes_the_old_steps_and_bumps_the_version():
    state = make("plan", [step("s1", "blocked")])
    out = on(state, req("submit_plan", plan_args(step_args("n1"))))
    assert [op["op"] for op in out.steps] == ["obsolete", "create"] and out.steps[1]["version"] == 2
    assert out.run["plan"]["version"] == 2


@pytest.mark.parametrize("bad", [
    plan_args(step_args("a"), step_args("a")), plan_args(step_args("a", deps=["a"])),
    plan_args(step_args("a", deps=["zzz"])), plan_args(step_args("a", deps=["b"]), step_args("b", deps=["a"])),
    plan_args(step_args("a", tools=[])), plan_args(step_args("a", tools=["finish"])),
    plan_args(step_args("a", tools=["nope"])),
])
def test_invalid_plans_are_rejected_with_paths_and_do_not_advance(bad):
    out = on(make("plan"), req("submit_plan", bad))
    error = receipt(out)["error"]
    assert error["code"] == "PLAN_INVALID" and error["details"]["fields"] and out.run["counters"]["formatRepairs"] == 1
    assert "stage" not in out.run and out.steps == []


def test_malformed_plan_arguments_and_exhausted_repairs_end_the_run():
    out = on(make("plan"), req("submit_plan", {"goal": "x", "steps": []}))
    assert receipt(out)["error"]["code"] == "INVALID_TOOL_ARGUMENTS"
    last = on(make("plan", counters={"formatRepairs": 2}), req("submit_plan", {"goal": "x", "steps": []}))
    assert (last.kind, last.status, last.error["code"]) == ("end", "failed", "FORMAT_REPAIR_EXHAUSTED")


def test_plan_stage_allows_scouting_reads_but_not_other_stages_controls():
    out = on(make("plan"), req("read_canvas", {}), req("list_canvas_elements", {}, "c2"))
    assert out.kind == "tools" and [p.status for p in out.plans] == ["pending", "pending"]
    for name, args in [("finish", finish_args()), ("complete_step", {"summary": "x", "evidenceRefs": []}),
                       ("submit_reflection", {"verdict": "accept"})]:
        refused = on(make("plan"), req(name, args))
        assert receipt(refused)["error"]["code"] == "TOOL_NOT_ALLOWED"
        assert "submit_plan" in receipt(refused)["error"]["details"]["allowedTools"]
    unknown = on(make("plan"), req("write_canvas", {}))
    assert unknown.plans[0].kind == "read" and unknown.plans[0].output["error"]["code"] == "UNKNOWN_TOOL"
    assert unknown.plans[0].output["evidenceId"] == "E1"


def test_a_control_tool_mixed_with_other_calls_is_a_format_error_but_reads_still_run():
    out = on(make("plan"), req("read_canvas", {}), req("submit_plan", plan_args(step_args())))
    assert [p.status for p in out.plans] == ["pending", "failed"]
    assert receipt(out, 1)["error"]["code"] == "FORMAT_ERROR" and "stage" not in out.run


def test_plain_text_is_repaired_with_a_notice_marker_then_the_run_fails():
    out = on(make("plan"), finish="stop", content="直接回答", ordinal=4)
    assert out.kind == "continue" and out.run["counters"] == {"formatRepairs": 1, "repairAfter": [4]}
    last = on(make("plan", counters={"formatRepairs": 2}), finish="stop", content="仍然")
    assert (last.kind, last.status, last.error["code"]) == ("end", "failed", "FORMAT_REPAIR_EXHAUSTED")
    assert on(make("plan"), finish="length", content="半截").status == "limit_reached"
    assert on(make("plan"), finish="content_filter", content="x").error["code"] == "MODEL_RESPONSE_REJECTED"


def test_ask_user_ends_the_run_in_a_waiting_state_from_plan_or_execute():
    out = on(make("plan"), req("ask_user", {"question": "你指的是哪一张图？"}))
    assert (out.kind, out.status, out.error) == ("end", "waiting_input", None)
    assert out.run["waiting"] == {"question": "你指的是哪一张图？", "stage": "plan"} and out.run["stage"] == "waiting"
    assert receipt(out)["status"] == "accepted"
    again = on(make("execute", [step()]), req("ask_user", {"question": "确认？"}))
    assert again.run["waiting"]["stage"] == "execute"


def test_batch_overflow_is_rejected_whole():
    out = on(make("plan", tool_calls=23), req("read_canvas", {}), req("list_canvas_elements", {}, "c2"))
    assert (out.kind, out.error["code"]) == ("end", "TOOL_CALL_LIMIT") and not out.plans


# ---- execute ----------------------------------------------------------------------------------
def test_execute_only_offers_the_steps_tools_and_caps_calls_per_step():
    state = make("execute", [step(tools=("read_canvas",))])
    out = on(state, req("list_canvas_elements", {}))
    refused = out.plans[0].output["error"]
    assert refused["code"] == "TOOL_NOT_ALLOWED" and refused["details"]["allowedTools"][0] == "read_canvas"
    ok = on(state, req("read_canvas", {}))
    assert ok.plans[0].status == "pending" and ok.plans[0].step_id == "id-s1"
    busy = make("execute", [step(tools=("list_canvas_elements",))], rows=[
        {**read_row(n, "list_canvas_elements", {"offset": n}, "id-s1")} for n in range(1, 7)])
    capped = on(busy, req("list_canvas_elements", {"offset": 99}))
    assert "上限" in capped.plans[0].output["error"]["message"]


def test_completing_the_only_step_moves_to_respond_with_evidence_from_that_step():
    state = make("execute", [step()], rows=[read_row(1, "read_canvas", {}, "id-s1")])
    out = on(state, req("complete_step", {"summary": "共 3 个对象", "evidenceRefs": ["E1.f2"]}))
    assert receipt(out)["effect"]["next"] == {"stage": "respond"} and out.run["stage"] == "respond"
    assert out.steps == [{"op": "update", "key": "s1", "fields": {
        "status": "completed", "summary": "共 3 个对象", "evidence_refs": ["E1.f2"]}}]
    assert out.events == ["step.completed", "stage.entered"]


def test_completing_a_step_starts_the_next_one_and_keeps_the_stage():
    state = make("execute", [step("s1"), step("s2", "pending", ordinal=2, depends=["s1"])],
                 rows=[read_row(1, "read_canvas", {}, "id-s1")])
    out = on(state, req("complete_step", {"summary": "完成", "evidenceRefs": ["E1"]}))
    assert out.run["plan"]["current"] == "s2" and "stage" not in out.run
    assert {"op": "update", "key": "s2", "fields": {"status": "running"}} in out.steps
    assert receipt(out)["effect"]["next"] == {"stage": "execute", "stepKey": "s2"}


@pytest.mark.parametrize("refs,expected", [
    ([], "没有引用"), (["E9"], "不存在"), (["E1.f99"], "不存在"), (["E1"], "不是本步骤"), (["E2"], "失败的工具结果")])
def test_complete_step_needs_ok_evidence_produced_inside_the_step(refs, expected):
    rows = [read_row(1, "read_canvas", {}, "id-other"), read_row(2, "get_canvas_elements", {"objectIds": ["ghost"]}, "id-s1")]
    state = make("execute", [step("s1", tools=("read_canvas", "get_canvas_elements"))], rows=rows)
    out = on(state, req("complete_step", {"summary": "完成", "evidenceRefs": refs}))
    error = receipt(out)["error"]
    assert error["code"] == "STEP_UNSUPPORTED" and expected in error["message"] and not out.steps


def test_unfinished_required_reads_block_completion_unless_declared_partial():
    count = 60
    rows = [read_row(1, "list_canvas_elements", {"limit": 50}, "id-s1", count=count)]
    state = make("execute", [step(tools=("list_canvas_elements",))], rows=rows, count=count)
    blocked = on(state, req("complete_step", {"summary": "共 60 个", "evidenceRefs": ["E1.f1"]}))
    error = receipt(blocked)["error"]
    assert error["code"] == "STEP_UNSUPPORTED" and error["next"][0]["priority"] == "required"
    assert error["next"][0]["arguments"]["offset"] == 50
    partial = on(state, req("complete_step", {"summary": "前 50 个", "evidenceRefs": ["E1.f1"], "partial": True}))
    assert receipt(partial)["status"] == "accepted" and partial.steps[0]["fields"]["summary"].startswith("（部分）")


def test_report_blocker_replans_and_records_a_lesson_until_the_budget_runs_out():
    state = make("execute", [step()])
    out = on(state, req("report_blocker", {"code": "tool_error", "detail": "素材不可读"}))
    assert out.run["stage"] == "plan" and out.run["counters"]["planRevisions"] == 1
    assert out.steps == [{"op": "update", "key": "s1", "fields": {"status": "blocked", "summary": "素材不可读"}}]
    assert out.reflections[0]["source"] == "blocker" and out.reflections[0]["issues"][0]["stepKey"] == "s1"
    assert receipt(out)["effect"]["planRevisionsLeft"] == 1
    third = on(make("execute", [step()], counters={"planRevisions": 2}),
               req("report_blocker", {"code": "other", "detail": "仍然不行"}))
    assert (third.kind, third.error["code"]) == ("end", "PLAN_REVISIONS_EXHAUSTED") and third.status == "limit_reached"


# ---- respond ----------------------------------------------------------------------------------
def respond_state(**kw):
    rows = kw.pop("rows", [read_row(1, "read_canvas", {}, "id-s1")])
    steps = kw.pop("steps", [step(status="completed", summary="共 3 个对象", refs=("E1.f2",))])
    return make("respond", steps, rows=rows, **kw)


def test_a_verified_answer_becomes_the_draft_and_goes_to_reflection_not_to_done():
    out = on(respond_state(), req("finish", finish_args()))
    assert out.kind == "tools" and receipt(out)["status"] == "accepted" and out.run["stage"] == "reflect"
    assert out.run["claims"]["verification"]["passed"] is True and out.status is None


def test_an_unverified_answer_is_revised_with_failing_checks_and_never_completes():
    bad = finish_args("共 9 个", [{"text": "对象总数为 9", "evidenceRefs": ["E1.f2"], "scope": "complete"}])
    out = on(respond_state(), req("finish", bad))
    assert receipt(out)["error"]["code"] == "CLAIMS_INVALID" and "H4" in receipt(out)["error"]["message"]
    assert "stage" not in out.run and out.run["counters"]["answerRevisions"] == 1
    assert out.reflections[0]["source"] == "hard_check" and out.reflections[0]["verdict"] == "revise_answer"
    assert out.run["claims"]["verification"]["passed"] is False
    last = on(respond_state(counters={"answerRevisions": 2}), req("finish", bad))
    assert (last.kind, last.status, last.error["code"], last.reply) == ("end", "limit_reached", "ANSWER_UNVERIFIED", "共 9 个")


def test_citing_a_failed_read_sends_the_run_back_to_that_step_once():
    rows = [read_row(1, "get_canvas_elements", {"objectIds": ["ghost"]}, "id-s1")]
    steps = [step(status="completed", tools=("get_canvas_elements",))]
    bad = finish_args("对象为 1 个", [{"text": "对象总数为 1", "evidenceRefs": ["E1"], "scope": "item"}])
    out = on(respond_state(rows=rows, steps=steps), req("finish", bad))
    assert out.run["stage"] == "execute" and out.run["plan"]["current"] == "s1"
    assert out.steps == [{"op": "update", "key": "s1", "fields": {"status": "running", "retries": 1}}]
    assert out.reflections[0]["verdict"] == "retry_step" and "将回到对应步骤重试" in receipt(out)["error"]["message"]
    spent = on(respond_state(rows=rows, steps=[step(status="completed", tools=("get_canvas_elements",), retries=1)]),
               req("finish", bad))
    assert "stage" not in spent.run and spent.reflections[0]["verdict"] == "revise_answer"


def test_evidence_from_another_canvas_version_ends_the_run():
    state = respond_state()
    stale = State(**{**state.__dict__, "snapshot": {**state.snapshot, "revision": 8}})
    out = on(stale, req("finish", finish_args()))
    assert (out.kind, out.status, out.error["code"]) == ("end", "failed", "EVIDENCE_REVISION_MISMATCH")


def test_finish_must_stand_alone():
    out = on(respond_state(), req("read_evidence", {"evidenceId": "E1"}), req("finish", finish_args(), "f"))
    assert receipt(out, 0)["status"] == "accepted" and receipt(out, 1)["error"]["code"] == "FORMAT_ERROR"


def test_read_evidence_replays_a_stored_envelope_in_any_stage():
    state = respond_state()
    hit = on(state, req("read_evidence", {"evidenceId": "E1"}))
    assert receipt(hit)["effect"]["envelope"]["evidenceId"] == "E1"
    assert receipt(on(state, req("read_evidence", {"evidenceId": "E7"})))["error"]["code"] == "EVIDENCE_NOT_FOUND"
    assert receipt(on(state, req("read_evidence", {"evidenceId": "7"})))["error"]["code"] == "INVALID_TOOL_ARGUMENTS"


# ---- reflect ----------------------------------------------------------------------------------
def reflect_state(**kw):
    rows = [read_row(1, "read_canvas", {}, "id-s1")]
    return make("reflect", [step(status="completed", summary="共 3 个对象", refs=("E1.f2",))], rows=rows,
                draft=draft(), **kw)


def reflection(verdict, issues=(), question=None):
    return req("submit_reflection", {"verdict": verdict, "issues": list(issues), "question": question})


def issue(kind="ungrounded", problem="断言超出证据", **extra):
    return {"kind": kind, "problem": problem, "stepKey": None, "evidenceRefs": [], **extra}


def test_accept_is_the_only_way_to_complete_and_records_the_review():
    out = on(reflect_state(), reflection("accept"))
    assert (out.kind, out.status, out.reply) == ("end", "completed", "共 3 个")
    assert out.reflections[0]["source"] == "model" and out.reflections[0]["verdict"] == "accept"
    assert out.run["claims"]["reflection"] == {"verdict": "accept", "round": 1, "issues": []}
    assert out.run["counters"]["reflectRounds"] == 1


def test_revise_answer_returns_to_respond_and_counts_against_the_revision_budget():
    out = on(reflect_state(), reflection("revise_answer", [issue(evidenceRefs=["E1.f2"])]))
    assert out.run["stage"] == "respond" and out.run["counters"]["answerRevisions"] == 1 and out.status is None
    spent = on(reflect_state(counters={"answerRevisions": 2, "reflectRounds": 1}),
               reflection("revise_answer", [issue(evidenceRefs=["E1.f2"])]))
    assert (spent.status, spent.error["code"], spent.reply) == ("limit_reached", "REFLECTION_NOT_ACCEPTED", "共 3 个")


def test_retry_step_reopens_that_step_once():
    out = on(reflect_state(), reflection("retry_step", [issue("incomplete", "步骤未达成", stepKey="s1")]))
    assert out.run["stage"] == "execute" and out.run["plan"]["current"] == "s1"
    assert out.steps == [{"op": "update", "key": "s1", "fields": {"status": "running", "retries": 1}}]
    state = make("reflect", [step(status="completed", retries=1)], rows=[read_row(1, "read_canvas", {}, "id-s1")], draft=draft())
    spent = on(state, reflection("retry_step", [issue("incomplete", "步骤未达成", stepKey="s1")]))
    assert (spent.status, spent.error["code"]) == ("limit_reached", "STEP_RETRIES_EXHAUSTED")


def test_replan_goes_back_to_plan_and_is_bounded():
    out = on(reflect_state(), reflection("replan", [issue("off_target", "答非所问", stepKey="s1")]))
    assert out.run["stage"] == "plan" and out.run["counters"]["planRevisions"] == 1
    spent = on(reflect_state(counters={"planRevisions": 2}), reflection("replan", [issue("off_target", "x", stepKey="s1")]))
    assert spent.error["code"] == "PLAN_REVISIONS_EXHAUSTED"


def test_a_review_may_ask_the_user_but_must_say_what():
    out = on(reflect_state(), reflection("ask_user", [issue("off_target", "无法判断指代", stepKey="s1")], "你说的是哪一张？"))
    assert (out.status, out.run["waiting"]) == ("waiting_input", {"question": "你说的是哪一张？", "stage": "reflect"})
    missing = on(reflect_state(), reflection("ask_user", [issue("off_target", "无法判断", stepKey="s1")]))
    assert receipt(missing)["error"]["code"] == "REFLECTION_INVALID"


@pytest.mark.parametrize("verdict,issues,text", [
    ("accept", [issue(evidenceRefs=["E1"])], "issues 必须为空"),
    ("revise_answer", [issue()], "没有 evidenceRefs 或 stepKey"),
    ("revise_answer", [issue(evidenceRefs=["E9"])], "引用的 E9 不存在"),
    ("revise_answer", [issue(stepKey="ghost")], "stepKey ghost 不存在"),
    ("retry_step", [issue(evidenceRefs=["E1"])], "必须在 issue 中给出 stepKey"),
])
def test_invalid_reviews_are_rejected_and_never_accepted(verdict, issues, text):
    out = on(reflect_state(), reflection(verdict, issues))
    error = receipt(out)["error"]
    assert error["code"] == "REFLECTION_INVALID" and text in error["message"]
    assert out.kind == "tools" and out.status is None and out.run["counters"]["reflectionInvalid"] == 1
    assert out.reflections == [] and "stage" not in out.run


def test_two_invalid_reviews_in_a_row_fall_back_to_a_rewrite_never_to_accept():
    out = on(reflect_state(counters={"reflectionInvalid": 1}), reflection("accept", [issue(evidenceRefs=["E1"])]))
    assert receipt(out)["error"]["code"] == "REFLECTION_INVALID" and out.run["stage"] == "respond"
    assert out.reflections[0]["verdict"] == "revise_answer" and out.status is None
    assert out.run["counters"]["reflectionInvalid"] == 0


def test_the_review_round_cap_ends_the_run_with_the_draft():
    out = on(reflect_state(counters={"reflectRounds": 2}), reflection("revise_answer", [issue(evidenceRefs=["E1"])]))
    assert (out.status, out.error["code"], out.reply) == ("limit_reached", "REFLECTION_NOT_ACCEPTED", "共 3 个")


def test_only_the_reflection_tool_is_available_in_reflect():
    out = on(reflect_state(), req("finish", finish_args()))
    assert receipt(out)["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert receipt(out)["error"]["details"]["allowedTools"] == ["submit_reflection", "read_evidence"]
    read = on(reflect_state(), req("read_canvas", {}))
    assert read.plans[0].output["error"]["code"] == "TOOL_NOT_ALLOWED"


def test_stage_change_resets_the_format_repair_budget_but_keeps_repair_markers():
    out = on(make("plan", counters={"formatRepairs": 2, "repairAfter": [3]}), req("submit_plan", plan_args(step_args())))
    assert out.run["counters"]["formatRepairs"] == 0 and out.run["counters"]["repairAfter"] == [3]
