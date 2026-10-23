"""Locked-catalog planning tools. Retrieval is local BM25; the directory stays the only source."""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from backend.core.supervisor.contracts import Settings
from backend.core.tools.catalog import ToolCatalog
from backend.core.tools.contracts import VersionLock

_PLAN_METADATA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "depends_on": {"type": "array", "items": {"type": "string", "pattern": r"^t[1-9][0-9]*$"}},
        "placement": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"enum": ["right", "below", "replace", "region"]},
                "anchor_id": {"type": "string"},
                "region_id": {"type": "string"},
            },
        },
    },
}
_VISIBLE_MODES = frozenset({"read_only", "generate", "deterministic"})
_CONTROL_ORDER = ("ask_user_form", "answer_only", "set_invariants")
_TOKEN = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")

_CONTROLS = {
    "ask_user_form": {
        "description": "一次问完仍缺的结构化字段。不生成、不改 Run。禁止 HTML。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "fields"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "fields": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "type", "label"],
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "type": {"enum": ["text", "number", "boolean", "choice"]},
                            "label": {"type": "string", "minLength": 1},
                            "required": {"type": "boolean"},
                            "default": {"type": ["string", "number", "boolean", "null"]},
                            "choices": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
    },
    "answer_only": {
        "description": "只回答用户，不创建生成任务。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "minLength": 1}},
        },
    },
    "set_invariants": {
        "description": "只提案冻结约束，必须附用户原话和位置。不能修改 Run。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["invariants", "source_quote", "source"],
            "properties": {
                "invariants": {"type": "object"},
                "source_quote": {"type": "string", "minLength": 1},
                "source": {"type": "string", "minLength": 1},
                "location": {"type": "string", "minLength": 1},
            },
        },
    },
}


@dataclass(frozen=True)
class PlanningToolList:
    tools: list[dict]
    catalog_version: str
    catalog_hash: str
    retrieval_version: str
    retrieved: bool


def planning_tools(
    catalog: ToolCatalog,
    user: Mapping[str, object],
    version_lock: VersionLock,
    *,
    scope: Mapping[str, object] | None = None,
    query: str = "",
    settings: Settings | None = None,
) -> PlanningToolList:
    """Decorate exported tools for planning. Does not copy execution bindings or mutate the catalog."""

    settings = settings or Settings()
    catalog.check_lock(version_lock)
    allowed = _scope_names(scope)
    visible = []
    for entry in catalog.to_model_tools(user, version_lock):
        name = entry["function"]["name"]
        if name == "spawn_helper":
            continue
        tool = catalog.get_tool(name, version_lock.version)
        if tool.mode not in _VISIBLE_MODES or not tool.available:
            continue
        if allowed is not None and name not in allowed:
            continue
        visible.append(_decorate(entry))
    retrieved = len(visible) > settings.planner_tool_threshold
    if retrieved:
        visible = _retrieve(catalog, version_lock, visible, query, settings)
    controls = [_control(name) for name in _CONTROL_ORDER]
    return PlanningToolList(
        tools=visible + controls,
        catalog_version=version_lock.version,
        catalog_hash=version_lock.content_hash,
        retrieval_version=settings.planner_retrieval_version,
        retrieved=retrieved,
    )


def _scope_names(scope: Mapping[str, object] | None) -> set[str] | None:
    if scope is None or "tools" not in scope:
        return None
    return {str(name) for name in scope.get("tools") or []}


def _decorate(entry: dict) -> dict:
    function = dict(entry["function"])
    parameters = copy.deepcopy(function["parameters"])
    properties = dict(parameters.get("properties") or {})
    properties["_plan"] = copy.deepcopy(_PLAN_METADATA)
    parameters["properties"] = properties
    function["parameters"] = parameters
    return {"type": "function", "function": function}


def _control(name: str) -> dict:
    spec = _CONTROLS[name]
    return {"type": "function", "function": {
        "name": name,
        "description": spec["description"],
        "parameters": copy.deepcopy(spec["parameters"]),
    }}


def _retrieve(catalog, version_lock, visible, query: str, settings: Settings) -> list[dict]:
    ranked = _bm25(query, visible, settings)
    limit = settings.planner_tool_candidates
    chosen = ranked[:limit]
    names = {entry["function"]["name"] for entry in chosen}
    for entry in list(chosen):
        tool = catalog.get_tool(entry["function"]["name"], version_lock.version)
        if not any(port.media_type == "mask" for port in tool.inputs):
            continue
        for candidate in visible:
            producer = catalog.get_tool(candidate["function"]["name"], version_lock.version)
            if producer.name in names or not producer.available:
                continue
            if any(port.media_type == "mask" for port in producer.outputs):
                chosen.append(candidate)
                names.add(producer.name)
    return chosen


def _bm25(query: str, tools: list[dict], settings: Settings) -> list[dict]:
    k1, b = settings.planner_bm25_k1, settings.planner_bm25_b
    docs = [_tokens(f"{entry['function']['name']} {entry['function'].get('description', '')} "
                    + " ".join((entry["function"].get("parameters") or {}).get("properties") or {}))
            for entry in tools]
    query_terms = _tokens(query)
    count = len(docs) or 1
    avg = sum(len(doc) for doc in docs) / count
    df: dict[str, int] = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    scored = []
    for index, entry in enumerate(tools):
        score = 0.0
        frequencies: dict[str, int] = {}
        for term in docs[index]:
            frequencies[term] = frequencies.get(term, 0) + 1
        for term in query_terms:
            if term not in frequencies:
                continue
            idf = math.log(1 + (count - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            freq = frequencies[term]
            denom = freq + k1 * (1 - b + b * len(docs[index]) / (avg or 1))
            score += idf * freq * (k1 + 1) / denom
        scored.append((-score, entry["function"]["name"], index, entry))
    scored.sort()
    return [item[-1] for item in scored]


def _tokens(text: str) -> list[str]:
    parts = [part.lower() for part in _TOKEN.findall(text)]
    split = [piece for part in parts if "_" in part for piece in part.split("_") if piece]
    chars = [part for part in parts if len(part) == 1 and "\u4e00" <= part <= "\u9fff"]
    bigrams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    return parts + split + bigrams
