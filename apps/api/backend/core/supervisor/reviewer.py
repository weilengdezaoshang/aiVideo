"""Adapt the locked catalog to the existing three-gate reviewer and typed-port gate."""
from jsonschema.exceptions import ValidationError as SchemaError
from backend.agent.admission.tool_types import TypedTask, review_tool_types
from backend.core.review.policy import validate
from backend.core.tools.catalog import estimate_cost
from backend.core.tools.contracts import VersionLock
from .contracts import Settings


def review(plan, run, catalog, balance):
    issues, tools, steps, constraints = [], {}, [], []
    if len(plan.steps) > Settings().max_tasks:
        issues.append({"gate": "legal", "code": "task_limit"})
    typed = []
    for tool in catalog.snapshot.tools:
        params = {}
        for name, schema in tool.parameters.get("properties", {}).items():
            if schema.get("type") in {"string", "number", "integer", "boolean"}:
                params[name] = {"type": "number" if schema["type"] == "integer" else schema["type"],
                    **{k: schema[k] for k in ("minimum", "maximum", "enum") if k in schema},
                    "required": name in tool.parameters.get("required", [])}
        output = next((p.media_type for p in tool.outputs), None)
        tools[tool.name] = {"id": tool.name, "version": tool.version, "enabled": tool.available,
            "output_type": output, "input_types": {p.path: [p.media_type] for p in tool.inputs if "[*]" not in p.path},
            "params_schema": params, "cost_credits": tool.price.credits or 0,
            "ambiguous_without": [k for k in ("duration_seconds",) if output == "video" and k in tool.parameters.get("properties", {})]}
    for step in plan.steps:
        if step.kind == "ask_user":
            steps.append({"id": step.id, "kind": "ask_user", "depends_on": step.depends_on})
            continue
        try:
            tool = catalog.get_tool(step.tool, catalog.lock.version)
            quote = estimate_cost(tool, step.parameters)
            if tool.name != "detect_subject" and not any(p.media_type in {"image", "video", "audio"} for p in tool.outputs):
                issues.append({"gate": "legal", "code": "executor_not_supported", "step": step.id})
            typed.append(TypedTask(id=step.id, tool=step.tool, parameters=step.parameters,
                depends_on=[key for key in step.depends_on if next((s for s in plan.steps if s.id == key), None).kind == "call_tool"]))
            inputs = {}
            for port in tool.inputs:
                value = step.parameters.get(port.path)
                if value is not None and "[*]" not in port.path:
                    inputs[port.path] = value if isinstance(value, str) else {"asset_id": value.get("asset_id", value.get("source_asset_id", ""))}
            steps.append({"id": step.id, "tool": step.tool, "tool_version": tool.version,
                "params": {k: v for k, v in step.parameters.items() if k in tools[tool.name]["params_schema"]},
                "inputs": inputs, "depends_on": step.depends_on, "estimated_cost": quote})
        except (ValueError, TypeError, AttributeError, SchemaError):
            issues.append({"gate": "legal", "code": "invalid_tool_or_parameters", "step": step.id})
    if not issues:
        result = review_tool_types(typed, catalog, VersionLock.model_validate(run.version_lock["tool_catalog"]))
        issues.extend(result.issues)
        constraints = result.execution_constraints
        context = {"user_id": run.user_id, "user_credits": balance, "run_budget": run.credits_limit - run.credits_spent,
            "scope": {k: run.scope.get(k, []) for k in ("tools", "readable_assets", "writable_regions")},
            "version_lock": {"tools": tools}, "invariants": run.invariants,
            "canvas": {"assets": run.snapshot.get("assets", []), "nodes": [{"id": str(k)} for k in run.snapshot.get("objects", {})]}}
        result = validate({"goal": plan.goal, "steps": steps}, context)
        issues.extend(r.model_dump() for r in result.reasons)
    request = run.snapshot["request"]
    if request.get("action") == "tool" and any(s.tool != request.get("tool") for s in plan.steps if s.kind == "call_tool"):
        issues.append({"gate": "intent", "code": "requested_tool_changed"})
    if plan.goal != run.snapshot["request"]["message"]:
        issues.append({"gate": "intent", "code": "goal_changed"})
    # This block does not permit a writer to invent an unsupported cutout flow.
    by_id = {s.id: s for s in plan.steps}
    for step in plan.steps:
        if set(step.placement) - {"mode", "anchor_id", "anchor_version", "region_id"} or step.placement.get("mode", "right") not in {"right", "below", "replace", "region"}:
            issues.append({"gate": "legal", "code": "program_coordinates_required", "step": step.id})
        if step.tool == "cutout":
            ref = step.parameters.get("mask", "")
            source = by_id.get(ref.split(".")[0].lstrip("$")) if isinstance(ref, str) else None
            if not source or source.tool != "detect_subject" or source.parameters.get("image") != step.parameters.get("image"):
                issues.append({"gate": "types", "code": "cutout_requires_detection", "step": step.id})
    return {"execution_constraints": constraints, "verdict": "reject" if issues else "pass", "reasons": issues,
        "checks": {gate: not any(i["gate"] == gate for i in issues) for gate in ("legal", "types", "intent")}}
