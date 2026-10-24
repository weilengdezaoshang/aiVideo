"""Draft a personal skill from one successful run: concrete values become parameters."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from backend.core.orm import CorePlan, CoreRun, CoreTask
from backend.core.states import EntityNotFound

from .contracts import SkillPhase, SkillSpec

# Scalar parameters worth templating; references ($tN.output / asset dicts) stay verbatim.
SLOT_PARAMETERS = ("prompt", "count", "width", "height", "duration_seconds", "fps", "seed")


def extract_defaults(steps: list[dict]) -> tuple[list[dict], dict]:
    """Return templated steps plus the original values as defaults.

    Prompts get positional slots ({{prompt}}, {{prompt_2}}); numeric knobs share
    one slot per equal value and gain a numeric suffix on conflict.
    """
    defaults: dict = {}
    templated = []
    prompts = 0
    for step in steps:
        parameters = dict(step.get("parameters") or {})
        slots: dict[str, str] = {}
        for key in SLOT_PARAMETERS:
            value = parameters.get(key)
            if key == "prompt":
                if isinstance(value, str) and value.strip():
                    prompts += 1
                    slot = "{{prompt}}" if prompts == 1 else f"{{{{prompt_{prompts}}}}}"
                    slots[key], defaults[slot] = slot, value
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            base = f"{{{{{key}}}}}"
            if base not in defaults:
                slots[key], defaults[base] = base, value
            elif defaults[base] == value:
                slots[key] = base
            else:
                suffix = 2
                while f"{{{{{key}_{suffix}}}}}" in defaults:
                    suffix += 1
                slot = f"{{{{{key}_{suffix}}}}}"
                slots[key], defaults[slot] = slot, value
        templated.append({"tool": step.get("tool"), "kind": step.get("kind", "call_tool"),
                          "parameters": {**parameters, **slots},
                          "depends_on": list(step.get("depends_on") or [])})
    return templated, defaults


def fill_parameters(steps: list[dict], values: dict) -> list[dict]:
    """Apply user-supplied slot values to a templated draft (unknown slots rejected)."""
    filled = []
    for step in steps:
        parameters = {}
        for key, value in (step.get("parameters") or {}).items():
            if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
                if value not in values:
                    raise ValueError(f"missing slot value {value}")
                parameters[key] = values[value]
            else:
                parameters[key] = value
        filled.append({**step, "parameters": parameters})
    return filled


async def draft_from_run(tx, run_id: uuid.UUID) -> SkillSpec:
    """One phase per distinct tool, original values as defaults, message as the scene."""
    run = await tx.get(CoreRun, run_id)
    if run is None or run.status != "completed":
        raise EntityNotFound("run_not_completed")
    plan = await tx.scalar(select(CorePlan).where(CorePlan.run_id == run.id,
        CorePlan.stage != "obsolete").order_by(CorePlan.version.desc()).limit(1))
    if plan is None:
        raise EntityNotFound("run_plan_missing")
    tasks = (await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id,
        CoreTask.status == "succeeded", CoreTask.kind == "call_tool")
        .order_by(CoreTask.created_at, CoreTask.task_key))).all()
    steps = [{"tool": task.tool_ref, "kind": "call_tool",
              "parameters": dict(task.parameters or {}), "depends_on": []} for task in tasks]
    if not steps:
        raise EntityNotFound("run_has_no_tool_steps")
    templated, defaults = extract_defaults(steps)
    message = str((run.snapshot or {}).get("request", {}).get("message") or "")
    tools = list(dict.fromkeys(step["tool"] for step in templated if step["tool"]))
    return SkillSpec(name="我的技能 " + message[:20].strip(), version="1",
        scenes=[message[:200]] if message else ["从一次成功运行保存的技能"],
        role="按保存的流程复用这次创作", body="阶段：\n" + "\n".join(
            f"- {tool}" for tool in tools),
        allowlist=tools,
        phases=[SkillPhase(name=tool, tools=[tool], required=True) for tool in tools],
        routing={}, boundaries=[], defaults=defaults, source="user")
