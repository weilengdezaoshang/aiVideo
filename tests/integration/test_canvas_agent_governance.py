"""Harness behaviour on isolated PostgreSQL: governance, verified answers, personas and context."""
import asyncio
import json
import uuid

from sqlalchemy import select

from backend.agent import tools
from backend.agent.contracts import PersonaProfile
from backend.agent.harness_loop import REPAIR_NOTICE, HarnessLoop
from backend.agent.orm import AgentModelCall, AgentReflection, AgentRun, AgentToolCall
from backend.infrastructure.orm import Document
from test_canvas_agent_runtime import (DB, ROUTE, ScriptedModel, answer, body, claim, complete_turn,  # noqa: F401
                                       finish_call, fixture, happy, invocation, plan_step, plan_turn,
                                       reflect_turn, run_to_end)


class CountingTools:
    """Delegates to the real gateway and records every execution."""

    def __init__(self):
        self.executed = []
        self.tool_schemas = staticmethod(tools.tool_schemas)

    def execute_envelope(self, name, *args, **kwargs):
        self.executed.append(name)
        return tools.execute_envelope(name, *args, **kwargs)


async def drive(service, ws, ident, model, tmp_path, counting):
    for _ in range(60):
        value = await service.get_run(ws, ident)
        if value["status"] not in {"queued", "running"}:
            return value
        await service.advance(ident, uuid.uuid4().hex, model, counting, tmp_path)
    raise AssertionError("Agent did not finish")


def scenario_runner(coroutine):
    asyncio.run(coroutine())


def reads(result):
    return [c for c in result["toolCalls"] if c["kind"] == "read"]


def test_repeated_calls_are_resolved_as_duplicates_without_executing(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        counting = CountingTools()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(
                plan_turn(),
                answer(calls=[invocation(ident="c1"), invocation(ident="c2")]),
                answer(calls=[invocation(ident="c3")]),
                complete_turn("画布共有 1 个对象", ["E1.f2"]),
                answer(calls=[finish_call("对象总数为 1", [claim("对象总数为 1", ["E1.f2"], "complete")])]),
                reflect_turn())
            result = await drive(service, ws, ident, model, tmp_path, counting)
            assert result["status"] == "completed", result
            assert counting.executed == ["read_canvas"]
            rows = reads(result)
            assert [c["dedupeOf"] for c in rows] == [None, "E1", "E1"] and [c["evidenceId"] for c in rows] == ["E1", "E2", "E3"]
            assert [c["status"] for c in rows] == ["completed"] * 3
            assert rows[1]["output"]["status"] == "duplicate" and rows[2]["output"]["dedupeOf"] == "E1"
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_a_tool_that_keeps_failing_is_refused_and_then_ends_the_run(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        counting = CountingTools()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(plan_turn(plan_step("s1", ("inspect_image",))),
                *(answer(calls=[invocation("inspect_image", {"assetId": name}, name)]) for name in "abcd"))
            result = await drive(service, ws, ident, model, tmp_path, counting)
            assert result["status"] == "failed" and result["errorCode"] == "REPEATED_FAILURE"
            assert model.calls == 5 and counting.executed == ["inspect_image", "inspect_image"]
            codes = [c["output"]["error"]["code"] for c in reads(result)]
            assert codes == ["ASSET_OUT_OF_SCOPE", "ASSET_OUT_OF_SCOPE", "CIRCUIT_OPEN", "REPEATED_FAILURE"]
            assert all(c["status"] == "failed" for c in reads(result))
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_an_answer_with_a_wrong_number_is_rejected_then_accepted_when_fixed(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(plan_turn(), answer(calls=[invocation()]), complete_turn("画布共有 1 个对象", ["E1.f2"]),
                answer(calls=[finish_call("共 5 个", [claim("对象总数为 5", ["E1.f2"], "complete")])]),
                answer(calls=[finish_call("共 1 个[E1.f2]", [claim("对象总数为 1", ["E1.f2"], "complete")], "f2")]),
                reflect_turn())
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed" and result["reply"] == "共 1 个[E1.f2]"
            first, second = result["toolCalls"][3], result["toolCalls"][4]
            assert first["name"] == second["name"] == "finish"
            assert first["status"] == "failed" and first["output"]["error"]["code"] == "CLAIMS_INVALID"
            assert "H4" in first["output"]["error"]["message"] and second["output"]["status"] == "accepted"
            assert result["claims"]["verification"]["passed"] is True
            assert [r["source"] for r in result["reflections"]] == ["hard_check", "model"]
            assert result["reflections"][0]["verdict"] == "revise_answer"
            rejected_input = model.inputs[4][-2]
            assert rejected_input["role"] == "tool" and "CLAIMS_INVALID" in rejected_input["content"]
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_a_run_never_completes_with_an_unverified_answer(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            wrong = [finish_call("共 5 个", [claim("对象总数为 5", ["E1.f2"], "complete")], f"f{i}") for i in range(3)]
            model = ScriptedModel(plan_turn(), answer(calls=[invocation()]), complete_turn("画布共有 1 个对象", ["E1.f2"]),
                                  *(answer(calls=[w]) for w in wrong))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "limit_reached" and result["errorCode"] == "ANSWER_UNVERIFIED"
            assert result["claims"]["verification"]["passed"] is False and result["reply"] == "共 5 个"
            assert [c["status"] for c in result["toolCalls"]][-3:] == ["failed", "failed", "failed"]
            created = await service.create_run(ws, doc, body(message="next"), ROUTE)
            async with factory() as db:
                assert (await db.get(AgentRun, uuid.UUID(created["run"]["id"]))).history == []
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_plain_text_is_repaired_with_a_notice_and_gives_up_after_two_repairs(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(answer("直接回答", finish="stop"), answer("第二次直接回答", finish="stop"), *happy())
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed" and model.calls == 7
            roles = [m["role"] for m in model.inputs[2]]
            assert roles[-5:-1] == ["assistant", "user", "assistant", "user"]
            assert model.inputs[2][-3]["content"] == "第二次直接回答" and model.inputs[2][-2]["content"] == REPAIR_NOTICE
            second = await service.create_run(ws, doc, body(message="again"), ROUTE)
            ident = uuid.UUID(second["run"]["id"])
            model = ScriptedModel(*(answer(f"文字{i}", finish="stop") for i in range(3)))
            failed = await run_to_end(service, ws, ident, model, tmp_path)
            assert failed["status"] == "failed" and failed["errorCode"] == "FORMAT_REPAIR_EXHAUSTED"
            assert failed["reply"] is None and model.calls == 3
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_pre_envelope_rows_still_project_and_replay_into_context(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(answer(calls=[invocation()]), plan_turn())
            await service.advance(ident, "w", model, media_root=tmp_path)
            await service.advance(ident, "w", model, media_root=tmp_path)
            async with factory.begin() as db:
                row = await db.scalar(select(AgentToolCall).where(AgentToolCall.run_id == ident,
                                                                   AgentToolCall.name == "read_canvas"))
                row.output, row.kind, row.dedupe_of, row.tool_version = {"objectCount": 1}, "read", None, None
                run = await db.get(AgentRun, ident)
                run.claims, run.counters = None, {}
            projected = await service.get_run(ws, ident)
            assert projected["claims"] is None and projected["toolCalls"][0]["output"] == {"objectCount": 1}
            async with factory() as db:
                built = await HarnessLoop().context(db, await db.get(AgentRun, ident), ROUTE, service.limits())
            tool_messages = [m for m in built.messages if m["role"] == "tool"]
            assert '"objectCount":1' in tool_messages[0]["content"]
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def persona(identity, version=1, name="小帧"):
    return PersonaProfile(id="warm", version=version, displayName=name, identity=identity,
                          voice=["语气温和"], principles=["没有证据不下结论"], expression={})


def test_persona_is_frozen_on_the_run_and_every_model_call_records_a_manifest(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            first = await service.create_run(ws, doc, body(), ROUTE, persona=persona("你是第一版人格。", 1))
            ident = uuid.UUID(first["run"]["id"])
            assert first["run"]["persona"] == {"id": "warm", "version": 1, "displayName": "小帧"}
            model = ScriptedModel(*happy("对象总数为 1[E1.f2]"))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed"
            plan_system, respond_system = model.inputs[0][0]["content"], model.inputs[3][0]["content"]
            for system in (plan_system, respond_system):
                assert system.index("[规则 v1]") < system.index("[人格 warm@v1]") < system.index("[角色 ")
                assert "没有证据不下结论" in system and "第二版" not in system
            assert "你是第一版人格。" not in plan_system and "你是第一版人格。" in respond_system   # voice only where it speaks
            assert "[角色 planner@v1]" in plan_system and "[角色 responder@v1]" in respond_system
            async with factory() as db:
                calls = list(await db.scalars(select(AgentModelCall).where(AgentModelCall.run_id == ident)
                                              .order_by(AgentModelCall.ordinal)))
                assert [c.stage for c in calls] == ["plan", "execute", "execute", "respond", "reflect"]
                assert [c.manifest["roleId"] for c in calls] == ["planner", "executor", "executor", "responder", "reflector"]
                assert all(c.manifest["personaId"] == "warm" for c in calls)
                assert calls[4].manifest["tools"] == ["submit_reflection", "read_evidence"]
                assert calls[1].manifest["tools"] == ["read_canvas", "complete_step", "report_blocker", "ask_user", "read_evidence"]
                assert calls[1].step_id is not None and calls[0].step_id is None
                assert calls[3].manifest["totalBytes"] == len(json.dumps(calls[3].messages, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode())
                assert (await db.get(AgentRun, ident)).persona["version"] == 1
            second = await service.create_run(ws, doc, body(message="再问一次"), ROUTE, persona=persona("你是第二版人格。", 2))
            model2 = ScriptedModel(*happy())
            await run_to_end(service, ws, uuid.UUID(second["run"]["id"]), model2, tmp_path)
            assert "你是第二版人格。" in model2.inputs[3][0]["content"]
            default = await service.create_run(ws, doc, body(message="默认人格"), ROUTE)
            assert default["run"]["persona"]["id"] == "canvas-assistant"
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_history_drops_citations_and_marks_answers_about_an_older_canvas_version(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            first = await service.create_run(ws, doc, body(), ROUTE)
            ident = uuid.UUID(first["run"]["id"])
            await run_to_end(service, ws, ident, ScriptedModel(*happy("对象总数为 1[E1.f2]")), tmp_path)
            same = await service.create_run(ws, doc, body(message="同版本追问"), ROUTE)
            async with factory() as db:
                run = await db.get(AgentRun, uuid.UUID(same["run"]["id"]))
                assert run.history[1]["content"] == "对象总数为 1" and run.counters["memorySources"] == [str(ident)]
            await service.stop_run(ws, uuid.UUID(same["run"]["id"]))
            async with factory.begin() as db:
                (await db.get(Document, doc)).revision = 2
            later = await service.create_run(ws, doc, body(message="改动后追问", baseRevision=2), ROUTE)
            async with factory() as db:
                run = await db.get(AgentRun, uuid.UUID(later["run"]["id"]))
                assert run.history[1]["content"].startswith("（基于画布版本 1 的旧回答，当前版本为 2，需重新核实）")
                assert "[E" not in run.history[1]["content"]
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_a_tight_budget_trims_history_but_a_required_overflow_never_calls_the_model(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            for index in range(3):
                created = await service.create_run(ws, doc, body(message=f"问题{index}" + "问" * 400), ROUTE)
                await run_to_end(service, ws, uuid.UUID(created["run"]["id"]), ScriptedModel(*happy("答" * 400)), tmp_path)
            probe = await service.create_run(ws, doc, body(message="现在的问题"), ROUTE)
            async with factory() as db:
                built = await HarnessLoop().context(db, await db.get(AgentRun, uuid.UUID(probe["run"]["id"])), ROUTE,
                                                    service.limits())
            await service.stop_run(ws, uuid.UUID(probe["run"]["id"]))
            layers = {item["name"]: item["bytes"] for item in built.manifest["layers"]}
            reserve = ROUTE.context_max_bytes - built.manifest["budgetBytes"]
            required = built.manifest["totalBytes"] - layers["episodic"]
            tight = ROUTE.model_copy(update={"context_max_bytes": reserve + required + 300})
            created = await service.create_run(ws, doc, body(message="现在的问题"), tight)
            ident = uuid.UUID(created["run"]["id"])
            await service.advance(ident, "w", ScriptedModel(plan_turn()), media_root=tmp_path)
            async with factory() as db:
                call = await db.scalar(select(AgentModelCall).where(AgentModelCall.run_id == ident))
                assert any(t["layer"] == "episodic" for t in call.manifest["trimmed"])
                assert call.manifest["totalBytes"] <= call.manifest["budgetBytes"]
                assert len(call.messages) < len(built.messages)
            await service.stop_run(ws, ident)
            hopeless = ROUTE.model_copy(update={"context_max_bytes": 4096})
            created = await service.create_run(ws, doc, body(message="长" * 800), hopeless)
            model = ScriptedModel()
            result = await run_to_end(service, ws, uuid.UUID(created["run"]["id"]), model, tmp_path)
            assert result["status"] == "limit_reached" and result["errorCode"] == "CONTEXT_LIMIT" and model.calls == 0
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_blocker_replans_with_new_steps_and_the_lesson_reaches_the_next_plan(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(
                plan_turn(plan_step("s1", ("inspect_image",))),
                answer(calls=[invocation("report_blocker", {"code": "tool_error", "detail": "素材不可读"}, "b")]),
                plan_turn(plan_step("n1", ("read_canvas",)), ident="plan2"),
                answer(calls=[invocation()]), complete_turn("画布共有 1 个对象", ["E1.f2"]),
                answer(calls=[finish_call("对象总数为 1", [claim("对象总数为 1", ["E1.f2"], "complete")])]),
                reflect_turn())
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed", result
            by_version = {}
            for step in result["steps"]:
                by_version.setdefault(step["planVersion"], []).append((step["key"], step["status"]))
            assert by_version == {1: [("s1", "obsolete")], 2: [("n1", "completed")]}
            assert result["plan"]["version"] == 2 and [r["source"] for r in result["reflections"]] == ["blocker", "model"]
            replan_context = [m["content"] for m in model.inputs[2] if m["role"] == "user"]
            assert any("素材不可读" in text for text in replan_context)       # the blocked step is in the working state
            assert result["claims"]["reflection"]["verdict"] == "accept"
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_ask_user_leaves_a_waiting_run_that_the_next_message_can_answer(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(answer(calls=[invocation("ask_user", {"question": "你指的是哪一张图？"}, "q")]))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "waiting_input" and result["stage"] == "waiting"
            assert result["waiting"] == {"question": "你指的是哪一张图？", "stage": "plan"} and result["reply"] is None
            follow = await service.create_run(ws, doc, body(message="第一张"), ROUTE)
            async with factory() as db:
                run = await db.get(AgentRun, uuid.UUID(follow["run"]["id"]))
                assert run.history[0]["content"] == "查看画布" and "你指的是哪一张图？" in run.history[1]["content"]
                assert run.history[1]["content"].startswith("（上一轮我向你提出了一个问题")
            plan_input = ScriptedModel(plan_turn())
            await service.advance(uuid.UUID(follow["run"]["id"]), "w", plan_input, media_root=tmp_path)
            assert any("你指的是哪一张图？" in m["content"] for m in plan_input.inputs[0] if m["role"] == "assistant")
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_a_review_can_send_the_run_back_and_lessons_outlive_the_run(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            issue = {"kind": "off_target", "problem": "回答没有回应用户的问题", "stepKey": "s1", "evidenceRefs": []}
            model = ScriptedModel(*happy()[:-1], reflect_turn("retry_step", [issue]),
                answer(calls=[invocation(ident="again")]), complete_turn("再次确认画布共有 1 个对象", ["E2"], ident="done2"),
                answer(calls=[finish_call("对象总数为 1", [claim("对象总数为 1", ["E1.f2"], "complete")], "f2")]),
                reflect_turn(ident="reflect2"))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed", result
            step = result["steps"][0]
            assert step["status"] == "completed" and step["retries"] == 1
            assert [r["verdict"] for r in result["reflections"]] == ["retry_step", "accept"]
            async with factory() as db:
                lessons = list(await db.scalars(select(AgentReflection).where(AgentReflection.run_id == ident,
                                                                              AgentReflection.verdict != "accept")))
                assert len(lessons) == 1 and lessons[0].issues[0]["problem"] == "回答没有回应用户的问题"
            nxt = await service.create_run(ws, doc, body(message="再来一次"), ROUTE)
            probe = ScriptedModel(plan_turn())
            await service.advance(uuid.UUID(nxt["run"]["id"]), "w", probe, media_root=tmp_path)
            assert any("[复盘记忆]" in m["content"] and "回答没有回应用户的问题" in m["content"]
                       for m in probe.inputs[0] if m["role"] == "user")
        finally:
            await engine.dispose()
    scenario_runner(scenario)
