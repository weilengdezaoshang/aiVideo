"""Strict scenarios, redaction and pagination contracts; no external infrastructure."""

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from backend.evaluation.reliability.runner import load
from backend.evaluation.reliability.schema import Case, Fault
from backend.core.diagnostics.reader import redact, encode, decode


def test_stage1_manifest_covers_contracts_and_faults():
    suite = load("tests/reliability/stage1.yaml")
    assert len(suite.scenarios) >= 20 and suite.timeout_seconds <= 300
    assert {
        "db_outage",
        "queue_backlog",
        "event_order",
        "event_parallel",
        "timer_locked",
        "event_integrity",
    } <= {c.probe for c in suite.scenarios}
    assert {"wake", "planner", "plan", "provider_accepted", "t1", "t2"} <= {
        c.fault.checkpoint for c in suite.scenarios
    }


@pytest.mark.parametrize(
    "value",
    [
        {"checkpoint": "arbitrary_python", "action": "kill_process"},
        {"checkpoint": "plan"},
        {"checkpoint": "none", "action": "kill_process"},
    ],
)
def test_faults_are_whitelisted_and_paired(value):
    with pytest.raises(ValidationError):
        Fault.model_validate(value)


def test_yaml_cannot_import_or_execute(tmp_path):
    file = tmp_path / "bad.yaml"
    file.write_text('!!python/object/apply:os.system ["echo bad"]')
    import yaml

    with pytest.raises(yaml.constructor.ConstructorError):
        load(file)
    with pytest.raises(ValidationError):
        Case(id="bad", covers=["x"], provider={"credential": "never-allowed"})


def test_redaction_preserves_correlation_but_removes_secrets():
    value = {
        "run_id": "run",
        "parent_run_id": "parent",
        "task_id": "task",
        "credentialRef": "secret",
        "nested": {
            "ticket": "signed",
            "mask": {"handle": "mask"},
            "url": "https://user:password@provider.test/file?signature=secret#fragment",
        },
        "reference": "$t1.output",
        "tokens_used": 2,
    }
    text = json.dumps(redact(value))
    assert all(
        secret not in text for secret in ["signature", "password", "signed", '"mask"}', '"secret"']
    )
    assert "run_id" in text and "$t1.output" in text and "tokens_used" in text


def test_cursor_round_trip_and_malformed_rejection():
    at = datetime.now(timezone.utc)
    ident = uuid.uuid4()
    assert decode(encode(SimpleNamespace(created_at=at, id=ident))) == (at, ident)
    for invalid in ("not-base64", "a" * 1000, "e30="):
        with pytest.raises(ValueError):
            decode(invalid)
