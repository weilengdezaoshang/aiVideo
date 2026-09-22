"""Run via stdin inside an isolated Mock-only Compose API container, never production."""
import asyncio
import json
import os
import uuid

import httpx

from backend.infrastructure.database import create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Workspace, WorkspaceMember
from backend.production import load_routes
from backend.production_settings import ProductionSettings
from backend.services.identity import issue_session, SESSION_COOKIE


async def main():
    assert os.environ.get("AIVERO_ALLOW_ISOLATED_SMOKE") == "1"
    settings = ProductionSettings()
    assert all(route["provider"] == "mock" for route in load_routes(settings.provider_routes_file).values())
    engine = create_async_database_engine()
    factory = create_async_session_factory(engine)
    workspace, principal = uuid.uuid4(), uuid.uuid4().hex
    try:
        async with factory.begin() as session:
            session.add(Workspace(id=workspace, name="isolated-compose-smoke"))
            await session.flush()
            session.add(WorkspaceMember(workspace_id=workspace, principal_id=principal, role="admin"))
        token, csrf = await issue_session(factory, principal, 300)
    finally:
        await engine.dispose()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:7801", timeout=10, headers={
            "origin": settings.public_origin, "x-csrf-token": csrf, "cookie": f"{SESSION_COOKIE}={token}"}) as client:
        assert (await client.get("/api/health/ready")).status_code == 200
        document = (await client.post("/api/documents", json={"name": "compose-smoke"})).json()["document"]
        accepted = await client.post("/api/generate", json={"requestId": uuid.uuid4().hex,
            "model": "mock-test", "prompt": "isolated compose smoke", "documentId": document["id"],
            "width": 128, "height": 128, "steps": 1})
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["jobId"]
        async def wait(path, key):
            async with asyncio.timeout(90):
                while True:
                    response = await client.get(path)
                    assert response.status_code == 200, response.text
                    result = response.json()[key]
                    assert result["status"] not in {"failed", "unknown"}, result
                    if result["status"] == "completed":
                        return result
                    await asyncio.sleep(.5)
        job = await wait(f"/api/jobs/{job_id}", "job")
        image = await client.get(job["images"][0]["url"])
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
        timeline = {"version": 1, "fps": 30, "width": 128, "height": 128, "clips": [{
            "id": "smoke", "name": "image", "inFrame": 0, "outFrame": 3,
            "source": {"nodeId": "image", "kind": "image", "durationFrames": 30,
                       "url": job["images"][0]["url"]}}]}
        response = await client.post("/api/exports", json={"documentId": document["id"], "timeline": timeline,
            "requestId": uuid.uuid4().hex})
        assert response.status_code == 202, response.text
        export_id = response.json()["export"]["id"]
        await wait(f"/api/exports/{export_id}", "export")
        video = await client.get(f"/api/exports/{export_id}/download")
        assert video.status_code == 200 and video.content[4:8] == b"ftyp"
        async with client.stream("GET", "/api/events") as stream:
            assert stream.status_code == 200
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    snapshot = json.loads(line[6:])
                    assert any(row["id"] == job_id for row in snapshot["jobs"])
                    break
        print(json.dumps({"accepted": True, "generation": "completed", "export": "completed", "sse": "snapshot", "workspace": str(workspace)}))


if __name__ == "__main__":
    asyncio.run(main())
