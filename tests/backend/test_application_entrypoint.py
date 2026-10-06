"""Deployment startup cannot fall back to the retired JSON/FIFO application."""

import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from backend import __main__ as entrypoint
from backend.app import create_app
from backend.infrastructure.database import DatabaseSettings
from backend.paths import ROOT
from backend.production import create_app as compatible_factory
from backend.production_settings import ProductionSettings


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    import os

    for name in os.environ:
        if name.startswith("AIVERO_"):
            monkeypatch.delenv(name, raising=False)
    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps({"image": {
        "provider": "mock", "cloudModel": "isolated-mock", "configVersion": "test-v1",
    }}))
    return ProductionSettings(
        public_origin="https://api.test", oidc_issuer="https://identity.test",
        oidc_client_id="test", oidc_client_secret="synthetic", session_secret="x" * 40,
        provider_routes_file=routes, media_root=tmp_path / "media",
    )


@pytest.mark.parametrize("old_mode", [None, "local", "production"])
@pytest.mark.parametrize("reload", [False, True])
def test_cli_always_uses_durable_app_without_loading_legacy_config(monkeypatch, old_mode, reload):
    from backend import config

    def forbidden(*_args, **_kwargs):
        raise AssertionError("application startup must not load config.json")

    monkeypatch.setattr(config, "load_config", forbidden)
    if old_mode is None:
        monkeypatch.delenv("AIVERO_API_MODE", raising=False)
    else:
        monkeypatch.setenv("AIVERO_API_MODE", old_mode)
    calls = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    entrypoint.main(["--reload"] if reload else [])
    args, options = calls.pop()
    assert args == ("backend.app:create_app",)
    assert options["factory"] is True
    assert options["reload"] is reload
    assert options["access_log"] is False  # OIDC query strings must not enter default access logs.
    assert options["port"] == 7801


def test_missing_deployment_configuration_fails_instead_of_starting_mock(deployment):
    with pytest.raises(ValidationError):
        create_app()
    with pytest.raises(RuntimeError, match="explicit AIVERO_DB_URL"):
        create_app(deployment)


def test_canonical_and_compatible_imports_share_one_factory(deployment, tmp_path):
    assert create_app is compatible_factory
    # Constructing the API does not connect to services or instantiate a local scheduler.
    app = create_app(deployment, DatabaseSettings(url="postgresql+psycopg://test@invalid/test"),
                     root=tmp_path)
    assert not hasattr(app.state, "jobs")
    assert {"/auth/login", "/api/generate", "/api/events"} <= {
        route.path for route in app.routes
    }
    assert not (tmp_path / "media" / "jobs.json").exists()


def test_application_import_does_not_load_historical_sandbox():
    code = (
        "import sys; from backend.app import create_app; "
        "assert 'backend.evaluation.sandbox' not in sys.modules; "
        "assert 'backend.evaluation.sandbox_jobs' not in sys.modules"
    )
    import os

    env = {**os.environ, "PYTHONPATH": str(ROOT / "apps/api")}
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=15)


def test_historical_fixture_requires_explicit_isolation():
    from backend.config import Config
    from backend.evaluation.sandbox import create_sandbox_app

    with pytest.raises(TypeError):
        create_sandbox_app()
    with pytest.raises(ValueError, match="独立数据目录"):
        create_sandbox_app(ROOT, config=Config(provider="mock"))
