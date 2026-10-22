"""Gate ② for versioned typed ports. This check alone NEVER grants execution admission.

The legacy MediaPlan remains unchanged. New task contracts can reuse this gate;
legal and intent review plus a durable admission record are still mandatory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator

from backend.core.tools.catalog import CatalogError, ToolCatalog
from backend.core.tools.contracts import Contract, VersionLock


class TypedTask(Contract):
    id: str
    tool: str
    parameters: dict
    depends_on: list[str]


@dataclass
class TypeReview:
    issues: list[dict] = field(default_factory=list)
    execution_constraints: list[dict] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues


def _values(value, path):
    if not path:
        return [value]
    head, *tail = path
    if head.endswith("[*]"):
        items = value.get(head[:-3], []) if isinstance(value, dict) else []
        return [v for item in items if isinstance(items, list) for v in _values(item, tail)]
    if not isinstance(value, dict) or head not in value:
        return []
    return _values(value[head], tail)


def _read(value, path):
    for part in path.split("."):
        match = re.fullmatch(r"([a-zA-Z_][a-zA-Z0-9_]*)(?:\[(\d+)\])?", part)
        if not match or not isinstance(value, dict):
            raise ValueError("invalid reference path")
        value = value[match[1]]
        if match[2] is not None:
            value = value[int(match[2])]
    return value


def review_tool_types(tasks: list[TypedTask], catalog: ToolCatalog, version_lock: VersionLock, *,
                      results: dict[str, dict] | None = None) -> TypeReview:
    catalog.check_lock(version_lock)
    results = results or {}
    review = TypeReview()
    by_id = {task.id: task for task in tasks}
    def issue(task, code, path):
        review.issues.append({"gate": "types", "task_id": task.id, "code": code, "path": path})
    if len(by_id) != len(tasks):
        return TypeReview(issues=[{"gate": "types", "code": "duplicate_task_id"}])
    completed = set()
    for task in tasks:
        try:
            tool = catalog.get_tool(task.tool, version_lock.version)
        except CatalogError:
            issue(task, "unknown_tool", "tool")
            continue
        if not tool.available:
            issue(task, "tool_unavailable", "tool")
        if not set(task.depends_on) <= completed:
            issue(task, "dependency_not_prior_or_unknown", "depends_on")
        completed.add(task.id)
        errors = list(Draft202012Validator(tool.parameters).iter_errors(task.parameters))
        if errors:
            issue(task, "parameter_schema_invalid", "parameters")
            continue
        resolved = {}
        for port in tool.inputs:
            values = _values(task.parameters, port.path.split("."))
            if port.required and not values:
                issue(task, "required_input_missing", port.path)
            for value in values:
                if isinstance(value, str):
                    match = re.fullmatch(r"\$([a-z][a-z0-9_]*)\.(.+)", value)
                    if not match or match[1] not in task.depends_on or match[1] not in by_id:
                        issue(task, "reference_not_dependent", port.path)
                        continue
                    upstream = by_id[match[1]]
                    try:
                        producer = catalog.get_tool(upstream.tool, version_lock.version)
                    except CatalogError:
                        issue(task, "unknown_producer_tool", port.path)
                        continue
                    generic = re.sub(r"\[\d+\]", "[*]", match[2])
                    output = next((p for p in producer.outputs if p.path == generic), None)
                    if output is None or output.media_type != port.media_type:
                        issue(task, "reference_type_mismatch", port.path)
                        continue
                    constraint = {"task_id": task.id, "input": port.path, "producer": upstream.id,
                                  "output": match[2], "when": output.when, "must_exist": True}
                    review.execution_constraints.append(constraint)
                    if upstream.id in results:
                        result = results[upstream.id]
                        if not Draft202012Validator(producer.result_schema).is_valid(result):
                            issue(task, "producer_result_invalid", port.path)
                            continue
                        if any(result.get(k) != v for k, v in output.when.items()):
                            issue(task, "producer_result_branch_mismatch", port.path)
                            continue
                        try:
                            value = _read(result, match[2])
                        except (KeyError, IndexError, TypeError, ValueError):
                            issue(task, "reference_output_missing", port.path)
                            continue
                    else:
                        continue
                if not isinstance(value, dict) or value.get("media_type") != port.media_type:
                    issue(task, "literal_type_mismatch", port.path)
                resolved[port.path] = value
        if tool.limits.get("same_source_required"):
            review.execution_constraints.append({"task_id": task.id, "same_source": ["image", "mask"],
                                                 "same_dimensions": True, "same_source_sha256": True})
            image, mask = resolved.get("image"), resolved.get("mask")
            if image and mask and mask.get("source_asset_id") != image.get("asset_id"):
                issue(task, "mask_source_mismatch", "mask")
    return review
