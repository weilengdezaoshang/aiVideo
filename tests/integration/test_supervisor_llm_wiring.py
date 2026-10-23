"""Isolated PostgreSQL coverage for the llm-v1 supervisor entry. No paid model calls."""
import asyncio
import json
import os

import pytest
from sqlalchemy import func, select

from backend.agent.config import AgentRoute
from backend.agent.orm import HostCreditLedger
from backend.core.orm import CorePlan, CoreRun, CoreSession, CoreSessionMessage, CoreTask
from backend.core.supervisor.orm import Invocation
from backend.core.supervisor.planners import CascadePlanner, RulePlanner
from backend.core.supervisor.service import answer, list_feedback, record_feedback
from backend.core.supervisor.wiring import build_supervisor
from tests.integration.test_supervisor_pipeline import fixture

DB = os.environ.get("AIVERO_SUPERVISOR_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated supervisor DB required")

ROUTE = AgentRoute(id="planner", label="Planner", provider="deepseek", model="deepseek-chat",
    baseUrl="https://model.example/v1", credentialRef="agent", timeoutSeconds=45)


def _result(plan, invariants=None):
    return {"plan": plan, "invariants": invariants or {}, "confidence": 1, "tokens": 1,
            "raw_output": {"writer": plan.get("writer", "llm")}}


class Counting:
    def __init__(self, builder):
        self.builder = builder
        self.calls = []

    async def plan(self, context):
        self.calls.append(context)
        return self.builder(context, len(self.calls))


def test_api_and_worker_select_cascade_and_old_lock_stays_on_rules(tmp_path, monkeypatch):
    async def test():
        monkeypatch.setenv("AIVERO_PRODUCTION_AGENT_ROUTES_FILE", str(tmp_path / "routes.json"))
        monkeypatch.setenv("AIVERO_CREDENTIALS_FILE", str(tmp_path / "credentials.json"))
        (tmp_path / "routes.json").write_text(json.dumps({"routes": [ROUTE.snapshot()]}), encoding="utf-8")
        (tmp_path / "credentials.json").write_text(json.dumps({"agent": {"apiKey": "not-used"}}), encoding="utf-8")
        async with fixture(tmp_path, planner="chain", message="改图后生成视频") as (factory, _, drive, run_id, workspace, *_):
            assert await drive() == "completed"
            from fastapi import FastAPI
            import httpx
            from backend.core.supervisor.api import install
            from backend.services.identity import Identity, authenticated
            app = FastAPI()
            app.state.factory = factory
            app.state.agent_routes = [ROUTE]
            install(app)
            app.dependency_overrides[authenticated] = lambda: Identity("owner", workspace, "owner")
            async with factory() as tx:
                run = await tx.get(CoreRun, run_id)
                session = await tx.get(CoreSession, run.session_id)
                asset = run.snapshot["request"]["selected_assets"][0]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                created = await client.post("/api/core/runs", json={
                    "document_id": str(session.canvas_id), "request_key": "api-run", "message": "抠图",
                    "selected_assets": [asset], "anchor_id": "source", "language": "en",
                    "viewport": {"x": 1, "y": 2, "width": 30, "height": 40}})
                assert created.status_code == 201, created.text
                body = created.json()
            async with factory() as tx:
                created_run = await tx.get(CoreRun, body["id"])
                assert created_run.version_lock["supervisor"] == "llm-v1"
                assert created_run.version_lock["agent_route"]["id"] == "planner"
                assert created_run.version_lock["prompt_hash"]
                assert created_run.snapshot["viewport"] == {"x": 1, "y": 2, "width": 30, "height": 40}
                assert created_run.snapshot["request"]["language"] == "en"
                assert "not-used" not in json.dumps(created_run.version_lock)
                assert created_run.snapshot["recent_assets"]
                assert all(item["role"] == "generated" for item in created_run.snapshot["recent_assets"])
                assert asset not in {item["asset_id"] for item in created_run.snapshot["recent_assets"]}
                worker = build_supervisor(factory)
                assert isinstance(worker.planner_for(created_run), CascadePlanner)
                assert worker.planner_for(created_run).llm is not None
                from types import SimpleNamespace
                legacy = SimpleNamespace(version_lock={**created_run.version_lock, "supervisor": "rule-v1"})
                assert isinstance(worker.planner_for(legacy), RulePlanner)
    asyncio.run(test())


def test_three_rejections_then_one_form(tmp_path):
    planner = Counting(lambda context, _: _result({
        "goal": context["request"]["message"], "writer": "llm",
        "steps": [{"id": "t1", "tool": "missing"}]}))

    async def test():
        async with fixture(tmp_path, planner=planner, message="画一张猫") as (factory, _, drive, run_id, *_):
            assert await drive() == "paused"
            assert len(planner.calls) == 3
            async with factory() as tx:
                task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run_id, CoreTask.kind == "ask_user"))
                run = await tx.get(CoreRun, run_id)
                assert task.status == "waiting_user"
                assert task.parameters["form"]["fields"][0]["id"] == "detail"
                contexts = list(await tx.scalars(select(Invocation.context).where(Invocation.run_id == run_id)))
                assert any(item.get("clarification_submitted") for item in contexts)
                assert run.invariants.get("deliverable", {}).get("count") != 99
    asyncio.run(test())


def test_form_keeps_the_run_and_second_plan_succeeds(tmp_path):
    def build(context, attempt):
        message = context["request"]["message"]
        if attempt == 1:
            return _result({"goal": message, "writer": "llm", "result_type": "clarify", "steps": [
                {"id": "t1", "kind": "ask_user", "parameters": {"form": {"id": "need-duration", "fields": [
                    {"id": "duration_seconds", "type": "number", "label": "时长", "required": True},
                    {"id": "note", "type": "text", "label": "备注", "default": "ok"}]}}}]})
        fields = [item.get("content", {}).get("fields", {}) for item in context["messages"] if isinstance(item, dict)]
        assert {"duration_seconds": 5, "note": "ok"} in fields
        assert context["request"]["message"] == message
        asset = context["request"]["selected_assets"][0]
        return _result({"goal": message, "writer": "llm", "steps": [{"id": "t1", "tool": "edit", "parameters": {
            "prompt": "edit", "image": {"asset_id": asset, "media_type": "image"}}}]})

    planner = Counting(build)

    async def test():
        async with fixture(tmp_path, planner=planner, message="生成一张图") as (factory, _, drive, run_id, workspace, *_):
            assert await drive() == "paused"
            async with factory.begin() as tx:
                task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run_id, CoreTask.kind == "ask_user"))
                await answer(tx, task_id=task.id, option_id="", request_key="form", workspace_id=workspace,
                    user_id="owner", fields={"duration_seconds": 5})
                await answer(tx, task_id=task.id, option_id="", request_key="form", workspace_id=workspace,
                    user_id="owner", fields={"duration_seconds": 5})
                with pytest.raises(ValueError):
                    await answer(tx, task_id=task.id, option_id="", request_key="form", workspace_id=workspace,
                        user_id="owner", fields={"duration_seconds": 9})
            assert await drive() == "completed"
            async with factory() as tx:
                plans = list(await tx.scalars(select(CorePlan).where(CorePlan.run_id == run_id).order_by(CorePlan.version)))
                run = await tx.get(CoreRun, run_id)
                assert [plan.version for plan in plans] == [1, 2]
                assert plans[0].id != plans[1].id
                assert run.snapshot["request"]["message"] == "生成一张图"
                answered = await tx.scalar(select(CoreSessionMessage).where(CoreSessionMessage.run_id == run_id,
                    CoreSessionMessage.role == "user", CoreSessionMessage.content["fields"].is_not(None)))
                assert answered.content["fields"]["duration_seconds"] == 5
                assert answered.content["fields"]["note"] == "ok"
                task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run_id, CoreTask.tool_ref == "edit"))
                assert task.status == "succeeded"
    asyncio.run(test())


def test_answer_only_finishes_with_no_charge(tmp_path):
    planner = Counting(lambda context, _: _result({
        "goal": context["request"]["message"], "writer": "llm", "result_type": "answer_only",
        "answer": "一张图是静态画面。", "steps": []}))

    async def test():
        async with fixture(tmp_path, planner=planner, message="什么是图片") as (factory, _, drive, run_id, workspace, *_):
            assert await drive() == "completed"
            async with factory() as tx:
                assert await tx.scalar(select(func.count()).select_from(CoreTask).where(CoreTask.run_id == run_id)) == 0
                assert await tx.scalar(select(func.coalesce(func.sum(HostCreditLedger.charged), 0)).where(
                    HostCreditLedger.core_run_id == run_id)) == 0
                plan = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run_id))
                assert plan.writer == "llm" and plan.result_type == "answer_only"
                with pytest.raises(LookupError):
                    await record_feedback(tx, run_id=run_id, workspace_id=workspace, user_id="other",
                        request_key="fb", text="不对", correction=True)
                row = await record_feedback(tx, run_id=run_id, workspace_id=workspace, user_id="owner",
                    request_key="fb", text="时长应该是5秒", correction=True, related={"plan_id": str(plan.id)})
                again = await record_feedback(tx, run_id=run_id, workspace_id=workspace, user_id="owner",
                    request_key="fb", text="时长应该是5秒", correction=True, related={"plan_id": str(plan.id)})
                assert again.id == row.id
                listed = await list_feedback(tx, run_id=run_id, workspace_id=workspace, user_id="owner")
                assert listed[0]["correction"] is True and listed[0]["related"]["plan_id"] == str(plan.id)
                assert not any(item.content.get("feedback") for item in await tx.scalars(select(CoreSessionMessage).where(
                    CoreSessionMessage.run_id == run_id, CoreSessionMessage.request_key == "start")))
    asyncio.run(test())


def test_unproven_constraint_is_rejected_and_not_stored(tmp_path):
    def build(context, _attempt):
        return _result({"goal": context["request"]["message"], "writer": "llm", "steps": [
            {"id": "t1", "tool": "edit", "parameters": {"prompt": "edit", "image": {
                "asset_id": context["request"]["selected_assets"][0], "media_type": "image"}}}]},
            {"deliverable": {"count": 99}})

    planner = Counting(build)

    async def test():
        async with fixture(tmp_path, planner=planner, message="画一张猫") as (factory, _, drive, run_id, *_):
            assert await drive() == "paused"
            async with factory() as tx:
                run = await tx.get(CoreRun, run_id)
                assert run.invariants.get("deliverable", {}).get("count") != 99
                assert "99" not in run.snapshot["request"]["message"]
                assert len(planner.calls) == 3
    asyncio.run(test())


def test_language_replies_use_ledger_and_placement(tmp_path):
    def build(context, _attempt):
        asset = context["request"]["selected_assets"][0]
        return _result({"goal": context["request"]["message"], "writer": "llm", "steps": [
            {"id": "t1", "tool": "edit", "parameters": {"prompt": "edit", "image": {"asset_id": asset, "media_type": "image"}}}]})

    async def test():
        async with fixture(tmp_path, planner=Counting(build), message="改这张图", mode="auto") as (factory, _, drive, run_id, *_):
            assert await drive() == "completed"
            async with factory() as tx:
                texts = [row.content["text"] for row in await tx.scalars(select(CoreSessionMessage).where(
                    CoreSessionMessage.run_id == run_id, CoreSessionMessage.role == "assistant"))]
                assert any("预估花费 10 积分" in text and "价目" not in text for text in texts)
                assert any("放置位置" in text and "x=" in text for text in texts)
        async with fixture(tmp_path / "en", planner=Counting(build), message="edit this") as (factory, _, drive, run_id, workspace, *_):
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run_id)
                session = await tx.get(CoreSession, run.session_id)
                session.language = "en"
            assert await drive(fail_video=False) == "completed"
            async with factory() as tx:
                texts = [row.content["text"] for row in await tx.scalars(select(CoreSessionMessage).where(
                    CoreSessionMessage.run_id == run_id, CoreSessionMessage.role == "assistant"))]
                assert any("Estimated cost 10 credits" in text for text in texts)
                assert any(text.startswith("Done. Placement:") for text in texts)
        async with fixture(tmp_path / "fail", planner=Counting(lambda context, _: _result({
                "goal": context["request"]["message"], "writer": "llm", "steps": [
                    {"id": "t1", "tool": "video", "parameters": {"prompt": "video", "image": {
                        "asset_id": context["request"]["selected_assets"][0], "media_type": "image"}}}]})),
                message="做视频") as (factory, _, drive, run_id, *_):
            assert await drive(fail_video=True) == "failed"
            async with factory() as tx:
                refunded = int(await tx.scalar(select(func.coalesce(func.sum(HostCreditLedger.refunded), 0)).where(
                    HostCreditLedger.core_run_id == run_id)) or 0)
                texts = [row.content["text"] for row in await tx.scalars(select(CoreSessionMessage).where(
                    CoreSessionMessage.run_id == run_id, CoreSessionMessage.role == "assistant"))]
                assert any(f"已退回 {refunded} 积分" in text for text in texts)
                assert all("价目" not in text for text in texts)
    asyncio.run(test())
