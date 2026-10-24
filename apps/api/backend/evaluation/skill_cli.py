"""Offline skill-playbook gate: every official skill's suite must fully pass.

Exit codes mirror the selfcheck convention: 0 = ran and honest (all green),
2 = anything failed, missing, or dishonest. No network, no model.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .skill_cases import load_cases
from .skill_evaluation import evaluate_cases, suite_paths

SKILLS = ("ecommerce", "poster", "storyboard", "all")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="官方技能评测门禁")
    parser.add_argument("--skill", choices=SKILLS, default="all")
    parser.add_argument("--cases", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.skill != "all":
        cases = [case for case in cases if case.skill == args.skill]
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2
    summary = evaluate_cases(cases)
    suite_paths(args.output, summary)
    print({key: summary[key] for key in ("total", "passed", "failed")})
    for grade in summary["grades"]:
        if not grade["passed"]:
            print(f"FAILED {grade['case_id']}: {grade['reason']}", file=sys.stderr)
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
