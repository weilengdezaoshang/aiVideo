"""LLMPlanner + fake provider + Supervisor failure protocol on isolated PostgreSQL."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.agent.config import AgentRoute
from backend.agent.orm import HostCreditLedger
from backend.agent.providers import ModelCallError, _parse_response
from backend.core.events.store import emit
from backend.core.orm import CoreEvent, CorePlan, CoreRun, CoreTask
from backend.core.supervisor.orm import Invocation
from backend.core.supervisor.contracts import Settings
from backend.core.supervisor.planners import CascadePlanner, LLMPlanner
from backend.core.supervisor.service import answer, progress
from tests.integration.test_supervisor_pipeline import fixture

DB = os.environ.get("AIVERO_SUPERVISOR_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated supervisor DB required")

ROUTE = AgentRoute(id="planner", label="Planner", provider="deepseek", model="deployment-model",
                   baseUrl="https://model.example/v1", credentialRef="agent", timeoutSeconds=45)
ROOT = Path(__file__).resolve().parents[2]


def _raw(calls, usage=None):
    return {"id": "resp", "model": "deployment-model", "choices": [{"index": 0, "message": {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": item["id"], "type": "function", "function": {
                "name": item["name"], "arguments": json.dumps(item["arguments"], ensure_ascii=False)}}
            for item in calls]},
        "finish_reason": "tool_calls"}], "usage": usage or {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}}


class ScriptProvider:
    def __init__(self, builder):
        self.builder = builder
        self.calls = 0
        self.messages = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = False

    async def complete(self, route, messages, tools, response_sink=None):
        self.calls += 1
        self.messages.append(messages)
        self.started.set()
        if self.hold:
            await self.release.wait()
        raw = self.builder(self.calls, messages, tools)
        if isinstance(raw, Exception):
            raise raw
        if response_sink is not None:
            await response_sink(raw)
        return _parse_response(raw, None)


def _make(provider):
    settings = Settings(planner_tool_threshold=200)

    def build(factory):
        return CascadePlanner(LLMPlanner(provider, ROUTE, factory=factory, settings=settings), settings)
    return build


def _edit(asset):
    return {"id": "g", "name": "edit", "arguments": {
        "prompt": "edit", "image": {"asset_id": asset, "media_type": "image"}}}


def test_bad_tool_reason_then_good_call(tmp_path):
    asset_box = {}

    def build(n, messages, _tools):
        blob = json.dumps(messages, ensure_ascii=False)
        if n == 1:
            return _raw([{"id": "b", "name": "missing_tool", "arguments": {"prompt": "x"}}])
        assert "unknown_tool" in blob
        asset = asset_box["id"]
        return _raw([
            {"id": "e", "name": "edit", "arguments": {"prompt": "edit", "image": {"asset_id": asset, "media_type": "image"}}},
            {"id": "v", "name": "video", "arguments": {"prompt": "video", "image": "$t1.output"}},
        ], usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 7})

    provider = ScriptProvider(build)

    async def test():
        async with fixture(tmp_path, message="换背景再做成视频", make_planner=_make(provider)) as (
                factory, _supervisor, drive, run_id, *_):
            async with factory() as tx:
                asset_box["id"] = (await tx.get(CoreRun, run_id)).snapshot["request"]["selected_assets"][0]
            assert await drive() == "completed"
            assert provider.calls == 2
            async with factory() as tx:
                task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run_id, CoreTask.tool_ref == "edit"))
                assert task.status == "succeeded"
                run = await tx.get(CoreRun, run_id)
                body = await progress(tx, run)
                assert body["known_tokens"] == 4 + 7
                assert body["tokens_used"] == 11
                assert body["unknown_calls"] == 0
    asyncio.run(test())


def test_three_bad_tools_then_form_then_fail(tmp_path):
    provider = ScriptProvider(lambda n, messages, tools: _raw(
        [{"id": "b", "name": "missing_tool", "arguments": {"prompt": "x"}}]))

    async def test():
        async with fixture(tmp_path, message="换背景再做成视频", make_planner=_make(provider)) as (
                factory, _supervisor, drive, run_id, workspace, *_):
            assert await drive() == "paused"
            assert provider.calls == 3
            async with factory.begin() as tx:
                task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run_id, CoreTask.kind == "ask_user"))
                assert task.parameters["form"]["fields"][0]["id"] == "detail"
                reviews = list(await tx.scalars(select(Invocation.review).where(Invocation.run_id == run_id)))
                assert any(item and any(r.get("code") == "unknown_tool" for r in (item.get("reasons") or []))
                           for item in reviews)
                await answer(tx, task_id=task.id, option_id="", request_key="form", workspace_id=workspace,
                             user_id="owner", fields={"detail": "still bad"})
            assert await drive() == "failed"
            async with factory() as tx:
                run = await tx.get(CoreRun, run_id)
                assert run.status == "failed"
                workers = list(await tx.scalars(select(CoreTask).where(
                    CoreTask.run_id == run_id, CoreTask.kind == "call_tool")))
                assert workers == []
    asyncio.run(test())


@pytest.mark.parametrize("status,error,usage", [
    ("unavailable", ModelCallError("UNAVAILABLE", "down", usage={"total_tokens": 6}), {"total_tokens": 6}),
    ("unknown", ModelCallError("TIMEOUT", "lost", unknown=True), {"unknown": True}),
    ("blocked", ModelCallError("CONTENT_BLOCKED", "blocked", usage={"total_tokens": 3}), {"total_tokens": 3}),
])
def test_planner_failure_stops_run(tmp_path, status, error, usage):
    provider = ScriptProvider(lambda n, messages, tools: error)

    async def test():
        async with fixture(tmp_path, message="换背景再做成视频", make_planner=_make(provider)) as (
                factory, _supervisor, drive, run_id, *_):
            assert await drive() == "failed"
            assert provider.calls == 1
            async with factory() as tx:
                run = await tx.get(CoreRun, run_id)
                plan = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run_id))
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
                credits = await tx.scalar(select(func.coalesce(func.sum(HostCreditLedger.charged), 0)).where(
                    HostCreditLedger.core_run_id == run_id))
                body = await progress(tx, run)
                assert plan is not None
                assert plan.result_type == "answer_only"
                assert plan.raw_output["planner_failure"]["status"] == status
                assert tasks == []
                assert int(credits or 0) == 0
                if usage.get("unknown"):
                    assert body["unknown_calls"] == 1
                    assert body["known_tokens"] == 0
                    assert body["tokens_used"] == 0
                else:
                    assert body["unknown_calls"] == 0
                    assert body["known_tokens"] == usage["total_tokens"]
                    assert body["tokens_used"] == usage["total_tokens"]
                reviews = list(await tx.scalars(select(Invocation.review).where(Invocation.run_id == run_id)))
                assert any(item is not None for item in reviews)
    asyncio.run(test())


def test_late_response_after_cancel_meters_once(tmp_path):
    asset_box = {}
    provider = ScriptProvider(lambda n, messages, tools: _raw([_edit(asset_box["id"])],
                                                              usage={"total_tokens": 8}))
    provider.hold = True

    async def test():
        async with fixture(tmp_path, message="换背景再做成视频", make_planner=_make(provider)) as (
                factory, supervisor, drive, run_id, _ws, _root, _config, _fake, events):
            async with factory() as tx:
                asset_box["id"] = (await tx.get(CoreRun, run_id)).snapshot["request"]["selected_assets"][0]
            await events()
            ticking = asyncio.create_task(supervisor.tick())
            await asyncio.wait_for(provider.started.wait(), 5)
            async with factory.begin() as tx:
                cause = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == run_id).limit(1))
                await emit(tx, "user_cancelled", run_id, causation_id=cause)
            provider.release.set()
            await ticking
            assert await drive() in {"cancelled", "failed"}
            assert provider.calls == 1
            async with factory() as tx:
                run = await tx.get(CoreRun, run_id)
                body = await progress(tx, run)
                workers = list(await tx.scalars(select(CoreTask).where(
                    CoreTask.run_id == run_id, CoreTask.kind == "call_tool")))
                accounted = ((run.error or {}).get("planner_usage") or {}).get("accounted_invocations") or []
                assert workers == []
                assert body["known_tokens"] == 8
                assert len(accounted) == 1
    asyncio.run(test())


def test_guessed_video_duration_waits_for_eight_seconds(tmp_path):
    asset_box = {}

    def build(n, messages, _tools):
        blob = json.dumps(messages, ensure_ascii=False)
        asset = asset_box["id"]
        if n == 1:
            return _raw([{"id": "v", "name": "video", "arguments": {
                "prompt": "video", "image": {"asset_id": asset, "media_type": "image"}, "duration_seconds": 5}}])
        assert "duration_seconds=8" in blob or "8" in blob
        return _raw([{"id": "v", "name": "video", "arguments": {
            "prompt": "video", "image": {"asset_id": asset, "media_type": "image"}, "duration_seconds": 8}}])

    provider = ScriptProvider(build)

    async def test():
        async with fixture(tmp_path, message="换背景再做成视频", sized_video=True,
                           make_planner=_make(provider)) as (factory, _supervisor, drive, run_id, workspace, *_):
            async with factory() as tx:
                asset_box["id"] = (await tx.get(CoreRun, run_id)).snapshot["request"]["selected_assets"][0]
            assert await drive() == "paused"
            async with factory.begin() as tx:
                task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run_id, CoreTask.kind == "ask_user"))
                assert task.parameters["form"]["fields"][0]["id"] == "duration_seconds"
                assert task.parameters["form"]["fields"][0].get("default") is None
                await answer(tx, task_id=task.id, option_id="", request_key="dur", workspace_id=workspace,
                             user_id="owner", fields={"duration_seconds": 8})
            assert await drive() == "completed"
            async with factory() as tx:
                video = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run_id, CoreTask.tool_ref == "video"))
                assert video.status == "succeeded"
                assert video.parameters["duration_seconds"] == 8
    asyncio.run(test())


def test_raw_stored_crash_resumes_without_second_network_call(tmp_path):
    env = dict(os.environ)
    env["AIVERO_SUPERVISOR_TEST_DB"] = DB
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + str(ROOT / "apps" / "api") + os.pathsep + env.get("PYTHONPATH", "")
    env["AIVERO_CRASH_AFTER_RAW"] = "1"
    env["AIVERO_CRASH_TMP"] = str(tmp_path)
    child = str(Path(__file__).with_name("_supervisor_crash_child.py"))
    first = subprocess.run([sys.executable, child], env=env, cwd=str(ROOT), capture_output=True, text=True)
    assert first.returncode == 42, first.stdout + first.stderr
    from sqlalchemy import create_engine, text
    engine = create_engine(DB)
    run_key = json.loads((tmp_path / "crash-state.json").read_text())["run"]
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT i.result, m.raw_response IS NOT NULL FROM core_planner_invocations i "
            "JOIN core_planner_model_calls m ON m.invocation_id = i.id "
            "WHERE i.run_id = CAST(:run AS uuid)"), {"run": run_key}).one()
        assert row[0] is None
        assert bool(row[1]) is True
    engine.dispose()
    env.pop("AIVERO_CRASH_AFTER_RAW")
    second = subprocess.run([sys.executable, child], env=env, cwd=str(ROOT), capture_output=True, text=True)
    assert second.returncode == 0, second.stdout + second.stderr
    payload = json.loads((tmp_path / "crash-state.json").read_text())
    assert payload["calls"] == 1
    assert payload["status"] == "completed"
