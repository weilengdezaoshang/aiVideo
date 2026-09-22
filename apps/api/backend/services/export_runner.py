"""FFmpeg executes only in the export pool; PG leases fence artifact publication."""
import asyncio
from contextlib import suppress
from pathlib import Path
import uuid

from backend.errors import ErrorCode
from backend.exports import Exports, ffmpeg_binary
from backend.infrastructure.artifacts import ArtifactSpool
from backend.providers.policy import Operation, ProviderFailure, ProviderPolicy, remaining_seconds
from backend.services.execution import ExecutionStore


async def execute_export(job_id: uuid.UUID, factory, root: Path, policy=None):
    policy = policy or ProviderPolicy()
    store = ExecutionStore(factory, policy)
    claim = await store.claim(job_id)
    if claim is None:
        return {"skipped": True}
    spool = ArtifactSpool(root)
    if claim.phase == "persist":
        await asyncio.to_thread(spool.verify, claim.payload)
        return {"completed": await store.complete(claim, claim.payload)}
    def source(url):
        key = claim.params["sources"][url]
        path = (root / key).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("Export source unavailable")
        return path
    renderer = Exports(root, durable=True, source_resolver=source,
                       work_root=root / "export-work" / str(job_id) / str(claim.epoch))
    local = {"id": str(job_id), "status": "queued", "timeline": claim.params["timeline"]}
    task = asyncio.create_task(renderer.render(local, ffmpeg_binary()))
    async def heartbeat():
        while True:
            await asyncio.sleep(policy.heartbeat_s)
            try:
                owned = await store.heartbeat(claim)
            except Exception:
                owned = False
            if not owned:
                task.cancel()
                return
    alive = asyncio.create_task(heartbeat())
    try:
        async with asyncio.timeout(remaining_seconds(claim.deadline)):
            await task
        if local["status"] != "completed":
            raise ValueError("Export did not complete")
        path = renderer.root / str(job_id) / "output.mp4"
        artifact = await asyncio.to_thread(spool.save_file, job_id, claim.epoch, path, "mp4")
        return {"completed": await store.complete(claim, artifact)}
    except (ValueError, TimeoutError):
        await store.fail(claim, ProviderFailure(ErrorCode.UPSTREAM_TIMEOUT if remaining_seconds(claim.deadline) == 0
            else ErrorCode.INVALID_PARAM, operation=Operation.DOWNLOAD))
        return {"failed": True}
    finally:
        alive.cancel()
        with suppress(asyncio.CancelledError):
            await alive
