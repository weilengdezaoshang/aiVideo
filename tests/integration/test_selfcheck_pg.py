"""Isolated PG: real barriers while the checker is in flight. No paid APIs."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from backend.agent.orm import HostCreditLedger
from backend.agent.providers import ModelCallError, ModelResponse
from backend.core.execution.orm import ExecutionAdmission
from backend.core.orm import CorePlan, CoreRun, CoreTask
from backend.core.selfcheck.contracts import SEMANTIC_KEY
from backend.core.selfcheck.provider import ChatSelfCheckProvider, FakeSelfCheckProvider
from backend.core.supervisor.contracts import Settings
from backend.core.supervisor.engine import Supervisor, now
from backend.core.supervisor.orm import Invocation
from backend.core.supervisor.service import start
from tests.backend.test_selfcheck_runtime import control_flow_enforce_gate
from tests.integration.test_supervisor_pipeline import fixture

DB = (os.environ.get("AIVERO_SUPERVISOR_TEST_DB") or os.environ.get("AIVERO_CORE_TEST_DB")
      or os.environ.get("AIVERO_DURABLE_TEST_DB"))
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL required")


def _settings(**over):
    data = dict(selfcheck_enabled=True, selfcheck_mode="shadow", selfcheck_cost_threshold=1,
                selfcheck_timeout_seconds=8, selfcheck_checkpoint_margin_seconds=1, review_retries=2)
    data.update(over)
    return Settings(**data)


class ScriptInner:
    def __init__(self, payload, *, gate=None, fail=None):
        self.payload = payload
        self.gate = gate
        self.fail = fail
        self.calls = 0

    async def complete(self, route, messages, tools, *, response_sink=None):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail == "blocked":
            raise ModelCallError("CONTENT_BLOCKED", "blocked", usage={"total_tokens": 6})
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        raw = {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 8}}
        if response_sink is not None:
            await response_sink(raw)
        return ModelResponse(content=content, tool_calls=[], usage={"total_tokens": 8},
                             finish_reason="stop", raw_response=raw)


async def _counts(tx, run_id):
    tasks = int(await tx.scalar(select(func.count()).select_from(CoreTask).where(CoreTask.run_id == run_id)) or 0)
    admissions = int(await tx.scalar(select(func.count()).select_from(ExecutionAdmission).where(
        ExecutionAdmission.plan_id.in_(select(CorePlan.id).where(CorePlan.run_id == run_id)))) or 0)
    ledger = int(await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(
        HostCreditLedger.core_run_id == run_id)) or 0)
    return tasks, admissions, ledger


async def _pump_until_calls(factory, supervisor, events, provider, run_id, *, ticks=120):
    for _ in range(ticks):
        await events(run_id)
        await supervisor.tick()
        if getattr(provider, "calls", 0) >= 1:
            async with factory() as tx:
                return await _counts(tx, run_id)
        await asyncio.sleep(0.002)
    raise AssertionError("checker was never called")


def test_barrier_zero_work_then_exact_one_call(tmp_path):
    async def test():
        gate = asyncio.Event()
        provider = FakeSelfCheckProvider([{"pass": True, "problems": []}], gate=gate)
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=provider) as (factory, supervisor, drive, run, *rest):
            events = rest[4]
            tasks, admissions, ledger = await _pump_until_calls(factory, supervisor, events, provider, run)
            assert provider.calls == 1
            assert tasks == 0 and admissions == 0 and ledger == 0
            async with factory() as tx:
                plans = list(await tx.scalars(select(CorePlan).where(CorePlan.run_id == run)))
                assert plans and all(p.stage == "reviewing" for p in plans)
                assert (plans[0].self_check or {}).get(SEMANTIC_KEY, {}).get("status") in {"sending", "reserved"}
            gate.set()
            assert await drive() == "completed"
            assert provider.calls == 1
            async with factory() as tx:
                current = await tx.get(CorePlan, (await tx.get(CoreRun, run)).current_plan_id)
                assert current.stage == "execute"
                assert (current.self_check or {}).get(SEMANTIC_KEY, {}).get("status") == "parsed"
                tasks, admissions, ledger = await _counts(tx, run)
                assert tasks == 2
                assert ledger == 2
    asyncio.run(test())


def test_two_runs_progress_while_first_awaits(tmp_path):
    async def test():
        gate = asyncio.Event()
        provider = FakeSelfCheckProvider([{"pass": True, "problems": []}], gate=gate)
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=provider, include_canvas=True) as ctx:
            factory, supervisor, drive, run, workspace_id, root, config, fake, events, asset_id, document_id = ctx
            await _pump_until_calls(factory, supervisor, events, provider, run)
            first_calls = provider.calls
            from backend.infrastructure.orm import Document, AssetReference
            async with factory.begin() as tx:
                doc = Document(workspace_id=workspace_id, data={"objects": {
                    "source": {"id": "source", "kind": "image", "assetId": str(asset_id), "x": 0, "y": 0,
                               "width": 8, "height": 8}}, "order": ["source"]})
                tx.add(doc)
                await tx.flush()
                tx.add(AssetReference(asset_id=asset_id, owner_type="document", owner_id=str(doc.id)))
                second = await start(tx, workspace_id=workspace_id, user_id="owner", document_id=doc.id,
                    request_key="second", request={"message": "改图后生成视频", "selected_assets": [str(asset_id)],
                    "anchor_id": "source"}, budget=100, token_budget=100, max_rounds=20, mode="auto")
                second_id = second.id
            from backend.core.supervisor.planners import FakePlanner
            plan = {"goal": "改图后生成视频", "steps": [
                {"id": "t1", "tool": "edit", "parameters": {"prompt": "edit", "image": {"asset_id": str(asset_id), "media_type": "image"}}},
                {"id": "t2", "tool": "video", "parameters": {"prompt": "video", "image": "$t1.output"}, "depends_on": ["t1"]}]}
            supervisor.planner = FakePlanner([{"plan": plan, "invariants": {}, "confidence": 1, "tokens": 2,
                "raw_output": {"plan": plan}}])
            for _ in range(80):
                await events(second_id)
                await supervisor.tick()
                async with factory() as tx:
                    reviewing = await tx.scalar(select(func.count()).select_from(CorePlan).where(
                        CorePlan.run_id == second_id, CorePlan.stage == "reviewing"))
                if reviewing:
                    break
                await asyncio.sleep(0.002)
            async with factory() as tx:
                t1, a1, l1 = await _counts(tx, run)
                t2, a2, l2 = await _counts(tx, second_id)
            assert t1 == 0 and t2 == 0 and l1 == 0 and l2 == 0
            assert provider.calls >= first_calls
            gate.set()
            assert await drive() == "completed"
            assert await drive(run_id=second_id) == "completed"
    asyncio.run(test())


def test_sink_crash_reuses_raw_single_call(tmp_path):
    async def test():
        provider = FakeSelfCheckProvider([{"pass": True, "problems": []}], fail="sink")
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=provider) as (factory, supervisor, drive, run, *rest):
            events = rest[4]
            for _ in range(80):
                await events(run)
                await supervisor.tick()
                async with factory() as tx:
                    inv = await tx.scalar(select(Invocation).where(Invocation.run_id == run).order_by(Invocation.created_at.desc()))
                    journal = ((inv.review or {}).get(SEMANTIC_KEY) if inv else None) or {}
                    if journal.get("raw"):
                        break
                await asyncio.sleep(0.002)
            assert provider.calls == 1
            provider.fail = None
            assert await drive() == "completed"
            assert provider.calls == 1
            async with factory() as tx:
                current = await tx.get(CorePlan, (await tx.get(CoreRun, run)).current_plan_id)
                journal = (current.self_check or {}).get(SEMANTIC_KEY) or {}
                assert journal.get("raw")
    asyncio.run(test())


def test_sending_without_raw_unknown_no_resubmit(tmp_path):
    async def test():
        gate = asyncio.Event()
        provider = FakeSelfCheckProvider([{"pass": True, "problems": []}], gate=gate)
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=provider) as (factory, supervisor, drive, run, *rest):
            events = rest[4]
            await _pump_until_calls(factory, supervisor, events, provider, run)
            async with factory.begin() as tx:
                plan = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run).limit(1))
                inv = await tx.scalar(select(Invocation).where(Invocation.run_id == run).order_by(Invocation.created_at.desc()))
                bag = dict(inv.review or plan.self_check or {})
                semantic = dict(bag.get(SEMANTIC_KEY) or {})
                semantic["status"] = "sending"
                semantic["raw"] = None
                bag[SEMANTIC_KEY] = semantic
                inv.review = bag
                inv.status = "returned"
                inv.lease_token = None
                inv.lease_until = now() - timedelta(seconds=30)
            before = provider.calls
            status = reason = None
            for _ in range(40):
                await supervisor.tick()
                async with factory() as tx:
                    plan = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run).order_by(CorePlan.version.desc()))
                    journal = (plan.self_check or {}).get(SEMANTIC_KEY, {}) if plan else {}
                    status, reason = journal.get("status"), journal.get("fallback_reason")
                if status in {"unknown", "rule_only"} or reason == "sending_without_raw":
                    break
                await asyncio.sleep(0.002)
            gate.set()
            assert provider.calls == before
            assert status in {"unknown", "rule_only"} or reason == "sending_without_raw"
    asyncio.run(test())


def test_two_consumers_same_candidate_one_call(tmp_path):
    async def test():
        gate = asyncio.Event()
        provider = FakeSelfCheckProvider([{"pass": True, "problems": []}], gate=gate)
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=provider) as (factory, supervisor, drive, run, *rest):
            events = rest[4]
            other = Supervisor(factory, supervisor.planner, settings=_settings(), selfcheck_provider=provider)
            await _pump_until_calls(factory, supervisor, events, provider, run)
            await other.tick()
            assert provider.calls == 1
            async with factory() as tx:
                tasks, admissions, ledger = await _counts(tx, run)
            assert tasks == 0 and ledger == 0
            gate.set()
            assert await drive() == "completed"
            assert provider.calls == 1
    asyncio.run(test())


def test_semantic_reject_history_and_retry_bound(tmp_path):
    async def test():
        inner = ScriptInner({"pass": False, "problems": [{"item": "数量", "expected": "1", "actual": "2"}]})
        provider = ChatSelfCheckProvider(inner, SimpleNamespace(id="r"))
        settings = _settings(selfcheck_mode="enforce", review_retries=2)
        async with fixture(tmp_path, planner="chain", supervisor_settings=settings, selfcheck_provider=provider) as ctx:
            factory, supervisor, drive, run, *_ = ctx
            supervisor.selfcheck_evidence_gate = control_flow_enforce_gate
            status = await drive()
            assert status in {"failed", "paused", "completed"}
            async with factory() as tx:
                plans = list(await tx.scalars(select(CorePlan).where(CorePlan.run_id == run).order_by(CorePlan.version)))
                rejected = [p for p in plans if p.stage == "rejected"]
                assert len(rejected) >= 1
                assert all(p.raw_output for p in rejected)
                tasks, admissions, ledger = await _counts(tx, run)
            assert inner.calls <= settings.review_retries + 1
            assert ledger == 0
    asyncio.run(test())


def test_invalid_output_rules_fallback(tmp_path):
    async def test():
        provider = FakeSelfCheckProvider(["not-json"])
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=provider) as (factory, supervisor, drive, run, *_):
            assert await drive() == "completed"
            assert provider.calls == 1
            async with factory() as tx:
                current = await tx.get(CorePlan, (await tx.get(CoreRun, run)).current_plan_id)
                journal = (current.self_check or {}).get(SEMANTIC_KEY) or {}
                assert journal.get("status") == "rule_only"
                assert journal.get("fallback_reason")
    asyncio.run(test())


def test_no_route_unavailable_not_fake_pass(tmp_path):
    async def test():
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=None) as (factory, supervisor, drive, run, *_):
            assert await drive() == "completed"
            async with factory() as tx:
                current = await tx.get(CorePlan, (await tx.get(CoreRun, run)).current_plan_id)
                journal = (current.self_check or {}).get(SEMANTIC_KEY) or {}
                assert journal.get("status") in {"unavailable", "rule_only"}
                assert journal.get("fallback_reason") in {"no_route", "unavailable", "provider_missing"}
                assert journal.get("parsed") is None
    asyncio.run(test())


def test_stale_revision_no_tasks_usage_once(tmp_path):
    async def test():
        gate = asyncio.Event()
        provider = FakeSelfCheckProvider([{"pass": True, "problems": []}], gate=gate)
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=provider) as (factory, supervisor, drive, run, *rest):
            events = rest[4]
            await _pump_until_calls(factory, supervisor, events, provider, run)
            async with factory.begin() as tx:
                row = await tx.get(CoreRun, run)
                row.input_revision = int(row.input_revision or 1) + 1
            gate.set()
            for _ in range(40):
                await events(run)
                await supervisor.tick()
                async with factory() as tx:
                    plan = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run).order_by(CorePlan.version.desc()))
                    if plan is not None and plan.stage == "obsolete":
                        break
                await asyncio.sleep(0.002)
            async with factory() as tx:
                tasks, admissions, ledger = await _counts(tx, run)
                plan = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run).order_by(CorePlan.version.desc()))
                usage = ((await tx.get(CoreRun, run)).error or {}).get("planner_usage") or {}
            assert tasks == 0 and ledger == 0
            assert plan.stage == "obsolete"
            accounted = usage.get("accounted_selfcheck") or []
            assert len(accounted) == 1
            assert provider.calls == 1
    asyncio.run(test())


def test_content_block_stops_without_tasks(tmp_path):
    async def test():
        provider = FakeSelfCheckProvider(fail="blocked")
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=provider) as (factory, supervisor, drive, run, *_):
            status = await drive()
            assert status == "failed"
            assert provider.calls == 1
            async with factory() as tx:
                tasks, admissions, ledger = await _counts(tx, run)
                plan = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run).order_by(CorePlan.version.desc()))
                journal = (plan.self_check or {}).get(SEMANTIC_KEY) or {}
            assert tasks == 0 and ledger == 0
            assert journal.get("status") == "blocked"
            usage = ((await _run_usage(factory, run)))
            assert int(usage.get("known_tokens") or 0) >= 4
    asyncio.run(test())


async def _run_usage(factory, run):
    async with factory() as tx:
        row = await tx.get(CoreRun, run)
        return ((row.error or {}).get("planner_usage") or {})


def test_late_owner_audits_without_admit(tmp_path):
    async def test():
        gate = asyncio.Event()
        provider = FakeSelfCheckProvider([{"pass": True, "problems": []}], gate=gate)
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=provider) as (factory, supervisor, drive, run, *rest):
            events = rest[4]
            await _pump_until_calls(factory, supervisor, events, provider, run)
            async with factory.begin() as tx:
                inv = await tx.scalar(select(Invocation).where(Invocation.run_id == run).order_by(Invocation.created_at.desc()))
                bag = dict(inv.review or {})
                semantic = dict(bag.get(SEMANTIC_KEY) or {})
                semantic["status"] = "sending"
                semantic["raw"] = None
                bag[SEMANTIC_KEY] = semantic
                inv.review = bag
                inv.lease_token = None
                inv.lease_until = now() - timedelta(seconds=30)
            for _ in range(40):
                await supervisor.tick()
                async with factory() as tx:
                    latest = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run).order_by(CorePlan.version.desc()))
                    if latest is not None and latest.stage != "reviewing":
                        break
                await asyncio.sleep(0.002)
            gate.set()
            await asyncio.sleep(0.02)
            await supervisor.tick()
            async with factory() as tx:
                tasks, admissions, ledger = await _counts(tx, run)
                usage = ((await tx.get(CoreRun, run)).error or {}).get("planner_usage") or {}
                latest = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run).order_by(CorePlan.version.desc()))
            journal = (latest.self_check or {}).get(SEMANTIC_KEY) or {}
            assert latest.stage in {"execute", "obsolete", "rejected"}
            assert journal.get("status") in {"unknown", "rule_only"}
            assert journal.get("parsed") is None
            assert usage.get("selfcheck_late_audit")
            assert provider.calls == 1
            execute_count = 0
            async with factory() as tx:
                execute_count = int(await tx.scalar(select(func.count()).select_from(CorePlan).where(
                    CorePlan.run_id == run, CorePlan.stage == "execute")) or 0)
            assert execute_count <= 1
    asyncio.run(test())


def test_selfcheck_over_budget_no_admission(tmp_path):
    async def test():
        provider = FakeSelfCheckProvider([{"content": '{"pass": true, "problems": []}',
                                           "usage": {"total_tokens": 500}}])
        async with fixture(tmp_path, planner="chain", supervisor_settings=_settings(),
                           selfcheck_provider=provider) as (factory, supervisor, drive, run, *_):
            status = await drive()
            assert status == "failed"
            async with factory() as tx:
                tasks, admissions, ledger = await _counts(tx, run)
                latest = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run).order_by(CorePlan.version.desc()))
            assert tasks == 0 and ledger == 0
            assert latest.stage == "rejected"
    asyncio.run(test())


def test_missing_selfcheck_route_id_is_unavailable(tmp_path):
    async def test():
        settings = _settings(selfcheck_route_id="does-not-exist")
        async with fixture(tmp_path, planner="chain", supervisor_settings=settings,
                           selfcheck_provider=None) as (factory, supervisor, drive, run, *_):
            assert await drive() == "completed"
            async with factory() as tx:
                row = await tx.get(CoreRun, run)
                entry = (row.version_lock or {}).get("selfcheck") or {}
                current = await tx.get(CorePlan, row.current_plan_id)
                journal = (current.self_check or {}).get(SEMANTIC_KEY) or {}
            assert entry.get("route") is None
            assert entry.get("reason") == "missing_route_id"
            assert journal.get("status") in {"unavailable", "rule_only"}
            assert journal.get("fallback_reason") in {"no_route", "unavailable", "provider_missing"}
    asyncio.run(test())
