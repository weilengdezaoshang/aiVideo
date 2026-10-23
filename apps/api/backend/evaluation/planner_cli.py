"""Run the labeled planner cases through RulePlanner and write a baseline.

Does not call a model, does not generate media, and does not read provider secrets.
Schema errors exit non-zero and do not write a passing summary.

Example:
  PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.planner_cli \\
    --output /tmp/aivideo-planner-rule-baseline --concurrency 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from backend.core.supervisor.planners import RulePlanner
from backend.evaluation.planner_cases import FIXTURE_PATH, PlannerCase, load_cases
from backend.evaluation.planner_evaluation import (
    RULE_BASELINE_MODE,
    evaluate_cases,
    mock_catalog,
    rule_context,
    rule_review,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RulePlanner baseline over labeled planner cases")
    parser.add_argument("--cases", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args(argv)


def _load(path: Path, limit: int | None) -> list[PlannerCase]:
    cases = load_cases(path)
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be >= 1")
        cases = cases[:limit]
    if not cases:
        raise ValueError("no planner cases to score")
    return cases


async def _run(args: argparse.Namespace) -> int:
    cases = _load(args.cases, args.limit)
    catalog = mock_catalog()

    def context_builder(case: PlannerCase) -> dict:
        return rule_context(case, catalog)

    records, summary = await evaluate_cases(
        cases,
        RulePlanner(),
        context_builder,
        rule_review,
        concurrency=args.concurrency,
        mode=RULE_BASELINE_MODE,
    )
    # Re-validate before any success file is treated as authoritative.
    payload = summary.model_dump(mode="json")
    summary.model_validate(payload)
    args.output.mkdir(parents=True, exist_ok=True)
    summary_path = args.output / "summary.json"
    cases_path = args.output / "cases.jsonl"
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [record.model_dump_json() for record in records]
    cases_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "mode": summary.mode,
        "case_count": summary.case_count,
        "coverage": summary.coverage,
        "tool_match_rate": summary.tool_match_rate,
        "order_match_rate": summary.order_match_rate,
        "order_match_denominator": summary.order_match_denominator,
        "invariants_match_rate": summary.invariants_match_rate,
        "first_review_pass_rate": summary.first_review_pass_rate,
        "first_review_pass_denominator": summary.first_review_pass_denominator,
        "llm_tested": summary.llm_tested,
        "summary": str(summary_path),
        "cases": str(cases_path),
    }, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.concurrency < 1:
            raise ValueError("--concurrency must be >= 1")
        return asyncio.run(_run(args))
    except (ValidationError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"planner-cli schema or input failure: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
