"""Compose existing runtime resources with a locked tool binding, never prompt routing."""
import json
import os
import uuid
from pathlib import Path
from backend.core.orm import CoreTask
from backend.core.tools.contracts import VersionLock
from backend.core.tools.store import load_catalog
from backend.core.orm import CoreRun
from .engine import Worker
from .contracts import WorkerSettings
from .adapters import StagedAdapter


async def build_worker(factory, root, job_id, client, *, settings=None, artifact_client=None):
    from backend.infrastructure.orm import Job
    async with factory() as tx:
        job = await tx.get(Job, job_id)
        task = await tx.get(CoreTask, uuid.UUID(job.params["coreTaskId"]))
        run = await tx.get(CoreRun, task.run_id)
        lock = VersionLock.model_validate(run.version_lock["tool_catalog"])
        catalog = await load_catalog(tx, lock.version, expected_lock=lock)
        tool = catalog.get_tool(task.tool_ref, lock.version)
    settings = settings or WorkerSettings()
    if tool.execution.adapter == "execution_test_fake":
        if os.environ.get("AIVERO_RUNTIME_ENVIRONMENT") != "development":
            raise ValueError("fault-injection provider is development-only")
        from .fake import FakeProvider, Scenario
        config = tool.execution.binding
        provider = FakeProvider(Path(config["database_path"]), Scenario.model_validate(config["scenario"]))
    elif tool.execution.adapter == "staged":
        secret_file = os.environ.get("AIVERO_CREDENTIALS_FILE")
        secrets = json.loads(Path(secret_file).read_text()) if secret_file else {}
        origins = {s.strip().rstrip("/") for s in os.environ.get("AIVERO_ARTIFACT_ORIGINS", "").split(",") if s.strip()}
        provider = StagedAdapter(tool, client, root, secrets, origins, artifact_client=artifact_client)
    else:
        from .local import LocalAdapter
        provider = LocalAdapter(tool, factory, root)
    return Worker(factory, root, provider, settings=settings)
