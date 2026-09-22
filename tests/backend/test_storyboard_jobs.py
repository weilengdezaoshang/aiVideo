import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend.storyboard_jobs import StoryboardJobs


def result():
    return dict(version=1, outline="计划", shots=[])


def test_planning_persistence_idempotence_and_cancel(tmp_path):
    async def run():
        entered = asyncio.Event()
        gate = asyncio.Event()
        calls = []

        async def planner(prompt, config, client, workflow):
            calls.append(prompt)
            entered.set()
            await gate.wait()
            return result()

        jobs = StoryboardJobs(tmp_path, planner)
        ident = str(uuid4())
        jobs.submit(ident, "doc", "广告", "product", {}, None)
        await entered.wait()
        assert jobs.submit(ident, "doc", "广告", "product", {}, None)["status"] == "running"
        with pytest.raises(HTTPException) as conflict:
            jobs.submit(ident, "doc", "不同需求", "product", {}, None)
        assert conflict.value.status_code == 409
        queued = str(uuid4())
        jobs.submit(queued, "doc", "取消的任务", "general", {}, None)
        jobs.cancel(queued)
        gate.set()
        await asyncio.gather(*list(jobs.tasks.values()), return_exceptions=True)
        assert calls == ["广告"]
        assert jobs.get(ident)["storyboard"] == result()
        restored = StoryboardJobs(tmp_path, planner)
        assert restored.get(ident)["status"] == "completed"
        assert restored.submit(ident, "doc", "广告", "product", {}, None)["status"] == "completed"
        assert restored.get(queued)["status"] == "cancelled"
        assert not restored.tasks
        await jobs.close()
        await restored.close()

    asyncio.run(run())


def test_shutdown_is_terminal_and_errors_are_sanitized(tmp_path):
    async def run():
        entered = asyncio.Event()

        async def planner(*args):
            entered.set()
            await asyncio.Event().wait()

        jobs = StoryboardJobs(tmp_path, planner)
        ident = str(uuid4())
        jobs.submit(ident, "doc", "广告", "general", {}, None)
        await entered.wait()
        await jobs.close()
        assert StoryboardJobs(tmp_path).get(ident)["status"] == "failed"

        async def broken(*args):
            raise RuntimeError("private-provider-token")

        jobs = StoryboardJobs(tmp_path, broken)
        failed = str(uuid4())
        jobs.submit(failed, "doc", "广告", "general", {}, None)
        await asyncio.gather(*list(jobs.tasks.values()))
        assert jobs.get(failed)["status"] == "failed"
        assert "private-provider-token" not in str(jobs.get(failed))
        await jobs.close()

    asyncio.run(run())


def test_job_api_roundtrip(client):
    async def planner(*args):
        return result()

    client.app.state.storyboard_jobs.planner = planner
    doc = client.post("/api/documents", json={"name": "分镜"}).json()["document"]
    payload = dict(id=str(uuid4()), documentId=doc["id"], prompt="广告", workflow="product")
    response = client.post("/api/storyboards/jobs", json=payload)
    assert response.status_code == 202
    job = client.get(f"/api/storyboards/jobs/{payload['id']}").json()["job"]
    assert job["status"] == "completed"
    assert job["storyboard"] == result()
    assert client.post("/api/storyboards/jobs", json=payload).json()["job"]["status"] == "completed"
    assert (
        client.post("/api/storyboards/jobs", json={**payload, "prompt": "改需求"}).status_code
        == 409
    )
    assert (
        client.delete(f"/api/storyboards/jobs/{payload['id']}").json()["job"]["status"]
        == "completed"
    )
    assert client.get(f"/api/storyboards/jobs/{uuid4()}").status_code == 404


def test_late_result_cannot_complete_cancelled_job(tmp_path):
    async def run():
        entered = asyncio.Event()

        async def planner(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return result()

        jobs = StoryboardJobs(tmp_path, planner)
        ident = str(uuid4())
        jobs.submit(ident, "doc", "广告", "general", {}, None)
        await entered.wait()
        assert jobs.cancel(ident)["status"] == "cancelled"
        await asyncio.gather(*list(jobs.tasks.values()), return_exceptions=True)
        assert jobs.get(ident)["status"] == "cancelled"
        assert "storyboard" not in jobs.get(ident)
        await jobs.close()

    asyncio.run(run())


def test_history_is_scoped_to_document(client):
    async def planner(*args):
        return result()

    client.app.state.storyboard_jobs.planner = planner
    docs = [
        client.post("/api/documents", json={"name": name}).json()["document"]
        for name in ("一", "二")
    ]
    identifiers = []
    for doc in docs:
        ident = str(uuid4())
        identifiers.append(ident)
        assert (
            client.post(
                "/api/storyboards/jobs",
                json=dict(id=ident, documentId=doc["id"], prompt=doc["name"], workflow="general"),
            ).status_code
            == 202
        )
    jobs = client.get("/api/storyboards/jobs", params={"documentId": docs[0]["id"]}).json()["jobs"]
    assert [job["id"] for job in jobs] == [identifiers[0]]
    assert "storyboard" not in jobs[0]
    assert "fingerprint" not in jobs[0]
    assert (
        client.get("/api/storyboards/jobs", params={"documentId": str(uuid4())}).status_code == 404
    )
