"""Governance on isolated PostgreSQL: duplicates, circuit breaking, verified answers, format repair."""
import asyncio
import json
import uuid

from sqlalchemy import select

from backend.agent import tools
from backend.agent.legacy_loop import REPAIR_NOTICE
from backend.agent.orm import AgentRun, AgentToolCall
from test_canvas_agent_runtime import (DB, ROUTE, ScriptedModel, answer, body, claim, finish_call,  # noqa: F401
                                       fixture, invocation, run_to_end)


class CountingTools:
    """Delegates to the real gateway and records every execution."""
    executed: list

    def __init__(self):
        self.executed = []
        self.tool_schemas = staticmethod(tools.tool_schemas)

    def execute_envelope(self, name, *args, **kwargs):
        self.executed.append(name)
        return tools.execute_envelope(name, *args, **kwargs)


async def drive(service, ws, ident, model, tmp_path, counting):
    for _ in range(40):
        value = await service.get_run(ws, ident)
        if value["status"] not in {"queued", "running"}:
            return value
        await service.advance(ident, uuid.uuid4().hex, model, counting, tmp_path)
    raise AssertionError("Agent did not finish")


def scenario_runner(coroutine):
    asyncio.run(coroutine())


def test_repeated_calls_are_resolved_as_duplicates_without_executing(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        counting = CountingTools()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(
                answer(calls=[invocation(ident="c1"), invocation(ident="c2")]),
                answer(calls=[invocation(ident="c3")]),
                answer(calls=[finish_call("对象总数为 1", [claim("对象总数为 1", ["E1.f2"], "complete")])]))
            result = await drive(service, ws, ident, model, tmp_path, counting)
            assert result["status"] == "completed", result
            assert counting.executed == ["read_canvas"]
            calls = result["toolCalls"]
            assert [c["dedupeOf"] for c in calls] == [None, "E1", "E1", None]
            assert [c["status"] for c in calls] == ["completed"] * 4
            assert calls[1]["output"]["status"] == "duplicate" and calls[1]["output"]["dedupeOf"] == "E1"
            assert calls[2]["evidenceId"] == "E3"
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_a_tool_that_keeps_failing_is_refused_and_then_ends_the_run(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        counting = CountingTools()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(*(answer(calls=[invocation("inspect_image", {"assetId": name}, name)])
                                    for name in "abcd"))
            result = await drive(service, ws, ident, model, tmp_path, counting)
            assert result["status"] == "failed" and result["errorCode"] == "REPEATED_FAILURE"
            assert model.calls == 4 and counting.executed == ["inspect_image", "inspect_image"]
            codes = [c["output"]["error"]["code"] for c in result["toolCalls"]]
            assert codes == ["ASSET_OUT_OF_SCOPE", "ASSET_OUT_OF_SCOPE", "CIRCUIT_OPEN", "REPEATED_FAILURE"]
            assert all(c["status"] == "failed" for c in result["toolCalls"])
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def test_an_answer_with_a_wrong_number_is_rejected_then_accepted_when_fixed(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            ident = uuid.UUID((await service.create_run(ws, doc, body(), ROUTE))["run"]["id"])
            model = ScriptedModel(
                answer(calls=[invocation()]),
                answer(calls=[finish_call("共 5 个", [claim("对象总数为 5", ["E1.f2"], "complete")])]),
                answer(calls=[finish_call("共 1 个[E1.f2]", [claim("对象总数为 1", ["E1.f2"], "complete")], "f2")]))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed" and result["reply"] == "共 1 个[E1.f2]"
            first, second = result["toolCalls"][1], result["toolCalls"][2]
            assert first["status"] == "failed" and first["output"]["error"]["code"] == "CLAIMS_INVALID"
            assert "H4" in first["output"]["error"]["message"]
            assert second["status"] == "completed" and second["output"]["status"] == "accepted"
            assert result["claims"]["verification"]["passed"] is True
            rejected_input = model.inputs[2][-1]
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
            model = ScriptedModel(answer(calls=[invocation()]), *(answer(calls=[w]) for w in wrong))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "limit_reached" and result["errorCode"] == "ANSWER_UNVERIFIED"
            assert result["claims"]["verification"]["passed"] is False and result["reply"] == "共 5 个"
            assert [c["status"] for c in result["toolCalls"]] == ["completed", "failed", "failed", "failed"]
            # Later Runs do not inherit an unverified answer as history.
            async with factory() as db:
                assert (await db.get(AgentRun, ident)).status == "limit_reached"
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
            model = ScriptedModel(answer("直接回答", finish="stop"), answer("第二次直接回答", finish="stop"),
                                  answer(calls=[finish_call("你好")]))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed" and model.calls == 3
            roles = [m["role"] for m in model.inputs[2]]
            assert roles[-4:] == ["assistant", "user", "assistant", "user"]
            assert model.inputs[2][-1]["content"] == REPAIR_NOTICE and model.inputs[2][-4]["content"] == "直接回答"
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
            model = ScriptedModel(answer(calls=[invocation()]), answer(calls=[finish_call("好")]))
            await service.advance(ident, "w", model, media_root=tmp_path)
            await service.advance(ident, "w", model, media_root=tmp_path)
            async with factory.begin() as db:
                row = await db.scalar(select(AgentToolCall).where(AgentToolCall.run_id == ident))
                row.output, row.kind, row.dedupe_of, row.tool_version = {"objectCount": 1}, "read", None, None
                run = await db.get(AgentRun, ident)
                run.claims, run.counters = None, {}
            projected = await service.get_run(ws, ident)
            assert projected["claims"] is None and projected["toolCalls"][0]["output"] == {"objectCount": 1}
            async with factory() as db:
                from backend.agent.legacy_loop import LegacyLoop
                messages = (await LegacyLoop().context(db, await db.get(AgentRun, ident), ROUTE)).messages
            assert [m["role"] for m in messages][-2:] == ["assistant", "tool"]
            assert '"objectCount":1' in messages[-1]["content"]
        finally:
            await engine.dispose()
    scenario_runner(scenario)


def persona(identity, version=1, name="小帧"):
    from backend.agent.contracts import PersonaProfile
    return PersonaProfile(id="warm", version=version, displayName=name, identity=identity,
                          voice=["语气温和"], principles=["没有证据不下结论"], expression={})


def test_persona_is_frozen_on_the_run_and_every_model_call_records_a_manifest(tmp_path):
    async def scenario():
        engine, factory, service, ws, doc, _ = await fixture()
        try:
            first = await service.create_run(ws, doc, body(), ROUTE, persona=persona("你是第一版人格。", 1))
            ident = uuid.UUID(first["run"]["id"])
            assert first["run"]["persona"] == {"id": "warm", "version": 1, "displayName": "小帧"}
            # Changing the deployment persona later cannot reach a Run that already exists.
            model = ScriptedModel(answer(calls=[invocation()]), answer(calls=[finish_call(
                "对象总数为 1", [claim("对象总数为 1", ["E1.f2"], "complete")])]))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed"
            system = model.inputs[0][0]["content"]
            assert system.index("[规则 v1]") < system.index("[人格 warm@v1]") < system.index("[角色 analyst@v1]")
            assert "你是第一版人格。" in system and "第二版" not in system
            async with factory() as db:
                from backend.agent.orm import AgentModelCall
                calls = list(await db.scalars(select(AgentModelCall).where(AgentModelCall.run_id == ident)
                                              .order_by(AgentModelCall.ordinal)))
                assert [c.manifest["personaId"] for c in calls] == ["warm", "warm"]
                assert calls[0].manifest["evidenceIds"] == [] and calls[1].manifest["evidenceIds"] == ["E1"]
                assert calls[1].manifest["totalBytes"] == len(json.dumps(calls[1].messages, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode())
                assert (await db.get(AgentRun, ident)).persona["version"] == 1
            second = await service.create_run(ws, doc, body(message="再问一次"), ROUTE, persona=persona("你是第二版人格。", 2))
            ident2 = uuid.UUID(second["run"]["id"])
            model2 = ScriptedModel(answer("好"))
            await run_to_end(service, ws, ident2, model2, tmp_path)
            assert "你是第二版人格。" in model2.inputs[0][0]["content"]
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
            model = ScriptedModel(answer(calls=[invocation()]), answer(calls=[finish_call(
                "对象总数为 1[E1.f2]", [claim("对象总数为 1", ["E1.f2"], "complete")])]))
            await run_to_end(service, ws, ident, model, tmp_path)
            same = await service.create_run(ws, doc, body(message="同版本追问"), ROUTE)
            async with factory() as db:
                run = await db.get(AgentRun, uuid.UUID(same["run"]["id"]))
                assert run.history[1]["content"] == "对象总数为 1" and run.counters["memorySources"] == [str(ident)]
            await service.stop_run(ws, uuid.UUID(same["run"]["id"]))
            async with factory.begin() as db:
                from backend.infrastructure.orm import Document
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
                await run_to_end(service, ws, uuid.UUID(created["run"]["id"]), ScriptedModel(answer("答" * 400)), tmp_path)
            from backend.agent.legacy_loop import tool_reserve_bytes
            tight = ROUTE.model_copy(update={"context_max_bytes": tool_reserve_bytes() + 4300})
            created = await service.create_run(ws, doc, body(message="现在的问题"), tight)
            ident = uuid.UUID(created["run"]["id"])
            model = ScriptedModel(answer("好"))
            result = await run_to_end(service, ws, ident, model, tmp_path)
            assert result["status"] == "completed" and model.calls == 1
            async with factory() as db:
                from backend.agent.orm import AgentModelCall
                call = await db.scalar(select(AgentModelCall).where(AgentModelCall.run_id == ident))
                assert any(t["layer"] == "episodic" for t in call.manifest["trimmed"])
                assert call.manifest["totalBytes"] <= call.manifest["budgetBytes"]
                assert len(call.messages) < 2 + 2 * 3 + 1
            hopeless = ROUTE.model_copy(update={"context_max_bytes": 4096})
            created = await service.create_run(ws, doc, body(message="长" * 800), hopeless)
            model = ScriptedModel()
            result = await run_to_end(service, ws, uuid.UUID(created["run"]["id"]), model, tmp_path)
            assert result["status"] == "limit_reached" and result["errorCode"] == "CONTEXT_LIMIT" and model.calls == 0
        finally:
            await engine.dispose()
    scenario_runner(scenario)
