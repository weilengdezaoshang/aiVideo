"""Tool registry and gateway: validate, execute, wrap into a numbered evidence envelope."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

from backend.agent import evidence as ev
from backend.agent.contracts import Coverage, FailureDetails, NextAction, Omitted, Page
from backend.agent.tools.errors import ToolError


@dataclass(frozen=True)
class ToolContext:
    snapshot: dict
    assets: dict
    media_root: Path
    max_output_bytes: int


@dataclass(frozen=True)
class Outcome:
    """What a handler measured. ``count`` is the number of returned items for list-like tools."""
    data: dict
    coverage: Coverage
    page: Page | None = None
    omitted: Omitted = field(default_factory=Omitted)
    count: int | None = None
    asset_ids: tuple[str, ...] = ()

    def root(self) -> dict:
        """The object fact paths resolve against: the result minus its own evidence."""
        return {"data": self.data, "page": self.page.model_dump(mode="json") if self.page else None,
                "coverage": self.coverage.model_dump(mode="json"),
                "omitted": self.omitted.model_dump(mode="json")}


Handler = Callable[[BaseModel, ToolContext, "int | None"], Outcome]
FactsFn = Callable[["Outcome", BaseModel], "list[ev.RawFact]"]
SuggestFn = Callable[["Outcome", BaseModel], "list[NextAction]"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: int
    description: str
    input: type[BaseModel]
    kind: Literal["read", "control", "effect"]
    stages: frozenset[str]
    timeout_s: float
    handler: Handler | None
    facts: FactsFn
    suggest: SuggestFn


class ToolGateway:
    def __init__(self, specs: list[ToolSpec]):
        self.specs = {spec.name: spec for spec in specs}

    def schemas(self) -> list[dict]:
        return [{"type": "function", "function": {"name": spec.name, "description": spec.description,
                  "parameters": spec.input.model_json_schema(by_alias=True)}}
                for spec in self.specs.values()]

    def _checked_next(self, suggestions: list[NextAction]) -> list[NextAction]:
        """Drop suggestions whose ready-made arguments would not pass their own tool's schema."""
        kept = []
        for item in suggestions:
            if item.action == "call_tool" and item.arguments is not None:
                spec = self.specs.get(item.tool or "")
                if spec is None:
                    continue
                try:
                    spec.input.model_validate(item.arguments)
                except ValidationError:
                    continue
            kept.append(item)
        return kept

    def execute(self, name: str, args: dict, snapshot: dict, assets: dict, media_root: Path | str, *,
                max_output_bytes: int, ordinal: int, call_id: str, step_key: str | None = None) -> dict:
        meta: dict[str, Any] = dict(tool=name, ordinal=ordinal, call_id=call_id, step_key=step_key,
                                    snapshot=snapshot)
        spec = self.specs.get(name)
        if spec is None or spec.handler is None:
            return ev.error_envelope(**meta, code="UNKNOWN_TOOL", message="工具不存在或不在只读工具列表中",
                details=FailureDetails(allowedTools=sorted(self.specs)))
        try:
            parsed = spec.input.model_validate(args)
        except ValidationError as error:
            return ev.error_envelope(**meta, code="INVALID_TOOL_ARGUMENTS", message="工具参数无效",
                details=ev.failure_from_validation(error))
        context = ToolContext(snapshot, assets, Path(media_root), max_output_bytes)
        keep: int | None = None
        try:
            while True:
                outcome = spec.handler(parsed, context, keep)
                root = outcome.root()
                facts = [item for item in spec.facts(outcome, parsed) if ev.fact_is_grounded(root, item)]
                envelope = ev.ok_envelope(**meta, asset_ids=list(outcome.asset_ids), data=outcome.data, facts=facts,
                    coverage=outcome.coverage, omitted=outcome.omitted, page=outcome.page,
                    next_actions=self._checked_next(spec.suggest(outcome, parsed)))
                if ev.size_of(envelope) <= max_output_bytes:
                    return envelope
                if not outcome.count or outcome.count <= 1:
                    raise ToolError("TOOL_OUTPUT_LIMIT", "工具结果超过大小限制，请减少查询的对象数量",
                        FailureDetails(limit={"maxBytes": max_output_bytes}))
                keep = outcome.count - max(1, outcome.count // 4)
        except ToolError as error:
            return ev.error_envelope(**meta, code=error.code, message=error.message, details=error.details)
