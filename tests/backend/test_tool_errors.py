import pytest

from backend.core.tools.errors import FAILURE_RULES, normalize_error, normalize_legacy_cutout


@pytest.mark.parametrize("rule", FAILURE_RULES, ids=lambda rule: rule.provider + ":" + rule.code)
def test_every_error_mapping(rule):
    result = normalize_error(rule.provider, rule.code, rules=FAILURE_RULES)
    assert result.kind == rule.failure
    if rule.failure in {"blocked_by_model", "param_invalid", "no_subject", "quota"}:
        assert not result.retry_allowed


def test_unknown_error_alert_has_correlation_context(caplog):
    alerts = []
    result = normalize_error("new-provider", "new-code", alert=alerts.append,
                             event_id="event", run_id="run", task_id="task")
    assert result.kind == "unknown" and not result.retry_allowed
    assert alerts[0]["event_id"] == "event" and alerts[0]["run_id"] == "run"
    assert "unknown_tool_failure" in caplog.text


@pytest.mark.parametrize("status,kind", [(400, "param_invalid"), (429, "retryable"), (503, "provider_down"), (401, "unknown")])
def test_http_fallback(status, kind):
    assert normalize_error("provider", status=status).kind == kind


def test_quota_and_blocked_codes_override_http_and_submit_timeout_never_blind_retries():
    assert normalize_error("openai", "insufficient_quota", status=429).kind == "quota"
    blocked = normalize_error("aliyun", "DataInspectionFailed", status=503)
    assert blocked.kind == "blocked_by_model" and not blocked.retry_allowed
    for status in (None, 408, 502):
        result = normalize_error("cloud", status=status, operation="submit")
        assert result.submission_uncertain and not result.retry_allowed


@pytest.mark.parametrize("status,kind", [(429, "retryable"), (504, "retryable"), (502, "provider_down"), (400, "param_invalid")])
def test_existing_cutout_failures_are_not_no_subject(status, kind):
    assert normalize_legacy_cutout(status).kind == kind


def test_documented_siliconflow_missing_model_is_invalid_not_a_credit_error():
    assert normalize_error("siliconflow", "20012", status=400).kind == "param_invalid"


def test_staged_provider_certainty_is_preserved():
    from backend.core.tools.errors import normalize_provider_failure
    from backend.errors import ErrorCode
    from backend.providers.policy import Certainty, Operation, ProviderFailure

    failure = ProviderFailure(ErrorCode.UPSTREAM_TIMEOUT, operation=Operation.SUBMIT,
                              certainty=Certainty.UNKNOWN, retryable=True)
    result = normalize_provider_failure(failure)
    assert result.kind == "unknown" and result.submission_uncertain and not result.retry_allowed
    quota = ProviderFailure(ErrorCode.QUOTA_EXHAUSTED, operation=Operation.POLL, retryable=False)
    assert normalize_provider_failure(quota).kind == "quota"


def test_numeric_provider_error_codes_are_supported():
    assert normalize_error("siliconflow", 20012).kind == "param_invalid"
    alerts = []
    assert normalize_error("siliconflow", 123456, alert=alerts.append).kind == "unknown"
    assert alerts[0]["code"] == "123456"
