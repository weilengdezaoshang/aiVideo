"""Plan self-check scoring. Gold labels never become observations or prompt text.

Rule results come from review.validate. Model results come from a stored provider
raw payload parsed by the runtime strict parser. Grading does not call the model
or the reviewer again. Scripted raw is a protocol fixture: llm_tested stays false.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.review.contracts import ParamRule, PlanView, ToolSpec
from backend.core.review.policy import validate
from backend.core.selfcheck.contracts import POLICY_HASH, TEMPLATE_VERSION, EnforcementEvidence
from backend.core.selfcheck.policy import call_key, content_from_raw, parse_verdict, public_route
from backend.core.selfcheck.policy import TriggerPolicy
from backend.core.selfcheck.prompt import load_prompt, render_messages
from backend.core.selfcheck.summary import summarize
from backend.evaluation.selfcheck_cases import SelfCheckCase, build_cases, dataset_hash

SCHEMA_VERSION = 1
OBSERVATION_SCHEMA = "plan-selfcheck-observation-v1"


class StageLimits(BaseModel):
    """output_cap is one response. token_budget is the whole run, not model-quality identity."""

    model_config = ConfigDict(extra="forbid")
    output_cap: int = Field(default=1024, ge=1)
    token_budget: int = Field(default=250_000, ge=1)
    max_context_bytes: int = Field(default=24_000, ge=256)
    timeout_seconds: float = Field(default=8, gt=0)
    max_output_bytes: int = Field(default=16_384, ge=256)
    max_param_chars: int = Field(default=400, ge=32)
    protocol_overhead: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0, le=0)
    concurrency: int = Field(default=2, ge=1, le=4)
    max_calls: int = Field(default=80, ge=1)

    @model_validator(mode="after")
    def _no_blind_retry(self) -> "StageLimits":
        if self.retries != 0:
            raise ValueError("timeout、unknown 和 blocked 不重试；retries 只能是 0")
        return self


class ClassCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    valid: int
    drift_total: int
    drift_valid: int
    normal_total: int
    normal_valid: int
    fallback: int


class RateBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tpr: float | None
    fpr: float | None
    p50_ms: float | None = None
    p95_ms: float | None = None
    known_tokens: int | None = None
    known_usage_count: int | None = None
    counts: ClassCounts
    fallback_counts: dict[str, int]


class SelfCheckSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    mode: str
    dataset_hash: str
    template_version: str
    template_hash: str
    policy_hash: str
    policy_fingerprint: str | None = None
    policy_snapshot: dict | None = None
    fixture_sha256: str | None = None
    llm_tested: bool = False
    llm_untested_reason: str
    enforce_eligible: bool = False
    model: RateBlock | None = None
    rule: RateBlock | None = None
    protocol: RateBlock | None = None
    comparison: dict | None = None
    unknown_usage: int = 0
    legal_type_pass: int
    legal_type_total: int
    valid_observation_rate: float
    fallback_count: int


def catalog() -> dict[str, ToolSpec]:
    return {
        "text_to_image": ToolSpec(
            id="text_to_image", version="1.2.0", output_type="image",
            params_schema={
                "prompt": ParamRule(type="string", required=True),
                "aspect_ratio": ParamRule(type="string", enum=["9:16", "16:9", "1:1"]),
                "count": ParamRule(type="number", minimum=1, maximum=4),
                "width": ParamRule(type="number", minimum=1),
                "height": ParamRule(type="number", minimum=1),
            },
            cost_credits=20,
        ),
        "image_edit": ToolSpec(
            id="image_edit", version="1.1.0", output_type="image",
            input_types={"image": ["image"]},
            params_schema={
                "prompt": ParamRule(type="string", required=True),
                "aspect_ratio": ParamRule(type="string", enum=["9:16", "16:9", "1:1"]),
            },
            cost_credits=30,
        ),
        "image_to_video": ToolSpec(
            id="image_to_video", version="1.4.0", output_type="video",
            input_types={"image": ["image"]},
            params_schema={
                "duration_seconds": ParamRule(type="number", minimum=1, maximum=15, required=True),
                "prompt": ParamRule(type="string"),
            },
            ambiguous_without=["duration_seconds"],
            cost_credits=120,
        ),
    }


def review_context(invariants: dict) -> dict:
    return {
        "user_id": "u1",
        "team_user_ids": ["u2"],
        "user_credits": 10_000,
        "run_budget": 5_000,
        "scope": {
            "tools": ["text_to_image", "image_edit", "image_to_video"],
            "readable_assets": ["a_own"],
            "writable_regions": ["r1"],
        },
        "version_lock": {"tools": {key: spec.model_dump(mode="json") for key, spec in catalog().items()}},
        "invariants": invariants or {},
        "canvas": {
            "nodes": [{"id": "n1"}],
            "assets": [{"asset_id": "a_own", "owner_id": "u1", "kind": "image", "width": 1024, "height": 1024}],
        },
    }


def catalog_hash() -> str:
    payload = {key: spec.model_dump(mode="json") for key, spec in catalog().items()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(quantile * len(ordered) + 0.999) - 1))
    return ordered[index]


def review_case(case: SelfCheckCase):
    """Run the reviewer once. Extensions only capture the view used to build the prompt."""

    captured: list[PlanView] = []

    def capture(view: PlanView):
        captured.append(view)
        return []

    result = validate(case.plan, review_context(case.invariants), intent_extensions=[capture])
    view = captured[0] if captured else None
    return result, view


def legal_type_pass(result) -> bool:
    return not any(reason.gate in {"legal", "types"} for reason in result.reasons)


def intent_reject(result) -> bool:
    return any(reason.gate == "intent" for reason in result.reasons)


def trusted_messages(
    case: SelfCheckCase,
    view: PlanView | None,
    *,
    limits: StageLimits | None = None,
) -> tuple[list[dict], dict]:
    """User text, invariants, refs, and summarize() output. Labels are not arguments."""

    current = limits or StageLimits()
    prompt = load_prompt()
    if view is None:
        summary = {"degraded": True, "reason": "no_plan_view"}
    else:
        summary = summarize(
            view,
            max_param_chars=current.max_param_chars,
            max_bytes=current.max_context_bytes,
            catalog_hash=catalog_hash(),
            proposal_hash=hashlib.sha256(
                json.dumps(case.plan, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
        )
    messages = render_messages(
        prompt,
        user_message=case.user_message,
        invariants=case.invariants,
        refs=list(case.refs),
        plan_summary=summary,
    )
    return messages, summary


def input_hash(messages: list[dict]) -> str:
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def case_call_key(
    case: SelfCheckCase,
    messages: list[dict],
    route: dict | None,
    *,
    policy_fingerprint_value: str | None = None,
) -> str:
    prompt = load_prompt()
    fingerprint = policy_fingerprint_value if policy_fingerprint_value is not None else (try_policy_fingerprint() or "")
    return call_key(
        run_id="plan-selfcheck",
        input_revision=1,
        invocation_id=case.case_id,
        candidate_hash=input_hash(messages),
        template_hash=prompt.content_hash,
        route=route,
        policy=TriggerPolicy(),
        policy_fingerprint_value=fingerprint,
    )


_GOLD_KEYS = frozenset({"label_pass", "caught_by", "notes"})


def _collect_keys(value, found: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _GOLD_KEYS:
                found.append(str(key))
            _collect_keys(child, found)
    elif isinstance(value, list):
        for item in value:
            _collect_keys(item, found)


def assert_prompt_has_no_labels(messages: list[dict]) -> None:
    """Gold must not be injected as DATA keys. User prose may mention the same words."""

    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        found: list[str] = []
        _collect_keys(payload, found)
        if found:
            raise ValueError(f"self-check prompt contains gold field {found[0]}")


def parse_provider_raw(raw: dict | None, *, unknown: bool = False) -> tuple[bool | None, str | None]:
    """Strict runtime parser. A missing or unknown payload is not a pass."""

    if unknown:
        return None, "unknown"
    if not isinstance(raw, dict):
        return None, "missing_raw"
    try:
        verdict = parse_verdict(content_from_raw(raw))
    except (ValueError, TypeError) as exc:
        return None, str(exc) or type(exc).__name__
    return verdict.pass_, None


def _blank_observation(
    case: SelfCheckCase,
    messages: list[dict],
    *,
    mode: str,
    route: dict | None,
    limits: StageLimits | None = None,
) -> dict:
    prompt = load_prompt()
    current = limits or StageLimits()
    fingerprint = try_policy_fingerprint(effective_settings(current))
    return {
        "schema": OBSERVATION_SCHEMA,
        "case_id": case.case_id,
        "mode": mode,
        "status": "sending",
        "provenance": "synthetic",
        "provider_kind": "none",
        "llm_tested": False,
        "input_hash": input_hash(messages),
        "call_key": case_call_key(case, messages, route, policy_fingerprint_value=fingerprint or ""),
        "template_version": prompt.version,
        "template_hash": prompt.content_hash,
        "policy_hash": POLICY_HASH,
        "policy_fingerprint": fingerprint,
        "dataset_hash": dataset_hash(),
        "route": public_route(route),
        "model": (public_route(route).get("model") if route else None),
        "limits": quality_limits(current),
        "provider_raw": None,
        "observed_pass": None,
        "fallback": None,
        "legal_type_pass": None,
        "review_reasons": [],
        "latency_ms": None,
        "usage": {},
        "reservation": None,
        "expected_used_as_observation": False,
        "transport": None,
    }


def _persist(path: Path, record: dict) -> None:
    """Atomic replace. A crash during write leaves the previous file intact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _usage_tokens(usage: dict | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, bool) or not isinstance(total, int):
        return None
    return total


def rule_observation(case: SelfCheckCase, *, limits: StageLimits | None = None) -> dict:
    started = time.perf_counter()
    result, view = review_case(case)
    messages, _summary = trusted_messages(case, view, limits=limits)
    assert_prompt_has_no_labels(messages)
    legal = legal_type_pass(result)
    record = _blank_observation(case, messages, mode="rules", route=None, limits=limits)
    record.update({
        "status": "rule_only",
        "provider_kind": "rules",
        "legal_type_pass": legal,
        "review_reasons": [reason.model_dump(mode="json") for reason in result.reasons],
        "observed_pass": (not intent_reject(result)) if legal else None,
        "fallback": None if legal else "illegal_or_mistyped",
        "latency_ms": (time.perf_counter() - started) * 1000,
        "usage": {},
    })
    return record


def offline_observation(case: SelfCheckCase, *, limits: StageLimits | None = None) -> dict:
    result, view = review_case(case)
    messages, _summary = trusted_messages(case, view, limits=limits)
    assert_prompt_has_no_labels(messages)
    legal = legal_type_pass(result)
    record = _blank_observation(case, messages, mode="offline", route=None, limits=limits)
    record.update({
        "status": "skipped",
        "provider_kind": "none",
        "legal_type_pass": legal,
        "review_reasons": [reason.model_dump(mode="json") for reason in result.reasons],
        "observed_pass": None,
        "fallback": "offline_preflight",
        "latency_ms": None,
        "usage": {},
    })
    return record


def scripted_observation(case: SelfCheckCase, raw: dict, *, latency_ms: float, usage: dict | None = None) -> dict:
    """Protocol fixture. The caller supplies raw. This function does not read label_pass."""

    result, view = review_case(case)
    messages, _summary = trusted_messages(case, view)
    assert_prompt_has_no_labels(messages)
    record = _blank_observation(case, messages, mode="protocol", route=None)
    record["provider_raw"] = raw
    record["status"] = "stored"
    record["provider_kind"] = "fake"
    record["provenance"] = "synthetic"
    record["latency_ms"] = latency_ms
    raw_usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    record["usage"] = dict(usage or raw_usage)
    record["legal_type_pass"] = legal_type_pass(result)
    record["review_reasons"] = [reason.model_dump(mode="json") for reason in result.reasons]
    observed, fallback = parse_provider_raw(raw)
    record["observed_pass"] = observed
    record["fallback"] = fallback
    record["status"] = "parsed" if fallback is None else "unknown"
    return record


def _output_byte_cap(record: dict) -> int:
    limits = record.get("limits")
    if isinstance(limits, dict):
        value = limits.get("max_output_bytes")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return StageLimits().max_output_bytes


def rule_intent_reject(record: dict) -> bool:
    reasons = record.get("review_reasons") or []
    return any(isinstance(item, dict) and item.get("gate") == "intent" for item in reasons)


def grade_record(record: dict, *, label_pass: bool) -> dict:
    """Score stored bytes. Live and protocol verdicts are reparsed from raw."""

    if record.get("expected_used_as_observation") is True:
        raise GradeRejected("expected_used_as_observation")
    mode = record.get("mode")
    provenance = record.get("provenance")
    legal = record.get("legal_type_pass") is True
    raw = record.get("provider_raw")
    live = provenance == "live" or mode == "llm"
    if live or mode == "protocol":
        if not isinstance(raw, dict):
            observed, fallback = None, record.get("fallback") or "missing_raw"
        else:
            observed, fallback = parse_provider_raw(raw)
            cap = _output_byte_cap(record)
            if len(content_from_raw(raw).encode("utf-8")) > cap:
                observed, fallback = None, "output_too_large"
    elif mode == "rules":
        observed = record.get("observed_pass")
        fallback = record.get("fallback")
        if observed is not None and not isinstance(observed, bool):
            observed, fallback = None, fallback or "non_boolean"
    else:
        observed, fallback = None, record.get("fallback") or "unsupported_mode"
    if fallback:
        observed = None
    if not legal:
        observed = None
        fallback = fallback or "illegal_or_mistyped"
    tokens = _usage_tokens(record.get("usage")) if isinstance(raw, dict) or mode == "rules" else None
    usage_known = tokens is not None or mode in {"rules", "offline"}
    if live and isinstance(raw, dict) and tokens is None and not fallback:
        usage_known = False
    rejected = observed is False
    valid = observed is not None and fallback is None and legal
    latency = record.get("latency_ms")
    return {
        "case_id": record.get("case_id"),
        "label_pass": label_pass,
        "provenance": provenance,
        "provider_kind": record.get("provider_kind"),
        "observed_pass": observed,
        "fallback": fallback,
        "legal_type_pass": legal,
        "valid": valid,
        "rejected": rejected if valid else None,
        "rule_rejected": rule_intent_reject(record) if legal else None,
        "latency_ms": latency if isinstance(latency, (int, float)) else None,
        "tokens": tokens,
        "usage_known": bool(usage_known) if valid else tokens is not None,
    }


def _rates(grades: list[dict], predicate) -> tuple[float | None, float | None, int, int]:
    drift = [item for item in grades if item["label_pass"] is False and predicate(item)]
    normal = [item for item in grades if item["label_pass"] is True and predicate(item)]
    tpr = (sum(1 for item in drift if item["hit"]) / len(drift)) if drift else None
    fpr = (sum(1 for item in normal if item["hit"]) / len(normal)) if normal else None
    return tpr, fpr, len(drift), len(normal)


def comparison_from_grades(grades: list[dict]) -> dict:
    """Rule catch comes from saved intent reasons. Model catch is parsed raw."""

    ruled = [item for item in grades if item.get("rule_rejected") is not None]
    rule_rows = [{**item, "hit": item["rule_rejected"] is True} for item in ruled]
    model_rows = [
        {**item, "hit": item["rejected"] is True}
        for item in grades
        if item["valid"]
    ]
    rule_tpr, rule_fpr, rule_drift, rule_normal = _rates(rule_rows, lambda item: True)
    model_tpr, model_fpr, model_drift, _model_normal = _rates(model_rows, lambda item: True)
    combined_rows = []
    additional = 0
    rule_caught = sum(1 for item in ruled if item["label_pass"] is False and item["rule_rejected"])
    for item in grades:
        if not item["valid"] or item.get("rule_rejected") is None:
            continue
        hit = item["rule_rejected"] is True or item["rejected"] is True
        combined_rows.append({**item, "hit": hit})
        if item["label_pass"] is False and item["rule_rejected"] is False and item["rejected"] is True:
            additional += 1
    _valid_only_tpr, _valid_only_fpr, _, _ = _rates(combined_rows, lambda item: True)
    operational = []
    for item in grades:
        if item.get("rule_rejected") is None:
            continue
        # Model fallback keeps the rule decision. A valid model rejection also counts.
        hit = item["rule_rejected"] is True or (item["valid"] and item["rejected"] is True)
        operational.append({**item, "hit": hit})
    combined_tpr, combined_fpr, combined_drift, combined_normal = _rates(operational, lambda item: True)
    unknown_usage = sum(
        1 for item in grades
        if item.get("provenance") == "live" and item.get("usage_known") is False
    )
    del _valid_only_tpr, _valid_only_fpr
    return {
        "ruleCaught": rule_caught,
        "ruleDrift": rule_drift,
        "ruleNormal": rule_normal,
        "ruleTpr": rule_tpr,
        "ruleFpr": rule_fpr,
        "modelAdditional": additional,
        "modelDrift": model_drift,
        "modelTpr": model_tpr,
        "modelFpr": model_fpr,
        "modelBasis": "valid_only",
        "combinedTpr": combined_tpr,
        "combinedFpr": combined_fpr,
        "combinedDrift": combined_drift,
        "combinedNormal": combined_normal,
        "combinedBasis": "rules_when_model_fallback",
        "unknownUsage": unknown_usage,
        "valid": sum(1 for item in grades if item["valid"]),
        "total": len(grades),
        "fallback": sum(1 for item in grades if item.get("fallback")),
    }


def _rate_block(
    grades: list[dict],
    *,
    include_latency: bool,
    rejected_key: str = "rejected",
    population: str = "valid",
) -> RateBlock:
    """`rule` uses every case with a reviewer decision. `model` rates use valid parses only."""

    drift_total = sum(1 for item in grades if item["label_pass"] is False)
    normal_total = sum(1 for item in grades if item["label_pass"] is True)
    if population == "rule":
        eligible = [item for item in grades if item.get("rule_rejected") is not None]
        drift_valid = [item for item in eligible if item["label_pass"] is False]
        normal_valid = [item for item in eligible if item["label_pass"] is True]
        caught = sum(1 for item in drift_valid if item.get("rule_rejected") is True)
        blocked = sum(1 for item in normal_valid if item.get("rule_rejected") is True)
        missing = [item for item in grades if item.get("rule_rejected") is None]
        fallbacks: Counter[str] = Counter(item.get("fallback") or "no_rule_decision" for item in missing)
        latency_rows = eligible
        token_rows: list[dict] = []
    else:
        drift_valid = [item for item in grades if item["valid"] and item["label_pass"] is False]
        normal_valid = [item for item in grades if item["valid"] and item["label_pass"] is True]
        caught = sum(1 for item in drift_valid if item.get(rejected_key))
        blocked = sum(1 for item in normal_valid if item.get(rejected_key))
        fallbacks = Counter(item["fallback"] for item in grades if item.get("fallback"))
        latency_rows = grades if population == "model" else [item for item in grades if item["valid"]]
        token_rows = grades if population == "model" else [item for item in grades if item["valid"]]
    latencies = [
        float(item["latency_ms"])
        for item in latency_rows
        if isinstance(item.get("latency_ms"), (int, float))
    ]
    tokens = [item["tokens"] for item in token_rows if isinstance(item.get("tokens"), int)]
    return RateBlock(
        tpr=(caught / len(drift_valid)) if drift_valid else None,
        fpr=(blocked / len(normal_valid)) if normal_valid else None,
        p50_ms=_percentile(latencies, 0.5) if include_latency and latencies else None,
        p95_ms=_percentile(latencies, 0.95) if include_latency and latencies else None,
        known_tokens=sum(tokens) if tokens else None,
        known_usage_count=len(tokens) if population == "model" else None,
        counts=ClassCounts(
            total=len(grades),
            valid=len(drift_valid) + len(normal_valid),
            drift_total=drift_total,
            drift_valid=len(drift_valid),
            normal_total=normal_total,
            normal_valid=len(normal_valid),
            fallback=sum(fallbacks.values()),
        ),
        fallback_counts=dict(fallbacks),
    )


def _live_model_grades(grades: list[dict]) -> bool:
    """Scripted and fake rows are not partial live quality, even in llm mode."""

    if not grades:
        return False
    return all(
        item.get("provenance") == "live" and item.get("provider_kind") == "chat"
        for item in grades
    )


def summarize_grades(
    grades: list[dict],
    *,
    mode: str,
    llm_tested: bool,
    reason: str,
    limits: StageLimits | None = None,
    cases: list[SelfCheckCase] | None = None,
) -> SelfCheckSummary:
    prompt = load_prompt()
    current = limits or StageLimits()
    from backend.evaluation.selfcheck_cases import FROZEN_DATASET_SHA256
    legal = sum(1 for item in grades if item["legal_type_pass"])
    valid = sum(1 for item in grades if item["valid"])
    total = len(grades)
    rule = _rate_block(grades, include_latency=mode == "rules", population="rule")
    protocol = _rate_block(grades, include_latency=True) if mode == "protocol" else None
    live_partial = mode == "llm" and _live_model_grades(grades)
    model = _rate_block(grades, include_latency=True, population="model") if live_partial else None
    compared = comparison_from_grades(grades)
    if not live_partial:
        compared["modelAdditional"] = None
        compared["modelTpr"] = None
        compared["modelFpr"] = None
        compared["combinedTpr"] = None
        compared["combinedFpr"] = None
        compared["combinedDrift"] = None
        compared["combinedNormal"] = None
    return SelfCheckSummary(
        mode=mode,
        dataset_hash=dataset_hash(cases),
        template_version=TEMPLATE_VERSION,
        template_hash=prompt.content_hash,
        policy_hash=POLICY_HASH,
        policy_fingerprint=try_policy_fingerprint(effective_settings(current)),
        policy_snapshot=policy_snapshot(effective_settings(current)),
        fixture_sha256=FROZEN_DATASET_SHA256,
        llm_tested=llm_tested,
        llm_untested_reason=reason,
        enforce_eligible=False,
        model=model,
        rule=rule,
        protocol=protocol,
        comparison=compared,
        unknown_usage=compared["unknownUsage"],
        legal_type_pass=legal,
        legal_type_total=total,
        valid_observation_rate=(valid / total) if total else 0.0,
        fallback_count=sum(1 for item in grades if item.get("fallback")),
    )


def evaluate_rules(
    cases: list[SelfCheckCase] | None = None,
    *,
    limits: StageLimits | None = None,
) -> tuple[list[dict], SelfCheckSummary]:
    rows = cases or build_cases()
    current = limits or StageLimits()
    records = [rule_observation(case, limits=current) for case in rows]
    _stamp_dataset(records, rows)
    grades = [grade_record(record, label_pass=case.label_pass) for record, case in zip(records, rows, strict=True)]
    summary = summarize_grades(
        grades,
        mode="rules",
        llm_tested=False,
        reason="规则基线来自 review.validate 的意图门，不是模型质量。",
        limits=current,
        cases=rows,
    )
    return records, summary


def evaluate_offline(
    cases: list[SelfCheckCase] | None = None,
    *,
    limits: StageLimits | None = None,
) -> tuple[list[dict], SelfCheckSummary]:
    rows = cases or build_cases()
    current = limits or StageLimits()
    records = [offline_observation(case, limits=current) for case in rows]
    _stamp_dataset(records, rows)
    grades = [grade_record(record, label_pass=case.label_pass) for record, case in zip(records, rows, strict=True)]
    summary = summarize_grades(
        grades,
        mode="offline",
        llm_tested=False,
        reason="离线预检只核对冻结集、合法性与提示词，不产生模型观测。",
        limits=current,
        cases=rows,
    )
    return records, summary


def grade_stored(
    records: list[dict],
    cases: list[SelfCheckCase],
    *,
    mode: str,
    llm_tested: bool,
    reason: str,
    limits: StageLimits | None = None,
):
    by_id = {case.case_id: case for case in cases}
    grades = []
    for record in records:
        case = by_id.get(record.get("case_id"))
        if case is None:
            raise ValueError(f"observation case_id 不在数据集中: {record.get('case_id')}")
        grades.append(grade_record(record, label_pass=case.label_pass))
    return grades, summarize_grades(grades, mode=mode, llm_tested=llm_tested, reason=reason, limits=limits, cases=cases)


class EvidenceRejected(ValueError):
    pass


class GradeRejected(ValueError):
    pass


class IdentityMismatch(ValueError):
    pass


class PolicyFingerprintUnavailable(RuntimeError):
    """Runtime has not exported policy_fingerprint(Settings) into this worktree."""


_POLICY_SNAPSHOT_EXCLUDED = {
    "selfcheck_enabled",
    "selfcheck_mode",
    "selfcheck_max_false_block_rate",
    "selfcheck_expected_dataset_hash",
    "selfcheck_evidence_path",
}


def policy_snapshot(settings=None) -> dict:
    """Behavior knobs only. Enable, mode, target, and evidence path stay out."""

    from backend.core.supervisor.contracts import Settings

    current = settings if settings is not None else Settings()
    raw = current.model_dump(mode="json")
    return {
        key: raw[key]
        for key in sorted(raw)
        if key.startswith("selfcheck_") and key not in _POLICY_SNAPSHOT_EXCLUDED
    }


def load_policy_fingerprint(settings=None):
    """Lazy import. Missing runtime helper is an error, not a local hash."""

    import importlib

    module = importlib.import_module("backend.core.selfcheck.policy")
    fingerprint = getattr(module, "policy_fingerprint", None)
    if not callable(fingerprint):
        raise PolicyFingerprintUnavailable(
            "backend.core.selfcheck.policy.policy_fingerprint(Settings) 尚未出现在本工作树。"
            "评测不发明替代指纹。"
        )
    from backend.core.supervisor.contracts import Settings

    return fingerprint(settings if settings is not None else Settings())


def try_policy_fingerprint(settings=None):
    try:
        return load_policy_fingerprint(settings)
    except PolicyFingerprintUnavailable:
        return None


def effective_settings(limits: StageLimits | None = None):
    """Behavior knobs that policy_fingerprint hashes. Budget and concurrency stay out."""

    from backend.core.supervisor.contracts import Settings

    current = limits or StageLimits()
    return Settings().model_copy(update={
        "selfcheck_timeout_seconds": current.timeout_seconds,
        "selfcheck_max_input_bytes": current.max_context_bytes,
        "selfcheck_max_output_bytes": current.max_output_bytes,
        "selfcheck_max_param_chars": current.max_param_chars,
    })


def quality_limits(limits: StageLimits) -> dict:
    """Model-quality caps. token_budget and concurrency stay on the run manifest."""

    data = limits.model_dump(mode="json")
    return {
        key: data[key]
        for key in ("output_cap", "max_context_bytes", "timeout_seconds", "max_output_bytes", "max_param_chars")
    }


def _stamp_dataset(records: list[dict], cases: list[SelfCheckCase]) -> None:
    digest = dataset_hash(cases)
    for record in records:
        record["dataset_hash"] = digest


def route_output_cap(route: dict | None) -> int | None:
    if not isinstance(route, dict):
        return None
    for key in ("maxTokens", "max_tokens"):
        value = route.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def assert_route_matches_limits(route: dict, limits: StageLimits) -> tuple[dict, StageLimits]:
    """Outer caps may be stricter. The returned route is the provider route, unchanged.

    A requested output cap below the SDK maxTokens would be a lie, because the provider
    still sends the route cap. A larger request is capped down to that route cap.
    """

    live = public_route(route)
    cap = route_output_cap(live)
    timeout = live.get("timeoutSeconds", live.get("timeout_s"))
    context = live.get("contextMaxBytes", live.get("context_max_bytes"))
    if not live.get("model") or cap is None or timeout is None or context is None:
        raise IdentityMismatch("route_identity_incomplete")
    if float(limits.timeout_seconds) > float(timeout):
        raise IdentityMismatch("timeout_exceeds_route")
    if int(limits.max_context_bytes) > int(context):
        raise IdentityMismatch("context_exceeds_route")
    if int(limits.output_cap) < int(cap):
        raise IdentityMismatch("output_cap_below_route")
    effective = limits.model_copy(update={"output_cap": int(cap)})
    return live, effective


def canonical_identity(case: SelfCheckCase, route: dict | None, limits: StageLimits) -> dict:
    _result, view = review_case(case)
    messages, _summary = trusted_messages(case, view, limits=limits)
    prompt = load_prompt()
    fingerprint = try_policy_fingerprint(effective_settings(limits))
    return {
        "case_id": case.case_id,
        "input_hash": input_hash(messages),
        "call_key": case_call_key(case, messages, route, policy_fingerprint_value=fingerprint or ""),
        "template_hash": prompt.content_hash,
        "template_version": prompt.version,
        "policy_hash": POLICY_HASH,
        "policy_fingerprint": fingerprint,
        "dataset_hash": dataset_hash(),
        "route": public_route(route),
        "limits": quality_limits(limits),
    }


def assert_record_identity(record: dict, case: SelfCheckCase, route: dict, limits: StageLimits) -> None:
    expected = canonical_identity(case, route, limits)
    for key in ("case_id", "input_hash", "call_key", "template_hash", "template_version", "policy_hash", "dataset_hash"):
        if record.get(key) != expected[key]:
            raise IdentityMismatch(key)
    if public_route(record.get("route")) != expected["route"]:
        raise IdentityMismatch("route")
    if record.get("limits") != expected["limits"]:
        raise IdentityMismatch("limits")
    if expected["policy_fingerprint"] is None:
        raise PolicyFingerprintUnavailable("policy_fingerprint")
    if record.get("policy_fingerprint") != expected["policy_fingerprint"]:
        raise IdentityMismatch("policy_fingerprint")


def genuine_observation(record: dict) -> bool:
    """True only after observe_llm_case stores a provider completion. Scripted raw is not enough."""

    return record.get("transport") == "provider_complete"


def export_evidence(
    records: list[dict],
    cases: list[SelfCheckCase],
    *,
    route: dict,
    limits: StageLimits | None = None,
    verifier=None,
) -> EnforcementEvidence:
    """Live evidence only. policy_hash on the artifact is the effective fingerprint."""

    by_id = {case.case_id: case for case in cases}
    seen: dict[str, dict] = {}
    for record in records:
        case_id = record.get("case_id")
        if case_id not in by_id:
            raise EvidenceRejected("unknown_case")
        if case_id in seen:
            raise EvidenceRejected("duplicate_observation")
        seen[case_id] = record
    if set(seen) != set(by_id):
        raise EvidenceRejected("case_set_mismatch")
    if limits is None:
        cap = route_output_cap(route)
        if cap is None:
            raise IdentityMismatch("route_identity_incomplete")
        limits = StageLimits(output_cap=cap)
    live, limits = assert_route_matches_limits(route, limits)
    fingerprint = try_policy_fingerprint(effective_settings(limits))
    if fingerprint is None:
        raise PolicyFingerprintUnavailable("policy_fingerprint")
    if len(cases) != 80 or dataset_hash(cases) != dataset_hash():
        raise EvidenceRejected("dataset_not_canonical")
    check = verifier if verifier is not None else genuine_observation
    grades = []
    for case_id, record in seen.items():
        case = by_id[case_id]
        if record.get("expected_used_as_observation") is True:
            raise EvidenceRejected("label_copied")
        if record.get("provenance") != "live" or record.get("llm_tested") is not True:
            raise EvidenceRejected("synthetic_or_untested")
        if record.get("provider_kind") != "chat":
            raise EvidenceRejected("synthetic_provider")
        if record.get("dataset_hash") != dataset_hash():
            raise EvidenceRejected("dataset_hash")
        if not check(record):
            raise EvidenceRejected("not_genuine")
        assert_record_identity(record, case, route, limits)
        graded = grade_record(record, label_pass=case.label_pass)
        if not graded["valid"] or graded["usage_known"] is False:
            raise EvidenceRejected("incomplete_observations")
        grades.append(graded)
    drift_count = sum(1 for item in grades if item["label_pass"] is False)
    normal_count = sum(1 for item in grades if item["label_pass"] is True)
    summary = summarize_grades(grades, mode="llm", llm_tested=True, reason="", limits=limits, cases=cases)
    if summary.model is None or summary.model.fpr is None or summary.fallback_count != 0:
        raise EvidenceRejected("incomplete_observations")
    if summary.valid_observation_rate != 1.0 or len(grades) != len(by_id):
        raise EvidenceRejected("incomplete_observations")
    return EnforcementEvidence(
        provenance="live",
        route=live,
        route_id=str(live.get("id") or ""),
        model=str(live["model"]),
        template_version=TEMPLATE_VERSION,
        template_hash=summary.template_hash,
        policy_hash=fingerprint,
        dataset_hash=dataset_hash(),
        drift_count=drift_count,
        normal_count=normal_count,
        valid_observation_rate=1.0,
        fallback_count=0,
        observed_false_block_rate=summary.model.fpr,
        case_count=len(grades),
        llm_tested=True,
    )


def evidence_document(evidence: EnforcementEvidence) -> dict:
    """Exact EnforcementEvidence dump. Eligibility and the snapshot stay off this file."""

    return evidence.model_dump(mode="json")


def load_observation(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def resume_decision(prior: dict | None, expected: dict | None = None) -> str:
    """Reuse only a matching identity. sending/unknown never starts another call."""

    if not isinstance(prior, dict):
        return "send"
    if expected is not None:
        for key, value in expected.items():
            if prior.get(key) != value:
                return "mismatch"
    if isinstance(prior.get("provider_raw"), dict):
        return "reuse"
    if prior.get("status") in {"sending", "unknown"} or prior.get("fallback") in {"unknown", "timeout", "blocked"}:
        return "unknown"
    if expected is None and prior.get("mode") in {"rules", "offline"} and prior.get("provider_raw") is None:
        return "reuse"
    if prior.get("mode") in {"rules", "offline"} and prior.get("status") in {"rule_only", "skipped"} and expected is not None:
        return "reuse" if prior.get("mode") == expected.get("mode") else "mismatch"
    return "mismatch" if expected is not None else "send"


def run_manifest(cases: list[SelfCheckCase], *, mode: str, route: dict | None, limits: StageLimits) -> dict:
    prompt = load_prompt()
    from backend.evaluation.selfcheck_cases import FROZEN_DATASET_SHA256

    settings = effective_settings(limits)
    return {
        "schema": "plan-selfcheck-run-v1",
        "mode": mode,
        "dataset_hash": dataset_hash(cases),
        "fixture_sha256": FROZEN_DATASET_SHA256,
        "template_version": prompt.version,
        "template_hash": prompt.content_hash,
        "policy_hash": POLICY_HASH,
        "policy_fingerprint": try_policy_fingerprint(settings),
        "policy_snapshot": policy_snapshot(settings),
        "route": public_route(route),
        "limits": limits.model_dump(mode="json"),
        "quality_limits": quality_limits(limits),
        "case_ids": [case.case_id for case in cases],
    }


def open_run_directory(output: Path, manifest: dict, *, resume: bool) -> None:
    output.mkdir(parents=True, exist_ok=True)
    path = output / "run-manifest.json"
    occupied = any(item.name != path.name for item in output.iterdir()) or path.is_file()
    if not resume:
        if occupied:
            raise IdentityMismatch("output_exists")
        _persist(path, manifest)
        return
    if not path.is_file():
        raise IdentityMismatch("resume_manifest_missing")
    stored = json.loads(path.read_text(encoding="utf-8"))
    if stored != manifest:
        raise IdentityMismatch("resume_manifest_mismatch")


def reservation_bound(messages: list[dict], limits: StageLimits) -> int:
    """Upper bound held before the provider returns: request bytes + route output cap + overhead."""

    request_bytes = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    return request_bytes + int(limits.output_cap) + int(limits.protocol_overhead)


class CallBudget:
    """Reserve the known upper bound before await. Settle exact usage, or keep the bound."""

    def __init__(self, limits: StageLimits):
        self.limits = limits
        self.calls = 0
        self.tokens = 0
        self.unknown_usage = 0
        self._lock = asyncio.Lock()

    async def note_resumed(self, record: dict) -> None:
        async with self._lock:
            self.calls += 1
            settled = record.get("reservation_settled")
            tokens = _usage_tokens(record.get("usage"))
            if isinstance(settled, int) and not isinstance(settled, bool):
                self.tokens += settled
                return
            reservation = record.get("reservation")
            if isinstance(reservation, int) and not isinstance(reservation, bool):
                self.tokens += reservation
                if isinstance(record.get("provider_raw"), dict) and tokens is None:
                    self.unknown_usage += 1
                return
            if isinstance(record.get("provider_raw"), dict) and tokens is None:
                self.unknown_usage += 1
            elif tokens:
                self.tokens += tokens

    async def reserve(self, upper: int) -> bool:
        async with self._lock:
            if upper < 0 or self.calls >= self.limits.max_calls or self.tokens + upper > self.limits.token_budget:
                return False
            self.calls += 1
            self.tokens += upper
            return True

    async def settle(self, reservation: int, usage: dict | None) -> int | None:
        """Replace the held bound with exact tokens. Unknown usage keeps the bound."""

        async with self._lock:
            self.tokens -= reservation
            tokens = _usage_tokens(usage)
            if tokens is None:
                self.tokens += reservation
                self.unknown_usage += 1
                return None
            self.tokens += tokens
            return tokens


def _identity_fields(case: SelfCheckCase, messages: list[dict], route: dict, limits: StageLimits) -> dict:
    prompt = load_prompt()
    fingerprint = try_policy_fingerprint(effective_settings(limits))
    return {
        "case_id": case.case_id,
        "input_hash": input_hash(messages),
        "call_key": case_call_key(case, messages, route, policy_fingerprint_value=fingerprint or ""),
        "template_hash": prompt.content_hash,
        "template_version": prompt.version,
        "policy_hash": POLICY_HASH,
        "policy_fingerprint": fingerprint,
        "dataset_hash": dataset_hash(),
        "route": public_route(route),
        "limits": quality_limits(limits),
        "mode": "llm",
    }


async def observe_llm_case(
    case: SelfCheckCase,
    provider,
    *,
    route: dict,
    limits: StageLimits,
    destination: Path,
    budget: CallBudget,
) -> dict:
    """Register the call before await. One attempt. Stored raw is never re-sent."""

    if limits.retries != 0:
        raise ValueError("retries_unsupported")
    live, limits = assert_route_matches_limits(route, limits)
    if try_policy_fingerprint(effective_settings(limits)) is None:
        raise PolicyFingerprintUnavailable("policy_fingerprint")
    result, view = review_case(case)
    messages, summary = trusted_messages(case, view, limits=limits)
    assert_prompt_has_no_labels(messages)
    expected = _identity_fields(case, messages, live, limits)
    prior = load_observation(destination)
    decision = resume_decision(prior, expected)
    if decision == "mismatch":
        raise IdentityMismatch(case.case_id)
    if decision in {"reuse", "unknown"}:
        await budget.note_resumed(prior)
        return prior
    record = _blank_observation(case, messages, mode="llm", route=live, limits=limits)
    record.update(expected)
    record["legal_type_pass"] = legal_type_pass(result)
    record["review_reasons"] = [reason.model_dump(mode="json") for reason in result.reasons]
    record["provenance"] = "live"
    record["provider_kind"] = getattr(provider, "kind", "chat")
    record["status"] = "sending"
    record["llm_tested"] = False
    record["transport"] = None
    encoded = json.dumps(messages, ensure_ascii=False).encode()
    upper = reservation_bound(messages, limits)
    record["reservation"] = upper
    if summary.get("degraded") or len(encoded) > limits.max_context_bytes:
        record["status"] = "unknown"
        record["fallback"] = "context_limit"
        record["reservation"] = 0
        _persist(destination, record)
        return record
    if not await budget.reserve(upper):
        record["status"] = "unknown"
        record["fallback"] = "budget_exhausted"
        record["reservation"] = upper
        _persist(destination, record)
        return record
    _persist(destination, record)

    async def sink(raw: dict) -> None:
        record["provider_raw"] = raw
        record["status"] = "stored"
        record["usage"] = raw.get("usage") if isinstance(raw.get("usage"), dict) else {"usage_known": False}
        _persist(destination, record)

    started = time.perf_counter()
    try:
        completed = await asyncio.wait_for(
            provider.complete(messages, response_sink=sink, timeout=limits.timeout_seconds),
            timeout=limits.timeout_seconds,
        )
    except TimeoutError:
        record["status"] = "unknown"
        record["fallback"] = "timeout"
        record["latency_ms"] = (time.perf_counter() - started) * 1000
        record["reservation_settled"] = await budget.settle(upper, None)
        _persist(destination, record)
        return record
    except Exception as exc:
        unknown = bool(getattr(exc, "unknown", False)) or getattr(exc, "code", "") in {
            "MODEL_RESULT_UNKNOWN", "CONTENT_BLOCKED", "MODEL_CONTENT_BLOCKED",
        }
        blocked = getattr(exc, "code", "") in {"CONTENT_BLOCKED", "MODEL_CONTENT_BLOCKED"}
        raw = getattr(exc, "raw_response", None)
        if isinstance(raw, dict) and not isinstance(record.get("provider_raw"), dict):
            await sink(raw)
        record["status"] = "unknown"
        record["fallback"] = "blocked" if blocked else ("unknown" if unknown else type(exc).__name__)
        record["latency_ms"] = (time.perf_counter() - started) * 1000
        record["llm_tested"] = False
        record["reservation_settled"] = await budget.settle(upper, record.get("usage") if isinstance(record.get("usage"), dict) else None)
        _persist(destination, record)
        return record
    record["latency_ms"] = (time.perf_counter() - started) * 1000
    if isinstance(getattr(completed, "raw", None), dict) and not isinstance(record.get("provider_raw"), dict):
        await sink(completed.raw)
    usage = getattr(completed, "usage", None)
    if isinstance(usage, dict):
        record["usage"] = usage
    settled = await budget.settle(upper, record.get("usage") if isinstance(record.get("usage"), dict) else None)
    record["reservation_settled"] = settled if settled is not None else upper
    raw = record.get("provider_raw") if isinstance(record.get("provider_raw"), dict) else None
    content = content_from_raw(raw)
    if len(content.encode("utf-8")) > limits.max_output_bytes:
        record["observed_pass"] = None
        record["fallback"] = "output_too_large"
        record["status"] = "unknown"
        record["llm_tested"] = False
        record["transport"] = None
        _persist(destination, record)
        return record
    observed, fallback = parse_provider_raw(raw)
    record["observed_pass"] = observed
    record["fallback"] = fallback
    record["status"] = "parsed" if fallback is None else "unknown"
    record["llm_tested"] = fallback is None and isinstance(raw, dict)
    record["transport"] = "provider_complete" if record["llm_tested"] else None
    _persist(destination, record)
    return record


async def observe_many(cases, provider, *, route: dict, limits: StageLimits, directory: Path, budget: CallBudget):
    """Bounded concurrency. The semaphore wraps the provider call, not just the schedule."""

    semaphore = asyncio.Semaphore(limits.concurrency)

    async def one(case: SelfCheckCase):
        async with semaphore:
            return await observe_llm_case(
                case,
                provider,
                route=route,
                limits=limits,
                destination=directory / f"{case.case_id}.json",
                budget=budget,
            )

    return list(await asyncio.gather(*(one(case) for case in cases)))
