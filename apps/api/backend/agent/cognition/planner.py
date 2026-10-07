"""Plan validation (C3) and execution order. Pure functions over the Plan contract."""
from __future__ import annotations

from dataclasses import dataclass

from backend.agent.contracts import Plan, PlanStep


@dataclass(frozen=True)
class PlanIssue:
    path: str
    problem: str      # type | missing | extra | range | value


def validate_plan(plan: Plan, read_tools: set[str]) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    keys = [step.key for step in plan.steps]
    for index, step in enumerate(plan.steps):
        prefix = f"steps.{index}"
        if keys.count(step.key) > 1:
            issues.append(PlanIssue(f"{prefix}.key", "value"))
        if not step.tools:
            issues.append(PlanIssue(f"{prefix}.tools", "missing"))
        issues.extend(PlanIssue(f"{prefix}.tools", "value") for tool in step.tools if tool not in read_tools)
        for dependency in step.dependsOn:
            if dependency == step.key or dependency not in keys:
                issues.append(PlanIssue(f"{prefix}.dependsOn", "value"))
    if not issues and execution_order(plan.steps) is None:
        issues.append(PlanIssue("steps", "value"))      # dependency cycle
    return issues


def execution_order(steps: list[PlanStep]) -> list[PlanStep] | None:
    """Stable topological order (declared order breaks ties); ``None`` when dependencies form a cycle."""
    remaining, done, ordered = list(steps), set(), []
    while remaining:
        ready = next((step for step in remaining if all(dep in done for dep in step.dependsOn)), None)
        if ready is None:
            return None
        remaining.remove(ready)
        done.add(ready.key)
        ordered.append(ready)
    return ordered
