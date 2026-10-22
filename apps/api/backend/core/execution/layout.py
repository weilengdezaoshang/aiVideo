"""Deterministic placement; only numeric output from code reaches the canvas."""
import copy
import asyncio
import math
from .contracts import WorkerSettings


def overlaps(a, b):
    return a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"] and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]


def choose(data: dict, placement: dict, settings: WorkerSettings):
    objects = data.get("objects", {})
    anchor = objects.get(placement.get("anchor_id"))
    mode = placement.get("mode", "right")
    width, height = settings.default_width, settings.default_height
    rect = {"x": 0.0, "y": 0.0, "width": width, "height": height}
    if mode == "region":
        rect.update(placement["region"])
    elif anchor:
        rect.update(x=anchor["x"], y=anchor["y"])
        if mode == "right":
            rect["x"] += anchor["width"] + settings.layout_gap
        elif mode == "below":
            rect["y"] += anchor["height"] + settings.layout_gap
        elif mode == "replace":
            rect.update(width=anchor["width"], height=anchor["height"])
    elif mode == "below":
        rect["y"] = max((o.get("y", 0) + o.get("height", 0) for o in objects.values()), default=0) + settings.layout_gap
    else:
        rect["x"] = max((o.get("x", 0) + o.get("width", 0) for o in objects.values()), default=0) + settings.layout_gap
    if not all(type(v) in (int, float) and math.isfinite(v) for v in rect.values()) or rect["width"] <= 0 or rect["height"] <= 0:
        raise ValueError("invalid code-owned region")
    cas_ok = anchor is not None and int(anchor.get("assetVersion", 0)) == placement.get("anchor_version") and not anchor.get("busyRunId")
    ignored = placement.get("anchor_id") if mode == "replace" and cas_ok else None
    conflict = bool(placement.get("anchor_id") and not cas_ok)
    occupied = [o for key, o in objects.items() if key != ignored and all(k in o for k in rect)]
    original = dict(rect)
    # Increasing Manhattan-distance rings, deterministic tie-breaks. Restricted by writable region.
    inspected = 0
    for distance in range(settings.layout_steps):
        for dy in range(-distance, distance + 1):
            dx = distance - abs(dy)
            for xsign in ((1,) if dx == 0 else (-1, 1)):
                if inspected >= settings.layout_steps:
                    return None, True, None
                inspected += 1
                candidate = {**rect, "x": original["x"] + xsign * dx * (width + settings.layout_gap),
                             "y": original["y"] + dy * (height + settings.layout_gap)}
                scope = placement.get("writable_region")
                inside = not scope or (candidate["x"] >= scope["x"] and candidate["y"] >= scope["y"] and
                    candidate["x"] + candidate["width"] <= scope["x"] + scope["width"] and
                    candidate["y"] + candidate["height"] <= scope["y"] + scope["height"])
                if inside and not any(overlaps(candidate, o) for o in occupied):
                    return candidate, conflict or distance > 0, ignored if not conflict and distance == 0 else None
    return None, True, None


async def place(doc, task_id, asset, placement, settings):
    data = copy.deepcopy(doc.data)
    rect, conflict, replacement = await asyncio.to_thread(choose, data, placement, settings)
    if rect is None:
        return None, conflict, {}
    node_id = replacement or "generated-" + str(task_id)
    old = data.setdefault("objects", {}).get(node_id, {})
    data["objects"][node_id] = {**old, **rect, "id": node_id, "kind": asset.kind,
        "assetId": str(asset.id), "ext": asset.ext, "src": f"/api/assets/{asset.id}/content",
        "assetVersion": int(old.get("assetVersion", 0)) + 1}
    if node_id not in data.setdefault("order", []):
        data["order"].append(node_id)
    doc.data, doc.revision = data, doc.revision + 1
    return node_id, conflict, rect
