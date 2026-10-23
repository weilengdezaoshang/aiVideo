"""Resume helper: crash after raw storage, then a second process finishes the same run."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import select

from backend.agent.config import AgentRoute
from backend.agent.providers import _parse_response
from backend.core.events.contracts import EventSettings
from backend.core.events.processor import Processor
from backend.core.events.registry import Registry
from backend.core.events.store import accept
from backend.core.execution.engine import Worker, scan_ready
from backend.core.execution.fake import FakeProvider, Scenario
from backend.core.orm import CoreEvent, CoreRun, CoreTask
from backend.core.supervisor.orm import Invocation
from backend.core.supervisor.contracts import Settings
from backend.core.supervisor.engine import Supervisor, register
from backend.core.supervisor.planners import CascadePlanner, LLMPlanner
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory

ROUTE = AgentRoute(id="planner", label="Planner", provider="deepseek", model="deployment-model",
                   baseUrl="https://model.example/v1", credentialRef="agent", timeoutSeconds=45)
SETTINGS = Settings(planner_tool_threshold=200)


def _raw(asset):
    return {"id": "resp", "model": "deployment-model", "choices": [{"index": 0, "message": {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": "e", "type": "function", "function": {"name": "edit", "arguments": json.dumps({
                "prompt": "edit", "image": {"asset_id": asset, "media_type": "image"}})}},
            {"id": "v", "type": "function", "function": {"name": "video", "arguments": json.dumps({
                "prompt": "video", "image": "$t1.output"})}}]},
        "finish_reason": "tool_calls"}], "usage": {"total_tokens": 5}}


class Provider:
    def __init__(self, asset, counter_path, factory=None, run_id=None, state_path=None, root=None):
        self.asset = asset
        self.counter_path = Path(counter_path)
        self.factory = factory
        self.run_id = run_id
        self.state_path = Path(state_path) if state_path else None
        self.root = root
        self.calls = int(self.counter_path.read_text()) if self.counter_path.exists() else 0

    async def complete(self, route, messages, tools, response_sink=None):
        self.calls += 1
        self.counter_path.write_text(str(self.calls))
        raw = _raw(self.asset)
        if response_sink is not None:
            await response_sink(raw)
        if os.environ.get("AIVERO_CRASH_AFTER_RAW") and self.factory is not None:
            from datetime import datetime, timedelta, timezone
            from sqlalchemy import update
            if self.state_path is not None:
                self.state_path.write_text(json.dumps({"run": str(self.run_id), "root": str(self.root)}))
            async with self.factory.begin() as tx:
                await tx.execute(update(Invocation).where(
                    Invocation.run_id == self.run_id, Invocation.result.is_(None)).values(
                    lease_token=None, lease_until=datetime.now(timezone.utc) - timedelta(seconds=5)))
            os._exit(42)
        return _parse_response(raw, None)


def _planner(factory, provider):
    return CascadePlanner(LLMPlanner(provider, ROUTE, factory=factory, settings=SETTINGS), SETTINGS)


async def drive(factory, supervisor, run_id, root, config, fake):
    registry = Registry()
    register(registry)
    processor = Processor(factory, EventSettings(concurrency=1, backoff_base_seconds=.001, backoff_max_seconds=.01),
                          registry)
    for _ in range(80):
        async with factory.begin() as tx:
            for event in await tx.scalars(select(CoreEvent).where(CoreEvent.run_id == run_id)
                    .order_by(CoreEvent.run_sequence)):
                await accept(tx, {"event_id": str(event.id), "run_id": str(run_id)})
        for _n in range(8):
            if not any(await processor.tick()):
                break
        await supervisor.tick()
        await scan_ready(factory)
        async with factory() as tx:
            tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
        for task in tasks:
            if task.status in {"queued", "submitted"} and task.kind == "call_tool":
                await Worker(factory, root, fake, settings=config).execute(task.job_id)
        async with factory() as tx:
            row = await tx.get(CoreRun, run_id)
            if row.status in {"completed", "failed", "cancelled", "paused"}:
                return row.status
        await asyncio.sleep(.002)
    async with factory() as tx:
        run = await tx.get(CoreRun, run_id)
        tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
        invs = list(await tx.scalars(select(Invocation).where(Invocation.run_id == run_id)))
    raise AssertionError(run.status, run.pause_reason,
        [(t.kind, t.status, t.tool_ref) for t in tasks],
        [(i.status, i.attempt, i.error) for i in invs])


async def main():
    from cryptography.fernet import Fernet
    from backend.core.execution.contracts import WorkerSettings
    from tests.integration.test_supervisor_pipeline import fixture

    tmp = Path(os.environ["AIVERO_CRASH_TMP"])
    state = tmp / "crash-state.json"
    counter = tmp / "calls.txt"
    crash = os.environ.get("AIVERO_CRASH_AFTER_RAW")
    if crash:
        placeholder = Provider("pending", counter)
        async with fixture(tmp, message="换个背景",
                           make_planner=lambda factory: _planner(factory, placeholder)) as pack:
            factory, supervisor, drive_fn, run_id, _ws, root, config, fake, _events = pack
            async with factory() as tx:
                asset = (await tx.get(CoreRun, run_id)).snapshot["request"]["selected_assets"][0]
            provider = Provider(asset, counter, factory, run_id, state, root)
            supervisor.planner = _planner(factory, provider)
            await drive_fn()
        return
    payload = json.loads(state.read_text())
    run_id = uuid.UUID(payload["run"])
    engine = create_async_database_engine(DatabaseSettings(url=os.environ["AIVERO_SUPERVISOR_TEST_DB"]))
    factory = create_async_session_factory(engine)
    try:
        async with factory() as tx:
            run = await tx.get(CoreRun, run_id)
            asset = run.snapshot["request"]["selected_assets"][0]
        root = Path(payload["root"])
        config = WorkerSettings(ticket_key=Fernet.generate_key().decode(), poll_seconds=.001,
            retry_base_seconds=.001, retry_max_seconds=.01, retry_jitter=0)
        fake = FakeProvider(tmp / "provider-resume.sqlite", Scenario(actual_credits=3))
        provider = Provider(asset, counter)
        supervisor = Supervisor(factory, _planner(factory, provider))
        status = await drive(factory, supervisor, run_id, root, config, fake)
        payload["status"] = status
        payload["calls"] = int(counter.read_text()) if counter.exists() else provider.calls
        state.write_text(json.dumps(payload))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
