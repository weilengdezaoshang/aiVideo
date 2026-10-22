"""Reusable durable fake provider; no application database or real credentials."""
import asyncio
import httpx
from backend.core.execution.contracts import before_call, after_result
from backend.core.execution.fake import FakeProvider, Scenario, create_fake_app


def test_fake_survives_restart_and_has_idempotency_switch(tmp_path):
    async def scenario():
        path = tmp_path / "external.sqlite"
        fake = FakeProvider(path, Scenario())
        first = await fake.submit("registration", {}, "idem")
        restarted = FakeProvider(path, Scenario())
        assert (await restarted.query(None, "registration")).task_id == first.task_id
        assert (await restarted.submit("registration", {}, "idem")).task_id == first.task_id
        assert restarted.counts()["submissions"] == 1
        no_idem = FakeProvider(tmp_path / "nonidem.sqlite", Scenario(idempotent=False))
        await no_idem.submit("same", {}, None)
        await no_idem.submit("same", {}, None)
        assert no_idem.counts()["submissions"] == 2
        assert await before_call({}) is None and await after_result({}, {}) is None
    asyncio.run(scenario())


def test_fake_http_interface_and_missing_callback(tmp_path):
    async def scenario():
        app = create_fake_app(tmp_path / "external.sqlite", Scenario(callback_copies=0))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fake") as client:
            result = (await client.post("/tasks", json={"registration_id": "r", "params": {}})).json()
            assert result["status"] == "succeeded"
            assert (await client.get("/callbacks")).json() == []
            assert (await client.get("/artifacts/" + result["task_id"])).headers["content-type"] == "image/png"
    asyncio.run(scenario())
