"""Convert one model tool-call group into a reviewable plan. This module executes nothing."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError

from backend.core.supervisor.contracts import (
    ControlCallAudit,
    EvidenceItem,
    FormDefinition,
    FormField,
    Plan,
    PlannerResult,
    Settings,
    Step,
)
from backend.core.supervisor.tool_selection import _CONTROLS as _CONTROL_SCHEMAS, planning_tools
from backend.core.tools.catalog import ToolCatalog
from backend.core.tools.contracts import VersionLock

_STEP_ID = re.compile(r"^t[1-9][0-9]*$")
_REF = re.compile(r"\$(t[1-9][0-9]*)((?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])+)")
_PLACEMENT_KEYS = frozenset({"mode", "anchor_id", "anchor_version", "region_id"})
_PLACEMENT_MODES = frozenset({"right", "below", "replace", "region"})
_COORDINATES = frozenset({
    "x", "y", "left", "top", "right", "bottom", "bounds", "position", "scale", "rotation",
    "camera", "viewport",
})
_CONTROLS = frozenset({"ask_user_form", "answer_only", "set_invariants"})
_FIELD_TYPES = frozenset({"text", "number", "boolean", "choice"})


class ConversionError(ValueError):
    """Machine-readable rejection. The supervisor can clarify; nothing is queued."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def convert_tool_calls(
    calls: list[dict],
    *,
    context: dict,
    catalog: ToolCatalog,
    user: Mapping[str, object],
    version_lock: VersionLock,
    settings: Settings | None = None,
) -> PlannerResult:
    """Turn a whole call group into a ``PlannerResult``. The goal stays the user message."""

    settings = settings or Settings()
    request = context.get("request") or {}
    message = str(request.get("message") or "")
    scope = context.get("scope") or request.get("scope") or {}
    presented = planning_tools(
        catalog, user, version_lock, scope=scope, query=message, settings=settings)
    visible = {entry["function"]["name"]: entry for entry in presented.tools}
    parsed = [_parse_call(call, index) for index, call in enumerate(calls)]
    _reject_duplicate_ids(parsed)
    for call in parsed:
        if call["name"] not in visible or call["name"] == "spawn_helper":
            raise ConversionError("unknown_tool")
    controls = _merge_controls([call for call in parsed if call["name"] in _CONTROLS])
    workers = [call for call in parsed if call["name"] not in _CONTROLS]
    for index, call in enumerate(workers, start=1):
        call["step_id"] = f"t{index}"
    audits = [_audit_control(call, message) for call in controls]
    names = {call["name"] for call in controls}
    if "answer_only" in names and workers:
        raise ConversionError("answer_only_mixed_with_generation")
    if names == {"set_invariants"} and not workers:
        raise ConversionError("invariants_without_action")
    proposed = _proposed_invariants(controls)
    raw = {
        "calls": calls,
        "writer": "llm",
        "catalog_version": presented.catalog_version,
        "catalog_hash": presented.catalog_hash,
        "retrieval_version": presented.retrieval_version,
    }
    for call in workers:
        _reject_coordinates(call["arguments"])
    needs_duration = _video_needs_duration(workers, catalog, version_lock)
    if "ask_user_form" in names or needs_duration:
        if "ask_user_form" not in names:
            _validate_queued_schemas(workers, catalog, version_lock)
        form = _explicit_form(controls) or _duration_form(workers, catalog, version_lock)
        step = Step(id="t1", kind="ask_user", parameters={"form": _form_payload(form)})
        plan = _plan(message, [step], "clarify", audits, "")
        return _result(plan, proposed, raw)
    if "answer_only" in names:
        answer = next(call["arguments"]["answer"] for call in controls if call["name"] == "answer_only")
        plan = _plan(message, [], "answer_only", audits, str(answer))
        return _result(plan, proposed, raw)
    snapshot = context.get("snapshot") or {}
    steps = [_worker_step(call, workers, scope, snapshot, catalog, version_lock) for call in workers]
    if not steps:
        raise ConversionError("empty_plan")
    plan = _plan(message, steps, "generate", audits, "")
    return _result(plan, proposed, raw)


def _parse_call(call: dict, index: int) -> dict:
    if not isinstance(call, dict):
        raise ConversionError("invalid_call")
    name = call.get("name")
    call_id = call.get("id")
    if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
        raise ConversionError("invalid_call")
    arguments = call.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ConversionError("invalid_arguments") from exc
    if not isinstance(arguments, dict):
        raise ConversionError("invalid_arguments")
    return {"id": call_id, "name": name, "arguments": arguments, "index": index}


def _reject_duplicate_ids(calls: list[dict]) -> None:
    seen = set()
    for call in calls:
        if call["id"] in seen:
            raise ConversionError("duplicate_call_id")
        seen.add(call["id"])


def _merge_controls(controls: list[dict]) -> list[dict]:
    """One form and one answer. Invariant objects merge; overlapping disagreements are rejected."""

    grouped: dict[str, list[dict]] = {name: [] for name in _CONTROLS}
    for call in controls:
        grouped[call["name"]].append(call)
    if len(grouped["ask_user_form"]) > 1 or len(grouped["answer_only"]) > 1:
        raise ConversionError("duplicate_control")
    invariants = grouped["set_invariants"]
    if len(invariants) > 1:
        merged: dict = {}
        evidence = None
        for call in invariants:
            _validate_schema(_CONTROL_SCHEMAS["set_invariants"]["parameters"], call["arguments"])
            payload = call["arguments"].get("invariants")
            if not isinstance(payload, dict):
                raise ConversionError("invalid_invariants")
            for key, value in payload.items():
                if key in merged and merged[key] != value:
                    raise ConversionError("conflicting_controls")
                merged[key] = value
            evidence = call
        if evidence is not None:
            arguments = {**evidence["arguments"], "invariants": merged}
            grouped["set_invariants"] = [{**evidence, "arguments": arguments}]
    return [*grouped["set_invariants"], *grouped["ask_user_form"], *grouped["answer_only"]]


def _audit_control(call: dict, message: str) -> ControlCallAudit:
    _validate_schema(_CONTROL_SCHEMAS[call["name"]]["parameters"], call["arguments"])
    if call["name"] == "ask_user_form":
        form = _validate_form(call["arguments"])
        return ControlCallAudit(tool=call["name"], arguments=call["arguments"], form=form)
    if call["name"] == "answer_only":
        answer = call["arguments"].get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise ConversionError("invalid_answer")
        return ControlCallAudit(tool=call["name"], arguments={"answer": answer})
    if call["name"] == "set_invariants":
        return ControlCallAudit(
            tool=call["name"], arguments=dict(call["arguments"]), evidence=_evidence(call["arguments"], message))
    raise ConversionError("unknown_tool")


def _evidence(arguments: dict, message: str) -> list[EvidenceItem]:
    quote = arguments.get("source_quote")
    source = arguments.get("source")
    location = arguments.get("location")
    if not isinstance(quote, str) or not quote.strip() or quote not in message:
        raise ConversionError("invariant_evidence_required")
    if not isinstance(source, str) or not source.strip() or not isinstance(location, str) or not location.strip():
        raise ConversionError("invariant_evidence_required")
    invariants = arguments.get("invariants")
    if not isinstance(invariants, dict):
        raise ConversionError("invalid_invariants")
    return [EvidenceItem(source=source, quote=quote)]


def _validate_form(arguments: dict) -> FormDefinition:
    form_id = arguments.get("id")
    fields = arguments.get("fields")
    if not isinstance(form_id, str) or not form_id.strip() or not isinstance(fields, list) or not fields:
        raise ConversionError("invalid_form")
    seen: set[str] = set()
    built = []
    for field in fields:
        if not isinstance(field, dict):
            raise ConversionError("invalid_form")
        field_id = field.get("id")
        field_type = field.get("type")
        label = field.get("label")
        if not isinstance(field_id, str) or not isinstance(label, str) or field_type not in _FIELD_TYPES:
            raise ConversionError("invalid_form")
        if field_id in seen:
            raise ConversionError("duplicate_form_field")
        seen.add(field_id)
        _reject_html(field_id, label)
        choices = field.get("choices") or []
        if not isinstance(choices, list) or any(not isinstance(item, str) for item in choices):
            raise ConversionError("invalid_form")
        if field_type == "choice":
            if len(choices) < 1 or len(set(choices)) != len(choices):
                raise ConversionError("duplicate_form_option")
        elif choices:
            raise ConversionError("invalid_form")
        for choice in choices:
            _reject_html(choice)
        default = field.get("default", None)
        _check_default(field_type, default, choices)
        required = field.get("required", False)
        if not isinstance(required, bool):
            raise ConversionError("invalid_form")
        built.append(FormField(
            name=field_id, id=field_id, label=label, required=required,
            type=field_type, default=default, choices=list(choices)))
    return FormDefinition(id=form_id, fields=built)


def _check_default(field_type: str, default, choices: list) -> None:
    if default is None:
        return
    if field_type == "boolean" and not isinstance(default, bool):
        raise ConversionError("invalid_form")
    if field_type == "number" and (isinstance(default, bool) or not isinstance(default, (int, float))):
        raise ConversionError("invalid_form")
    if field_type in {"text", "choice"} and not isinstance(default, str):
        raise ConversionError("invalid_form")
    if field_type == "choice" and default not in choices:
        raise ConversionError("invalid_form")


def _reject_html(*values: str) -> None:
    if any("<" in value or ">" in value for value in values):
        raise ConversionError("form_html_rejected")


def _proposed_invariants(controls: list[dict]) -> dict:
    for call in controls:
        if call["name"] == "set_invariants":
            return dict(call["arguments"]["invariants"])
    return {}


def _explicit_form(controls: list[dict]) -> FormDefinition | None:
    for call in controls:
        if call["name"] == "ask_user_form":
            return _validate_form(call["arguments"])
    return None


def _video_needs_duration(workers: list[dict], catalog: ToolCatalog, lock: VersionLock) -> bool:
    for call in workers:
        tool = catalog.get_tool(call["name"], lock.version)
        if not any(port.media_type == "video" for port in tool.outputs):
            continue
        if "duration_seconds" in (tool.parameters.get("required") or []) and (
                "duration_seconds" not in call["arguments"] or call["arguments"].get("duration_seconds") is None):
            return True
    return False


def _duration_form(workers: list[dict], catalog: ToolCatalog, lock: VersionLock) -> FormDefinition:
    fields = []
    seen = set()
    for call in workers:
        tool = catalog.get_tool(call["name"], lock.version)
        if not any(port.media_type == "video" for port in tool.outputs):
            continue
        for name in tool.parameters.get("required") or []:
            if name in call["arguments"] and call["arguments"].get(name) is not None:
                continue
            if name in seen or name == "_plan":
                continue
            if name not in {"duration_seconds", "fps"}:
                continue
            seen.add(name)
            fields.append(FormField(
                name=name, id=name, label="视频时长（秒）" if name == "duration_seconds" else "帧率",
                required=True, type="number"))
    if not fields:
        raise ConversionError("video_duration_required")
    return FormDefinition(id="video_duration", fields=fields)


def _form_payload(form: FormDefinition) -> dict:
    fields = []
    for field in form.fields:
        payload = {"id": field.id or field.name, "type": field.type, "label": field.label,
                   "required": field.required, "choices": list(field.choices)}
        if field.default is not None:
            payload["default"] = field.default
        fields.append(payload)
    return {"id": form.id, "fields": fields}


def _validate_queued_schemas(workers: list[dict], catalog: ToolCatalog, lock: VersionLock) -> None:
    """Schema-check calls that would have been queued. Missing video duration is a form, not null."""

    for call in workers:
        tool = catalog.get_tool(call["name"], lock.version)
        video = any(port.media_type == "video" for port in tool.outputs)
        missing_duration = video and "duration_seconds" in (tool.parameters.get("required") or []) and (
            "duration_seconds" not in call["arguments"] or call["arguments"].get("duration_seconds") is None)
        if missing_duration or call["name"] in _CONTROLS:
            continue
        arguments = {key: value for key, value in call["arguments"].items() if key != "_plan"}
        _validate_schema(tool.parameters, arguments)


def _validate_schema(schema: dict, arguments: dict) -> None:
    try:
        Draft202012Validator(schema).validate(arguments)
    except SchemaValidationError as exc:
        raise ConversionError("parameter_schema_invalid") from exc


def _worker_step(call, workers, scope, snapshot, catalog: ToolCatalog, lock: VersionLock) -> Step:
    arguments = dict(call["arguments"])
    plan_meta = arguments.pop("_plan", {}) or {}
    if not isinstance(plan_meta, dict):
        raise ConversionError("invalid_placement")
    _reject_coordinates(arguments)
    _reject_coordinates(plan_meta)
    tool = catalog.get_tool(call["name"], lock.version)
    _validate_schema(tool.parameters, arguments)
    readable = {str(item) for item in scope.get("readable_assets") or []}
    deps = _dependencies(arguments, plan_meta, call["step_id"], {item["step_id"] for item in workers})
    placement = _placement(plan_meta.get("placement") or {}, arguments, scope, tool, snapshot)
    _check_literals(arguments, readable)
    return Step(
        id=call["step_id"], kind="call_tool", tool=call["name"], parameters=arguments,
        depends_on=deps, placement=placement)


def _reject_coordinates(value: Any) -> None:
    if isinstance(value, dict):
        if _COORDINATES & set(value):
            raise ConversionError("coordinates_rejected")
        for item in value.values():
            _reject_coordinates(item)
    elif isinstance(value, list):
        for item in value:
            _reject_coordinates(item)


def _check_literals(value: Any, readable: set[str]) -> None:
    if isinstance(value, dict):
        if "asset_id" in value or "assetId" in value:
            asset_id = str(value.get("asset_id") or value.get("assetId") or "")
            if asset_id not in readable:
                raise ConversionError("asset_out_of_scope")
        if "source_asset_id" in value and str(value.get("source_asset_id") or "") not in readable:
            raise ConversionError("asset_out_of_scope")
        for item in value.values():
            _check_literals(item, readable)
    elif isinstance(value, list):
        for item in value:
            _check_literals(item, readable)
    elif isinstance(value, str) and value and not value.startswith("$"):
        return


def _dependencies(arguments: dict, plan_meta: dict, step_id: str, known: set[str]) -> list[str]:
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            # Only a whole argument equal to $tN.output (or another $tN path) is a dependency.
            # "$100" inside a prompt is ordinary text.
            match = _REF.fullmatch(value)
            if match is not None:
                ref = match.group(1)
                if ref == step_id:
                    raise ConversionError("self_dependency")
                if ref not in known:
                    raise ConversionError("invalid_reference")
                if ref not in found:
                    found.append(ref)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(arguments)
    for ref in plan_meta.get("depends_on") or []:
        if not isinstance(ref, str) or not _STEP_ID.fullmatch(ref):
            raise ConversionError("invalid_reference")
        if ref == step_id:
            raise ConversionError("self_dependency")
        if ref not in known:
            raise ConversionError("invalid_reference")
        if ref not in found:
            found.append(ref)
    return found


def _writes_canvas(tool) -> bool:
    return tool.mode != "read_only" and any(
        port.media_type in {"image", "video", "audio"} for port in tool.outputs)


def _needs_anchor(tool) -> bool:
    return _writes_canvas(tool) and any(
        port.media_type in {"image", "video", "audio", "mask"} for port in tool.inputs)


def _placement(raw: dict, arguments: dict, scope: Mapping, tool, snapshot: Mapping) -> dict:
    if not isinstance(scope, Mapping):
        raise ConversionError("placement_unauthorized")
    if set(raw) - _PLACEMENT_KEYS:
        raise ConversionError("invalid_placement")
    if not _writes_canvas(tool):
        return {}
    mode = raw.get("mode", "right")
    if mode not in _PLACEMENT_MODES:
        raise ConversionError("invalid_placement")
    placement = {"mode": mode}
    regions = {str(item) for item in scope.get("writable_regions") or []}
    if mode == "region":
        region_id = raw.get("region_id")
        if not isinstance(region_id, str) or region_id not in regions:
            raise ConversionError("placement_unauthorized")
        placement["region_id"] = region_id
        return placement
    anchor_id = raw.get("anchor_id")
    if mode == "replace" and not anchor_id:
        raise ConversionError("placement_unauthorized")
    if not anchor_id and not _needs_anchor(tool):
        return placement
    # A later step whose only media input is $tN.output has no literal anchor.
    # right/below may stay unresolved so the worker applies its default placement.
    if not anchor_id and mode in {"right", "below"} and not _literal_asset(arguments):
        return placement
    if not anchor_id:
        asset = _literal_asset(arguments)
        objects = snapshot.get("objects") or {} if isinstance(snapshot, Mapping) else {}
        readable = {str(item) for item in scope.get("readable_assets") or []}
        nodes = [key for key, node in objects.items()
                 if isinstance(node, Mapping) and str(node.get("assetId") or "") == asset and asset in readable]
        if len(nodes) == 1:
            anchor_id = nodes[0]
    if not isinstance(anchor_id, str) or not anchor_id:
        raise ConversionError("placement_unauthorized")
    node = (snapshot.get("objects") or {}).get(anchor_id) if isinstance(snapshot, Mapping) else None
    asset_id = str(node.get("assetId") or "") if isinstance(node, Mapping) else ""
    readable = {str(item) for item in scope.get("readable_assets") or []}
    if not asset_id or asset_id not in readable:
        raise ConversionError("placement_unauthorized")
    placement["anchor_id"] = anchor_id
    if "anchor_version" in raw:
        if not isinstance(raw["anchor_version"], str):
            raise ConversionError("invalid_placement")
        placement["anchor_version"] = raw["anchor_version"]
    return placement


def _literal_asset(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("asset_id") or value.get("assetId"):
            return str(value.get("asset_id") or value.get("assetId"))
        for item in value.values():
            found = _literal_asset(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _literal_asset(item)
            if found:
                return found
    return ""


def _plan(message: str, steps: list[Step], result_type: str, audits: list[ControlCallAudit], answer: str) -> Plan:
    _reject_cycles(steps)
    primary = audits[0] if audits else ControlCallAudit()
    if any(item.tool == "ask_user_form" for item in audits):
        primary = next(item for item in audits if item.tool == "ask_user_form")
    elif any(item.tool == "answer_only" for item in audits):
        primary = next(item for item in audits if item.tool == "answer_only")
    return Plan(
        goal=message, steps=steps, result_type=result_type, writer="llm", answer=answer,
        control_audit=primary, control_audits=audits)


def _reject_cycles(steps: list[Step]) -> None:
    order = {step.id: index for index, step in enumerate(steps)}
    graph = {step.id: list(step.depends_on) for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> None:
        if node in visiting:
            raise ConversionError("dependency_cycle")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, []):
            if dep not in graph or order[dep] >= order[node]:
                raise ConversionError("dependency_cycle" if dep in graph else "invalid_reference")
            walk(dep)
        visiting.remove(node)
        visited.add(node)

    for step_id in graph:
        walk(step_id)


def _result(plan: Plan, invariants: dict, raw: dict) -> PlannerResult:
    return PlannerResult(plan=plan, invariants=invariants, confidence=1, raw_output=raw, tokens=0)
