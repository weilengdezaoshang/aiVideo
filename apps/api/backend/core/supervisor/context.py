"""Pure context assembly for the supervisor planner. No I/O and no inference over frozen constraints."""

from __future__ import annotations

import json
from typing import Any

from backend.core.supervisor.contracts import Settings
from backend.core.supervisor.prompts import load_prompt

_COORDINATE_KEYS = frozenset({
    "x", "y", "width", "height", "left", "top", "right", "bottom",
    "bounds", "position", "scale", "rotation", "camera", "viewport",
})
_CARD_FIELDS = ("id", "asset_id", "node_id", "kind", "type", "name", "label", "text", "media_type",
    "card_id", "card_kind", "category", "starred", "description", "source_asset_id",
    "version_number", "head_status", "recognition")
_RULE_SPLIT = "【当前信息】"
_BOUNDARY = "【当前阶段边界】"
_SOFT_LISTS = ("messages", "tasks", "asset_cards", "recent_outputs", "upload")


class ContextLimitError(ValueError):
    """Required context cannot fit the configured character budget."""


def encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":")))


def build_context(context: dict, settings: Settings | None = None) -> dict:
    """Tier snapshot objects and return a bounded view. Coordinates never leave this function."""

    settings = settings or Settings()
    request = context.get("request") or {}
    readable = _readable(context)
    readable_nodes = _readable_nodes(context)
    selected_ids = _selected_ids(context)
    viewport = context.get("viewport")
    objects = _objects(context.get("snapshot") or {})
    visible = [item for item in objects if _visible(item, readable, readable_nodes)]

    selected: list[dict] = []
    inside: list[dict] = []
    outside_kinds: dict[str, int] = {}
    for item in visible:
        identity = _ids(item)
        if identity & selected_ids:
            selected.append(_full(item))
        elif viewport is not None and _intersects(item, viewport):
            inside.append(_coarse(item, settings))
        else:
            kind = str(item.get("kind") or item.get("type") or "object")
            outside_kinds[kind] = outside_kinds.get(kind, 0) + 1

    batch = _explicit_batch(request)
    collection = None
    if batch:
        collection = _batch_collection(selected)
        _check_batch_collection(collection)
        selected, inside = [], inside[: settings.context_max_objects]
    else:
        selected, inside = _cap_objects(selected, inside, settings.context_max_objects)
    cards = _filter_cards(context.get("asset_cards") or [], readable, _readable_cards(context))
    recent = _filter_cards(context.get("recent_outputs") or [], readable)
    dynamic = {
        "canvas": {"selected": selected, "viewport": inside, "outside": {
            "count": sum(outside_kinds.values()), "by_kind": outside_kinds}},
        "upload": _uploads(context.get("snapshot") or {}, readable),
        "selection": selected,
        "recent_outputs": recent,
        "asset_cards": cards,
        "skill": context.get("skill") or {},
        "summary": context.get("summary") or "",
        "language": context.get("language") or request.get("language") or "",
        "invariants": context.get("invariants") or {},
        "form_answers": context.get("form_answers") or {},
        "plan": context.get("plan"),
        "tasks": list(context.get("tasks") or []),
        "events": _events(context),
        "messages": list(context.get("messages") or [])[: settings.context_messages],
    }
    if collection is not None:
        dynamic["collections"] = {"selected_images": collection}
    if "confirmed_decisions" in context:
        dynamic["confirmed_decisions"] = list(context.get("confirmed_decisions") or [])
    return {
        "request_message": str(request.get("message") or ""),
        "dynamic": _truncate(dynamic, settings.context_max_chars),
        "prompt": load_prompt(),
    }


def assemble_messages(
    context: dict,
    settings: Settings | None = None,
    *,
    prompt_version: str | None = None,
    prompt_hash: str | None = None,
) -> list[dict[str, str]]:
    """Fixed prompt rules first, shielded dynamic tags after, and the raw request as its own user turn.

    A frozen version or hash is loaded through ``load_prompt`` and rejected on mismatch.
    Without a pin, the current default template is used.
    """

    built = context if "dynamic" in context and "prompt" in context else build_context(context, settings)
    version = prompt_version if prompt_version is not None else context.get("prompt_version")
    expected = prompt_hash if prompt_hash is not None else context.get("prompt_hash")
    if version is not None or expected is not None:
        prompt = load_prompt(str(version or "supervisor-v1"), None if expected is None else str(expected))
    else:
        prompt = built["prompt"]
    fixed, _, tail = prompt.text.partition(_RULE_SPLIT)
    boundary = ""
    if _BOUNDARY in tail:
        boundary = _BOUNDARY + tail.partition(_BOUNDARY)[2]
    dynamic = _tags(built["dynamic"])
    system = (
        f"prompt_version={prompt.version}\nprompt_hash={prompt.content_hash}\n"
        + fixed.rstrip()
        + ("\n\n" + boundary.rstrip() if boundary else "")
        + "\n\n"
        + dynamic
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": built["request_message"]},
    ]


def _explicit_batch(request: dict) -> bool:
    """Only an explicit batch request may replace the selected list with a collection handle."""

    return request.get("batch") is True or request.get("inputs_collection") == "selected_images"


def _batch_collection(selected: list[dict]) -> dict:
    """Compact description for the model. The compiler reads the frozen snapshot, not this text."""

    types = sorted({str(item.get("kind") or item.get("type") or "image") for item in selected}) or ["image"]
    return {"id": "selected_images", "media_type": types[0] if len(types) == 1 else "mixed",
            "count": len(selected), "types": types}


def _check_batch_collection(collection: dict) -> None:
    from backend.core.batch.contracts import BatchSettings

    limits = BatchSettings()
    if collection["count"] > limits.max_batch_inputs:
        raise ContextLimitError("input_limit")
    if encoded_size(collection) > limits.max_rule_bytes:
        raise ContextLimitError("rule_too_large")


def _scope(context: dict) -> dict:
    scope = context.get("scope") or (context.get("request") or {}).get("scope") or {}
    return scope if isinstance(scope, dict) else {}


def _readable(context: dict) -> set[str]:
    return {str(item) for item in _scope(context).get("readable_assets") or []}


def _readable_cards(context: dict) -> set[str]:
    """Frozen card ids the Run may reference; cards authorize their own source image."""

    return {str(item) for item in _scope(context).get("readable_cards") or [] if str(item)}


def _readable_nodes(context: dict) -> set[str]:
    """Explicit node whitelist. Empty means no unscoped text; this module does not freeze the snapshot."""

    return {str(item) for item in _scope(context).get("readable_nodes") or [] if str(item)}


def _selected_ids(context: dict) -> set[str]:
    raw = context.get("selection") or []
    node_ids: list = []
    if isinstance(raw, dict):
        node_ids = list(raw.get("nodeIDs") or raw.get("node_ids") or [])
        raw = raw.get("ids") or raw.get("asset_ids") or []
    request = context.get("request") or {}
    request_nodes = request.get("nodeIDs") or request.get("node_ids") or []
    return {str(item) for item in list(raw) + node_ids + list(request.get("selected_assets") or []) + list(request_nodes) if str(item)}


def _is_text(item: dict) -> bool:
    kind = str(item.get("kind") or item.get("type") or "").lower()
    return kind == "text"


def _visible(item: dict, readable_assets: set[str], readable_nodes: set[str]) -> bool:
    """Assets stay on the asset allowlist. Text without an asset id needs an explicit node id."""

    asset_id = _asset_of(item)
    if asset_id:
        return asset_id in readable_assets
    if not _is_text(item):
        return False
    return bool(_ids(item) & readable_nodes)


def _objects(snapshot: dict) -> list[dict]:
    """Accept a legacy list or the production ``dict[node_id, node]`` map."""

    rows = snapshot.get("objects")
    if rows is None:
        rows = snapshot.get("nodes") or []
    if isinstance(rows, dict):
        items = []
        for node_id, node in rows.items():
            if not isinstance(node, dict):
                continue
            copied = dict(node)
            copied["node_id"] = str(node_id)
            items.append(copied)
        return items
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def _asset_of(item: dict) -> str:
    return str(item.get("asset_id") or item.get("assetId") or "")


def _ids(item: dict) -> set[str]:
    values = (
        _asset_of(item),
        str(item.get("id") or ""),
        str(item.get("node_id") or item.get("nodeId") or ""),
    )
    return {value for value in values if value}


def _box(item: dict) -> tuple[float, float, float, float] | None:
    bounds = item.get("bounds") if isinstance(item.get("bounds"), dict) else item
    keys = ("x", "y", "width", "height")
    if not all(key in bounds for key in keys):
        return None
    return tuple(float(bounds[key]) for key in keys)  # type: ignore[return-value]


def _view_box(viewport: dict) -> tuple[float, float, float, float] | None:
    if {"x", "y", "width", "height"} <= viewport.keys():
        return _box(viewport)
    if {"left", "top", "width", "height"} <= viewport.keys():
        return (float(viewport["left"]), float(viewport["top"]), float(viewport["width"]), float(viewport["height"]))
    return None


def _intersects(item: dict, viewport: dict) -> bool:
    left = _box(item)
    view = _view_box(viewport)
    if left is None or view is None:
        return False
    ax, ay, aw, ah = left
    vx, vy, vw, vh = view
    return ax < vx + vw and ax + aw > vx and ay < vy + vh and ay + ah > vy


def _public(item: dict, keys: tuple[str, ...]) -> dict:
    result = {}
    for key in keys:
        if key in item and key not in _COORDINATE_KEYS:
            result[key] = item[key]
    return result


def _with_identity(item: dict, kept: dict) -> dict:
    asset_id = _asset_of(item)
    if asset_id:
        kept["asset_id"] = asset_id
    node_id = item.get("node_id") or item.get("nodeId")
    if node_id:
        kept["node_id"] = str(node_id)
    if "kind" not in kept and "type" in kept:
        kept["kind"] = kept.pop("type")
    return kept


def _full(item: dict) -> dict:
    return _with_identity(item, _public(item, _CARD_FIELDS))


def _coarse(item: dict, settings: Settings) -> dict:
    kept = _with_identity(item, _public(item, ("id", "asset_id", "node_id", "kind", "type")))
    text = item.get("text")
    if isinstance(text, str) and text:
        kept["text"] = text[: settings.context_text_snippet_chars]
    return kept


def _cap_objects(selected: list[dict], inside: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    if len(selected) > limit:
        raise ContextLimitError("selected_objects_exceed_context_limit")
    room = max(limit - len(selected), 0)
    return selected, inside[:room]


def _filter_cards(cards: list, readable: set[str], readable_cards: set[str] | None = None) -> list:
    """Keep allowlisted fields of an authorized asset or Run-frozen card. Anything else is dropped."""

    allowed_cards = readable_cards if readable_cards is not None else set()
    kept = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        asset_id = _asset_of(card)
        card_id = str(card.get("card_id") or "")
        if not ((asset_id and asset_id in readable) or (card_id and card_id in allowed_cards)):
            continue
        view = _full(card)
        if view.get("asset_id") in readable or (view.get("card_id") or "") in allowed_cards:
            kept.append(view)
    return kept


def _uploads(snapshot: dict, readable: set[str]) -> list:
    rows = []
    for item in snapshot.get("uploads") or []:
        if isinstance(item, dict) and _asset_of(item) in readable:
            rows.append(_full(item))
    return rows


def _events(context: dict) -> list:
    if "events" in context:
        return list(context.get("events") or [])
    event = context.get("event")
    return [] if event is None else [event]


def _review_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    kind = str(event.get("type") or event.get("kind") or "")
    return kind.startswith("review") or "reason" in event or "gate" in event


def _truncate(dynamic: dict, limit: int) -> dict:
    """Fit soft fields into the budget. Required invariants and review reasons are not clipped."""

    events = list(dynamic.get("events") or [])
    required_events = [event for event in events if _review_event(event)]
    trimmed = {key: value for key, value in dynamic.items() if key != "tools"}
    trimmed["events"] = required_events + [event for event in events if not _review_event(event)]
    required = {"invariants": trimmed.get("invariants") or {}, "events": required_events,
                "form_answers": trimmed.get("form_answers") or {}}
    if trimmed.get("confirmed_decisions"):
        required["confirmed_decisions"] = trimmed["confirmed_decisions"]
    if encoded_size(required) > limit:
        raise ContextLimitError("context_required_fields_exceed_limit")
    if encoded_size(trimmed) <= limit:
        return trimmed
    for key in _SOFT_LISTS:
        trimmed[key] = []
        if encoded_size(trimmed) <= limit:
            return trimmed
    trimmed["skill"] = {}
    trimmed["plan"] = None
    trimmed["summary"] = ""
    trimmed["events"] = required_events
    canvas = dict(trimmed.get("canvas") or {})
    selected = [dict(item) for item in canvas.get("selected") or [] if isinstance(item, dict)]
    canvas["viewport"] = []
    canvas["selected"] = selected
    trimmed["canvas"] = canvas
    trimmed["selection"] = selected
    if encoded_size(trimmed) <= limit:
        return _fill_text(trimmed, dynamic, limit)
    outside = dict(canvas.get("outside") or {})
    canvas["outside"] = {"count": outside.get("count", 0), "by_kind": {}}
    if encoded_size(trimmed) > limit:
        raise ContextLimitError("context_required_fields_exceed_limit")
    return _fill_text(trimmed, dynamic, limit)


def _fill_text(trimmed: dict, original: dict, limit: int) -> dict:
    """Restore summary and selected text while the encoded document stays within the limit."""

    summary = str(original.get("summary") or "")
    _prefix_that_fits(trimmed, "summary", summary, limit)
    canvas = dict(trimmed.get("canvas") or {})
    selected = [dict(item) for item in canvas.get("selected") or []]
    originals = [item for item in (original.get("canvas") or {}).get("selected") or [] if isinstance(item, dict)]
    canvas["selected"] = selected
    trimmed["canvas"] = canvas
    trimmed["selection"] = selected
    for index, item in enumerate(selected):
        text = str(originals[index].get("text") or "") if index < len(originals) else ""
        if text and _prefix_that_fits(item, "text", text, limit, root=trimmed) != text:
            raise ContextLimitError("selected_objects_exceed_context_limit")
    if encoded_size(trimmed) > limit:
        raise ContextLimitError("context_required_fields_exceed_limit")
    return trimmed


def _prefix_that_fits(owner: dict, key: str, text: str, limit: int, root: dict | None = None) -> str:
    document = root or owner
    owner[key] = ""
    if encoded_size(document) > limit or not text:
        owner.pop(key, None) if key == "text" else owner.__setitem__(key, "")
        if key == "text" and encoded_size(document) > limit:
            return ""
        return "" if key == "text" else ""
    owner[key] = text
    if encoded_size(document) <= limit:
        return text
    low, high, best = 0, len(text), ""
    while low <= high:
        mid = (low + high) // 2
        owner[key] = text[:mid]
        if encoded_size(document) <= limit:
            best = owner[key]
            low = mid + 1
        else:
            high = mid - 1
    if best:
        owner[key] = best
    elif key == "text":
        owner.pop(key, None)
    else:
        owner[key] = ""
    return best


def _shield(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"), sort_keys=True)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _tags(dynamic: dict) -> str:
    mapping = (
        ("canvas", dynamic.get("canvas")),
        ("upload", dynamic.get("upload")),
        ("selection", dynamic.get("selection")),
        ("recent_outputs", dynamic.get("recent_outputs")),
        ("asset_cards", dynamic.get("asset_cards")),
        ("skill", dynamic.get("skill")),
        ("summary", dynamic.get("summary")),
        *(((("confirmed_decisions", dynamic["confirmed_decisions"]),)) if "confirmed_decisions" in dynamic else ()),
        ("language", {"code": dynamic.get("language") or "", "reply_in_this_language": True}),
        ("invariants", dynamic.get("invariants")),
        ("form_answers", dynamic.get("form_answers")),
        ("plan_state", {"plan": dynamic.get("plan"), "tasks": dynamic.get("tasks")}),
        ("events", dynamic.get("events")),
        ("history", dynamic.get("messages")),
    )
    return "\n".join(f"<{name}>{_shield(value)}</{name}>" for name, value in mapping)
