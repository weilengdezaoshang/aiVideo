"""Pure checks for edit policy, switch canvas CAS and redo seed copy. No paid calls."""
import inspect
import json
from uuid import uuid4

from backend.core import session_redo_service

import pytest

from backend.core.asset_edit import PUBLISH_HOOK, EditRejected, capability_of
from backend.core.session_redo import RedoSettings
from backend.core.session_redo_service import CREDIT_WARNING, SUPERVISOR_HOOK, RedoRequest, choose_seed, copied_result, request_hash
from backend.core.supervisor.contracts import Plan
from backend.core.tools import build_catalog
from backend.core.version_switch import API_HOOK, DAEMON_HOOK, RECOVERY_SQL, SwitchCommand, SwitchRejected, apply_switch_canvas, request_hash as switch_hash
from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig


def test_routers_use_authenticated_identity():
    from types import SimpleNamespace
    from uuid import UUID
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.services.identity import Identity, authenticated
    from backend.core.session_redo_service import build_redo_router
    from backend.core.version_switch import build_switch_router

    app = FastAPI()
    app.include_router(build_switch_router())
    app.include_router(build_redo_router())
    app.state.settings = SimpleNamespace(public_origin="https://app.example")
    app.state.factory = SimpleNamespace()
    client = TestClient(app)
    denied = client.post("/api/core/sessions/" + str(uuid4()) + "/redo", json={
        "request_key": "k", "source_run_id": str(uuid4()), "source_plan_id": str(uuid4()),
        "credits_limit": 1, "tokens_limit": 1})
    assert denied.status_code == 401
    seen = {}

    class _Tx:
        pass

    class _Begin:
        async def __aenter__(self):
            return _Tx()

        async def __aexit__(self, *args):
            return False

    class _Factory:
        def begin(self):
            return _Begin()

    async def allow_switch(tx, **kwargs):
        seen["user"] = kwargs["user_id"]
        seen["workspace"] = kwargs["workspace_id"]
        return SimpleNamespace(id=uuid4(), outbox_id=uuid4(), status="pending")

    app.state.factory = _Factory()
    identity = Identity("principal-1", UUID("00000000-0000-0000-0000-000000000001"), "member")
    app.dependency_overrides[authenticated] = lambda: identity
    import backend.core.version_switch as switch_mod
    original = switch_mod.submit_switch
    switch_mod.submit_switch = allow_switch
    try:
        response = client.post(
            "/api/core/assets/" + str(uuid4()) + "/versions/" + str(uuid4()) + "/switch",
            json={"document_id": str(uuid4()), "node_id": "n", "expected_document_revision": 1,
                  "expected_anchor_version": 1, "idempotency_key": "k"})
    finally:
        switch_mod.submit_switch = original
    assert response.status_code == 200
    assert seen["user"] == "principal-1"
    assert seen["workspace"] == identity.workspace_id


def test_authenticated_csrf_failure_is_forbidden():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.services.identity import SESSION_COOKIE, authenticated
    from backend.core.version_switch import build_switch_router

    app = FastAPI()
    app.include_router(build_switch_router())
    app.state.settings = SimpleNamespace(public_origin="https://app.example")

    class _Session:
        async def get(self, model, key):
            return SimpleNamespace(expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                                   csrf_hash="nope", principal_id="p")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    app.state.factory = lambda: _Session()
    client = TestClient(app)
    response = client.post(
        "/api/core/assets/" + str(uuid4()) + "/versions/" + str(uuid4()) + "/switch",
        json={"document_id": str(uuid4()), "node_id": "n", "expected_document_revision": 1,
              "expected_anchor_version": 1, "idempotency_key": "k"},
        cookies={SESSION_COOKIE: "token"}, headers={"origin": "https://app.example"})
    assert response.status_code == 403
    assert authenticated


def test_hooks_name_the_integration_points():
    assert "append_image_edit" in PUBLISH_HOOK and "cutout" in PUBLISH_HOOK
    assert "run_once" in DAEMON_HOOK and "daemon.py" in DAEMON_HOOK
    assert "build_switch_router" in API_HOOK
    assert "clock_timestamp" in RECOVERY_SQL
    assert "Planner.plan" in SUPERVISOR_HOOK
    assert "再来一次" in inspect.getsource(session_redo_service)
    assert "重新扣减" in CREDIT_WARNING


def test_only_image_edit_and_inpaint_append_versions():
    assert capability_of("image_edit_preview") == "image_edit"
    assert capability_of("inpaint") == "inpaint"
    with pytest.raises(EditRejected, match="cutout_preserves_original_as_new_artifact"):
        capability_of("cutout")
    assert capability_of("text_to_image_preview") is None


def test_switch_canvas_is_deterministic_and_cas_explicit():
    stored = uuid4()
    data = {"objects": {"n": {"assetId": str(stored), "assetVersion": 2, "src": "/old", "x": 1}}}
    updated = apply_switch_canvas(data, node_id="n", stored_asset_id=stored, anchor_version=2,
                                  chain_asset_ids={str(stored)})
    assert updated["objects"]["n"]["assetVersion"] == 3
    assert updated["objects"]["n"]["src"] == f"/api/assets/{stored}/content"
    assert data["objects"]["n"]["assetVersion"] == 2
    with pytest.raises(SwitchRejected, match="anchor_cas_conflict"):
        apply_switch_canvas(data, node_id="n", stored_asset_id=stored, anchor_version=1, chain_asset_ids={str(stored)})
    with pytest.raises(SwitchRejected, match="node_not_in_asset_chain"):
        apply_switch_canvas(data, node_id="n", stored_asset_id=stored, anchor_version=2, chain_asset_ids={"other"})
    body = SwitchCommand(document_id=uuid4(), node_id="n", expected_document_revision=4, expected_anchor_version=2, idempotency_key="k")
    assert switch_hash(uuid4(), uuid4(), body, user_id="a") != switch_hash(uuid4(), uuid4(), body, user_id="b")


def test_redo_seed_differs_and_prompt_is_not_rewritten():
    raw = {"version": "unit", "environment": "development", "enabled": True, "capabilities": [{
        "id": "text_to_image", "enabled": True, "outputType": "image",
        "tiers": {"preview": {"credits": 1}},
        "binding": {"version": "unit", "providerSnapshot": {
            "provider": "mock", "configVersion": "unit", "cloudModel": "mock-image"}}}]}
    catalog = build_catalog(Catalog(CatalogConfig.model_validate(raw)), "redo").snapshot
    plan = Plan(goal="rabbit", writer="llm", steps=[{"id": "t1", "tool": "text_to_image_preview",
        "parameters": {"prompt": "兔子", "seed": 1}},
        {"id": "t2", "tool": "text_to_image_preview", "parameters": {"prompt": "暗一点", "seed": 2}, "depends_on": ["t1"]}])
    seed = choose_seed({1, 2}, RedoSettings(seed_minimum=1, seed_maximum=3))
    assert seed == 3
    from backend.core.session_redo import clone_plan
    cloned = clone_plan(plan, catalog, seed, settings=RedoSettings(seed_minimum=1, seed_maximum=3))
    assert cloned.steps[0].parameters["prompt"] == "兔子"
    assert cloned.steps[1].depends_on == ["t1"]
    assert {step.parameters["seed"] for step in cloned.steps} == {3}
    result = copied_result(cloned, source_plan_id=uuid4(), seed=seed, invariants={"locked": True})
    assert result.tokens == 0 and result.raw_output["no_planner_call"] is True
    assert result.invariants == {"locked": True}
    body = RedoRequest(request_key="k", source_run_id=uuid4(), source_plan_id=uuid4(), credits_limit=5, tokens_limit=9)
    other = body.model_copy(update={"credits_limit": 6})
    assert request_hash(body) != request_hash(other)
    with pytest.raises(Exception):
        choose_seed({1}, RedoSettings(seed_minimum=1, seed_maximum=1))
    huge = choose_seed(set(), RedoSettings(seed_minimum=0, seed_maximum=2**31 - 1))
    assert 0 <= huge <= 2**31 - 1
    assert choose_seed({0, 2}, RedoSettings(seed_minimum=0, seed_maximum=2), bounds=(0, 2)) == 1
    assert json.loads(result.model_dump_json())["plan"]["steps"][0]["parameters"]["prompt"] == "兔子"
