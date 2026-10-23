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
_CARD_FIELDS = ("id", "asset_id", "node_id", "kind", "type", "name", "label", "text", "media_type")
_RULE_SPLIT = "【当前信息】"
_BOUNDARY = "【当前阶段边界】"
_SOFT_LISTS = ("messages", "tasks", "asset_cards", "recent_outputs", "upload")


class ContextLimitError(ValueError):
    """Required context cannot fit the configured character budget."""


def encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))


def build_context(context: dict, settings: Settings | None = None) -> dict:
    """Tier snapshot objects and return a bounded view. Coordinates never leave this function."""

    settings = settings or Settings()
    request = context.get("request") or {}
    readable = _readable(context)
    selected_ids = _selected_ids(context)
    viewport = context.get("viewport")
    objects = _objects(context.get("snapshot") or {})
    visible = [item for item in objects if _asset_of(item) in readable]

    selected: list[dict] = []
    inside: list[dict] = []
    outside_kinds: dict[str, int] = {}
    for item in visible:
        identity = _ids(item)
        if identity & selected_ids:
            selected.append(_full(item))
        elif viewport is not None and _intersects(item, viewport):
            inside.append(_coarse(item))
        else:
            kind = str(item.get("kind") or item.get("type") or "object")
            outside_kinds[kind] = outside_kinds.get(kind, 0) + 1

    selected, inside = _cap_objects(selected, inside, settings.context_max_objects)
    cards = _filter_cards(context.get("asset_cards") or [], readable)
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
        "invariants": context.get("invariants") or {},
        "plan": context.get("plan"),
        "tasks": list(context.get("tasks") or []),
        "events": _events(context),
        "messages": list(context.get("messages") or [])[: settings.context_messages],
    }
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


def _readable(context: dict) -> set[str]:
    scope = context.get("scope") or (context.get("request") or {}).get("scope") or {}
    return {str(item) for item in scope.get("readable_assets") or []}


def _selected_ids(context: dict) -> set[str]:
    raw = context.get("selection") or []
    if isinstance(raw, dict):
        raw = raw.get("ids") or raw.get("asset_ids") or []
    request = context.get("request") or {}
    return {str(item) for item in list(raw) + list(request.get("selected_assets") or [])}


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


def _coarse(item: dict) -> dict:
    return _with_identity(item, _public(item, ("id", "asset_id", "node_id", "kind", "type")))


def _cap_objects(selected: list[dict], inside: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    selected = selected[:limit]
    room = max(limit - len(selected), 0)
    return selected, inside[:room]


def _filter_cards(cards: list, readable: set[str]) -> list:
    """Keep allowlisted fields of an authorized asset. Anything else is dropped."""

    kept = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        asset_id = _asset_of(card)
        if not asset_id or asset_id not in readable:
            continue
        view = _full(card)
        if view.get("asset_id") in readable:
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
    required = {"invariants": trimmed.get("invariants") or {}, "events": required_events}
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
    selected = [_scalar_view(item) for item in canvas.get("selected") or [] if isinstance(item, dict)]
    canvas["viewport"] = []
    canvas["selected"] = selected
    trimmed["canvas"] = canvas
    trimmed["selection"] = selected
    if encoded_size(trimmed) <= limit:
        return _fill_text(trimmed, dynamic, limit)
    outside = dict(canvas.get("outside") or {})
    canvas["outside"] = {"count": outside.get("count", 0), "by_kind": {}}
    while selected and encoded_size(trimmed) > limit:
        selected.pop()
        canvas["selected"] = list(selected)
        trimmed["selection"] = list(selected)
    if encoded_size(trimmed) > limit:
        raise ContextLimitError("context_required_fields_exceed_limit")
    return _fill_text(trimmed, dynamic, limit)


def _scalar_view(item: dict) -> dict:
    return {
        key: value for key, value in item.items()
        if key != "text" and not isinstance(value, (dict, list))
    }


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
        if text:
            _prefix_that_fits(item, "text", text, limit, root=trimmed)
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
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
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
        ("invariants", dynamic.get("invariants")),
        ("plan_state", {"plan": dynamic.get("plan"), "tasks": dynamic.get("tasks")}),
        ("events", dynamic.get("events")),
        ("history", dynamic.get("messages")),
    )
    return "\n".join(f"<{name}>{_shield(value)}</{name}>" for name, value in mapping)
