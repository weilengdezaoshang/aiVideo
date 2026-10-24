"""Adapt the locked catalog to the existing three-gate reviewer and typed-port gate."""
import copy
import re
from pydantic import ValidationError
from jsonschema.exceptions import ValidationError as SchemaError
from backend.agent.admission.tool_types import TypedTask, review_tool_types
from backend.core.review.contracts import Invariants
from backend.core.review.policy import validate
from backend.core.tools.catalog import estimate_cost
from backend.core.tools.contracts import VersionLock
from .contracts import Settings

_DELIVERABLE = {"count", "modality", "aspect_ratio", "width", "height", "duration_seconds", "required_asset_ids"}
_CN_COUNT = {"一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_EN_COUNT = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM = r"((?:(?<![A-Za-z])(?:one|two|three|four|five|six|seven|eight|nine|ten)|[一二两俩三四五六七八九十]|\d+))"
_COUNT = re.compile(_NUM + r"\s*(?:张|幅|(?:images?|pictures?|photos?)(?![A-Za-z]))", re.I)
_DURATION = re.compile(_NUM + r"\s*(?:秒钟|秒|(?:seconds|second|secs|sec|s)(?![A-Za-z]))", re.I)
_SIZE = re.compile(r"(?<![\d.])(\d+)\s*[x×*]\s*(\d+)(?![\d.])", re.I)
_WIDTH = re.compile(r"(?:宽度|width|宽)\s*[:：]?\s*(\d+)|(?<![\d.])(\d+)\s*(?:px)?\s*(?:宽|wide)\b", re.I)
_HEIGHT = re.compile(r"(?:高度|height|高)\s*[:：]?\s*(\d+)|(?<![\d.])(\d+)\s*(?:px)?\s*(?:高|tall)\b", re.I)
_RATIO = re.compile(r"^\d+:\d+$")


def _norm(value):
    if value == []:
        return None
    return value


def _message_run(message):
    if isinstance(message, dict):
        return message.get("run_id")
    return getattr(message, "run_id", None)


def _sources(run, messages):
    """This run's user messages in sequence, including authorized corrections. Snapshot is fallback."""

    request = (run.snapshot or {}).get("request") or {}
    fields = {}
    current = getattr(run, "id", None)
    ordered = []
    for message in messages or []:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if role != "user" or not isinstance(content, dict):
            continue
        owner = _message_run(message)
        if current is not None and str(owner) != str(current):
            continue
        if isinstance(content.get("fields"), dict) and content.get("task_id"):
            fields.update(content["fields"])
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                ordered.append(text.strip())
            continue
        if content.get("task_id") and not content.get("fields"):
            continue
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            ordered.append(text.strip())
    if ordered:
        return ordered, fields
    stored = request.get("messages")
    if isinstance(stored, list) and stored:
        return [str(item) for item in stored if str(item).strip()], fields
    return [str(request.get("message") or "")], fields


def _same(left, right):
    if isinstance(right, bool) or isinstance(left, bool):
        return left == right
    if isinstance(right, int) and isinstance(left, (int, float)) and not isinstance(left, bool):
        return float(left) == float(right) and float(left).is_integer()
    return left == right


def _from_form(key, value, fields):
    return key in fields and _same(fields[key], value)


def _quantity(token):
    if token in _CN_COUNT:
        return _CN_COUNT[token]
    lowered = token.lower()
    if lowered in _EN_COUNT:
        return _EN_COUNT[lowered]
    return int(token)


def _counts(texts):
    found = set()
    for text in texts:
        for match in _COUNT.finditer(text):
            found.add(_quantity(match.group(1)))
    return found


def _durations(texts):
    found = set()
    for text in texts:
        for match in _DURATION.finditer(text):
            found.add(_quantity(match.group(1)))
    return found


def _axis(texts, pattern, size_group):
    found = set()
    for text in texts:
        for match in _SIZE.finditer(text):
            found.add(int(match.group(size_group)))
        for match in pattern.finditer(text):
            token = next(group for group in match.groups() if group)
            found.add(int(token))
    return found


def _supported(key, value, texts, fields, run):
    if _from_form(key, value, fields):
        return True
    if key == "count":
        return value in _counts(texts)
    if key == "duration_seconds":
        return isinstance(value, int) and not isinstance(value, bool) and value in _durations(texts)
    if key == "width":
        return isinstance(value, int) and not isinstance(value, bool) and value in _axis(texts, _WIDTH, 1)
    if key == "height":
        return isinstance(value, int) and not isinstance(value, bool) and value in _axis(texts, _HEIGHT, 2)
    if key == "modality":
        for text in texts:
            lowered = text.lower()
            if value == "video" and re.search(r"视频|动画|短片|\bvideo\b", lowered, re.I):
                return True
            if value == "image" and re.search(r"图片|图像|\bimage\b", lowered, re.I):
                return True
        return False
    if key == "aspect_ratio":
        return isinstance(value, str) and any(value in text.replace("：", ":") for text in texts)
    if key == "required_asset_ids":
        selected = {str(item) for item in ((run.snapshot or {}).get("request") or {}).get("selected_assets") or []}
        return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item in selected for item in value)
    return False


def _coerce(key, value):
    if key in {"count", "duration_seconds", "width", "height"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
            return None
        return int(value)
    if key == "modality" and value in {"image", "video"}:
        return value
    if key == "aspect_ratio" and isinstance(value, str) and _RATIO.fullmatch(value):
        return value
    if key == "required_asset_ids" and isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _dump_invariants(value):
    dumped = Invariants.model_validate(value).model_dump(exclude_none=True)
    deliverable = {key: item for key, item in dict(dumped.get("deliverable") or {}).items() if item != []}
    if not deliverable:
        dumped.pop("deliverable", None)
    else:
        dumped["deliverable"] = deliverable
    return dumped


def apply_form_answers(invariants, fields):
    """Program-approve only missing deliverable fields that the user actually submitted."""

    current = copy.deepcopy(invariants or {})
    item = dict(current.get("deliverable") or {})
    for key, value in (fields or {}).items():
        if key not in _DELIVERABLE or key in item:
            continue
        coerced = _coerce(key, value)
        if coerced is None:
            continue
        item[key] = coerced
    if item:
        current["deliverable"] = item
    return _dump_invariants(current)


def effective_invariants(run, plan=None):
    """Approved plan constraints, otherwise the immutable run row. Missing keys keep the old fallback."""

    fallback = copy.deepcopy(getattr(run, "invariants", None) or {})
    for bag in (getattr(plan, "link_constraints", None), getattr(plan, "rationale", None)):
        if isinstance(bag, dict) and isinstance(bag.get("approved_invariants"), dict):
            return copy.deepcopy(bag["approved_invariants"])
    return fallback


def merge_proposed(run, proposed, messages, *, frozen=None, base=None, revision_texts=None):
    """Reject conflicts and unproven values. Empty lists match a missing field and never erase one."""

    current = copy.deepcopy(run.invariants or {} if base is None else base)
    if not proposed:
        return current, []
    if not isinstance(proposed, dict) or set(proposed) - {"deliverable"} or not isinstance(proposed.get("deliverable"), dict):
        return current, [{"gate": "intent", "code": "invariants_conflict"}]
    current_item = dict((current.get("deliverable") or {}))
    texts, fields = _sources(run, messages)
    if frozen is None:
        frozen = bool((run.snapshot or {}).get("constraints_frozen"))
    reasons = []
    merged_item = dict(current_item)
    for key, value in proposed["deliverable"].items():
        if _norm(value) is None:
            continue
        if key not in _DELIVERABLE:
            reasons.append({"gate": "intent", "code": "invariants_conflict", "field": key})
            continue
        user_revised = bool(revision_texts) and _supported(key, value, revision_texts, {}, run)
        if key in current_item and _norm(current_item[key]) != _norm(value):
            if user_revised:
                merged_item[key] = value
                continue
            reasons.append({"gate": "intent", "code": "invariants_frozen" if frozen else "invariants_conflict", "field": key})
            continue
        if key in current_item:
            continue
        if frozen and not _from_form(key, value, fields) and not user_revised:
            reasons.append({"gate": "intent", "code": "invariants_frozen", "field": key})
            continue
        if not _supported(key, value, texts, fields, run):
            reasons.append({"gate": "intent", "code": "invariant_unproven", "field": key})
            continue
        merged_item[key] = value
    if reasons:
        return current, reasons
    try:
        cleaned = _dump_invariants({"deliverable": merged_item} if merged_item else {})
    except ValidationError:
        return current, [{"gate": "intent", "code": "invariants_conflict"}]
    return cleaned, []


def conversion_rejection(raw):
    """Forced reviewer reject. Reasons carry redacted conversion codes only."""

    if not isinstance(raw, dict) or not raw.get("rejected_planning_result"):
        return None
    reasons = []
    seen = set()
    for item in raw.get("conversion_errors") or []:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if not isinstance(code, str) or not code or code in seen:
            continue
        seen.add(code)
        reasons.append({"gate": "legal", "code": code})
    if not reasons:
        reasons.append({"gate": "legal", "code": "rejected_planning_result"})
    return {"execution_constraints": [], "verdict": "reject", "reasons": reasons, "quoted_credits": 0,
            "checks": {"legal": False, "types": True, "intent": True}}


def review(plan, run, catalog, balance, *, invariants=None, intent_extensions=None):
    if plan.result_type == "answer_only":
        issues = []
        if plan.steps:
            issues.append({"gate": "legal", "code": "answer_only_has_tasks"})
        if not str(plan.answer or "").strip():
            issues.append({"gate": "intent", "code": "empty_answer"})
        if plan.goal != run.snapshot["request"]["message"]:
            issues.append({"gate": "intent", "code": "goal_changed"})
        return {"execution_constraints": [], "verdict": "reject" if issues else "pass", "reasons": issues,
                "quoted_credits": 0,
                "checks": {gate: not any(i["gate"] == gate for i in issues) for gate in ("legal", "types", "intent")}}
    issues, tools, steps, constraints = [], {}, [], []
    expansion = getattr(plan, "expansion", None) or {}
    task_cap = Settings().max_tasks
    if expansion.get("compiled"):
        from backend.core.batch.contracts import BatchSettings
        task_cap = BatchSettings().max_expanded_tasks
    if len(plan.steps) > task_cap:
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
    batch_identity = {member["step_id"]: item["item_id"]
                      for item in expansion.get("items", []) for member in item.get("steps", [])}
    for step in plan.steps:
        if step.kind == "ask_user":
            steps.append({"id": step.id, "kind": "ask_user", "depends_on": step.depends_on})
            continue
        try:
            tool = catalog.get_tool(step.tool, catalog.lock.version)
            quote = estimate_cost(tool, step.parameters)
            if (tool.mode != "read_only" and not any(p.media_type in {"image", "video", "audio"} for p in tool.outputs)
                    and not (tool.name == "text_layout" and tool.execution.adapter == "composition.text_layout")):
                issues.append({"gate": "legal", "code": "executor_not_supported", "step": step.id})
            typed.append(TypedTask(id=step.id, tool=step.tool, parameters=step.parameters,
                depends_on=[key for key in step.depends_on if next((s for s in plan.steps if s.id == key), None).kind == "call_tool"]))
            inputs = {}
            for port in tool.inputs:
                value = step.parameters.get(port.path)
                if value is not None and "[*]" not in port.path:
                    inputs[port.path] = value if isinstance(value, str) else {"asset_id": value.get("asset_id", value.get("source_asset_id", ""))}
            params = {k: v for k, v in step.parameters.items() if k in tools[tool.name]["params_schema"]}
            if step.id in batch_identity:
                params = {**params, "_batch_item": batch_identity[step.id]}
            if expansion.get("compiled"):
                asset_id = next((value.get("asset_id") for value in step.parameters.values()
                                 if isinstance(value, dict) and value.get("asset_id")), "")
                if asset_id:
                    params = {**params, "_batch_asset": asset_id}
            steps.append({"id": step.id, "tool": step.tool, "tool_version": tool.version,
                "params": params, "inputs": inputs, "depends_on": step.depends_on, "estimated_cost": quote})
        except (ValueError, TypeError, AttributeError, SchemaError):
            issues.append({"gate": "legal", "code": "invalid_tool_or_parameters", "step": step.id})
    if not issues:
        result = review_tool_types(typed, catalog, VersionLock.model_validate(run.version_lock["tool_catalog"]))
        issues.extend(result.issues)
        constraints = result.execution_constraints
        context = {"user_id": run.user_id, "user_credits": balance, "run_budget": run.credits_limit - run.credits_spent,
            "scope": {k: run.scope.get(k, []) for k in ("tools", "readable_assets", "writable_regions")},
            "version_lock": {"tools": tools}, "invariants": run.invariants if invariants is None else invariants,
            "canvas": {"assets": run.snapshot.get("assets", []), "nodes": [{"id": str(k)} for k in run.snapshot.get("objects", {})]}}
        result = validate({"goal": plan.goal, "steps": steps}, context,
            intent_extensions=intent_extensions)
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
    quoted = sum(int(step.get("estimated_cost") or 0) for step in steps)
    return {"execution_constraints": constraints, "verdict": "reject" if issues else "pass", "reasons": issues,
        "quoted_credits": quoted,
        "checks": {gate: not any(i["gate"] == gate for i in issues) for gate in ("legal", "types", "intent")}}
