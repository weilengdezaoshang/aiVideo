"""Local dialogue config: isolation, redaction, persistence and provider wiring."""
import json
import asyncio
import stat
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.agent.local_config import (
    config_path, dialogue_route, install_local_config_routes, public_status, save_config,
)
from backend.agent.providers import ChatCompletionsProvider
from backend.services.identity import Identity, authenticated


def data(**patch):
    return {"baseUrl": "https://example.com/v1", "model": "test-model", "apiKey": "test-only-key", "timeoutMs": 45000, **patch}


def test_saved_config_preserves_key_is_private_redacted_and_workspace_scoped(tmp_path):
    workspace, other = uuid.uuid4(), uuid.uuid4()
    save_config(tmp_path, workspace, data())
    updated = save_config(tmp_path, workspace, {key: value for key, value in data(model="updated").items() if key != "apiKey"})
    assert updated.apiKey.get_secret_value() == "test-only-key"
    assert public_status(updated)["model"] == "updated"
    assert "test-only-key" not in json.dumps(public_status(updated))
    assert stat.S_IMODE(config_path(tmp_path, workspace).stat().st_mode) == 0o600
    assert dialogue_route(tmp_path, other) is None
    route = dialogue_route(tmp_path, workspace)
    assert route.model == "updated" and route.timeout_s == 45
    assert "test-only-key" not in json.dumps(route.snapshot())


@pytest.mark.parametrize("patch", [{"baseUrl": "http://example.com"}, {"baseUrl": "https://user:secret@example.com"},
    {"timeoutMs": 1}, {"timeoutMs": "45000"}, {"model": "  "}, {"apiKey": "bad\nkey"}])
def test_invalid_input_does_not_replace_existing_config(tmp_path, patch):
    workspace = uuid.uuid4()
    save_config(tmp_path, workspace, data())
    before = config_path(tmp_path, workspace).read_bytes()
    with pytest.raises(ValidationError):
        save_config(tmp_path, workspace, data(**patch))
    assert config_path(tmp_path, workspace).read_bytes() == before


def test_missing_initial_key_rejected(tmp_path):
    with pytest.raises(ValueError):
        save_config(tmp_path, uuid.uuid4(), {"baseUrl": "https://example.com", "model": "test"})


def test_endpoint_auth_development_and_workspace_admin_gates(tmp_path):
    app = FastAPI()
    app.state.settings = SimpleNamespace(runtime_environment="development", media_root=tmp_path)
    app.state.host = SimpleNamespace(catalog=SimpleNamespace(config=SimpleNamespace(enabled=True)))
    async def read(request):
        return await request.json()
    install_local_config_routes(app, read)
    identity = Identity("test", uuid.uuid4(), "owner")
    async def authorize():
        return identity
    app.dependency_overrides[authenticated] = authorize
    with TestClient(app) as client:
        url = "/api/creation/v1/llm-config"
        assert client.get(url).json()["configured"] is False
        response = client.post(url, json=data())
        assert response.status_code == 200 and response.json()["keyConfigured"]
        assert "test-only-key" not in response.text
        identity = Identity("reader", identity.workspace_id, "viewer")
        assert client.post(url, json=data()).status_code == 403
        assert client.get(url).status_code == 403
        identity = Identity("test", identity.workspace_id, "owner")
        app.state.settings.runtime_environment = "production"
        assert client.post(url, json=data()).status_code == 403
        assert client.get(url).status_code == 403


def test_provider_reads_saved_workspace_key_without_external_calls(tmp_path):
    workspace = uuid.uuid4()
    save_config(tmp_path, workspace, data())
    route = dialogue_route(tmp_path, workspace)
    seen = []
    async def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = ChatCompletionsProvider({}, client, local_config_root=tmp_path)
            result = await provider.complete(route, [{"role": "user", "content": "hello"}], [])
        return result
    result = asyncio.run(execute())
    assert result.content == "ok"
    assert seen[0].headers["authorization"] == "Bearer test-only-key"
    assert json.loads(seen[0].content)["model"] == "test-model"
