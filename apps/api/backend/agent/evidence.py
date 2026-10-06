"""Build tool envelopes: numbered facts, coverage and next-step suggestions.

Everything here is deterministic and fixed-template. Canvas text may travel inside
``data`` or as a ``Fact.value`` but never inside ``Fact.statement`` or ``reason``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any

from pydantic import ValidationError

from backend.agent.contracts import (
    Coverage, DuplicateEnvelope, ErrorEnvelope, FailureDetails, Fact, NextAction, OkEnvelope,
    OkResult, Omitted, Page, Source, ToolFailure,
)
from backend.agent.contracts.evidence import FieldProblem, MAX_NEXT_ACTIONS

KIND_LABELS = {"image": "图片", "video": "视频", "placeholder": "生成占位", "error": "失败", "board": "画板",
               "text": "文字", "draft": "草稿"}
TOOL_VERSION = {"read_canvas": 1, "list_canvas_elements": 2, "get_canvas_elements": 2, "inspect_image": 1}
MAX_ITEM_FACTS = 20


@dataclass(frozen=True)
class RawFact:
    """A fact before numbering; ``value`` must come from the subtree at ``path``."""
    statement: str
    path: str
    value: Any
    scope: str


@dataclass(frozen=True)
class FailureSpec:
    category: str
    retryable: bool


FAILURES: dict[str, FailureSpec] = {
    "INVALID_TOOL_ARGUMENTS": FailureSpec("invalid_arguments", True),
    "UNKNOWN_TOOL": FailureSpec("policy", False),
    "TOOL_NOT_ALLOWED": FailureSpec("policy", False),
    "CIRCUIT_OPEN": FailureSpec("policy", False),
    "OBJECT_OUT_OF_SCOPE": FailureSpec("out_of_scope", True),
    "ASSET_OUT_OF_SCOPE": FailureSpec("out_of_scope", True),
    "ASSET_UNAVAILABLE": FailureSpec("unavailable", False),
    "IMAGE_UNREADABLE": FailureSpec("unavailable", False),
    "ASSET_CHANGED": FailureSpec("changed", False),
    "IMAGE_LIMIT": FailureSpec("limit", False),
    "TOOL_OUTPUT_LIMIT": FailureSpec("limit", False),
    "TOOL_TIMEOUT": FailureSpec("internal", False),
    "TOOL_FAILED": FailureSpec("internal", False),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence_id(ordinal: int) -> str:
    return f"E{ordinal}"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def size_of(envelope: dict) -> int:
    return len(dumps(envelope).encode())


def resolve_path(data: Any, path: str) -> Any:
    """``$`` is the root, ``a.b[0].c`` walks keys and indexes, ``.length`` counts a list."""
    if path == "$":
        return data
    current = data
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            current = current[int(part[1:-1])]
        elif part == "length" and isinstance(current, list):
            current = len(current)
        else:
            current = current[part]
    return current


def _leaves(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from _leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from _leaves(child)
    else:
        yield value


def fact_is_grounded(data: Any, fact: RawFact | Fact) -> bool:
    """Every element of ``fact.value`` must be a leaf under ``fact.path`` (or the count)."""
    try:
        subtree = resolve_path(data, fact.path)
    except (KeyError, IndexError, ValueError, TypeError):
        return False
    leaves = list(_leaves(subtree))
    values = fact.value if isinstance(fact.value, list) else [fact.value]
    return all(any(value == leaf and type(value) is type(leaf) for leaf in leaves) for value in values)


def number_facts(ordinal: int, raw: list[RawFact]) -> list[Fact]:
    return [Fact(id=f"{evidence_id(ordinal)}.f{index}", statement=item.statement, path=item.path,
                 value=item.value, scope=item.scope) for index, item in enumerate(raw, 1)]


def _source(snapshot: dict, asset_ids: list[str]) -> Source:
    return Source(documentId=str(snapshot["id"]), revision=snapshot["revision"], assetIds=asset_ids)


def ok_envelope(*, tool: str, ordinal: int, call_id: str, step_key: str | None, snapshot: dict,
                asset_ids: list[str], data: dict, facts: list[RawFact], coverage: Coverage,
                next_actions: list[NextAction], omitted: Omitted, page: Page | None) -> dict:
    result = OkResult(data=data, evidence=number_facts(ordinal, facts), coverage=coverage,
                      next=next_actions[:MAX_NEXT_ACTIONS], truncated=not coverage.complete,
                      omitted=omitted, page=page)
    envelope = OkEnvelope(evidenceId=evidence_id(ordinal), tool=tool, toolVersion=TOOL_VERSION.get(tool, 1),
                          callId=call_id, stepKey=step_key, source=_source(snapshot, asset_ids),
                          producedAt=now_iso(), result=result)
    return envelope.model_dump(mode="json")


def next_for_error(code: str, tool: str) -> list[NextAction]:
    def call(name: str, arguments: dict | None, reason: str, priority: str = "required") -> NextAction:
        return NextAction(action="call_tool", tool=name, arguments=arguments, reason=reason, priority=priority)

    blocker = NextAction(action="report_blocker", tool=None, arguments=None,
                         reason="该读取无法完成，请在回答中说明限制", priority="required")
    if code == "OBJECT_OUT_OF_SCOPE":
        return [call("list_canvas_elements", {}, "获取有效对象 ID 后重新查询")]
    if code == "ASSET_OUT_OF_SCOPE":
        return [call("list_canvas_elements", {"kind": "image"}, "获取画布中真实存在的图片素材 ID")]
    if code == "INVALID_TOOL_ARGUMENTS":
        return [call(tool, None, "按 details.fields 指出的字段修正参数后重试")]
    if code in {"UNKNOWN_TOOL", "TOOL_NOT_ALLOWED"}:
        return []
    return [blocker]


def failure_from_validation(error: ValidationError) -> FailureDetails:
    fields: list[FieldProblem] = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        kind = item["type"]
        if kind == "missing":
            problem = "missing"
        elif kind == "extra_forbidden":
            problem = "extra"
        elif kind.endswith("_type") or kind.endswith("_parsing") or kind in {"bool_type", "dict_type"}:
            problem = "type"
        elif kind.startswith(("greater_than", "less_than", "too_short", "too_long", "string_too")):
            problem = "range"
        else:
            problem = "value"
        fields.append(FieldProblem(path=".".join(str(part) for part in item["loc"]) or "$", problem=problem))
    return FailureDetails(fields=fields[:20])


def error_envelope(*, tool: str, ordinal: int, call_id: str, step_key: str | None, snapshot: dict,
                   code: str, message: str, details: FailureDetails | None = None,
                   next_actions: list[NextAction] | None = None) -> dict:
    spec = FAILURES.get(code, FailureSpec("internal", False))
    failure = ToolFailure(code=code, category=spec.category, message=message, retryable=spec.retryable,
                          details=details or FailureDetails(),
                          next=(next_for_error(code, tool) if next_actions is None else next_actions))
    envelope = ErrorEnvelope(evidenceId=evidence_id(ordinal), tool=tool, toolVersion=TOOL_VERSION.get(tool, 1),
                             callId=call_id, stepKey=step_key, source=_source(snapshot, []),
                             producedAt=now_iso(), error=failure)
    return envelope.model_dump(mode="json")


def duplicate_envelope(*, tool: str, ordinal: int, call_id: str, step_key: str | None, snapshot: dict,
                       dedupe_of: str, next_actions: list[NextAction]) -> dict:
    envelope = DuplicateEnvelope(evidenceId=evidence_id(ordinal), tool=tool, toolVersion=TOOL_VERSION.get(tool, 1),
                                 callId=call_id, stepKey=step_key, source=_source(snapshot, []),
                                 producedAt=now_iso(), dedupeOf=dedupe_of, next=next_actions[:MAX_NEXT_ACTIONS])
    return envelope.model_dump(mode="json")
