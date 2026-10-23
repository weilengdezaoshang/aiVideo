"""Existing trace spans link new model receipts without exposing response bodies."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from backend.core.diagnostics.reader import page
from backend.core.supervisor.orm import Invocation, PlannerModelCall


def test_planner_model_receipt_is_visible_as_safe_trace_metadata():
    now = datetime.now(timezone.utc)
    run = SimpleNamespace(id=uuid.uuid4(), parent_run_id=uuid.uuid4())
    invocation = Invocation(id=uuid.uuid4(), run_id=run.id, event_id=uuid.uuid4(),
        created_at=now, status="calling", context={"credentialRef": "private"})
    call = PlannerModelCall(id=uuid.uuid4(), invocation_id=invocation.id, status="stored",
        model="test-model", route="deployment-route", template_version="supervisor-v1",
        template_hash="hash", tool_catalog_hash="catalog", usage={"total_tokens": 12},
        raw_response={"choices": [{"message": {"content": "SECRET_RESPONSE_BODY"}}]}, created_at=now)

    class Tx:
        async def scalars(self, query):
            return [invocation]

        async def scalar(self, query):
            return call

    result = asyncio.run(page(Tx(), run, "calls"))
    span = result["items"][0]
    assert span["run_id"] == str(run.id) and span["parent_run_id"] == str(run.parent_run_id)
    assert span["links"]["event_id"] == str(invocation.event_id)
    receipt = span["data"]["model_call"]
    assert receipt["raw_stored"] is True and receipt["usage"]["total_tokens"] == 12
    assert receipt["model"] == "test-model"
    assert "context" not in span["data"]
    assert "SECRET_RESPONSE_BODY" not in str(result) and "private" not in str(result)
