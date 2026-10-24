"""Plan facts for the checker: typed data-ref deliverable graph, paid steps, honest unknowns."""

from __future__ import annotations

import hashlib
import json
from math import gcd
from typing import Any

from backend.core.review.contracts import AssetRef, Plan, PlanStep, PlanView, TaskRef, ToolSpec
from backend.core.review.policy import MEDIA_OUTPUTS, _step_count

FREEFORM = frozenset({"prompt", "target_description", "question", "text", "answer"})


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _ratio(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    divisor = gcd(int(width), int(height)) or 1
    return f"{int(width) // divisor}:{int(height) // divisor}"


def _param_ratio(step: PlanStep) -> str | None:
    raw = step.params.get("aspect_ratio")
    if isinstance(raw, str) and ":" in raw:
        left, _, right = raw.partition(":")
        if left.strip().isdigit() and right.strip().isdigit():
            return _ratio(int(left), int(right))
    width = step.params.get("width")
    height = step.params.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        return _ratio(int(width), int(height))
    return None


def _depth(plan: Plan) -> int:
    index = {step.id: step for step in plan.steps}
    cache: dict[str, int] = {}

    def walk(ident: str, stack: frozenset[str]) -> int:
        if ident in cache:
            return cache[ident]
        if ident in stack:
            return 0
        step = index.get(ident)
        if step is None:
            return 0
        parents = list(step.depends_on)
        parents.extend(ref.task for ref in step.inputs.values() if isinstance(ref, TaskRef))
        best = 1
        nxt = stack | {ident}
        for parent in parents:
            best = max(best, 1 + walk(parent, nxt))
        cache[ident] = best
        return best

    return max((walk(step.id, frozenset()) for step in plan.steps), default=0)


def _truncate(value, limit: int) -> tuple[Any, bool]:
    text = value if isinstance(value, str) else _canonical(value)
    if len(text) <= limit:
        return value, False
    return {"truncated": True, "chars": len(text), "preview": text[:limit]}, True


def typed_port_view(view: PlanView, proposal: dict, catalog) -> PlanView:
    """Restore array/nested ports omitted by the legacy review adapter.

    Called only after the catalog type gate passed. Read the SAME declared ports
    as that gate; strings in ordinary prompts are never treated as references.
    The immutable planner output and the original review view remain untouched.
    """
    import copy
    import re
    from backend.agent.admission.tool_types import _values

    raw_steps = {step["id"]: step for step in proposal.get("steps") or []}
    expanded = []
    for step in view.plan.steps:
        raw = raw_steps.get(step.id) or {}
        params = copy.deepcopy(raw.get("parameters", raw.get("params", step.params)))
        inputs = dict(step.inputs)
        if step.tool:
            tool = catalog.get_tool(step.tool, catalog.lock.version)
            for port in tool.inputs:
                for index, value in enumerate(_values(params, port.path.split("."))):
                    reference = None
                    if isinstance(value, str):
                        match = re.fullmatch(r"\$(t[1-9][0-9]*)\.(.+)", value)
                        if match:
                            reference = TaskRef(task=match[1])
                    elif isinstance(value, dict) and value.get("asset_id"):
                        reference = AssetRef(asset_id=str(value["asset_id"]),
                            version_id=value.get("version_id"))
                    if reference is not None:
                        inputs[f"{port.path}#{index}"] = reference
        expanded.append(step.model_copy(update={"params": params, "inputs": inputs}))
    return view.model_copy(update={"plan": view.plan.model_copy(update={"steps": expanded})})


def data_consumed(plan: Plan, resolved: dict[str, ToolSpec | None]) -> set[str]:
    """Only media-producing typed input refs consume an upstream. depends_on-only does not."""

    consumed: set[str] = set()
    for step in plan.steps:
        spec = resolved.get(step.id)
        if spec is None or spec.output_type not in MEDIA_OUTPUTS:
            continue
        for ref in step.inputs.values():
            if isinstance(ref, TaskRef):
                consumed.add(ref.task)
    return consumed


def data_deliverables(view: PlanView) -> list[PlanStep]:
    consumed = data_consumed(view.plan, view.tools)
    out = []
    for step in view.plan.steps:
        spec = view.tools.get(step.id)
        if spec is not None and spec.output_type in MEDIA_OUTPUTS and step.id not in consumed:
            out.append(step)
    return out


def _lineage(step: PlanStep, steps: dict[str, PlanStep]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    queue = [step]
    while queue:
        current = queue.pop(0)
        if current.id in seen:
            continue
        seen.add(current.id)
        for ref in current.inputs.values():
            if isinstance(ref, AssetRef):
                found.append(ref.asset_id)
            elif isinstance(ref, TaskRef) and ref.task in steps:
                queue.append(steps[ref.task])
    return list(dict.fromkeys(found))


def summarize(
    view: PlanView,
    *,
    max_param_chars: int = 400,
    max_bytes: int = 24_000,
    catalog_hash: str = "",
    proposal_hash: str = "",
    revision: int = 1,
) -> dict[str, Any]:
    """Facts tied to the frozen catalog. Intermediate images are not final video deliverables."""

    steps = {step.id: step for step in view.plan.steps}
    deliverables = data_deliverables(view)
    listed: list[dict[str, Any]] = []
    paid: list[dict[str, Any]] = []
    unknown: list[str] = []
    truncated = False
    for step in view.plan.steps:
        spec: ToolSpec | None = view.tools.get(step.id)
        schema_keys = set(spec.params_schema) if spec is not None else set()
        keys = list(dict.fromkeys([*schema_keys, *step.params]))
        params = {}
        for key in keys:
            if key not in step.params:
                continue
            raw = step.params[key]
            rule = spec.params_schema.get(key) if spec is not None else None
            free = key in FREEFORM or (rule is not None and getattr(rule, "type", None) == "string")
            if free:
                value, cut = _truncate(raw, max_param_chars)
                truncated = truncated or cut
            else:
                value, cut = raw, False
            params[key] = value
        cost = step.estimated_cost
        if cost is None and spec is not None:
            cost = spec.cost_credits
        output = spec.output_type if spec is not None else None
        read_only = spec is not None and spec.cost_credits == 0 and spec.capability in {"read", "inspect"}
        item = {
            "id": step.id,
            "kind": step.kind,
            "tool": step.tool,
            "dependencies": list(step.depends_on),
            "inputs": {
                name: (ref.task if isinstance(ref, TaskRef) else ref.asset_id)
                for name, ref in step.inputs.items()
            },
            "params": params,
            "output_type": output,
            "count": _step_count(step),
            "estimated_cost": int(cost or 0),
            "read_or_deterministic": bool(step.kind == "ask_user" or read_only),
        }
        listed.append(item)
        if int(cost or 0) > 0:
            paid.append({"id": step.id, "tool": step.tool, "estimated_cost": int(cost)})
        if spec is None and step.kind == "call_tool":
            unknown.append(f"{step.id}: tool_unresolved")
    finals = []
    for step in deliverables:
        spec = view.tools.get(step.id)
        asset_ids = _lineage(step, steps)
        width = step.params.get("width")
        height = step.params.get("height")
        duration = step.params.get("duration_seconds")
        finals.append(
            {
                "id": step.id,
                "modality": spec.output_type if spec else None,
                "count": _step_count(step),
                "width": width,
                "height": height,
                "aspect_ratio": _param_ratio(step),
                "duration_seconds": duration,
                "target_assets": asset_ids,
            }
        )
    payload = {
        "steps": listed,
        "final_deliverables": finals,
        "paid_steps": paid,
        "depth": _depth(view.plan),
        "unknown_facts": unknown,
        "catalog_hash": catalog_hash,
        "proposal_hash": proposal_hash,
        "input_revision": revision,
        "truncated_freeform": truncated,
    }
    encoded = _canonical(payload).encode("utf-8")
    if len(encoded) > max_bytes:
        return {
            "degraded": True,
            "reason": "input_too_large",
            "bytes": len(encoded),
            "catalog_hash": catalog_hash,
            "proposal_hash": proposal_hash,
            "input_revision": revision,
        }
    payload["content_hash"] = hashlib.sha256(encoded).hexdigest()
    payload["degraded"] = False
    return payload


def refs_from_view(view: PlanView, extra: list[str] | None = None) -> list[str]:
    invariant = view.context.invariants.deliverable
    required = list(invariant.required_asset_ids) if invariant else []
    seen = set(required)
    for item in extra or []:
        if item not in seen:
            seen.add(item)
            required.append(item)
    for step in view.plan.steps:
        for ref in step.inputs.values():
            if isinstance(ref, AssetRef) and ref.asset_id not in seen:
                seen.add(ref.asset_id)
                required.append(ref.asset_id)
    return required
