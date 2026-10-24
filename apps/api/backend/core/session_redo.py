"""Copy an executable plan and replace seeds. Does not call a planner."""
from __future__ import annotations

import json

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.contracts import Contract
from backend.core.supervisor.contracts import Plan
from backend.core.tools.contracts import CatalogSnapshot


class RedoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_REDO_", frozen=True, extra="forbid")
    seed_minimum: int = Field(default=0, ge=0)
    seed_maximum: int = Field(default=2**31 - 1, ge=0)


class SeedUnsupported(ValueError):
    code = "seed_unsupported"

    def __init__(self, tool: str):
        self.tool = tool
        super().__init__(f"seed_unsupported:{tool}")


class SeedOutOfRange(ValueError):
    code = "seed_out_of_range"

    def __init__(self, seed: object):
        self.seed = seed
        super().__init__(f"seed_out_of_range:{seed}")


class SeedInvalid(ValueError):
    code = "seed_invalid"

    def __init__(self, seed: object):
        self.seed = seed
        super().__init__(f"seed_invalid:{seed}")


class SeedUnchanged(ValueError):
    code = "seed_unchanged"

    def __init__(self, tool: str):
        self.tool = tool
        super().__init__(f"seed_unchanged:{tool}")


class RedoNotExecutable(ValueError):
    code = "redo_not_executable"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"redo_not_executable:{reason}")


class SeedRange(Contract):
    minimum: int = Field(ge=0)
    maximum: int = Field(ge=0)


def _bounds(tool, configured: SeedRange) -> tuple[int, int]:
    props = (tool.parameters.get("properties") or {}).get("seed") or {}
    low = configured.minimum
    high = configured.maximum
    if "minimum" in props:
        low = max(low, int(props["minimum"]))
    if "maximum" in props:
        high = min(high, int(props["maximum"]))
    return low, high


def _supported(tool) -> bool:
    if tool is None or tool.mode != "generate":
        return False
    properties = (tool.parameters.get("properties") or {})
    return tool.execution.binding.get("seed_supported") is True and "seed" in properties


def occupied_seed(parameters: dict) -> int | None:
    """Seed the staged adapter would actually send.

    A missing or null seed is not "unset": StagedAdapter.resolve_seed returns 0.
    """
    if "seed" not in parameters or parameters.get("seed") is None:
        return 0
    value = parameters["seed"]
    if type(value) is int:
        return value
    return None


def clone_plan(plan: Plan, catalog: CatalogSnapshot, seed: int, *,
               seed_range: SeedRange | None = None, settings: RedoSettings | None = None) -> Plan:
    """Copy step identity, order, dependencies and non-seed parameters.

    Generate steps whose catalog snapshot advertises seed support receive `seed`.
    A generate step without that capability fails the whole copy.
    Read-only and deterministic steps are copied without a new random parameter.
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise SeedInvalid(seed)
    settings = settings or RedoSettings()
    configured = seed_range or SeedRange(minimum=settings.seed_minimum, maximum=settings.seed_maximum)
    if configured.minimum > configured.maximum or seed < configured.minimum or seed > configured.maximum:
        raise SeedOutOfRange(seed)
    if plan.result_type == "answer_only" or not any(step.kind == "call_tool" and step.tool for step in plan.steps):
        raise RedoNotExecutable(plan.result_type if plan.result_type == "answer_only" else "ask_user_only")
    tools = {tool.name: tool for tool in catalog.tools}
    copied = Plan.model_validate(json.loads(plan.model_dump_json()))
    steps = []
    for step in copied.steps:
        if step.kind != "call_tool" or not step.tool:
            steps.append(step)
            continue
        tool = tools.get(step.tool)
        if tool is None:
            raise SeedUnsupported(step.tool)
        if tool.mode != "generate":
            steps.append(step)
            continue
        if not _supported(tool):
            raise SeedUnsupported(step.tool)
        low, high = _bounds(tool, configured)
        if seed < low or seed > high:
            raise SeedOutOfRange(seed)
        previous = occupied_seed(step.parameters)
        if previous is not None and previous == seed:
            if low == high:
                raise SeedOutOfRange(seed)
            raise SeedUnchanged(step.tool)
        steps.append(step.model_copy(update={"parameters": {**step.parameters, "seed": seed}}))
    return copied.model_copy(update={"steps": steps})
