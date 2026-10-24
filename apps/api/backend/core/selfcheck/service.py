"""Durable semantic journal inside CorePlan.self_check. No model await under Session/Run lock."""

from __future__ import annotations

import hashlib
import json

from pydantic import ValidationError

from backend.core.review.contracts import PlanView
from backend.core.supervisor.contracts import Settings

from .contracts import POLICY_HASH, SEMANTIC_KEY, SemanticJournal
from .policy import (
    call_key,
    detect_triggers,
    enabled,
    policy_fingerprint,
    qualify_evidence,
    parse_verdict,
    public_route,
    reasons_from_verdict,
    trigger_policy_from_settings,
)
from .prompt import load_prompt, render_messages
from .summary import refs_from_view, summarize, typed_port_view


def build_messages(view: PlanView, summary: dict, prompt, settings: Settings, *, user_message: str, extra_refs=None) -> list[dict]:
    invariants = view.context.invariants.model_dump(mode="json")
    return render_messages(
        prompt,
        user_message=user_message,
        invariants=invariants,
        refs=refs_from_view(view, extra_refs),
        plan_summary=summary,
    )


def input_hash(messages: list[dict]) -> str:
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def prepare_journal(
    view: PlanView,
    *,
    settings: Settings,
    run_id: str,
    parent_run_id: str | None,
    invocation_id: str,
    input_revision: int,
    candidate_hash: str,
    catalog_hash: str,
    estimated_cost: int,
    route: dict | None,
    evidence=None,
    raw_plan=None,
    user_message: str = "",
    extra_refs=None,
    provider_kind: str = "none",
    evidence_gate=None,
    catalog=None,
) -> SemanticJournal:
    if catalog is not None and raw_plan is not None:
        view = typed_port_view(view, raw_plan, catalog)
    policy = trigger_policy_from_settings(settings)
    prompt = load_prompt(settings.selfcheck_template_version)
    fingerprint = policy_fingerprint(settings)
    triggers = detect_triggers(view, policy, estimated_cost=estimated_cost, raw_plan=raw_plan)
    on = enabled(settings) and bool(triggers)
    gate = evidence_gate or qualify_evidence
    decision = gate(settings, evidence, prompt.content_hash, route, provider_kind=provider_kind)
    key = call_key(
        run_id=run_id,
        input_revision=input_revision,
        invocation_id=invocation_id,
        candidate_hash=candidate_hash,
        template_hash=prompt.content_hash,
        route=route,
        policy=policy,
        policy_fingerprint_value=fingerprint,
    )
    summary = summarize(
        view,
        max_param_chars=settings.selfcheck_max_param_chars,
        max_bytes=settings.selfcheck_max_input_bytes,
        catalog_hash=catalog_hash,
        proposal_hash=candidate_hash,
        revision=input_revision,
    )
    journal = SemanticJournal(
        status="skipped" if not on else "reserved",
        mode=decision["mode"] if on else "off",
        triggered=on,
        triggers=triggers,
        call_key=key,
        template_version=prompt.version,
        template_hash=prompt.content_hash,
        policy_hash=POLICY_HASH,
        policy_fingerprint=fingerprint,
        route=public_route(route) or None,
        model=(public_route(route) or {}).get("model"),
        candidate_hash=candidate_hash,
        catalog_hash=catalog_hash,
        input_revision=input_revision,
        invocation_id=invocation_id,
        enforcement=decision,
        degraded=bool(summary.get("degraded")),
        trace={"run_id": run_id, "parent_run_id": parent_run_id, "task_id": None},
        provider_kind=provider_kind,
    )
    if summary.get("degraded"):
        journal.status = "rule_only"
        journal.fallback_reason = "input_too_large"
        journal.triggered = False
        return journal
    if on:
        message = user_message if isinstance(user_message, str) else ""
        messages = build_messages(view, summary, prompt, settings, user_message=message, extra_refs=extra_refs)
        encoded = json.dumps(messages, ensure_ascii=False).encode()
        if len(encoded) > settings.selfcheck_max_input_bytes:
            journal.status = "rule_only"
            journal.fallback_reason = "input_too_large"
            journal.triggered = False
            journal.degraded = True
            return journal
        journal.input_hash = input_hash(messages)
        journal.request_messages = messages
    return journal


def merge_self_check(verdict: dict, journal: SemanticJournal) -> dict:
    payload = dict(verdict or {})
    payload[SEMANTIC_KEY] = journal.model_dump(mode="json")
    return payload


def apply_parsed(journal: SemanticJournal, content: str, usage: dict, raw: dict | None) -> tuple[SemanticJournal, list, bool]:
    """Parse stored raw. Invalid JSON is rule_only, never a model_pass."""

    journal.raw = raw
    bag = dict(usage or {})
    tokens = bag.get("total_tokens")
    valid_tokens = isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0
    if bag.get("unknown") is True or not valid_tokens:
        bag["unknown"] = True
        if not valid_tokens:
            bag.pop("total_tokens", None)
    journal.usage = bag
    try:
        parsed = parse_verdict(content)
    except (ValueError, ValidationError, TypeError) as exc:
        journal.status = "rule_only"
        journal.fallback_reason = getattr(exc, "args", [type(exc).__name__])[0] if getattr(exc, "args", None) else type(exc).__name__
        if not isinstance(journal.fallback_reason, str):
            journal.fallback_reason = type(exc).__name__
        journal.parsed = None
        return journal, [], False
    journal.parsed = parsed.model_dump(mode="json", by_alias=True)
    journal.status = "parsed"
    allow = journal.enforcement.get("allowed") is True and journal.provider_kind not in {"fake", "synthetic", "none"}
    reject = (not parsed.pass_) and allow
    reasons = reasons_from_verdict(parsed) if reject else []
    if parsed.pass_ and parsed.problems:
        journal.enforcement = {**journal.enforcement, "uncertain": True}
    return journal, [r.model_dump() for r in reasons], reject
