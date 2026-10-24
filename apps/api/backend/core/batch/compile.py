"""Expand one for_each rule into concrete steps. Never calls a model."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass

from backend.core.supervisor.contracts import Step
from backend.core.tools.catalog import CatalogError, estimate_cost

from .contracts import COMPILER_VERSION, BatchSettings
from .presets import default_preset_table

_LOCAL_ID = re.compile(r"^t[1-9][0-9]*$")
_REF = re.compile(r"^\$(t[1-9][0-9]*)((?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])+)$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_TITLE_KEYS = frozenset({"title", "headline", "draw_title", "render_text"})
_PLACEMENT_KEYS = frozenset({"mode", "anchor_id", "region_id"})
_PLACEMENT_MODES = frozenset({"right", "below", "replace", "region"})
_SIZE_FIELDS = frozenset({"width", "height", "id"})
_COMPOSITION_TOOLS = ("compose_layers", "text_layout")


def plan_expansion(plan) -> dict:
    raw = getattr(plan, "expansion", None) or {}
    return dict(raw) if isinstance(raw, dict) else {}


class CompileError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CompiledBatch:
    steps: list[Step]
    document: dict

    @property
    def task_count(self) -> int:
        return len(self.steps)


def restore(document: dict) -> CompiledBatch:
    """Rebuild steps from a stored expansion. Does not read inputs again and does not call a model."""

    if not isinstance(document, dict) or document.get("compiler_version") != COMPILER_VERSION:
        raise CompileError("compiler_version_mismatch")
    if not document.get("compiled"):
        raise CompileError("not_compiled")
    steps = [Step.model_validate(item) for item in document.get("steps") or []]
    if len(steps) != int(document.get("task_count") or -1):
        raise CompileError("stored_expansion_corrupt")
    return CompiledBatch(steps=steps, document=copy.deepcopy(document))


def compile_rule(rule: dict, assets: dict, catalog, lock, *, scope: dict | None = None,
                 settings: BatchSettings | None = None) -> CompiledBatch:
    settings = settings or BatchSettings()
    if not isinstance(rule, dict):
        raise CompileError("invalid_rule")
    if rule.get("compiled") is True:
        raise CompileError("compiled_flag_rejected")
    if len(json.dumps(rule, ensure_ascii=False, default=str).encode()) > settings.max_rule_bytes:
        raise CompileError("rule_too_large")
    preset_version, preset_table, region_ids = _locked_composition(catalog, lock)
    inputs = _resolve_inputs(rule, assets, settings)
    template = rule.get("task_template")
    sizes = list(rule.get("sizes") or [])
    if not inputs:
        raise CompileError("inputs_required")
    if not isinstance(template, list) or not template:
        raise CompileError("template_required")
    if len(inputs) > settings.max_batch_inputs:
        raise CompileError("input_limit")
    if len(template) > settings.max_rule_steps:
        raise CompileError("rule_step_limit")
    if len(sizes) > settings.max_sizes:
        raise CompileError("size_limit")
    _check_template(template)
    copies = _text_copies(template)
    scope = scope or {}
    readable = {str(item) for item in scope.get("readable_assets") or []}
    frozen_inputs = []
    seen_items = set()
    for index, item in enumerate(inputs):
        frozen = _freeze_input(item, index, assets, readable)
        frozen_inputs.append(frozen)
    edges = _edge_limits(catalog, lock, settings)
    frozen_sizes = _freeze_sizes(sizes, settings, preset_table, edges)
    for source in frozen_inputs:
        for size in frozen_sizes or [None]:
            identity = (source["asset_id"], source["version"], None if size is None else size["id"])
            if identity in seen_items:
                raise CompileError("duplicate_item")
            seen_items.add(identity)
    dimensions = frozen_sizes or [None]
    expanded = len(frozen_inputs) * len(dimensions) * len(template)
    if expanded > settings.max_expanded_tasks:
        raise CompileError("expanded_task_limit")
    steps: list[Step] = []
    items = []
    cursor = 1
    for ordinal, source in enumerate(frozen_inputs):
        for size in dimensions:
            local_to_global = {}
            item_steps = []
            item_id = _item_id(source, size, ordinal)
            for local in template:
                global_id = f"t{cursor}"
                cursor += 1
                local_to_global[local["id"]] = global_id
            for local in template:
                global_id = local_to_global[local["id"]]
                try:
                    tool = catalog.get_tool(local["tool"], lock.version)
                except (CatalogError, ValueError, KeyError, AttributeError) as exc:
                    raise CompileError("tool_unavailable") from exc
                if not getattr(tool, "available", False):
                    raise CompileError("tool_unavailable")
                price = getattr(tool, "price", None)
                if getattr(price, "kind", None) == "unpriced":
                    raise CompileError("tool_unpriced")
                parameters, derived = _rewrite(
                    local.get("parameters") or {}, source, size, local_to_global, global_id,
                    list(getattr(tool, "inputs", []) or []))
                _reject_title_prompt(local["tool"], parameters, copies)
                try:
                    credits = int(estimate_cost(tool, parameters))
                except (CatalogError, ValueError, TypeError) as exc:
                    code = "tool_unpriced" if "unpriced" in str(exc) else "invalid_parameters"
                    raise CompileError(code) from exc
                depends = [local_to_global[dep] for dep in local.get("depends_on") or []]
                for dep in derived:
                    if dep not in depends:
                        depends.append(dep)
                logical = _logical_key(item_id, local["id"])
                placement = _freeze_placement(local.get("placement") or {}, scope, region_ids)
                step = Step(id=global_id, kind="call_tool", tool=local["tool"], parameters=parameters,
                            depends_on=depends, placement=placement)
                steps.append(step)
                item_steps.append({"local_id": local["id"], "step_id": global_id, "task_key": global_id,
                                   "logical_key": logical, "stage_key": logical, "tool": local["tool"], "credits": credits})
            items.append({"item_id": item_id, "ordinal": ordinal, "asset_id": source["asset_id"],
                          "asset_version": source["version"], "sha256": source.get("sha256"),
                          "size_id": None if size is None else size["id"],
                          "size": None if size is None else {"id": size["id"], "width": size["width"], "height": size["height"]},
                          "steps": item_steps})
    digest = hashlib.sha256(json.dumps(frozen_inputs, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    document = {
        "compiled": True,
        "compiler_version": COMPILER_VERSION,
        "source_rules": copy.deepcopy(rule),
        "preset_version": preset_version,
        "sizes": frozen_sizes,
        "input_count": len(frozen_inputs),
        "inputs_sha256": digest,
        "task_count": len(steps),
        "item_count": len(items),
        "items": items,
        "steps": [step.model_dump(mode="json") for step in steps],
    }
    return CompiledBatch(steps=steps, document=document)


def isolate_items(document: dict) -> dict[str, set[str]]:
    """Map each expanded step id to the only item that may see it."""

    owners = {}
    for item in document.get("items") or []:
        ids = {step["step_id"] for step in item.get("steps") or []}
        for step_id in ids:
            owners[step_id] = ids
    return owners


def _locked_composition(catalog, lock) -> tuple[str, dict, set[str]]:
    version = getattr(lock, "version", None)
    for name in _COMPOSITION_TOOLS:
        try:
            tool = catalog.get_tool(name, version)
        except (CatalogError, ValueError, KeyError, AttributeError, TypeError):
            continue
        execution = getattr(tool, "execution", None)
        binding = getattr(execution, "binding", None) or {}
        snapshot = binding.get("composition") if isinstance(binding, dict) else None
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("presets"), dict):
            continue
        table = {}
        for key, value in snapshot["presets"].items():
            if not isinstance(value, dict):
                raise CompileError("invalid_size")
            width, height = value.get("width"), value.get("height")
            if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int):
                raise CompileError("invalid_size")
            table[str(key)] = {"width": width, "height": height}
        regions = snapshot.get("regions") or {}
        region_ids = {str(key) for key in regions} if isinstance(regions, dict) else set()
        preset_version = str(snapshot.get("preset_version") or "")
        if not preset_version or not table:
            raise CompileError("invalid_size")
        return preset_version, table, region_ids
    preset_version, table, regions = default_preset_table()
    return preset_version, table, regions


def _edge_limits(catalog, lock, settings: BatchSettings) -> tuple[int, int]:
    version = getattr(lock, "version", None)
    minimum, maximum = 1, settings.max_edge_px
    for name in _COMPOSITION_TOOLS:
        try:
            tool = catalog.get_tool(name, version)
        except (CatalogError, ValueError, KeyError, AttributeError, TypeError):
            continue
        execution = getattr(tool, "execution", None)
        binding = getattr(execution, "binding", None) or {}
        snapshot = binding.get("composition") if isinstance(binding, dict) else None
        if not isinstance(snapshot, dict):
            continue
        low, high = snapshot.get("min_canvas_edge"), snapshot.get("max_canvas_edge")
        if isinstance(low, int) and not isinstance(low, bool):
            minimum = max(minimum, low)
        if isinstance(high, int) and not isinstance(high, bool):
            maximum = min(maximum, high)
        return minimum, maximum
    return minimum, maximum


def _check_template(template: list) -> None:
    seen: set[str] = set()
    known: set[str] = set()
    for step in template:
        if not isinstance(step, dict):
            raise CompileError("invalid_template")
        ident = step.get("id")
        if isinstance(ident, str):
            known.add(ident)
    for step in template:
        ident = step.get("id")
        tool = step.get("tool")
        if not isinstance(ident, str) or not _LOCAL_ID.fullmatch(ident) or ident in seen:
            raise CompileError("invalid_template")
        if not isinstance(tool, str) or not tool or tool == "for_each":
            raise CompileError("invalid_template")
        if step.get("kind") not in (None, "call_tool"):
            raise CompileError("template_kind")
        seen.add(ident)
        depends = step.get("depends_on") or []
        if not isinstance(depends, list) or ident in depends:
            raise CompileError("self_dependency" if ident in (depends or []) else "invalid_template")
        for dep in depends:
            if dep not in known:
                raise CompileError("invalid_reference")


def _text_copies(template: list) -> list[str]:
    copies = []
    for step in template:
        if step.get("tool") != "text_layout":
            continue
        parameters = step.get("parameters") or {}
        text = parameters.get("copy")
        if not isinstance(text, str):
            text = parameters.get("text")
        if isinstance(text, str) and text.strip():
            copies.append(text.strip())
    return copies


def _reject_title_prompt(tool: str, parameters: dict, copies: list[str]) -> None:
    if _TITLE_KEYS & set(parameters):
        raise CompileError("prompt_draws_title")
    prompt = parameters.get("prompt")
    if not isinstance(prompt, str):
        return
    if tool == "text_layout":
        return
    if any(len(copy) >= 2 and copy in prompt for copy in copies):
        raise CompileError("prompt_draws_title")


def _freeze_input(item, index: int, assets: dict, readable: set[str]) -> dict:
    if not isinstance(item, dict):
        raise CompileError("invalid_input")
    asset_id = str(item.get("asset_id") or "")
    if not asset_id:
        raise CompileError("invalid_input")
    if readable and asset_id not in readable:
        raise CompileError("asset_out_of_scope")
    if set(item) - {"asset_id", "version", "media_type"}:
        raise CompileError("invalid_input")
    known = assets.get(asset_id) if isinstance(assets, dict) else None
    if not isinstance(known, dict) or known.get("version") is None:
        raise CompileError("asset_unreadable")
    if "bytes" in known or "raw" in known:
        raise CompileError("raw_bytes_rejected")
    media_type = known.get("media_type") or known.get("kind") or "image"
    supplied = item.get("version")
    if supplied is not None and (isinstance(supplied, bool) or not isinstance(supplied, int) or supplied < 1):
        raise CompileError("asset_version_required")
    frozen_version = known.get("version")
    if isinstance(frozen_version, bool) or not isinstance(frozen_version, int) or frozen_version < 1:
        raise CompileError("asset_unreadable")
    if supplied is not None and int(supplied) != frozen_version:
        raise CompileError("asset_version_conflict")
    sha = known.get("sha256")
    if sha is not None and (not isinstance(sha, str) or _SHA.fullmatch(sha) is None):
        raise CompileError("asset_unreadable")
    frozen = {"asset_id": asset_id, "version": int(frozen_version), "media_type": str(media_type), "ordinal": index}
    if isinstance(sha, str):
        frozen["sha256"] = sha
    return frozen


def _resolve_inputs(rule: dict, assets: dict, settings: BatchSettings) -> list:
    """A collection name is a frozen snapshot handle, not an execution reference."""

    collection = rule.get("inputs_collection")
    if collection is not None:
        if collection != "selected_images" or rule.get("inputs") not in (None, []):
            raise CompileError("invalid_rule")
        rows = assets.get("$selection") if isinstance(assets, dict) else None
        if not isinstance(rows, list) or not rows:
            raise CompileError("selection_missing")
        if len(rows) > settings.max_batch_inputs:
            raise CompileError("input_limit")
        encoded = json.dumps(rows, ensure_ascii=False, default=str).encode()
        if len(encoded) > settings.max_rule_bytes:
            raise CompileError("rule_too_large")
        resolved = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("asset_id"):
                raise CompileError("asset_unreadable")
            if set(row) - {"asset_id", "version", "media_type", "sha256"}:
                raise CompileError("invalid_input")
            resolved.append({"asset_id": row["asset_id"], "version": row.get("version"),
                             **({"media_type": row["media_type"]} if row.get("media_type") else {})})
        return resolved
    return _coerce_inputs(rule.get("inputs"), assets)


def _coerce_inputs(inputs, assets: dict) -> list:
    if isinstance(inputs, dict) and inputs.get("$selection") is True and set(inputs) <= {"$selection"}:
        rows = assets.get("$selection") if isinstance(assets, dict) else None
        if not isinstance(rows, list) or not rows:
            raise CompileError("selection_missing")
        return [{"asset_id": row["asset_id"]} for row in rows if isinstance(row, dict)]
    if isinstance(inputs, list):
        return inputs
    raise CompileError("inputs_required")


def _freeze_sizes(sizes: list, settings: BatchSettings, table: dict, edges: tuple[int, int]) -> list:
    frozen = []
    seen = set()
    for item in sizes:
        size = _freeze_size(item, settings, table, edges)
        if size["id"] in seen:
            raise CompileError("duplicate_size")
        seen.add(size["id"])
        frozen.append(size)
    return frozen


def _freeze_size(item, settings: BatchSettings, table: dict, edges: tuple[int, int]) -> dict:
    minimum, maximum = edges
    if isinstance(item, str):
        preset = table.get(item)
        if preset is None:
            raise CompileError("invalid_size")
        return {"id": item, "width": preset["width"], "height": preset["height"]}
    if not isinstance(item, dict):
        raise CompileError("invalid_size")
    if set(item) <= {"id"} and item.get("id") in table:
        preset = table[item["id"]]
        return {"id": item["id"], "width": preset["width"], "height": preset["height"]}
    ident = item.get("id")
    if not isinstance(ident, str) or not ident:
        raise CompileError("invalid_size")
    width, height = item.get("width"), item.get("height")
    if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int):
        raise CompileError("invalid_size")
    if width < minimum or height < minimum or width > maximum or height > maximum or width > settings.max_edge_px or height > settings.max_edge_px:
        raise CompileError("invalid_size")
    if "preset" in item or set(item) - {"id", "width", "height"}:
        raise CompileError("invalid_size")
    return {"id": ident, "width": width, "height": height}


def _item_id(source: dict, size: dict | None, ordinal: int) -> str:
    size_id = "-" if size is None else size["id"]
    raw = f"{source['asset_id']}\n{source['version']}\n{size_id}\n{ordinal}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _logical_key(item_id: str, local_id: str) -> str:
    return f"i:{item_id}:{local_id}"


def _freeze_placement(raw, scope: dict, region_ids: set[str]) -> dict:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or set(raw) - _PLACEMENT_KEYS or _contains_coordinates(raw):
        raise CompileError("invalid_placement")
    mode = raw.get("mode")
    if mode not in _PLACEMENT_MODES:
        raise CompileError("invalid_placement")
    placement = {"mode": mode}
    writable = {str(item) for item in scope.get("writable_regions") or []}
    if mode == "region":
        region_id = raw.get("region_id")
        if not isinstance(region_id, str) or not region_id or raw.get("anchor_id"):
            raise CompileError("invalid_placement")
        allowed = writable | set(region_ids)
        if region_id not in allowed:
            raise CompileError("placement_unauthorized")
        placement["region_id"] = region_id
        return placement
    if raw.get("region_id"):
        raise CompileError("invalid_placement")
    anchor_id = raw.get("anchor_id")
    if mode == "replace" and not isinstance(anchor_id, str):
        raise CompileError("placement_unauthorized")
    if anchor_id is None:
        return placement
    if not isinstance(anchor_id, str) or not anchor_id:
        raise CompileError("invalid_placement")
    objects = scope.get("objects") or {}
    if isinstance(objects, dict) and objects and anchor_id not in objects:
        raise CompileError("placement_unauthorized")
    placement["anchor_id"] = anchor_id
    return placement


def _contains_coordinates(value) -> bool:
    if isinstance(value, dict):
        if {"x", "y", "left", "top", "rect", "box"} & set(value):
            return True
        return any(_contains_coordinates(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_coordinates(item) for item in value)
    return False


def _port_segments(path: str) -> list[str]:
    segments = []
    for chunk in path.split("."):
        if chunk.endswith("[*]"):
            segments.append(chunk[:-3])
            segments.append("*")
        else:
            segments.append(chunk)
    return segments


def _port_at(stack: list[str], ports: list):
    for port in ports:
        segments = _port_segments(port.path)
        if len(segments) != len(stack):
            continue
        if all(expected == "*" or expected == actual for expected, actual in zip(segments, stack)):
            return port
    return None


def _rewrite(value, source: dict, size: dict | None, local_to_global: dict, step_id: str, ports: list):
    derived: list[str] = []

    def reference(text: str) -> str:
        match = _REF.fullmatch(text)
        if match is None:
            return text
        local = match.group(1)
        if local not in local_to_global:
            raise CompileError("invalid_reference")
        global_id = local_to_global[local]
        if global_id == step_id:
            raise CompileError("self_dependency")
        if global_id not in derived:
            derived.append(global_id)
        return "$" + global_id + match.group(2)

    def size_value(marker: dict):
        field = marker.get("$size")
        if field not in _SIZE_FIELDS:
            raise CompileError("invalid_size")
        if size is None:
            raise CompileError("size_required")
        if field == "width":
            return size["width"]
        if field == "height":
            return size["height"]
        return size["id"]

    def walk(current, stack: list[str]):
        port = _port_at(stack, ports) if stack else None
        if port is not None:
            if isinstance(current, str):
                return reference(current)
            if isinstance(current, dict) and set(current) == {"$item"}:
                if current.get("$item") != "asset" or port.media_type != "image":
                    raise CompileError("invalid_local_ref")
                return {"asset_id": source["asset_id"], "media_type": "image"}
            if isinstance(current, dict) and set(current) == {"$size"}:
                raise CompileError("invalid_parameters")
            return current
        if isinstance(current, dict):
            if set(current) == {"$item"}:
                raise CompileError("invalid_local_ref")
            if set(current) == {"$size"}:
                return size_value(current)
            return {key: walk(item, stack + [str(key)]) for key, item in current.items()}
        if isinstance(current, list):
            return [walk(item, stack + ["*"]) for item in current]
        return current

    return walk(value, []), derived
