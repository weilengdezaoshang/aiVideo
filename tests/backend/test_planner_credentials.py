"""The new supervisor and retained agent use the same registry contract."""
import json
from backend.agent.config import load_credentials
from backend.core.supervisor.wiring import load_credential_map


def test_shared_registry_and_missing_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("AIVERO_CREDENTIALS_FILE", raising=False)
    assert load_credential_map() == load_credentials(None) == {}
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"test": {"apiKey": "fake-unit-credential"}}))
    monkeypatch.setenv("AIVERO_CREDENTIALS_FILE", str(path))
    assert load_credential_map() == load_credentials(path) == {"test": {"apiKey": "fake-unit-credential"}}


def test_invalid_registry_logs_no_contents_or_secrets(tmp_path, caplog):
    path = tmp_path / "invalid.json"
    path.write_text('{"apiKey":"fake-secret-unit"')
    assert load_credentials(path) == {}
    assert "unreadable or invalid" in caplog.text
    assert "fake-secret-unit" not in caplog.text
    path.write_text('[]')
    assert load_credentials(path) == {}
    assert "must be an object" in caplog.text


def test_model_audit_redacts_keys_headers_and_urls_without_changing_model_input():
    from backend.core.supervisor.planners import _redacted_request
    text = 'apiKey="fake-inline-secret" password=fake-password Bearer fake-bearer-token https://u:p@example.test/a?token=fake-query'
    messages = [{"role": "user", "content": text}]
    logged = _redacted_request(messages, [])
    assert messages[0]["content"] == text
    serialized = json.dumps(logged)
    for secret in ("fake-inline-secret", "fake-password", "fake-bearer-token", "fake-query", "u:p@"):
        assert secret not in serialized
    assert "https://example.test/a" in serialized
    assert "[redacted]" in serialized
