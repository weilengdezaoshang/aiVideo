"""Planner model-call reservation against an explicit migrated supervisor database."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy import select

from backend.agent.config import AgentRoute
from backend.agent.providers import _parse_response
from backend.core.orm import CoreEvent
from backend.core.supervisor.orm import Invocation, PlannerModelCall
from backend.core.supervisor.planners import LLMPlanner, PlannerUnknown
from tests.integration.test_supervisor_pipeline import fixture

DB = os.environ.get("AIVERO_SUPERVISOR_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated supervisor DB required")


def _route():
    return AgentRoute(id="deepseek", label="deepseek", provider="deepseek", model="deployment-model",
                      baseUrl="https://model.example/v1", credentialRef="agent")


def _raw():
    return {"id": "resp", "model": "deployment-model", "choices": [{"index": 0, "message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function", "function": {
            "name": "answer_only", "arguments": json.dumps({"answer": "好"})}}]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


class _Provider:
    def __init__(self, gate=None):
        self.calls = 0
        self.gate = gate
        self.messages = None

    async def complete(self, route, messages, tools, response_sink=None):
        self.calls += 1
        self.messages = messages
        if self.gate is not None:
            await self.gate.wait()
        if response_sink is not None:
            await response_sink(_raw())
        return _parse_response(_raw(), None)


def _context(invocation_id):
    from tests.backend.test_planner_cascade import _tool_context
    context = _tool_context("你好", review_reasons=["goal_changed"])
    context["invocation_key"] = str(invocation_id)
    return context


async def _invocation(factory, run_id):
    async with factory.begin() as tx:
        event = await tx.scalar(select(CoreEvent).where(CoreEvent.run_id == run_id))
        assert event is not None
        used = list(await tx.scalars(select(Invocation.attempt).where(Invocation.event_id == event.id)))
        ident = uuid.uuid4()
        tx.add(Invocation(id=ident, run_id=run_id, event_id=event.id, attempt=(max(used) + 1 if used else 3),
            status="pending", context={}))
        return ident


def test_concurrent_reservation_sends_once_and_recovery_does_not_send(tmp_path):
    async def run():
        async with fixture(tmp_path) as (factory, _supervisor, _drive, run_id, *_rest):
            ident = await _invocation(factory, run_id)
            gate = asyncio.Event()
            provider = _Provider(gate)
            planner = LLMPlanner(provider, _route(), factory)
            context = _context(ident)

            async def first():
                return await planner.plan(context)

            pending = asyncio.create_task(first())
            async def wait_sent():
                for _ in range(200):
                    if provider.calls:
                        return
                    await asyncio.sleep(0.05)
                raise AssertionError("model call was not sent")
            await asyncio.wait_for(wait_sent(), timeout=20)
            with pytest.raises(PlannerUnknown):
                await asyncio.wait_for(planner.plan(dict(context)), timeout=20)
            assert provider.calls == 1
            gate.set()
            result = await asyncio.wait_for(pending, timeout=20)
            assert result.plan.answer == "好"
            async with factory() as tx:
                row = await tx.scalar(select(PlannerModelCall).where(PlannerModelCall.invocation_id == ident))
            assert row.status == "stored"
            assert row.route == "deepseek"
            assert row.template_hash
            assert row.finished_at is not None
            assert row.redacted_request["messages"][1]["content"] == "你好"
            assert any("goal_changed" in item["content"] for item in row.redacted_request["messages"])
            assert row.request_hash
            provider.calls = 0
            again = await planner.plan(dict(context))
            assert provider.calls == 0
            assert again.plan.answer == "好"
            async with factory.begin() as tx:
                stored = await tx.scalar(select(PlannerModelCall).where(PlannerModelCall.invocation_id == ident))
                stored.request_hash = "0" * 64
            with pytest.raises(PlannerUnknown):
                await planner.plan(dict(context))
            assert provider.calls == 0
            unknown_id = await _invocation(factory, run_id)
            async with factory.begin() as tx:
                tx.add(PlannerModelCall(invocation_id=unknown_id, status="unknown", request_hash=row.request_hash,
                    template_version="supervisor-v1", template_hash=row.template_hash, model="deployment-model",
                    route="deepseek", tool_catalog_hash=row.tool_catalog_hash, usage={"unknown": True}))
            with pytest.raises(PlannerUnknown):
                await LLMPlanner(provider, _route(), factory).plan(_context(unknown_id))
            assert provider.calls == 0

    asyncio.run(run())
