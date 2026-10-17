"""Development startup never enables paid routes or overwrites custom config."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("dev_stack_host_test", Path("scripts/dev_stack.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RUNTIME", tmp_path)
    return module


def test_generated_host_catalog_is_mock_only_and_samples_stay_disabled(launcher):
    launcher.ensure_host_catalog()
    raw = json.loads((launcher.RUNTIME / "host-catalog.json").read_text())
    assert raw["enabled"] and raw["environment"] == "development"
    for cap in raw["capabilities"]:
        if cap["enabled"] and cap["binding"].get("engine", "provider") != "frames":
            assert cap["binding"]["providerSnapshot"]["provider"] == "mock"
    assert not json.loads((launcher.ROOT / "deploy/host-catalog.development.json").read_text())["enabled"]
    launcher.ensure_host_catalog()
    assert json.loads((launcher.RUNTIME / "host-catalog.json").read_text()) == raw


def test_paid_or_production_development_override_is_rejected_without_writing(launcher):
    path = launcher.RUNTIME / "host-catalog.json"
    for config in ({"environment": "production"}, {"environment": "development", "capabilities": [
        {"enabled": True, "binding": {"providerSnapshot": {"provider": "cloud"}}}]}):
        text = json.dumps(config)
        path.write_text(text)
        with pytest.raises(SystemExit):
            launcher.ensure_host_catalog()
        assert path.read_text() == text


def test_env_upgrade_preserves_existing_values_and_rejects_custom_host_path(launcher):
    path = launcher.RUNTIME / "api.env"
    path.write_text('PRESERVE_THIS=kept\n')
    launcher.ensure_api_env()
    assert 'PRESERVE_THIS=kept' in path.read_text()
    assert str(launcher.RUNTIME / 'host-catalog.json') in path.read_text()
    text = 'PRESERVE_THIS=kept\nAIVERO_PRODUCTION_HOST_CATALOG_FILE=/custom/path.json\n'
    path.write_text(text)
    with pytest.raises(SystemExit):
        launcher.ensure_api_env()
    assert path.read_text() == text


def test_failed_build_does_not_fall_back_to_stale_image_or_restart_api(launcher, monkeypatch):
    import subprocess
    calls = []
    for name in ("ensure_certificates", "ensure_host_catalog", "ensure_api_env", "ensure_compose_files"):
        monkeypatch.setattr(launcher, name, lambda: None)
    def failed_build(*args):
        calls.append(args)
        raise subprocess.CalledProcessError(1, ["docker", "compose"])
    monkeypatch.setattr(launcher, "compose", failed_build)
    monkeypatch.setattr(launcher, "stop_children", lambda: pytest.fail("build failure must preserve the running API"))
    with pytest.raises(subprocess.CalledProcessError):
        launcher.up()
    assert calls == [("build", "migrate")]


def test_development_environment_is_explicit_for_api_and_worker_contract(launcher, monkeypatch):
    path = launcher.RUNTIME / "api.env"
    path.write_text('AIVERO_RUNTIME_ENVIRONMENT=production\nAIVERO_PRODUCTION_RUNTIME_ENVIRONMENT=production\n')
    values = launcher.load_env()
    assert values["AIVERO_RUNTIME_ENVIRONMENT"] == "development"
    assert values["AIVERO_PRODUCTION_RUNTIME_ENVIRONMENT"] == "development"
