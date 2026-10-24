"""Choose the failed suffix of an item. Successful prefixes stay terminal and are not rewritten."""
from __future__ import annotations

import copy

from .compile import CompileError, isolate_items

_BLOCKED = frozenset({"blocked_by_model"})
_FAILED = frozenset({"failed"})
_SUCCESS = frozenset({"succeeded"})
_RETRYABLE_CODES = frozenset({"retryable", "provider_down"})
_BLIND = frozenset({"param_invalid", "no_subject"})
_OPEN = frozenset({"pending", "validated", "queued", "submitted", "waiting_user"})


def error_code(outcome: dict) -> str | None:
    error = outcome.get("error") or {}
    code = error.get("code")
    return str(code) if code else None


def is_blocked(outcome: dict) -> bool:
    status = outcome.get("status")
    return status in _BLOCKED or error_code(outcome) == "blocked_by_model"


def is_retryable_failure(outcome: dict) -> bool:
    """Only confirmed retryable provider failures may seed a child run."""

    if is_blocked(outcome) or outcome.get("status") not in _FAILED:
        return False
    if outcome.get("unknown_submitted"):
        return False
    code = error_code(outcome)
    if code in _BLIND or code == "unknown":
        return False
    if code == "queue_timeout":
        return not outcome.get("submitted")
    if code in _RETRYABLE_CODES:
        return True
    return False


def latest_by_logical(document: dict, overlays: list[dict[str, dict]]) -> dict[str, dict]:
    """Fold settled child outcomes onto root task_keys via logical item/local keys."""

    merged: dict[str, dict] = {}
    logical_to_root: dict[str, str] = {}
    for item in document.get("items") or []:
        for step in item.get("steps") or []:
            logical = step.get("logical_key") or f"i:{item['item_id']}:{step['local_id']}"
            logical_to_root[logical] = step["task_key"]
            merged[step["task_key"]] = {}
    for layer in overlays:
        for key, outcome in layer.items():
            target = logical_to_root.get(key, key)
            if target in merged or target in layer:
                merged[target] = dict(outcome)
    return merged


def retry_plan(document: dict, outcomes: dict[str, dict], item_ids: list[str] | None = None) -> dict:
    """Return a new expansion containing only retryable failed steps and their successors.

    ``outcomes`` is keyed by task_key. Explicitly selecting a blocked_by_model item
    rejects that request; other blocked items do not poison a legal retry set.
    Predecessor successes become literal outputs when those assets remain reusable.
    """

    if not document.get("compiled"):
        raise CompileError("not_compiled")
    wanted = list(item_ids or [])
    wanted_set = set(wanted)
    items_out = []
    steps = []
    links = []
    cursor = 1
    owners = isolate_items(document)
    stored_steps = {step["id"]: step for step in document.get("steps") or []}
    for item in document.get("items") or []:
        if wanted_set and item["item_id"] not in wanted_set:
            continue
        by_key = {step["task_key"]: step for step in item.get("steps") or []}
        states = {}
        blocked = False
        blind = False
        for step in item.get("steps") or []:
            outcome = outcomes.get(step["task_key"]) or {}
            status = outcome.get("status")
            if is_blocked(outcome):
                blocked = True
            if status in _FAILED and error_code(outcome) in _BLIND:
                blind = True
            if status in _OPEN or outcome.get("unknown_submitted"):
                raise CompileError("run_not_settled")
            states[step["step_id"]] = status
        if blocked:
            if wanted_set:
                raise CompileError("blocked_by_model")
            continue
        if blind:
            if wanted_set:
                raise CompileError("not_retryable")
            failed_retryable = {
                step["step_id"] for step in item.get("steps") or []
                if is_retryable_failure(outcomes.get(step["task_key"]) or {})}
            if not failed_retryable:
                continue
        failed_ids = {
            step["step_id"] for step in item.get("steps") or []
            if is_retryable_failure(outcomes.get(step["task_key"]) or {})}
        if not failed_ids:
            continue
        redo = _with_successors(item, failed_ids, stored_steps)
        redo = _with_unreusable_prefixes(item, redo, stored_steps, by_key, outcomes)
        local_map = {}
        kept = []
        for step in item["steps"]:
            if step["step_id"] not in redo:
                continue
            global_id = f"t{cursor}"
            cursor += 1
            local_map[step["step_id"]] = global_id
            kept.append(step)
        new_steps_meta = []
        for step in kept:
            source = stored_steps[step["step_id"]]
            parameters = _rebind(source.get("parameters") or {}, local_map, by_key, outcomes, owners)
            depends = [local_map[dep] for dep in source.get("depends_on") or [] if dep in local_map]
            dumped = {**source, "id": local_map[step["step_id"]], "parameters": parameters, "depends_on": depends}
            steps.append(dumped)
            logical = f"i:{item['item_id']}:{step['local_id']}"
            new_steps_meta.append({
                "local_id": step["local_id"], "step_id": dumped["id"], "task_key": dumped["id"],
                "logical_key": logical, "stage_key": logical, "tool": step["tool"],
                "source_task_key": step["task_key"]})
            links.append({"item_id": item["item_id"], "source_task_key": step["task_key"],
                          "task_key": dumped["id"], "logical_key": logical, "local_id": step["local_id"]})
        items_out.append({**{k: item[k] for k in ("item_id", "ordinal", "asset_id", "asset_version", "size_id") if k in item},
                          "steps": new_steps_meta})
    if wanted_set and not items_out:
        for ident in wanted:
            found = next((item for item in document.get("items") or [] if item["item_id"] == ident), None)
            if found is None:
                raise CompileError("nothing_to_retry")
    if not items_out:
        raise CompileError("nothing_to_retry")
    return {
        "compiled": True,
        "compiler_version": document.get("compiler_version"),
        "retry": True,
        "source_rules": document.get("source_rules") or {},
        "task_count": len(steps),
        "item_count": len(items_out),
        "items": items_out,
        "steps": steps,
        "links": links,
    }


def quote_of(redo: dict, prices: dict[str, int]) -> int:
    total = 0
    for step in redo.get("steps") or []:
        tool = step.get("tool")
        if tool not in prices:
            raise CompileError("tool_unpriced")
        total += int(prices[tool])
    return total


def quote_catalog(redo: dict, catalog) -> int:
    from jsonschema.exceptions import ValidationError

    from backend.core.tools.catalog import CatalogError, estimate_cost

    total = 0
    for step in redo.get("steps") or []:
        try:
            tool = catalog.get_tool(step["tool"], catalog.lock.version)
        except (CatalogError, ValueError, TypeError, KeyError, AttributeError):
            raise CompileError("tool_unpriced") from None
        try:
            total += int(estimate_cost(tool, step.get("parameters") or {}))
        except (CatalogError, ValidationError, ValueError, TypeError):
            if not tool.available or tool.price.kind == "unpriced" or tool.price.credits is None:
                raise CompileError("tool_unpriced") from None
            total += int(tool.price.credits)
    return total


def budget_allows(limit: int, reserved: int, allocated: int, spent: int, quote: int,
                  pending_quotes: int = 0) -> bool:
    """Reject a retry that would pass the parent run's remaining reserved or allocated budget."""

    if quote < 0 or pending_quotes < 0:
        return False
    held = max(reserved, allocated + spent) + pending_quotes
    return quote <= limit - held


def headroom(limit: int, reserved: int, allocated: int, spent: int, pending_quotes: int = 0) -> int:
    return max(0, limit - max(reserved, allocated + spent) - pending_quotes)


def _with_successors(item: dict, failed_ids: set[str], stored_steps: dict) -> set[str]:
    graph = {}
    for step in item["steps"]:
        source = stored_steps[step["step_id"]]
        for dep in source.get("depends_on") or []:
            graph.setdefault(dep, set()).add(step["step_id"])
    redo = set(failed_ids)
    stack = list(failed_ids)
    while stack:
        current = stack.pop()
        for child in graph.get(current, ()):
            if child not in redo:
                redo.add(child)
                stack.append(child)
    return redo


def _reusable(outcome: dict) -> bool:
    if outcome.get("status") not in _SUCCESS:
        return False
    if outcome.get("reusable") is False:
        return False
    output = outcome.get("output")
    if not isinstance(output, dict) or not output.get("asset_id"):
        return False
    return True


def _with_unreusable_prefixes(item: dict, redo: set[str], stored_steps: dict, by_key: dict,
                              outcomes: dict) -> set[str]:
    by_step = {meta["step_id"]: meta for meta in by_key.values()}
    changed = True
    while changed:
        changed = False
        for step in item["steps"]:
            if step["step_id"] not in redo:
                continue
            source = stored_steps[step["step_id"]]
            for dep in source.get("depends_on") or []:
                if dep in redo:
                    continue
                meta = by_step.get(dep)
                outcome = outcomes.get(meta["task_key"]) if meta else {}
                if not _reusable(outcome or {}):
                    redo.add(dep)
                    changed = True
    return redo


def _rebind(value, local_map, by_key, outcomes, owners):
    if isinstance(value, str) and value.startswith("$"):
        ref = value[1:].split(".", 1)[0]
        if ref in local_map:
            suffix = value[1 + len(ref):]
            return "$" + local_map[ref] + suffix
        if ref in owners and local_map:
            here = owners.get(next(iter(local_map)))
            if here is not None and owners.get(ref) is not here:
                raise CompileError("cross_item_reference")
        produced = _output_of(ref, by_key, outcomes)
        if produced is None:
            raise CompileError("missing_reused_output")
        return produced
    if isinstance(value, list):
        return [_rebind(item, local_map, by_key, outcomes, owners) for item in value]
    if isinstance(value, dict):
        return {key: _rebind(item, local_map, by_key, outcomes, owners) for key, item in value.items()}
    return copy.deepcopy(value)


def _output_of(step_id: str, by_key: dict, outcomes: dict):
    for meta in by_key.values():
        if meta["step_id"] != step_id:
            continue
        outcome = outcomes.get(meta["task_key"]) or {}
        if not _reusable(outcome):
            return None
        output = outcome.get("output")
        return {"asset_id": output["asset_id"], "media_type": output.get("media_type") or "image"}
    return None
