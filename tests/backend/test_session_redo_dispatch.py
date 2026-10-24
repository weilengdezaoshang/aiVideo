"""Pure redo dispatch checks. No paid calls. Optional PostgreSQL uses the existing block9 test."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.core.session_redo import RedoSettings, SeedUnchanged, clone_plan, occupied_seed
from backend.core.session_redo_service import (
    _old_seeds, build_redo_router, redo_request_override,
)
from backend.core.sessions.dispatch import (
    exact_redo_batch, explicit_budget, open_exact_redo, should_natural_redo,
)
from backend.core.supervisor.contracts import Plan
from backend.core.tools.catalog import ToolCatalog
from backend.core.tools.contracts import CatalogSnapshot, Execution, Port, Price, ToolDefinition


def _catalog():
    tool = ToolDefinition(
        name="fake_generate", version="unit", description="test only",
        parameters={"type": "object", "properties": {
            "prompt": {"type": "string"},
            "seed": {"type": "integer", "minimum": 0, "maximum": 20}},
            "required": ["prompt"], "additionalProperties": False},
        result_schema={"type": "object"}, inputs=[], outputs=[Port(path="output", media_type="image")],
        mode="generate", price=Price(kind="fixed", credits=1), limits={}, failures=[],
        execution=Execution(adapter="execution_test_fake", provider_idempotent=True, cancellable=True,
            cancellation="provider", result_url_policy="authenticated_asset", sla_seconds=60,
            binding={"seed_supported": True}))
    return ToolCatalog(CatalogSnapshot(version="unit", tools=[tool]))


def _plan(parameters):
    return Plan(goal="兔子", writer="rule", steps=[{"id": "t1", "tool": "fake_generate", "parameters": parameters}])


def test_omitted_seed_is_zero_and_clone_rejects_same_zero():
    assert occupied_seed({}) == 0
    assert occupied_seed({"seed": None}) == 0
    assert occupied_seed({"prompt": "兔子"}) == 0
    catalog = _catalog()
    plan = _plan({"prompt": "兔子"})
    occupied = _old_seeds(plan, catalog)
    assert 0 in occupied
    with pytest.raises(SeedUnchanged):
        clone_plan(plan, catalog.snapshot, 0, settings=RedoSettings(seed_minimum=0, seed_maximum=20))
    cloned = clone_plan(plan, catalog.snapshot, 4, settings=RedoSettings(seed_minimum=0, seed_maximum=20))
    assert cloned.steps[0].parameters["seed"] == 4
    assert cloned.steps[0].parameters["prompt"] == "兔子"
    explicit = _plan({"prompt": "兔子", "seed": 0})
    with pytest.raises(SeedUnchanged):
        clone_plan(explicit, catalog.snapshot, 0, settings=RedoSettings(seed_minimum=0, seed_maximum=20))


def test_request_override_keeps_goal_and_exact_selection():
    plan = _plan({"prompt": "兔子"})
    effective = {"message": "再来一次", "messages": ["再来一次"], "selected_assets": ["asset-1"],
                 "node_ids": ["n1"], "reference_clarification": {"status": "ambiguous"}}
    selection = {"selected_assets": ["asset-9"], "assets": [{"asset_id": "asset-9"}]}
    request = redo_request_override(plan, effective, selection)
    assert request["message"] == "兔子"
    assert request["messages"] == ["兔子"]
    assert request["selected_assets"] == ["asset-9"]
    assert request["selection_explicit"] is True
    assert "reference_clarification" not in request
    assert request["node_ids"] == ["n1"]


def test_redo_openapi_resolves_module_level_annotations():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(build_redo_router())
    schema = app.openapi()
    operation = schema["paths"]["/api/core/sessions/{session_id}/redo"]["post"]
    assert operation["operationId"]


def test_exact_phrases_without_active_run_are_redo():
    def message(text, **extra):
        return SimpleNamespace(content={"text": text, **extra})
    pending = [message("再来一次"), message("  再生成一次  "), message("重做一次")]
    assert exact_redo_batch(pending) is True
    assert should_natural_redo(pending, active_run=False) is True
    assert should_natural_redo(pending, active_run=True) is False
    assert should_natural_redo([message("再来一次，暗一点")], active_run=False) is False
    assert explicit_budget({"budget": 3, "tokens": 9}) == (3, 9)
    assert explicit_budget({"budget": True, "tokens": 9}) is None
    assert explicit_budget({"tokens": 9}) is None
    assert explicit_budget({"budget": 0, "tokens": 9}) is None


class _Nested:
    def __init__(self, tx):
        self.tx = tx

    async def __aenter__(self):
        self.tx.mark = len(self.tx.added)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            del self.tx.added[self.tx.mark:]
        return False


class _Tx:
    def __init__(self, scalar_result=None):
        self.added = []
        self.scalar_result = scalar_result
        self.mark = 0

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    def begin_nested(self):
        return _Nested(self)

    async def scalar(self, _query):
        if isinstance(self.scalar_result, Exception):
            raise self.scalar_result
        return self.scalar_result


def _session():
    return SimpleNamespace(id=uuid4(), workspace_id=uuid4(), user_id="user", message_cursor=1,
                           event_cursor=0, dispatch_at=object(), dispatch_lease_owner="x",
                           dispatch_lease_until=object(), active_run_id=None)


def _pending(**content):
    body = {"text": "再来一次", "budget": 4, "tokens": 8}
    body.update(content)
    return SimpleNamespace(id=uuid4(), content=body, processing_status="pending", intent=None, run_id=None)


def test_invalid_redo_explains_without_run_and_rolls_back_partial():
    from backend.core.session_redo_service import RedoRejected
    import backend.core.session_redo_service as service

    session = _session()
    pending = [_pending()]
    tx = _Tx(scalar_result=SimpleNamespace(id=uuid4(), current_plan_id=uuid4(), workspace_id=session.workspace_id,
                                           user_id=session.user_id))

    async def partial(tx, **kwargs):
        tx.add("partial-run")
        raise RedoRejected("seed_unsupported")

    original = service.redo_session
    service.redo_session = partial
    try:
        opened = asyncio.run(open_exact_redo(tx, session, pending))
    finally:
        service.redo_session = original
    assert opened is None
    assert "partial-run" not in tx.added
    assert pending[0].processing_status == "processed"
    assert pending[0].run_id is None
    notes = [row for row in tx.added if getattr(row, "role", None) == "assistant"]
    assert notes and notes[0].content["code"] == "seed_unsupported"
    assert session.dispatch_at is None
    assert session.active_run_id is None


def test_missing_budget_does_not_call_redo_or_planner():
    import backend.core.session_redo_service as service
    called = {"redo": 0, "planner": 0}
    original = service.redo_session

    async def redo(*args, **kwargs):
        called["redo"] += 1
        raise AssertionError("planner")

    service.redo_session = redo
    session = _session()
    pending = [_pending(budget=None)]
    pending[0].content.pop("budget")
    tx = _Tx()
    try:
        opened = asyncio.run(open_exact_redo(tx, session, pending))
    finally:
        service.redo_session = original
    assert opened is None and called["redo"] == 0 and called["planner"] == 0
    assert pending[0].processing_status == "processed"
    assert any(getattr(row, "role", None) == "assistant" for row in tx.added)


def test_unexpected_redo_error_is_not_swallowed():
    session = _session()
    pending = [_pending()]
    tx = _Tx(scalar_result=RuntimeError("database"))
    with pytest.raises(RuntimeError, match="database"):
        asyncio.run(open_exact_redo(tx, session, pending))
    assert not any(getattr(row, "role", None) == "assistant" for row in tx.added)


def test_successful_redo_marks_pending_on_new_run():
    import backend.core.session_redo_service as service
    session = _session()
    pending = [_pending()]
    created = SimpleNamespace(id=uuid4())
    source = SimpleNamespace(id=uuid4(), current_plan_id=uuid4())
    tx = _Tx(scalar_result=source)
    seen = {}

    async def fake_redo(tx, **kwargs):
        seen["plan"] = kwargs["body"].source_plan_id
        seen["credits"] = kwargs["body"].credits_limit
        seen["tokens"] = kwargs["body"].tokens_limit
        return created

    original = service.redo_session
    service.redo_session = fake_redo
    try:
        opened = asyncio.run(open_exact_redo(tx, session, pending))
    finally:
        service.redo_session = original
    assert opened is created
    assert seen["plan"] == source.current_plan_id
    assert seen["credits"] == 4 and seen["tokens"] == 8
    assert pending[0].processing_status == "processed"
    assert pending[0].run_id == created.id
