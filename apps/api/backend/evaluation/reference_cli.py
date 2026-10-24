"""Score session-reference cases with the pure resolver. No model and no media.

Example:
  PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.reference_cli \\
    --output /tmp/aivideo-reference-rule
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.evaluation.reference_cases import FIXTURE_PATH, load_cases
from backend.evaluation.reference_evaluation import score_case, summarize


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rule baseline for session reference cases")
    parser.add_argument("--cases", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases = load_cases(args.cases)
    scores = [score_case(case) for case in cases]
    summary = summarize(scores, cases)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(
        json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "cases.jsonl").write_text(
        "".join(item.model_dump_json() + "\n" for item in scores), encoding="utf-8")
    print(json.dumps({
        "case_count": summary.case_count,
        "rule_pass": summary.rule_pass,
        "llm_tested": summary.llm_tested,
    }, ensure_ascii=False))
    return 0 if summary.rule_pass == summary.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
