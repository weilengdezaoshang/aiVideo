"""Exact provider codes; never guess moderation or no-subject from free text."""
from __future__ import annotations

import logging
from collections.abc import Callable

from .contracts import Contract, FailureRule, FailureType

log = logging.getLogger(__name__)

# Public structured codes supported by current adapters/providers. No prose matching.
FAILURE_RULES = [
    FailureRule(provider=provider, code=code, failure=failure)
    for provider, codes, failure in [
        ("aliyun", ("DataInspectionFailed", "data_inspection_failed", "InputDataInspectionFailed", "OutputDataInspectionFailed"), "blocked_by_model"),
        ("aliyun", ("InvalidParameter", "InvalidParameterValue", "MissingParameter"), "param_invalid"),
        ("aliyun", ("Throttling", "Throttling.RateQuota"), "retryable"),
        ("aliyun", ("QuotaExhausted", "Arrearage", "InsufficientBalance"), "quota"),
        ("aliyun", ("ServiceUnavailable", "InternalError"), "provider_down"),
        ("openai", ("content_policy_violation", "moderation_blocked"), "blocked_by_model"),
        ("openai", ("invalid_request_error",), "param_invalid"),
        ("openai", ("rate_limit_exceeded",), "retryable"),
        ("openai", ("insufficient_quota",), "quota"),
        ("siliconflow", ("20012",), "param_invalid"),
        ("comfyui", ("prompt_outputs_failed_validation", "invalid_prompt"), "param_invalid"),
        ("rembg", ("no_subject",), "no_subject"),
        ("rembg", ("mask_invalid", "mask_source_mismatch"), "param_invalid"),
        ("rembg", ("recognition_busy", "recognition_duplicate", "recognition_timeout"), "retryable"),
        ("rembg", ("recognition_failed", "recognition_output_invalid"), "provider_down"),
    ] for code in codes
]


class Failure(Contract):
    kind: FailureType
    retry_allowed: bool
    submission_uncertain: bool = False


def normalize_error(provider: str, code: str | int | None = None, *, status: int | None = None,
                    operation: str = "poll", alert: Callable[[dict], None] | None = None,
                    event_id: str | None = None, run_id: str | None = None,
                    task_id: str | None = None, rules: list[FailureRule] | None = None) -> Failure:
    rules = FAILURE_RULES if rules is None else rules
    normalized_code = str(code) if code is not None else ""
    matches = [rule.failure for rule in rules if rule.provider == provider and rule.code == normalized_code]
    kind: FailureType = "blocked_by_model" if "blocked_by_model" in matches else matches[0] if matches else "unknown"
    # Stable structured codes take precedence over HTTP status (quota != rate limiting).
    if not matches:
        if status in {400, 404, 413, 422}:
            kind = "param_invalid"
        elif status in {408, 429}:
            kind = "retryable"
        elif status is not None and 500 <= status <= 599:
            kind = "provider_down"
    uncertain = operation == "submit" and (status is None or status == 408 or (status >= 500))
    if kind == "unknown":
        # Do not log raw provider bodies, prompts, credentials, or unbounded error codes.
        context = {"kind": "unknown_tool_failure", "provider": provider[:64],
                   "code": normalized_code[:128], "status": status,
                   "event_id": event_id, "run_id": run_id, "task_id": task_id}
        log.error("unknown_tool_failure", extra={"tool_failure": context})
        if alert is not None:
            alert(context)
    return Failure(kind=kind, retry_allowed=kind in {"retryable", "provider_down"} and not uncertain,
                   submission_uncertain=uncertain)


def normalize_legacy_cutout(status: int, *, code: str | int | None = None, **context) -> Failure:
    """Existing Recognition HTTP contract; 409 cancellation is handled as cancellation by its caller.

    No legacy status means no_subject. Only a validated empty foreground (or an
    explicit provider no_subject code) can produce that result.
    """
    code = code or {429: "recognition_busy", 504: "recognition_timeout", 502: "recognition_failed"}.get(status)
    return normalize_error("rembg", code, status=status, operation="read", **context)


async def normalize_error_durable(tx, provider: str, code: str | int | None = None, **context) -> Failure:
    """Use the existing alert repository inside the caller's transaction, without committing.

    Correlation IDs stay in context; an absent/deleted outbox row must not prevent
    recording an unknown-provider alert via its FK.
    """
    import hashlib
    import json
    from backend.core.events.store import alert as record_alert

    alerts = []
    failure = normalize_error(provider, code, alert=alerts.append, **context)
    for data in alerts:
        key = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        await record_alert(tx, "unknown_tool_failure", None, "tool-failure:" + key, context=data)
    return failure


def normalize_provider_failure(error, **context) -> Failure:
    """Bridge the staged provider's existing certainty contract without parsing messages."""
    from backend.errors import ErrorCode
    from backend.providers.policy import Certainty

    mapping = {
        ErrorCode.INVALID_PARAM: "param_invalid", ErrorCode.QUOTA_EXHAUSTED: "quota",
        ErrorCode.UPSTREAM_RATE_LIMIT: "retryable", ErrorCode.UPSTREAM_TIMEOUT: "retryable",
        ErrorCode.UPSTREAM_UNAVAILABLE: "provider_down",
    }
    kind = mapping.get(error.code)
    uncertain = error.certainty == Certainty.UNKNOWN
    if kind is None:
        result = normalize_error("staged", error.code.value, operation=error.operation.value, **context)
        return result.model_copy(update={"retry_allowed": False, "submission_uncertain": uncertain})
    return Failure(kind=kind, retry_allowed=bool(error.retryable and not uncertain),
                   submission_uncertain=uncertain)
