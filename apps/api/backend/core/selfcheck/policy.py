"""Trigger, enforcement, and JSON parse. Rules stay authoritative on any model failure."""

from __future__ import annotations

import hashlib
import json
from typing import Any


from backend.core.review.contracts import Plan, PlanView, ReviewReason
from backend.core.review.policy import intent_reason
from backend.core.supervisor.contracts import Settings

from .contracts import (
    EnforcementEvidence,
    EnforcementMode,
    POLICY_HASH,
    SCHEMA_VERSION,
    SemanticJournal,
    SemanticVerdict,
    TriggerPolicy,
)

ROUTE_IDENTITY_FIELDS = (
    "id", "label", "provider", "model", "baseUrl", "base_url",
    "timeoutSeconds", "timeout_s", "maxTokens", "max_tokens",
    "contextMaxBytes", "context_max_bytes", "personaId", "persona_id",
)


def content_from_raw(raw: dict | None) -> str:
    if not isinstance(raw, dict):
        return ""
    choices = raw.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    return content if isinstance(content, str) else ""


_FINGERPRINT_FIELDS = (
    "selfcheck_video_anywhere",
    "selfcheck_min_steps",
    "selfcheck_min_depth",
    "selfcheck_cost_threshold",
    "selfcheck_helper_marker",
    "selfcheck_timeout_seconds",
    "selfcheck_max_input_bytes",
    "selfcheck_max_output_bytes",
    "selfcheck_max_param_chars",
    "selfcheck_template_version",
    "selfcheck_checkpoint_margin_seconds",
)


def policy_fingerprint(settings: Settings) -> str:
    """Canonical hash of checker behavior. Enable/mode/FPR/evidence path stay out so a baseline can later enforce."""

    payload = {
        "schema": POLICY_HASH,
        "schema_version": SCHEMA_VERSION,
        **{name: getattr(settings, name) for name in _FINGERPRINT_FIELDS},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def trigger_policy_from_settings(settings: Settings) -> TriggerPolicy:
    return TriggerPolicy(
        video_anywhere=settings.selfcheck_video_anywhere,
        min_steps=settings.selfcheck_min_steps,
        min_depth=settings.selfcheck_min_depth,
        cost_threshold=settings.selfcheck_cost_threshold,
        helper_marker=settings.selfcheck_helper_marker,
    )


def public_route(route: dict | None) -> dict:
    if not isinstance(route, dict):
        return {}
    out = {}
    for key in ROUTE_IDENTITY_FIELDS:
        if key in route and route[key] is not None:
            out[key] = route[key]
    out.pop("apiKey", None)
    out.pop("credentialRef", None)
    out.pop("credential_ref", None)
    return out


def call_key(
    *,
    run_id: str,
    input_revision: int,
    invocation_id: str,
    candidate_hash: str,
    template_hash: str,
    route: dict | None,
    policy: TriggerPolicy,
    policy_fingerprint_value: str = "",
) -> str:
    payload = {
        "run_id": run_id,
        "input_revision": input_revision,
        "invocation_id": invocation_id,
        "candidate_hash": candidate_hash,
        "template_hash": template_hash,
        "policy_hash": POLICY_HASH,
        "policy_fingerprint": policy_fingerprint_value,
        "route": public_route(route),
        "trigger_policy": policy.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def plan_depth(plan: Plan) -> int:
    from .summary import _depth

    return _depth(plan)


def detect_triggers(view: PlanView, policy: TriggerPolicy, *, estimated_cost: int, raw_plan=None) -> list[str]:
    hits: list[str] = []
    if policy.video_anywhere:
        for spec in view.tools.values():
            if spec is not None and spec.output_type == "video":
                hits.append("video")
                break
    if len(view.plan.steps) >= policy.min_steps:
        hits.append("long_plan")
    if plan_depth(view.plan) >= policy.min_depth:
        hits.append("depth")
    if estimated_cost >= policy.cost_threshold:
        hits.append("cost")
    if policy.helper_marker:
        kinds = [step.kind for step in view.plan.steps]
        if "spawn_helper" in kinds:
            hits.append("helper")
        elif isinstance(raw_plan, dict) and (
            raw_plan.get("spawn_helper")
            or any(isinstance(step, dict) and step.get("kind") == "spawn_helper" for step in raw_plan.get("steps") or [])
        ):
            hits.append("helper")
        else:
            for step in view.plan.steps:
                if step.params.get("spawn_helper") or (raw_plan and _marker(raw_plan, step.id)):
                    hits.append("helper")
                    break
    return hits


def _marker(raw_plan, step_id: str) -> bool:
    for step in raw_plan.get("steps") or []:
        if isinstance(step, dict) and step.get("id") == step_id:
            return step.get("kind") == "spawn_helper" or bool(step.get("spawn_helper"))
    return False


def enabled(settings: Settings) -> bool:
    return bool(settings.selfcheck_enabled) and settings.selfcheck_mode != "off"


def _route_matches(evidence_route: dict, live_route: dict) -> bool:
    if not live_route:
        return False
    live = public_route(live_route)
    stored = public_route(evidence_route)
    if not live or not stored:
        return False
    keys = set(live) | set(stored)
    for key in keys:
        if live.get(key) != stored.get(key):
            alias = {"baseUrl": "base_url", "timeoutSeconds": "timeout_s", "maxTokens": "max_tokens",
                     "contextMaxBytes": "context_max_bytes", "personaId": "persona_id"}
            other = alias.get(key)
            if other and live.get(key, live.get(other)) == stored.get(key, stored.get(other)):
                continue
            reverse = {v: k for k, v in alias.items()}
            other = reverse.get(key)
            if other and live.get(key, live.get(other)) == stored.get(key, stored.get(other)):
                continue
            return False
    return True


def qualify_evidence(
    settings: Settings,
    evidence: EnforcementEvidence | None,
    template_hash: str,
    route: dict | None,
    *,
    provider_kind: str = "none",
) -> dict[str, Any]:
    """Production gate. Synthetic, forged, incomplete, or identity-mismatched evidence cannot enforce."""

    mode: EnforcementMode = settings.selfcheck_mode if settings.selfcheck_mode in {"off", "shadow", "enforce"} else "off"
    if mode != "enforce":
        return {"mode": mode, "allowed": False, "reason": "not_enforce"}
    live = public_route(route)
    if not live or not live.get("model"):
        return {"mode": "shadow", "allowed": False, "reason": "no_route"}
    if provider_kind in {"fake", "synthetic", "none"}:
        return {"mode": "shadow", "allowed": False, "reason": "synthetic_provider"}
    target = settings.selfcheck_max_false_block_rate
    if target is None:
        return {"mode": "shadow", "allowed": False, "reason": "missing_target"}
    expected_hash = settings.selfcheck_expected_dataset_hash
    if not expected_hash:
        return {"mode": "shadow", "allowed": False, "reason": "missing_expected_dataset_hash"}
    if evidence is None:
        return {"mode": "shadow", "allowed": False, "reason": "missing_evidence"}
    if evidence.provenance != "live" or evidence.llm_tested is not True:
        return {"mode": "shadow", "allowed": False, "reason": "synthetic_or_untested"}
    if evidence.case_count < 80 or evidence.drift_count < 30 or evidence.normal_count < 50:
        return {"mode": "shadow", "allowed": False, "reason": "insufficient_cases"}
    if evidence.case_count != evidence.drift_count + evidence.normal_count:
        return {"mode": "shadow", "allowed": False, "reason": "case_split_mismatch"}
    if evidence.valid_observation_rate != 1.0 or evidence.fallback_count != 0:
        return {"mode": "shadow", "allowed": False, "reason": "incomplete_observations"}
    if evidence.dataset_hash != expected_hash:
        return {"mode": "shadow", "allowed": False, "reason": "dataset_hash_mismatch"}
    if evidence.template_hash != template_hash or evidence.template_version != settings.selfcheck_template_version:
        return {"mode": "shadow", "allowed": False, "reason": "template_mismatch"}
    fingerprint = policy_fingerprint(settings)
    if evidence.policy_hash != fingerprint:
        return {"mode": "shadow", "allowed": False, "reason": "policy_hash_mismatch"}
    if evidence.model != live.get("model"):
        return {"mode": "shadow", "allowed": False, "reason": "model_mismatch"}
    if evidence.route_id and evidence.route_id != live.get("id"):
        return {"mode": "shadow", "allowed": False, "reason": "route_id_mismatch"}
    if not _route_matches(evidence.route, live):
        return {"mode": "shadow", "allowed": False, "reason": "route_identity_mismatch"}
    if evidence.observed_false_block_rate >= target:
        return {"mode": "shadow", "allowed": False, "reason": "rate_not_below_target"}
    return {"mode": "enforce", "allowed": True, "reason": "below_target", "provenance": "live"}


def enforcement_decision(settings: Settings, evidence: EnforcementEvidence | None, template_hash: str, route: dict | None, **kwargs) -> dict[str, Any]:
    return qualify_evidence(settings, evidence, template_hash, route, **kwargs)


def _reject_constant(_value):
    raise ValueError("nonfinite")


def _no_duplicate_keys(pairs):
    seen = set()
    out = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate_key")
        seen.add(key)
        out[key] = value
    return out


def parse_verdict(content: str) -> SemanticVerdict:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty_response")
    text = content.strip()
    try:
        raw = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("not_json") from exc
    if not isinstance(raw, dict):
        raise ValueError("not_object")
    return SemanticVerdict.model_validate(raw)


def reasons_from_verdict(verdict: SemanticVerdict) -> list[ReviewReason]:
    if verdict.pass_:
        return []
    return [
        intent_reason(
            "semantic_mismatch",
            f"{item.item}：期望 {item.expected}，计划是 {item.actual}",
        )
        for item in verdict.problems
    ]


def attach(self_check: dict, journal: SemanticJournal) -> dict:
    from .contracts import SEMANTIC_KEY

    payload = dict(self_check or {})
    payload[SEMANTIC_KEY] = journal.model_dump(mode="json")
    return payload


def read_journal(self_check: dict | None) -> SemanticJournal | None:
    from .contracts import SEMANTIC_KEY

    bag = (self_check or {}).get(SEMANTIC_KEY)
    if not isinstance(bag, dict):
        return None
    return SemanticJournal.model_validate(bag)
