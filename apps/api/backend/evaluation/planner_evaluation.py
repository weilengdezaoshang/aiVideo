"""Independent planner scoring and a live RulePlanner baseline.

This module does not call a model, does not read provider credentials, and does not
synthesize observations from `expected`. Latency and tokens come from the planner
object that `evaluate_cases` actually invokes. A zero-token rule run is recorded
only with `mode=rule_baseline`.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.core.review.contracts import Invariants
from backend.core.supervisor.reviewer import review as supervisor_review
from backend.core.tools.catalog import ToolCatalog
from backend.core.tools.contracts import (
    CatalogSnapshot,
    Execution,
    Port,
    Price,
    ToolDefinition,
)
from backend.evaluation.planner_cases import ExpectedPlan, PlannerCase

SCHEMA_VERSION = 1
RULE_BASELINE_MODE = "rule_baseline"

# Single width/height/fps so RulePlanner.single_step can emit a step. Durations stay
# a set: the rule still refuses a stated length that is not in this enum.
_IMAGE_EDGE = 1024
_DURATION_ENUM = [4, 5, 6, 8]
_FPS = 24


class Score(BaseModel):
    """Per-case booleans. `None` means the metric has no denominator for this row."""

    model_config = ConfigDict(extra="forbid")

    tool_match: bool
    order_match: bool | None
    order_required: bool
    invariants_match: bool
    bindings_match: bool
    must_ask_match: bool
    first_review_pass: bool | None
    review_unavailable_reason: str | None = None


class CaseEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    case_id: str
    category: str
    language: str
    mode: str
    input: dict
    output: dict | None = None
    review: dict | None = None
    usage: dict
    latency_ms: float | None = None
    errors: list[str] = Field(default_factory=list)
    score: Score | None = None


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    mode: str
    llm_tested: bool = False
    llm_untested_reason: str
    case_count: int
    scored_count: int
    coverage: float | None
    tool_match_rate: float | None
    tool_match_denominator: int
    order_match_rate: float | None
    order_match_denominator: int
    invariants_match_rate: float | None
    invariants_match_denominator: int
    first_review_pass_rate: float | None
    first_review_pass_denominator: int
    review_unavailable: int
    latency_ms: dict
    tokens: dict
    errors: int


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _steps(plan: Any) -> list[Any]:
    if isinstance(plan, dict):
        return list(plan.get("steps") or [])
    return list(getattr(plan, "steps", []) or [])


def _get(step: Any, key: str, default: Any = None) -> Any:
    if isinstance(step, dict):
        return step.get(key, default)
    return getattr(step, key, default)


def _result_type(plan: Any) -> str | None:
    if isinstance(plan, dict):
        value = plan.get("result_type")
    else:
        value = getattr(plan, "result_type", None)
    return value if isinstance(value, str) else None


def observed_tools(plan: Any) -> list[str]:
    """Tool choices in step order. `set_invariants` is not a choice.

    `ask_user` steps map to `ask_user_form`. `answer_only` is read from the plan
    result type, and only when the plan did not also emit a tool or ask step.
    """

    names: list[str] = []
    for step in _steps(plan):
        kind = _get(step, "kind", "call_tool")
        tool = _get(step, "tool")
        if tool == "set_invariants" or kind == "set_invariants":
            continue
        if kind == "ask_user" or tool in {"ask_user", "ask_user_form"}:
            names.append("ask_user_form")
            continue
        if tool:
            names.append(tool)
    if _result_type(plan) == "answer_only" and not names:
        return ["answer_only"]
    return names


def _index_by_id(plan: Any) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for step in _steps(plan):
        step_id = _get(step, "id")
        if isinstance(step_id, str):
            indexed[step_id] = step
    return indexed


def _depends(step: Any) -> list[str]:
    raw = _get(step, "depends_on", []) or []
    return [item for item in raw if isinstance(item, str)]


def order_match(expected: ExpectedPlan, plan: Any) -> bool | None:
    """Dependency edges, not list order and not parent-id array order.

    No ordered requirement => None. `depends_on: ["t2", "t1"]` matches `["t1", "t2"]`
    when both parents still appear earlier in the step list.
    """

    if not expected.dependencies:
        return None
    indexed = _index_by_id(plan)
    positions = {step_id: index for index, step_id in enumerate(indexed)}
    for task_id, deps in expected.dependencies.items():
        step = indexed.get(task_id)
        if step is None:
            return False
        actual = _depends(step)
        if sorted(actual) != sorted(deps):
            return False
        for dep in deps:
            if dep not in positions or positions[dep] >= positions[task_id]:
                return False
    return True


def _invariants_payload(value: Any) -> Any:
    if value is None:
        return {}
    dumped = _dump(value) if not isinstance(value, dict) else value
    if not isinstance(dumped, dict):
        return dumped
    deliverable = dumped.get("deliverable")
    if hasattr(deliverable, "model_dump"):
        deliverable = deliverable.model_dump()
    if isinstance(deliverable, dict) and deliverable.get("required_asset_ids") is None:
        deliverable = {**deliverable, "required_asset_ids": []}
        dumped = {**dumped, "deliverable": deliverable}
    return dumped


def _canonical_deliverable(invariants: Invariants) -> dict[str, Any]:
    """Drop unset fields. Missing, null, and [] required assets are the same claim."""

    if invariants.deliverable is None:
        return {}
    fields = invariants.deliverable.model_dump(exclude_none=True)
    asset_ids = fields.get("required_asset_ids") or []
    if asset_ids:
        fields["required_asset_ids"] = sorted(str(item) for item in asset_ids)
    else:
        fields.pop("required_asset_ids", None)
    return fields


def _contains_bool(value: Any) -> bool:
    """Pydantic would coerce True to 1. A boolean count is not a legal invariant."""

    if isinstance(value, bool):
        return True
    if isinstance(value, dict):
        return any(_contains_bool(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_bool(item) for item in value)
    return False


def _strict_deliverable(value: Any) -> dict[str, Any] | None:
    """`None` means the payload is not a legal Invariants object."""

    payload = _invariants_payload(value)
    if _contains_bool(payload):
        return None
    try:
        parsed = Invariants.model_validate(payload)
    except ValidationError:
        return None
    return _canonical_deliverable(parsed)


def invariants_match(expected: ExpectedPlan, actual_invariants: Any) -> bool:
    """Normalized deliverable equality after core.review.Invariants validation.

    Unknown root keys, unknown deliverable keys, and illegal types fail.
    Omitted `required_asset_ids`, null, and [] match. Key order does not matter.
    """

    expected_fields = _strict_deliverable(expected.invariants)
    actual_fields = _strict_deliverable(actual_invariants)
    if expected_fields is None or actual_fields is None:
        return False
    return actual_fields == expected_fields


def _binding_value(step: Any, field: str) -> Any:
    parameters = _get(step, "parameters", {}) or {}
    if not isinstance(parameters, dict):
        return None
    return parameters.get(field)


def bindings_match(expected: ExpectedPlan, plan: Any) -> bool:
    indexed = _index_by_id(plan)
    for key, ref in expected.bindings.items():
        if "." not in key:
            return False
        task_id, field = key.split(".", 1)
        step = indexed.get(task_id)
        if step is None or _binding_value(step, field) != ref:
            return False
    return True


def _ask_blob(plan: Any) -> str:
    parts: list[str] = []
    for step in _steps(plan):
        kind = _get(step, "kind", "call_tool")
        tool = _get(step, "tool")
        if kind != "ask_user" and tool not in {"ask_user", "ask_user_form"}:
            continue
        parameters = _get(step, "parameters", {}) or {}
        if isinstance(parameters, dict):
            fields = parameters.get("fields") or parameters.get("missing") or []
            if isinstance(fields, list):
                parts.extend(str(item) for item in fields)
            question = parameters.get("question")
            if isinstance(question, str):
                parts.append(question)
        parts.append(str(parameters))
    return "\n".join(parts)


def must_ask_match(expected: ExpectedPlan, plan: Any) -> bool:
    if not expected.must_ask:
        return True
    blob = _ask_blob(plan)
    return all(field in blob for field in expected.must_ask)


def _review_verdict(actual_review: Any) -> tuple[bool | None, str | None]:
    if actual_review is None:
        return None, "reviewer_returned_null"
    dumped = _dump(actual_review)
    if not isinstance(dumped, dict):
        return None, "reviewer_returned_non_object"
    reason = dumped.get("unavailable_reason")
    if dumped.get("verdict") is None:
        return None, str(reason or "review_verdict_missing")
    return dumped.get("verdict") == "pass", None


def score(
    expected: ExpectedPlan,
    actual_plan: Any,
    actual_invariants: Any,
    actual_review: Any,
) -> Score:
    """Compare one labeled case with one observed plan. Does not call a planner."""

    passed, unavailable = _review_verdict(actual_review)
    return Score(
        tool_match=observed_tools(actual_plan) == list(expected.tool_names),
        order_match=order_match(expected, actual_plan),
        order_required=bool(expected.dependencies),
        invariants_match=invariants_match(expected, actual_invariants),
        bindings_match=bindings_match(expected, actual_plan),
        must_ask_match=must_ask_match(expected, actual_plan),
        first_review_pass=passed,
        review_unavailable_reason=unavailable,
    )


def _rate(hits: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return hits / denominator


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(quantile * len(ordered) + 0.999) - 1))
    return ordered[index]


def _token_report(tokens: list[int]) -> dict:
    if not tokens:
        return {"per_call": [], "total": None, "mean": None, "distribution": None}
    counts = Counter(tokens)
    return {
        "per_call": tokens,
        "total": sum(tokens),
        "mean": sum(tokens) / len(tokens),
        "distribution": {str(key): counts[key] for key in sorted(counts)},
    }


def aggregate(records: list[CaseEvaluation], *, mode: str) -> Summary:
    scored = [row for row in records if row.score is not None]
    tool_hits = sum(row.score.tool_match for row in scored if row.score)
    invariant_hits = sum(row.score.invariants_match for row in scored if row.score)
    ordered = [row for row in scored if row.score and row.score.order_required]
    order_hits = sum(row.score.order_match is True for row in ordered)
    reviewed = [row for row in scored if row.score and row.score.first_review_pass is not None]
    review_hits = sum(row.score.first_review_pass is True for row in reviewed)
    unavailable = sum(
        1 for row in records if row.score is None or row.score.first_review_pass is None
    )
    latencies = [row.latency_ms for row in records if row.latency_ms is not None]
    tokens = [int(row.usage.get("tokens") or 0) for row in records if row.usage.get("tokens") is not None]
    return Summary(
        mode=mode,
        llm_tested=False,
        llm_untested_reason="本轮只跑 RulePlanner，未调用 LLM，不能当作模型质量或模型延迟。",
        case_count=len(records),
        scored_count=len(scored),
        coverage=_rate(len(scored), len(records)),
        tool_match_rate=_rate(tool_hits, len(scored)),
        tool_match_denominator=len(scored),
        order_match_rate=_rate(order_hits, len(ordered)),
        order_match_denominator=len(ordered),
        invariants_match_rate=_rate(invariant_hits, len(scored)),
        invariants_match_denominator=len(scored),
        first_review_pass_rate=_rate(review_hits, len(reviewed)),
        first_review_pass_denominator=len(reviewed),
        review_unavailable=unavailable,
        latency_ms={
            "n": len(latencies),
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "unit": "milliseconds",
            "clock": "time.perf_counter",
        },
        tokens=_token_report(tokens),
        errors=sum(1 for row in records if row.errors),
    )


def desensitized_input(case: PlannerCase) -> dict:
    """User message plus counts. Untrusted canvas text is omitted, not promoted."""

    context = case.context
    return {
        "case_id": case.case_id,
        "category": case.category,
        "language": case.language,
        "message": context.request.message,
        "selected_asset_count": len(context.request.selected_assets),
        "recent_output_count": len(context.recent_outputs),
        "readable_asset_count": len(context.scope.readable_assets),
        "tools": list(context.scope.tools),
        "untrusted_text_omitted": True,
    }


def _object_schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _reference(media_type: str) -> dict:
    literal = _object_schema(
        {"asset_id": {"type": "string", "minLength": 1}, "media_type": {"const": media_type}},
        ["asset_id", "media_type"],
    )
    return {
        "anyOf": [
            literal,
            {"type": "string", "pattern": r"^\$[a-z][a-z0-9_]*\.[a-zA-Z0-9_.\[\]]+$"},
        ]
    }


def _tool(
    name: str,
    description: str,
    parameters: dict,
    inputs: list[Port],
    output: str,
    mode: str,
    price: Price,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version="eval-mock-1",
        description=description,
        parameters=parameters,
        result_schema={"type": "object"},
        inputs=inputs,
        outputs=[Port(path="output", media_type=output)],  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        price=price,
        limits={},
        failures=[],
        execution=Execution(
            adapter="planner_eval_mock",
            result_url_policy="authenticated_asset",
            sla_seconds=30,
        ),
        available=True,
    )


def mock_catalog() -> ToolCatalog:
    """In-memory tool directory for RulePlanner and the existing reviewer.

    No provider binding, credential, or generation adapter is loaded.
    """

    image_size = {
        "width": {"type": "integer", "enum": [_IMAGE_EDGE]},
        "height": {"type": "integer", "enum": [_IMAGE_EDGE]},
    }
    prompt = {"prompt": {"type": "string", "minLength": 1}, "count": {"type": "integer", "minimum": 1}}
    video_extra = {
        "duration_seconds": {"type": "number", "enum": _DURATION_ENUM},
        "fps": {"type": "integer", "enum": [_FPS]},
    }
    image_ref = _reference("image")
    tools = [
        _tool(
            "text_to_image_preview",
            "Mock text-to-image ticket. Not executed.",
            _object_schema({**prompt, **image_size}, ["prompt", "count"]),
            [],
            "image",
            "generate",
            Price(kind="per_count", credits=0, count_parameter="count"),
        ),
        _tool(
            "image_edit_preview",
            "Mock image-edit ticket. Not executed.",
            _object_schema({**prompt, **image_size, "image": image_ref}, ["prompt", "count", "image"]),
            [Port(path="image", media_type="image")],
            "image",
            "generate",
            Price(kind="per_count", credits=0, count_parameter="count"),
        ),
        _tool(
            "text_to_video_preview",
            "Mock text-to-video ticket. Not executed.",
            _object_schema({**prompt, **image_size, **video_extra}, ["prompt", "count", "duration_seconds"]),
            [],
            "video",
            "generate",
            Price(kind="per_count", credits=0, count_parameter="count"),
        ),
        _tool(
            "image_to_video_preview",
            "Mock image-to-video ticket. Not executed.",
            _object_schema(
                {**prompt, **image_size, **video_extra, "image": image_ref},
                ["prompt", "count", "duration_seconds", "image"],
            ),
            [Port(path="image", media_type="image")],
            "video",
            "generate",
            Price(kind="per_count", credits=0, count_parameter="count"),
        ),
        _tool(
            "detect_subject",
            "Mock subject detection. Not executed.",
            _object_schema(
                {"image": image_ref, "target_description": {"type": "string"}},
                ["image"],
            ),
            [Port(path="image", media_type="image")],
            "mask",
            "read_only",
            Price(kind="fixed", credits=0),
        ),
        _tool(
            "cutout",
            "Mock cutout. Not executed.",
            _object_schema({"image": image_ref, "mask": _reference("mask")}, ["image", "mask"]),
            [Port(path="image", media_type="image"), Port(path="mask", media_type="mask")],
            "image",
            "deterministic",
            Price(kind="fixed", credits=0),
        ),
        _tool(
            "read_canvas",
            "Mock canvas read. Not executed.",
            _object_schema({}, []),
            [],
            "text",
            "read_only",
            Price(kind="fixed", credits=0),
        ),
        _tool(
            "read_asset",
            "Mock asset read. Not executed.",
            _object_schema({"asset_id": {"type": "string", "minLength": 1}}, ["asset_id"]),
            [],
            "text",
            "read_only",
            Price(kind="fixed", credits=0),
        ),
    ]
    return ToolCatalog(CatalogSnapshot(version="planner-eval-mock", tools=tools))


def planner_tool_view(catalog: ToolCatalog, allowed: list[str]) -> list[dict]:
    """Shape RulePlanner.single_step reads: name, available, parameters.properties."""

    allowed_set = set(allowed)
    view = []
    for tool in catalog.snapshot.tools:
        if tool.name not in allowed_set:
            continue
        view.append({
            "name": tool.name,
            "available": tool.available,
            "parameters": tool.parameters,
        })
    return view


def rule_context(case: PlannerCase, catalog: ToolCatalog | None = None) -> dict:
    """Planner context for RulePlanner and LLMPlanner.

    Canvas text is authorized by ``readable_nodes`` and selected by node id so
    ``assemble_messages`` can place it in ``<canvas>`` even when the fixture has
    no geometry. The user request and expected label are copied unchanged.
    """

    catalog = catalog or mock_catalog()
    context = case.context
    scope = context.scope.model_dump(mode="json")
    snapshot = context.snapshot.model_dump(mode="json")
    objects: dict[str, dict] = {}
    readable_nodes: list[str] = []
    text_nodes: list[str] = []
    for item in snapshot.get("objects") or []:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        node_id = str(copied.get("object_id") or copied.get("node_id") or "")
        if not node_id:
            continue
        copied["node_id"] = node_id
        if copied.get("asset_id") and "assetId" not in copied:
            copied["assetId"] = copied["asset_id"]
        objects[node_id] = copied
        readable_nodes.append(node_id)
        kind = str(copied.get("type") or copied.get("kind") or "")
        if copied.get("text") or kind == "text":
            text_nodes.append(node_id)
    snapshot["objects"] = objects
    if context.upload_note and scope.get("readable_assets"):
        snapshot["uploads"] = [{"asset_id": scope["readable_assets"][0], "kind": "text", "text": context.upload_note}]
    scope["readable_nodes"] = readable_nodes
    return {
        "request": context.request.model_dump(mode="json"),
        "language": case.language,
        "user": {"id": "planner-eval"},
        "invariants": context.invariants.model_dump(mode="json"),
        "tools": planner_tool_view(catalog, context.scope.tools),
        "scope": scope,
        "selection": {
            "ids": list(context.request.selected_assets),
            "node_ids": text_nodes,
        },
        "recent_outputs": [item.model_dump(mode="json") for item in context.recent_outputs],
        "snapshot": snapshot,
        "summary": context.summary,
        "catalog": catalog,
        "_catalog": catalog,
    }


def _review_assets(raw: Any) -> list[dict]:
    """Map fixture assets onto the reviewer contract. Owner is the eval user, not a secret."""

    if not isinstance(raw, list):
        return []
    assets = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("asset_id"):
            continue
        assets.append({
            "asset_id": item["asset_id"],
            "owner_id": "planner-eval",
            "kind": item.get("kind") or "image",
            "width": item.get("width"),
            "height": item.get("height"),
        })
    return assets


def rule_review(result: Any, context: dict) -> dict:
    """Call the existing three-gate reviewer. Failure to compute stays null."""

    catalog = context.get("_catalog")
    if catalog is None:
        return {"verdict": None, "unavailable_reason": "mock_catalog_missing", "reasons": []}
    request = context.get("request")
    if not isinstance(request, dict) or "message" not in request:
        return {"verdict": None, "unavailable_reason": "request_missing", "reasons": []}
    scope = context.get("scope") if isinstance(context.get("scope"), dict) else {}
    snapshot = context.get("snapshot") if isinstance(context.get("snapshot"), dict) else {}
    assets = _review_assets(snapshot.get("assets"))
    plan = getattr(result, "plan", None)
    if plan is None:
        return {"verdict": None, "unavailable_reason": "plan_missing", "reasons": []}
    invariants = getattr(result, "invariants", {})
    if not isinstance(invariants, dict):
        invariants = _dump(invariants)
    run = SimpleNamespace(
        user_id="planner-eval",
        credits_limit=10_000,
        credits_spent=0,
        scope={
            "tools": list(scope.get("tools") or []),
            "readable_assets": list(scope.get("readable_assets") or []),
            "writable_regions": [],
        },
        version_lock={"tool_catalog": catalog.lock.model_dump(mode="json")},
        invariants=invariants,
        snapshot={"request": request, "assets": assets, "objects": {}},
    )
    try:
        receipt = supervisor_review(plan, run, catalog, 10_000)
    except Exception as exc:  # reviewer bugs stay visible; they are not a pass
        return {
            "verdict": None,
            "unavailable_reason": f"{type(exc).__name__}: {exc}",
            "reasons": [],
        }
    if not isinstance(receipt, dict) or receipt.get("verdict") not in {"pass", "reject"}:
        return {
            "verdict": None,
            "unavailable_reason": "reviewer_receipt_missing_verdict",
            "reasons": [],
            "raw": receipt,
        }
    return receipt


async def _call_reviewer(reviewer: Callable[..., Any], result: Any, context: dict) -> Any:
    receipt = reviewer(result, context)
    if isinstance(receipt, Awaitable):
        receipt = await receipt
    return receipt


async def _evaluate_one(
    case: PlannerCase,
    planner: Any,
    context_builder: Callable[[PlannerCase], dict],
    reviewer: Callable[..., Any],
    semaphore: asyncio.Semaphore,
    mode: str,
) -> CaseEvaluation:
    async with semaphore:
        errors: list[str] = []
        context: dict | None = None
        result = None
        latency_ms: float | None = None
        try:
            context = context_builder(case)
            started = time.perf_counter()
            result = await planner.plan(context)
            latency_ms = (time.perf_counter() - started) * 1000
        except Exception as exc:
            errors.append(f"planner: {type(exc).__name__}: {exc}")
        review_payload = None
        if result is not None and context is not None:
            try:
                review_payload = await _call_reviewer(reviewer, result, context)
            except Exception as exc:
                errors.append(f"reviewer: {type(exc).__name__}: {exc}")
                review_payload = {"verdict": None, "unavailable_reason": f"{type(exc).__name__}: {exc}"}
        tokens = getattr(result, "tokens", None) if result is not None else None
        plan = getattr(result, "plan", None) if result is not None else None
        invariants = getattr(result, "invariants", None) if result is not None else None
        scored = None
        if plan is not None and not any(item.startswith("planner:") for item in errors):
            try:
                scored = score(case.expected, plan, invariants, review_payload)
            except Exception as exc:
                errors.append(f"score: {type(exc).__name__}: {exc}")
        return CaseEvaluation(
            case_id=case.case_id,
            category=case.category,
            language=case.language,
            mode=mode,
            input=desensitized_input(case),
            output=None if plan is None else {"plan": _dump(plan), "invariants": _dump(invariants)},
            review=None if review_payload is None else _dump(review_payload),
            usage={"tokens": tokens, "token_source": "planner_result.tokens"},
            latency_ms=latency_ms,
            errors=errors,
            score=scored,
        )


async def evaluate_cases(
    cases: list[PlannerCase],
    planner: Any,
    context_builder: Callable[[PlannerCase], dict],
    reviewer: Callable[..., Any],
    *,
    concurrency: int = 4,
    mode: str = RULE_BASELINE_MODE,
) -> tuple[list[CaseEvaluation], Summary]:
    """Plan each case with the given planner and score the returned objects.

    `expected` is used only by `score` after `planner.plan` returns.
    """

    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    for case in cases:
        if not isinstance(case, PlannerCase):
            raise TypeError("evaluate_cases requires PlannerCase rows; schema failures are not skipped")
    semaphore = asyncio.Semaphore(concurrency)
    records = await asyncio.gather(*[
        _evaluate_one(case, planner, context_builder, reviewer, semaphore, mode)
        for case in cases
    ])
    summary = aggregate(list(records), mode=mode)
    Summary.model_validate(summary.model_dump())
    return list(records), summary
