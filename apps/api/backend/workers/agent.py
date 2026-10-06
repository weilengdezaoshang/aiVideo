"""Dedicated Agent task: one durable phase per delivery, no automatic paid retry."""
import json
import logging
import os
from pathlib import Path
import uuid

from backend.agent.providers import ChatCompletionsProvider
from backend.agent.runtime import AgentService
from backend.workers.celery_app import celery_app
from backend.workers.runtime import get_runtime


@celery_app.task(name="aivideo.agent.advance", acks_late=True, reject_on_worker_lost=True,
                 soft_time_limit=0, time_limit=300)
def advance_agent(run_id: str) -> dict:
    runtime = get_runtime()
    ident = uuid.UUID(run_id)

    async def execute():
        path = os.environ.get("AIVERO_CREDENTIALS_FILE")
        try:
            credentials = json.loads(Path(path).read_text()) if path else {}
        except (OSError, ValueError):
            logging.getLogger(__name__).error("Agent credentials registry is unreadable or invalid")
            credentials = {}
        if not isinstance(credentials, dict):
            credentials = {}
        model = ChatCompletionsProvider(credentials, client=runtime.http())
        return await AgentService(runtime.session_factory()).advance(
            ident, uuid.uuid4().hex, model, media_root=Path(os.environ['SWARMUI_DATA_DIR']))

    # The coroutine bounds HTTP and waits for native tool threads after timeout.
    # An outer Future timeout or soft time limit would release this Celery slot
    # while its thread still runs. Only a whole-process hard limit is a fallback.
    return runtime.run(execute())
