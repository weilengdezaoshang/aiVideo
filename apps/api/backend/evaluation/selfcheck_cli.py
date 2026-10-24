"""Offline and rule preflight for plan self-check. LLM mode is explicit and not used by CI.

Example:
  PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.selfcheck_cli --mode offline --output /tmp/selfcheck
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from backend.evaluation.selfcheck_cases import FIXTURE_PATH, load_cases
from backend.evaluation.selfcheck_evaluation import (
    CallBudget,
    IdentityMismatch,
    PolicyFingerprintUnavailable,
    StageLimits,
    evaluate_offline,
    evaluate_rules,
    evidence_document,
    export_evidence,
    observe_many,
    open_run_directory,
    run_manifest,
    try_policy_fingerprint,
)
from backend.evaluation.selfcheck_factory import bind_selfcheck


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan self-check evaluation preflight")
    parser.add_argument("--mode", choices=("rules", "offline", "llm"), required=True)
    parser.add_argument("--cases", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--route-id", default=None)
    parser.add_argument("--local-config-root", type=Path, default=None)
    parser.add_argument("--workspace-id", default=None)
    parser.add_argument("--output-cap", type=int, default=None)
    parser.add_argument("--token-budget", type=int, default=250_000)
    parser.add_argument("--max-context-bytes", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--max-output-bytes", type=int, default=None)
    parser.add_argument("--max-param-chars", type=int, default=None)
    parser.add_argument("--protocol-overhead", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-calls", type=int, default=80)
    return parser.parse_args(argv)


def _atomic_write(path: Path, payload: dict) -> None:
    from backend.evaluation.selfcheck_evaluation import _persist

    _persist(path, payload)


def _write(output: Path, records: list[dict], summary) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _atomic_write(output / "summary.json", summary.model_dump(mode="json"))
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    body = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    target = output / "observations.jsonl"
    temporary = target.with_suffix(".jsonl.tmp")
    temporary.write_bytes(body)
    temporary.replace(target)


def _limits(args: argparse.Namespace, route: dict | None) -> StageLimits:
    from backend.core.supervisor.contracts import Settings
    from backend.evaluation.selfcheck_evaluation import assert_route_matches_limits, route_output_cap

    settings = Settings()
    provider_cap = route_output_cap(route) if route else None
    if route and provider_cap is None:
        raise IdentityMismatch("route_output_cap_missing")
    if route and args.output_cap is not None and args.output_cap < provider_cap:
        raise IdentityMismatch("output_cap_below_route")
    cap = provider_cap if provider_cap is not None else (args.output_cap or 1024)
    limits = StageLimits(
        output_cap=cap,
        token_budget=args.token_budget,
        max_context_bytes=args.max_context_bytes if args.max_context_bytes is not None else settings.selfcheck_max_input_bytes,
        timeout_seconds=args.timeout_seconds if args.timeout_seconds is not None else settings.selfcheck_timeout_seconds,
        max_output_bytes=args.max_output_bytes if args.max_output_bytes is not None else settings.selfcheck_max_output_bytes,
        max_param_chars=args.max_param_chars if args.max_param_chars is not None else settings.selfcheck_max_param_chars,
        protocol_overhead=args.protocol_overhead,
        retries=0,
        concurrency=args.concurrency,
        max_calls=args.max_calls,
    )
    if route:
        _live, limits = assert_route_matches_limits(route, limits)
    return limits


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases = load_cases(args.cases)
    if args.mode in {"offline", "rules"}:
        limits = _limits(args, None)
        try:
            open_run_directory(
                args.output,
                run_manifest(cases, mode=args.mode, route=None, limits=limits),
                resume=args.resume,
            )
        except IdentityMismatch as exc:
            print(json.dumps({"error": str(exc), "llm_tested": False}, ensure_ascii=False))
            return 2
        records, summary = (evaluate_offline if args.mode == "offline" else evaluate_rules)(cases, limits=limits)
        _write(args.output, records, summary)
        print(json.dumps({
            "mode": args.mode,
            "cases": len(records),
            "llm_tested": False,
            "model": None,
            "rule_tpr": None if summary.rule is None else summary.rule.tpr,
            "comparison": None if summary.comparison is None else summary.comparison.get("ruleCaught"),
        }, ensure_ascii=False))
        return 0
    binding = bind_selfcheck(
        "llm",
        args.route_id,
        local_config_root=args.local_config_root,
        workspace_id=args.workspace_id,
    )
    if binding.get("unavailable") or binding.get("provider") is None or binding.get("synthetic"):
        payload = {
            "mode": "llm",
            "llm_tested": False,
            "model": None,
            "unavailable": binding.get("unavailable") or "synthetic_binding_not_quality",
            "cases": len(cases),
        }
        args.output.mkdir(parents=True, exist_ok=True)
        _atomic_write(args.output / "summary.json", payload)
        print(json.dumps(payload, ensure_ascii=False))
        return 2
    route = binding["route"]
    try:
        limits = _limits(args, route)
        from backend.evaluation.selfcheck_evaluation import effective_settings

        if try_policy_fingerprint(effective_settings(limits)) is None:
            print(json.dumps({
                "mode": "llm",
                "llm_tested": False,
                "model": None,
                "unavailable": "policy_fingerprint_unavailable",
            }, ensure_ascii=False))
            return 2
        open_run_directory(
            args.output,
            run_manifest(cases, mode="llm", route=route, limits=limits),
            resume=args.resume,
        )
    except (IdentityMismatch, ValueError) as exc:
        print(json.dumps({"error": str(exc), "llm_tested": False}, ensure_ascii=False))
        return 2
    budget = CallBudget(limits)

    async def _run():
        return await observe_many(
            cases,
            binding["provider"],
            route=route,
            limits=limits,
            directory=args.output / "calls",
            budget=budget,
        )

    try:
        records = asyncio.run(_run())
    except (IdentityMismatch, PolicyFingerprintUnavailable) as exc:
        print(json.dumps({"error": str(exc), "llm_tested": False}, ensure_ascii=False))
        return 2
    from backend.evaluation.selfcheck_evaluation import grade_stored

    try:
        _grades, summary = grade_stored(
            records,
            cases,
            mode="llm",
            llm_tested=False,
            reason="等待完整 live 观测后才导出证据。",
            limits=limits,
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc), "llm_tested": False}, ensure_ascii=False))
        return 2
    evidence = None
    try:
        evidence = export_evidence(records, cases, route=route, limits=limits)
    except (PolicyFingerprintUnavailable, IdentityMismatch, ValueError):
        evidence = None
    if evidence is not None and evidence.llm_tested and evidence.case_count == len(cases):
        summary.llm_tested = True
        summary.llm_untested_reason = ""
        summary.enforce_eligible = True
        _atomic_write(args.output / "evidence.json", evidence_document(evidence))
    _write(args.output, records, summary)
    print(json.dumps({
        "mode": "llm",
        "llm_tested": summary.llm_tested,
        "enforce_eligible": summary.enforce_eligible,
        "enforce_enabled": False,
        "cases": len(records),
        "evidence": evidence is not None,
    }, ensure_ascii=False))
    return 0 if summary.llm_tested else 2


if __name__ == "__main__":
    sys.exit(main())
