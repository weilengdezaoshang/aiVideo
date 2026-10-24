"""Plan self-check evaluation: frozen cases, rule baseline, and protocol boundaries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core.selfcheck.contracts import POLICY_HASH
from backend.core.selfcheck.prompt import load_prompt
from backend.evaluation.api import build_eval_router
from backend.evaluation.registry import register_builtin
from backend.evaluation.runner import EvaluationRunner, EventLog
from backend.evaluation.selfcheck_cases import (
    FROZEN_DATASET_SHA256,
    build_cases,
    dataset_hash,
    file_sha256,
    load_cases,
    write_fixture,
)
from backend.evaluation.selfcheck_dataset import DATASET_ID, write_selfcheck_dataset
from backend.evaluation.selfcheck_evaluation import (
    CallBudget,
    EvidenceRejected,
    GradeRejected,
    IdentityMismatch,
    PolicyFingerprintUnavailable,
    StageLimits,
    assert_prompt_has_no_labels,
    assert_record_identity,
    assert_route_matches_limits,
    canonical_identity,
    case_call_key,
    effective_settings,
    evaluate_rules,
    evidence_document,
    export_evidence,
    grade_record,
    grade_stored,
    observe_llm_case,
    observe_many,
    open_run_directory,
    reservation_bound,
    review_case,
    run_manifest,
    scripted_observation,
    trusted_messages,
)
from backend.evaluation.selfcheck_factory import bind_selfcheck, set_selfcheck_factory
from backend.evaluation.selfcheck_cli import main as selfcheck_main


def _raw(passed: bool, tokens: int = 11) -> dict:
    body = {"pass": passed, "problems": [] if passed else [{"item": "count", "expected": "3", "actual": "1"}]}
    return {"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}], "usage": {"total_tokens": tokens}}


def test_frozen_fixture_hash_and_split():
    cases = build_cases()
    assert file_sha256(Path(load_cases.__globals__["FIXTURE_PATH"])) == FROZEN_DATASET_SHA256
    assert len(cases) == 80
    assert sum(case.label_pass for case in cases) == 50
    assert sum(not case.label_pass for case in cases) == 30
    with pytest.raises(RuntimeError):
        write_fixture()


def test_prompt_excludes_gold_and_legal_type_comes_from_review():
    failures = []
    for case in build_cases():
        result, view = review_case(case)
        messages, _summary = trusted_messages(case, view)
        assert_prompt_has_no_labels(messages)
        blob = json.dumps(messages, ensure_ascii=False)
        assert "label_pass" not in blob
        legal = not any(reason.gate in {"legal", "types"} for reason in result.reasons)
        if not legal:
            failures.append(case.case_id)
    assert failures == []


def test_rule_baseline_is_not_model_quality():
    _records, summary = evaluate_rules()
    assert summary.llm_tested is False
    assert summary.model is None
    assert summary.rule is not None
    assert summary.rule.tpr == pytest.approx(10 / 30)
    assert summary.rule.fpr == pytest.approx(0)
    assert summary.rule.counts.drift_valid == 30
    assert summary.rule.counts.normal_valid == 50
    assert summary.policy_hash == POLICY_HASH
    assert summary.template_hash == load_prompt().content_hash
    assert summary.enforce_eligible is False


def test_reversed_confusion_and_mutation():
    cases = [case for case in build_cases() if case.drift][:5] + [case for case in build_cases() if case.label_pass][:5]
    passed_flags = [False, False, False, True, True, False, True, True, True, True]
    records = []
    for case, passed in zip(cases, passed_flags, strict=True):
        records.append(scripted_observation(
            case, _raw(passed, tokens=15 + len(records)), latency_ms=20 + len(records),
        ))
    grades, summary = grade_stored(
        records, cases, mode="protocol", llm_tested=False, reason="scripted protocol",
    )
    assert summary.model is None
    assert summary.protocol.tpr == pytest.approx(0.6)
    assert summary.protocol.fpr == pytest.approx(0.2)
    assert summary.llm_tested is False
    assert summary.protocol.known_tokens == sum(15 + index for index in range(10))
    original = grade_record(records[0], label_pass=cases[0].label_pass)
    mutated = json.loads(json.dumps(records[0]))
    mutated["provider_raw"] = _raw(True)
    changed = grade_record(mutated, label_pass=cases[0].label_pass)
    assert original["rejected"] is True
    assert changed["rejected"] is False
    assert [item["valid"] for item in grades].count(True) == 10


def test_unknown_does_not_zero_fpr_or_inflate_coverage():
    normal = [case for case in build_cases() if case.label_pass][:5]
    records = [scripted_observation(normal[0], _raw(False), latency_ms=5)]
    for case in normal[1:]:
        records.append(scripted_observation(case, {"choices": [{"message": {"content": "nope"}}]}, latency_ms=1))
    grades, summary = grade_stored(records, normal, mode="protocol", llm_tested=False, reason="partial")
    assert summary.protocol.fpr == pytest.approx(1.0)
    assert summary.protocol.counts.valid == 1
    assert summary.valid_observation_rate == pytest.approx(0.2)
    for record in records:
        record["provider_raw"] = None
        record["status"] = "unknown"
        record["fallback"] = "unknown"
        record["observed_pass"] = True
    _grades, empty = grade_stored(records, normal, mode="protocol", llm_tested=False, reason="all unknown")
    assert empty.protocol.fpr is None
    assert empty.protocol.tpr is None
    assert empty.model is None
    with pytest.raises(EvidenceRejected):
        export_evidence(records, build_cases(), route={"id": "r", "model": "m"})


def test_missing_factory_is_unknown(tmp_path: Path):
    set_selfcheck_factory(None)
    binding = bind_selfcheck("llm", None)
    assert binding["provider"] is None
    assert binding["llm_tested"] is False
    code = selfcheck_main(["--mode", "llm", "--output", str(tmp_path / "llm")])
    assert code == 2
    payload = json.loads((tmp_path / "llm" / "summary.json").read_text())
    assert payload["llm_tested"] is False
    assert payload["model"] is None


def test_offline_and_rules_cli(tmp_path: Path):
    assert selfcheck_main(["--mode", "offline", "--output", str(tmp_path / "off")]) == 0
    off = json.loads((tmp_path / "off" / "summary.json").read_text())
    assert off["llm_tested"] is False
    assert off["model"] is None
    assert off["legal_type_pass"] == 80
    assert selfcheck_main(["--mode", "rules", "--output", str(tmp_path / "rules")]) == 0
    rules = json.loads((tmp_path / "rules" / "summary.json").read_text())
    assert rules["model"] is None
    assert rules["rule"]["tpr"] == pytest.approx(10 / 30)
    assert rules["rule"]["fpr"] == pytest.approx(0)


def test_runner_rules_and_mutated_observation(tmp_path: Path):
    source = write_selfcheck_dataset(tmp_path / "cases.jsonl")
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    frozen = runner.freeze_dataset(source, DATASET_ID, split="regression")
    assert frozen["caseCount"] == 80
    with pytest.raises(ValueError):
        runner.create_run("bad", DATASET_ID, mode="mock", sandbox_config={"selfcheckMode": "rules", "apiKey": "sk"})
    runner.create_run("selfcheck-rules", DATASET_ID, mode="mock", sandbox_config={"selfcheckMode": "rules"})
    runner.execute_run("selfcheck-rules")
    loaded = runner.load_run("selfcheck-rules")
    assert loaded.status == "completed"
    summary = json.loads((runner.run_dir("selfcheck-rules") / "selfcheck-summary.json").read_text())
    assert summary["llmTested"] is False
    assert summary["model"]["tpr"] is None
    assert summary["rule"]["tpr"] == pytest.approx(10 / 30)
    assert summary["rule"]["fpr"] == pytest.approx(0)
    sample = next(trial for trial in loaded.trials if trial.caseId == "drift-051")
    path = runner._trial_dir(sample.runId, sample.trialId) / "observation.json"
    record = json.loads(path.read_text())
    assert record["expected_used_as_observation"] is False
    assert "label_pass" not in record
    record["provider_raw"] = _raw(True)
    record["mode"] = "protocol"
    path.write_text(json.dumps(record), encoding="utf-8")
    case = next(item for item in runner.load_run("selfcheck-rules").trials if item.caseId == "drift-051")
    from backend.evaluation.datasets import DatasetStore

    versions = {item.caseId: item for item in DatasetStore.load_run_snapshot(runner.run_dir("selfcheck-rules"))}
    before = next(grade.status for grade in loaded.grades if grade.trialId == sample.trialId and grade.checkId == "should_reject")
    runner._grade_selfcheck(case, versions[case.caseId], EventLog(runner.run_dir("selfcheck-rules")))
    after = runner.load_run("selfcheck-rules")
    status = next(grade.status for grade in after.grades if grade.trialId == sample.trialId and grade.checkId == "should_reject")
    assert before == "pass"
    assert status == "fail"
    register_builtin()
    declared = next(item for item in __import__("backend.evaluation.registry", fromlist=["all_evaluators"]).all_evaluators() if item.id == "plan-selfcheck")
    assert declared.applies_to == {"plan_selfcheck"}


def _route(cap: int = 128) -> dict:
    return {
        "id": "route-1",
        "provider": "bailian",
        "model": "qwen-plus",
        "baseUrl": "https://example.invalid/v1",
        "maxTokens": cap,
        "timeoutSeconds": 8,
        "contextMaxBytes": 24000,
    }


def _limits() -> StageLimits:
    return StageLimits(output_cap=128, token_budget=250_000, timeout_seconds=8, max_context_bytes=24000, concurrency=2)


def test_interrupt_before_raw_is_not_resent(tmp_path, monkeypatch):
    import backend.evaluation.selfcheck_evaluation as mod

    monkeypatch.setattr(mod, "try_policy_fingerprint", lambda settings=None: "fp-test")
    case = build_cases()[0]
    destination = tmp_path / f"{case.case_id}.json"
    route = _route()
    limits = _limits()

    class Cut:
        kind = "chat"
        calls = 0

        async def complete(self, messages, response_sink=None, timeout=None):
            self.calls += 1
            assert json.loads(destination.read_text())["status"] == "sending"
            raise SystemExit("cut")

    first = Cut()
    budget = CallBudget(limits)
    try:
        asyncio.run(observe_llm_case(case, first, route=route, limits=limits, destination=destination, budget=budget))
    except SystemExit:
        pass
    assert first.calls == 1
    stored = json.loads(destination.read_text())
    assert stored["status"] == "sending"
    assert stored["provider_raw"] is None

    class Again:
        kind = "chat"
        calls = 0

        async def complete(self, messages, response_sink=None, timeout=None):
            self.calls += 1
            raise AssertionError("resume sent another call")

    second = Again()
    resumed = asyncio.run(observe_llm_case(
        case, second, route=route, limits=limits, destination=destination, budget=CallBudget(limits),
    ))
    assert second.calls == 0
    assert resumed["status"] == "sending"
    assert json.loads(destination.read_text())["status"] == "sending"


def test_nonzero_retry_rejected_and_raw_kept():
    with pytest.raises(ValueError):
        StageLimits(retries=1)
    record = scripted_observation(build_cases()[0], _raw(False), latency_ms=3)
    record["mode"] = "llm"
    record["provenance"] = "live"
    record["status"] = "unknown"
    record["observed_pass"] = True
    graded = grade_record(record, label_pass=record and True)
    assert graded["observed_pass"] is False
    assert graded["valid"] is True
    missing = dict(record)
    missing["provider_raw"] = None
    missing["observed_pass"] = True
    missing["fallback"] = None
    missed = grade_record(missing, label_pass=True)
    assert missed["valid"] is False
    assert missed["fallback"] == "missing_raw"
    copied = dict(record)
    copied["expected_used_as_observation"] = True
    with pytest.raises(GradeRejected):
        grade_record(copied, label_pass=True)


def test_parallel_concurrency(tmp_path, monkeypatch):
    import backend.evaluation.selfcheck_evaluation as mod

    monkeypatch.setattr(mod, "try_policy_fingerprint", lambda settings=None: "fp-test")
    cases = build_cases()[:2]
    route = _route()
    state = {"inflight": 0, "max": 0}

    class Slow:
        kind = "chat"

        async def complete(self, messages, response_sink=None, timeout=None):
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
            await asyncio.sleep(0.05)
            state["inflight"] -= 1
            raw = _raw(True, tokens=4)
            if response_sink is not None:
                await response_sink(raw)
            return SimpleNamespace(raw=raw, usage=raw["usage"])

    asyncio.run(observe_many(
        cases, Slow(), route=route, limits=_limits(), directory=tmp_path / "wide", budget=CallBudget(_limits()),
    ))
    assert state["max"] == 2
    state["max"] = 0
    serial = _limits()
    serial = serial.model_copy(update={"concurrency": 1})
    asyncio.run(observe_many(
        cases, Slow(), route=route, limits=serial, directory=tmp_path / "serial", budget=CallBudget(serial),
    ))
    assert state["max"] == 1


def test_resume_refuses_stale_identity(tmp_path, monkeypatch):
    import backend.evaluation.selfcheck_evaluation as mod

    monkeypatch.setattr(mod, "try_policy_fingerprint", lambda settings=None: "fp-test")
    cases = build_cases()[:1]
    limits = _limits()
    manifest = run_manifest(cases, mode="rules", route=None, limits=limits)
    open_run_directory(tmp_path, manifest, resume=False)
    with pytest.raises(IdentityMismatch):
        open_run_directory(tmp_path, manifest, resume=False)
    other = dict(manifest)
    other["template_hash"] = "stale"
    with pytest.raises(IdentityMismatch):
        open_run_directory(tmp_path, other, resume=True)
    assert json.loads((tmp_path / "run-manifest.json").read_text())["template_hash"] == manifest["template_hash"]


def test_export_rejects_duplicate_and_identity_gaps(monkeypatch):
    import backend.evaluation.selfcheck_evaluation as mod

    monkeypatch.setattr(mod, "try_policy_fingerprint", lambda settings=None: "fp-test")
    cases = build_cases()
    route = _route()
    limits = _limits()
    one = cases[0]
    with pytest.raises(EvidenceRejected, match="duplicate"):
        export_evidence([{"case_id": one.case_id}, {"case_id": one.case_id}], cases, route=route, limits=limits)
    with pytest.raises(EvidenceRejected, match="unknown_case"):
        export_evidence([{"case_id": "not-a-case"}], cases, route=route, limits=limits)
    with pytest.raises(EvidenceRejected, match="case_set"):
        export_evidence([{"case_id": one.case_id}], cases, route=route, limits=limits)
    identity = canonical_identity(one, route, limits)
    raw = _raw(False, tokens=9)
    record = scripted_observation(one, raw, latency_ms=4)
    record.update(identity)
    record.update({"provenance": "live", "provider_kind": "chat", "llm_tested": True, "mode": "llm", "provider_raw": raw})
    assert_record_identity(record, one, route, limits)
    changed = dict(record)
    changed["input_hash"] = "changed"
    with pytest.raises(IdentityMismatch, match="input_hash"):
        assert_record_identity(changed, one, route, limits)
    stale = dict(record)
    stale["route"] = {**record["route"], "model": "other"}
    with pytest.raises(IdentityMismatch, match="route"):
        assert_record_identity(stale, one, route, limits)
    monkeypatch.setattr(mod, "try_policy_fingerprint", lambda settings=None: None)
    with pytest.raises(PolicyFingerprintUnavailable):
        export_evidence([record], [one], route=route, limits=limits)


def test_rule_comparison_counts_saved_reasons():
    _records, summary = evaluate_rules()
    assert summary.comparison["ruleCaught"] == 10
    assert summary.comparison["modelAdditional"] is None
    assert summary.model is None
    assert summary.rule.tpr == pytest.approx(10 / 30)


def _local_route() -> dict:
    return {
        "id": "local-dialogue",
        "provider": "bailian",
        "model": "qwen-plus",
        "baseUrl": "https://example.invalid/v1",
        "maxTokens": 2048,
        "timeoutSeconds": 45,
        "contextMaxBytes": 131072,
    }


def test_outer_caps_keep_route_identity():
    route = _local_route()
    limits = StageLimits(output_cap=2048, timeout_seconds=8, max_context_bytes=24000)
    live, effective = assert_route_matches_limits(route, limits)
    assert live["timeoutSeconds"] == 45
    assert live["maxTokens"] == 2048
    assert live["contextMaxBytes"] == 131072
    assert effective.timeout_seconds == 8
    assert effective.output_cap == 2048
    _live, capped = assert_route_matches_limits(
        route, StageLimits(output_cap=4096, timeout_seconds=8, max_context_bytes=24000),
    )
    assert capped.output_cap == 2048
    with pytest.raises(IdentityMismatch, match="output_cap_below_route"):
        assert_route_matches_limits(route, StageLimits(output_cap=1024, timeout_seconds=8, max_context_bytes=24000))
    with pytest.raises(IdentityMismatch, match="timeout_exceeds_route"):
        assert_route_matches_limits(route, StageLimits(output_cap=2048, timeout_seconds=60, max_context_bytes=24000))


def test_cli_override_changes_fingerprint_not_budget():
    from backend.core.selfcheck.policy import policy_fingerprint
    from backend.core.supervisor.contracts import Settings

    base = policy_fingerprint(Settings())
    overridden = StageLimits(timeout_seconds=4, max_context_bytes=8000, max_output_bytes=1024, max_param_chars=64)
    fingerprint = policy_fingerprint(effective_settings(overridden))
    assert fingerprint != base
    snapshot = run_manifest(build_cases()[:1], mode="offline", route=None, limits=overridden)
    assert snapshot["policy_fingerprint"] == fingerprint
    assert snapshot["policy_hash"] == POLICY_HASH
    assert "token_budget" not in snapshot["quality_limits"]
    assert snapshot["limits"]["token_budget"] == 250_000
    assert snapshot["fixture_sha256"] == FROZEN_DATASET_SHA256
    assert snapshot["dataset_hash"] != FROZEN_DATASET_SHA256


def test_user_prose_may_mention_gold_words():
    case = build_cases()[0].model_copy(update={
        "user_message": "please keep notes and do not treat label_pass as a field",
    })
    _result, view = review_case(case)
    messages, _summary = trusted_messages(case, view)
    assert_prompt_has_no_labels(messages)
    payload = json.loads(messages[1]["content"])
    assert "label_pass" in payload["user_message"]
    assert "notes" in payload["user_message"]
    assert "label_pass" not in payload
    assert "notes" not in payload


def test_modified_prompt_and_gold_change_dataset_hash():
    cases = build_cases()
    original = dataset_hash(cases)
    assert original != FROZEN_DATASET_SHA256
    prompted = cases[0].model_copy(update={"user_message": cases[0].user_message + " extra"})
    prompt_cases = [prompted, *cases[1:]]
    assert dataset_hash(prompt_cases) != original
    gold = cases[0].model_copy(update={"label_pass": not cases[0].label_pass, "notes": "edited"})
    gold_cases = [gold, *cases[1:]]
    assert dataset_hash(gold_cases) != original
    route = _route()
    limits = _limits()
    identity = canonical_identity(cases[0], route, limits)
    stale = dict(identity)
    with pytest.raises(IdentityMismatch, match="input_hash"):
        assert_record_identity(stale, prompted, route, limits)
    claimed = [{"case_id": case.case_id, "dataset_hash": original} for case in gold_cases]
    with pytest.raises(EvidenceRejected, match="dataset_not_canonical"):
        export_evidence(claimed, gold_cases, route=route, limits=limits)
    custom = [{"case_id": case.case_id, "dataset_hash": dataset_hash(gold_cases)} for case in gold_cases]
    with pytest.raises(EvidenceRejected, match="dataset_not_canonical"):
        export_evidence(custom, gold_cases, route=route, limits=limits)
    _result, view = review_case(gold)
    messages, _summary = trusted_messages(gold, view)
    assert_prompt_has_no_labels(messages)


def test_call_key_includes_effective_fingerprint():
    from backend.core.selfcheck.policy import TriggerPolicy, call_key, policy_fingerprint

    case = build_cases()[0]
    route = _local_route()
    limits = StageLimits(output_cap=2048, timeout_seconds=8, max_context_bytes=24000)
    fingerprint = policy_fingerprint(effective_settings(limits))
    _result, view = review_case(case)
    messages, _summary = trusted_messages(case, view, limits=limits)
    assert case_call_key(case, messages, route, policy_fingerprint_value=fingerprint) != case_call_key(
        case, messages, route, policy_fingerprint_value="",
    )
    from backend.evaluation.selfcheck_evaluation import input_hash

    assert case_call_key(case, messages, route, policy_fingerprint_value=fingerprint) == call_key(
        run_id="plan-selfcheck",
        input_revision=1,
        invocation_id=case.case_id,
        candidate_hash=input_hash(messages),
        template_hash=load_prompt().content_hash,
        route=route,
        policy=TriggerPolicy(),
        policy_fingerprint_value=fingerprint,
    )


def test_oversize_output_is_not_valid_quality():
    case = build_cases()[0]
    raw = _raw(False, tokens=17)
    record = scripted_observation(case, raw, latency_ms=1)
    record["mode"] = "llm"
    record["provenance"] = "live"
    record["limits"] = {"max_output_bytes": 8}
    graded = grade_record(record, label_pass=True)
    assert graded["valid"] is False
    assert graded["fallback"] == "output_too_large"
    assert record["usage"]["total_tokens"] == 17


def test_budget_reservation_cannot_overrun_and_covers_fixture(tmp_path: Path):
    cases = build_cases()[:2]
    route = _route()
    sample = _limits()
    _result, view = review_case(cases[0])
    messages, _summary = trusted_messages(cases[0], view, limits=sample)
    upper = reservation_bound(messages, sample)
    tight = sample.model_copy(update={"token_budget": upper, "concurrency": 2})
    calls = {"n": 0}

    class Gate:
        kind = "chat"

        async def complete(self, messages, response_sink=None, timeout=None):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            raw = _raw(True, tokens=4)
            if response_sink is not None:
                await response_sink(raw)
            return SimpleNamespace(raw=raw, usage=raw["usage"])

    budget = CallBudget(tight)
    records = asyncio.run(observe_many(
        cases, Gate(), route=route, limits=tight, directory=tmp_path / "calls", budget=budget,
    ))
    assert calls["n"] == 1
    assert budget.tokens <= tight.token_budget
    assert any(record.get("fallback") == "budget_exhausted" for record in records)

    wide = StageLimits(output_cap=2048, timeout_seconds=8, max_context_bytes=24000)
    total = 0
    for case in build_cases():
        _result, view = review_case(case)
        payload, _summary = trusted_messages(case, view, limits=wide)
        total += reservation_bound(payload, wide)
    # Request UTF-8 bytes plus route maxTokens 2048 is 342859 for this fixture.
    # The configured 250000 budget is smaller than that pre-call bound.
    assert total == 342859
    assert StageLimits().token_budget == 250_000
    assert total > StageLimits().token_budget


def test_evidence_roundtrip_matches_runtime_gate(tmp_path: Path):
    from backend.core.selfcheck.policy import policy_fingerprint, qualify_evidence
    from backend.core.selfcheck.prompt import load_prompt as load_template
    from backend.core.supervisor.contracts import Settings
    from backend.core.supervisor.wiring import load_selfcheck_evidence

    cases = build_cases()
    route = _local_route()
    limits = StageLimits(output_cap=2048, timeout_seconds=8, max_context_bytes=24000)
    records = []
    for case in cases:
        identity = canonical_identity(case, route, limits)
        raw = _raw(True, tokens=3)
        records.append({
            **identity,
            "provenance": "live",
            "provider_kind": "chat",
            "llm_tested": True,
            "mode": "llm",
            "provider_raw": raw,
            "usage": raw["usage"],
            "legal_type_pass": True,
            "status": "parsed",
        })
    with pytest.raises(EvidenceRejected, match="not_genuine"):
        export_evidence(records, cases, route=route, limits=limits)
    evidence = export_evidence(records, cases, route=route, limits=limits, verifier=lambda _record: True)
    document = evidence_document(evidence)
    assert set(document) == set(evidence.model_dump(mode="json"))
    assert "eligible" not in document
    assert "policy_snapshot" not in document
    fingerprint = policy_fingerprint(effective_settings(limits))
    assert document["policy_hash"] == fingerprint
    assert document["policy_hash"] != POLICY_HASH
    assert document["dataset_hash"] == dataset_hash()
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    settings = Settings()
    loaded, meta = load_selfcheck_evidence(settings.model_copy(update={"selfcheck_evidence_path": str(path)}))
    assert meta["reason"] == "loaded"
    assert loaded is not None
    template = load_template().content_hash
    denied = qualify_evidence(settings, loaded, template, route, provider_kind="chat")
    assert denied["allowed"] is False
    missing_target = qualify_evidence(
        settings.model_copy(update={"selfcheck_mode": "enforce"}),
        loaded,
        template,
        route,
        provider_kind="chat",
    )
    assert missing_target["reason"] == "missing_target"
    allowed = qualify_evidence(
        settings.model_copy(update={
            "selfcheck_mode": "enforce",
            "selfcheck_max_false_block_rate": 0.01,
            "selfcheck_expected_dataset_hash": dataset_hash(),
        }),
        loaded,
        template,
        route,
        provider_kind="chat",
    )
    assert allowed == {"mode": "enforce", "allowed": True, "reason": "below_target", "provenance": "live"}


def test_incomplete_live_keeps_partial_model_and_full_rule_denominator():
    cases = build_cases()
    drift = [case for case in cases if not case.label_pass]
    invalid_ids = {case.case_id for case in drift[:4]}
    accept_ids = {case.case_id for case in drift[4:6]}
    records = []
    for case in cases:
        if case.case_id in invalid_ids:
            continue
        observed_pass = True if case.label_pass or case.case_id in accept_ids else False
        record = scripted_observation(case, _raw(observed_pass, tokens=11), latency_ms=40)
        record["provenance"] = "live"
        record["provider_kind"] = "chat"
        record["mode"] = "llm"
        records.append(record)
    nonstring = {
        "choices": [{"message": {"content": json.dumps({
            "pass": False,
            "problems": [{"item": "count", "expected": 2, "actual": 1}],
        }, ensure_ascii=False)}}],
        "usage": {"total_tokens": 9},
    }
    for index, case in enumerate(drift[:3]):
        record = scripted_observation(case, nonstring, latency_ms=100 + index)
        record["provenance"] = "live"
        record["provider_kind"] = "chat"
        record["mode"] = "llm"
        graded = grade_record(record, label_pass=False)
        assert graded["valid"] is False
        assert graded["observed_pass"] is None
        records.append(record)
    timeout_case = drift[3]
    timeout = scripted_observation(timeout_case, _raw(False, tokens=4), latency_ms=1)
    timeout["provider_raw"] = None
    timeout["fallback"] = "timeout"
    timeout["latency_ms"] = 8000
    timeout["usage"] = {}
    timeout["provenance"] = "live"
    timeout["provider_kind"] = "chat"
    timeout["mode"] = "llm"
    timeout["llm_tested"] = False
    records.append(timeout)
    assert len(records) == 80
    _grades, summary = grade_stored(
        records, cases, mode="llm", llm_tested=False, reason="不完整 live，不导出证据。",
    )
    assert summary.llm_tested is False
    assert summary.enforce_eligible is False
    assert summary.model is not None
    assert summary.model.tpr == pytest.approx(24 / 26)
    assert summary.model.fpr == pytest.approx(0)
    assert summary.model.counts.drift_valid == 26
    assert summary.model.counts.normal_valid == 50
    assert summary.model.counts.valid == 76
    assert summary.model.counts.fallback == 4
    assert summary.model.p50_ms is not None
    assert summary.model.known_usage_count == 79
    assert summary.unknown_usage == 1
    assert summary.rule.tpr == pytest.approx(10 / 30)
    assert summary.rule.fpr == pytest.approx(0)
    assert summary.rule.counts.drift_valid == 30
    assert summary.rule.counts.normal_valid == 50
    assert summary.rule.counts.fallback == 0
    assert summary.comparison["modelBasis"] == "valid_only"
    assert summary.comparison["combinedBasis"] == "rules_when_model_fallback"
    assert summary.comparison["combinedDrift"] == 30
    assert summary.comparison["modelDrift"] == 26
    with pytest.raises(EvidenceRejected):
        export_evidence(records, cases, route=_route(), limits=_limits())
    scripted = [scripted_observation(case, _raw(False), latency_ms=3) for case in cases[:4]]
    _script_grades, protocol = grade_stored(
        scripted, cases[:4], mode="llm", llm_tested=False, reason="scripted",
    )
    assert protocol.model is None
    assert protocol.llm_tested is False


def test_api_freeze_run_report(tmp_path: Path):
    app = FastAPI()
    app.include_router(build_eval_router(tmp_path))
    with TestClient(app) as client:
        created = client.post("/api/evals/v1/plan-selfcheck", json={"runId": "api-rules", "mode": "rules", "maxTokens": 100})
        assert created.status_code == 202
        waited = client.post("/api/evals/v1/runs/api-rules/wait")
        assert waited.json()["status"] == "completed"
        detail = client.get("/api/evals/v1/runs/api-rules")
        body = detail.json()
        assert body["planSelfcheck"]["llmTested"] is False
        assert body["planSelfcheck"]["model"]["tpr"] is None
        assert body["planSelfcheck"]["rule"]["tpr"] == pytest.approx(10 / 30)
        refused = client.post("/api/evals/v1/plan-selfcheck", json={"runId": "x", "apiKey": "sk"})
        assert refused.status_code == 422
    from backend.evaluation.scheduler import EvaluationScheduler
    EvaluationScheduler.instance(tmp_path / "evaluation").shutdown(wait=True)
