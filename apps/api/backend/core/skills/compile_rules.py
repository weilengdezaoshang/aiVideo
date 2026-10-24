"""Compile a skill playbook into reviewer intent rules.

Gate-① already rejects tools outside the tightened ``scope.tools``; these rules
add the playbook semantics: required phases must be present and confirmation
points must gate the phase they declare. Rules are pure functions of PlanView.
"""
from __future__ import annotations

from typing import Callable

from backend.core.review.contracts import PlanView, ReviewReason
from backend.core.review.policy import intent_reason

from .contracts import SkillSpec

IntentRule = Callable[[PlanView], list[ReviewReason]]


def _ancestors(plan_view: PlanView, step_ids: set[str]) -> set[str]:
    by_id = {step.id: step for step in plan_view.plan.steps}
    seen: set[str] = set()
    frontier = list(step_ids)
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        step = by_id.get(current)
        frontier.extend(dep for dep in (step.depends_on if step else []) if dep not in seen)
    return seen


def skill_intent_rules(spec: SkillSpec) -> list[IntentRule]:
    """One rule per required or confirming phase; boundaries stay advisory prose.

    A confirming phase gates its successor: every tool step of the next phase
    must reach back to an ask_user step, so the user confirms the phase output
    before downstream work starts.
    """
    required = [phase for phase in spec.phases if phase.required]
    confirming = []
    for index, phase in enumerate(spec.phases):
        if phase.confirm and index + 1 < len(spec.phases):
            confirming.append((phase, spec.phases[index + 1]))

    def phase_rule(phase):
        def rule(plan_view: PlanView) -> list[ReviewReason]:
            steps = plan_view.plan.steps
            tools_used = {step.tool for step in steps if step.kind == "call_tool" and step.tool}
            if not (set(phase.tools) & tools_used):
                return [intent_reason("skill_phase_missing",
                    f"技能「{spec.name}」阶段「{phase.name}」需要以下工具之一：{('、'.join(phase.tools))}")]
            return []
        return rule

    def confirmation_rule(phase, successor):
        def rule(plan_view: PlanView) -> list[ReviewReason]:
            steps = plan_view.plan.steps
            gated = [step for step in steps
                     if step.kind == "call_tool" and step.tool in set(successor.tools)]
            if not gated:
                return []
            ask_ids = {step.id for step in steps if step.kind == "ask_user"}
            if all(_ancestors(plan_view, {step.id}) & ask_ids for step in gated):
                return []
            return [intent_reason("skill_confirmation_missing",
                f"技能「{spec.name}」阶段「{phase.name}」确认后才能进入「{successor.name}」")]
        return rule

    return [phase_rule(phase) for phase in required] + \
           [confirmation_rule(phase, successor) for phase, successor in confirming]
