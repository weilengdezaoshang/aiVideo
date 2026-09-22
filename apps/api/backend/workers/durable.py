"""Durable Celery task. Deployment credentials are read only from an explicit secret mount."""

import json
import os
from pathlib import Path
import uuid

from backend.infrastructure.orm import Job
from backend.providers.policy import Operation, ProviderPolicy, remaining_seconds
from backend.services.generation_runner import execute_phase
from backend.workers.celery_app import celery_app
from backend.workers.runtime import get_runtime


async def phase_budget(factory, job_id: uuid.UUID) -> float:
    policy = ProviderPolicy()
    async with factory() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return 30
        if job.kind == "export":
            return remaining_seconds(job.execution_deadline) if job.execution_deadline else policy.export_s
        if job.phase == "submit" and job.kind == "image" and job.provider_snapshot.get("provider") != "comfyui":
            return (remaining_seconds(job.execution_deadline) if job.execution_deadline
                    else float(job.provider_snapshot.get("imageTimeoutMin", 20)) * 60)
        if job.phase not in {x.value for x in Operation}:
            return 30
        return policy.operation_seconds(Operation(job.phase), video=job.kind == "video")


@celery_app.task(name="aivideo.execute_phase", acks_late=True, reject_on_worker_lost=True)
def run_provider_phase(job_id: str) -> dict:
    runtime = get_runtime()
    ident = uuid.UUID(job_id)

    async def budget():
        return await phase_budget(runtime.session_factory(), ident)

    async def execute():
        async with runtime.session_factory()() as session:
            row = await session.get(Job, ident)
            is_export = row is not None and row.kind == "export"
        if is_export:
            from backend.services.export_runner import execute_export
            return await execute_export(ident, runtime.session_factory(), Path(os.environ["SWARMUI_DATA_DIR"]))
        secret_file = os.environ.get("AIVERO_CREDENTIALS_FILE")
        credentials = json.loads(Path(secret_file).read_text()) if secret_file else {}
        allowed = {x.strip().rstrip("/") for x in os.environ.get("AIVERO_ARTIFACT_ORIGINS", "").split(",") if x.strip()}
        return await execute_phase(ident, runtime.session_factory(), runtime.http(), runtime.redis(),
            Path(os.environ["SWARMUI_DATA_DIR"]), credentials, allowed, artifact_client=runtime.artifact_http())

    seconds = runtime.run(budget(), timeout=10)
    return runtime.run(execute(), timeout=max(1, seconds) + 15)
