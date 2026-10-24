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


def canvas_insert_allowed(*, cancel_requested: bool, run_cancelled: bool, task_cancelled: bool, permitted: bool) -> bool:
    """A finished local result still stays history-only when the run or task was cancelled."""
    return bool(permitted) and not (cancel_requested or run_cancelled or task_cancelled)


def place_layered(data, task_id, asset, placement, settings, manifest):
    """Place one native-aspect group; a replay preserves subsequent user edits."""
    group = "generated-" + str(task_id)
    for key, value in data.get("objects", {}).items():
        if value.get("layerRole") == "group" and value.get("compositionTaskId") == str(task_id):
            return key, False, {name: value[name] for name in ("x", "y", "width", "height")}
    scratch = copy.deepcopy(data)
    scratch["objects"] = {key: value for key, value in data.get("objects", {}).items()
                          if key != group and not key.startswith(group + ":")}
    rect, conflict, replacement = choose(scratch, placement, settings)
    if rect is None:
        return None, conflict, {}
    canvas = manifest["canvas"]
    scale = min(rect["width"] / canvas["width"], rect["height"] / canvas["height"])
    native_width, native_height = canvas["width"] * scale, canvas["height"] * scale
    rect = {**rect, "x": rect["x"] + (rect["width"] - native_width) / 2,
            "y": rect["y"] + (rect["height"] - native_height) / 2,
            "width": native_width, "height": native_height}
    scale_x = scale_y = scale
    if replacement:
        group = replacement
    objects = data.setdefault("objects", {})
    order = data.setdefault("order", [])
    removed = {key for key, value in objects.items()
               if key != group and value.get("groupId") == group}
    for key in removed:
        objects.pop(key)
    if removed:
        data["order"] = order = [key for key in order if key not in removed]

    def put(suffix, fields):
        node_id = group if suffix == "" else f"{group}:{suffix}"
        previous = objects.get(node_id, {})
        transform = fields.pop("transform")
        objects[node_id] = {"id": node_id, "groupId": group,
            "x": rect["x"] + transform["x"] * scale_x, "y": rect["y"] + transform["y"] * scale_y,
            "width": max(1, transform["width"] * scale_x), "height": max(1, transform["height"] * scale_y),
            "assetVersion": int(previous.get("assetVersion", 0)) + 1, **fields}
        if node_id not in order:
            order.append(node_id)
        return node_id

    put("", {"kind": "board", "name": "海报", "layerRole": "group", "fit": "contain",
             "compositionTaskId": str(task_id),
             "layers": manifest, "backgroundColor": "#ffffff", "transform": {"x": 0, "y": 0, "width": canvas["width"], "height": canvas["height"]},
             "assetId": str(asset.id), "ext": asset.ext, "src": None})
    for layer in manifest["layers"]:
        role = layer["role"]
        if role == "background" and layer["kind"] == "color":
            put("background", {"kind": "board", "layerRole": "background", "fit": "contain",
                               "backgroundColor": layer["color"], "transform": dict(layer["transform"])})
        elif role == "background":
            put("background", {"kind": "image", "layerRole": "background", "fit": "contain",
                               "assetId": layer.get("asset_id"), "ext": "png", "transform": dict(layer["transform"])})
        elif role == "subject":
            put("subject", {"kind": "image", "layerRole": "subject", "fit": "contain",
                            "assetId": layer.get("asset_id"), "ext": "png", "transform": dict(layer["transform"])})
        elif role == "text":
            box = layer["transform"]
            line_boxes = [{
                "text": line["text"],
                "x": (line["x"] - box["x"]) * scale_x,
                "y": (line["y"] - box["y"]) * scale_y,
                "width": max(1, line["width"] * scale_x) if line["text"] else 0,
                "height": max(1, line["height"] * scale_y),
            } for line in layer.get("lines") or []]
            put(layer["id"], {"kind": "text", "layerRole": "text", "text": layer["text"],
                              "fontSize": max(1, round(layer["font_size"] * scale_y)), "fontFamily": layer["font_id"],
                              "fill": layer["color"], "textAlign": layer["align"], "region": layer["region"],
                              "lineBoxes": line_boxes, "transform": dict(box)})
    return group, conflict, rect


def insert_text_leaf(data, task_id, placement, settings, layout):
    """One editable text object. Line boxes stay in the object's local space."""
    rect, conflict, _replacement = choose(data, placement, settings)
    if rect is None:
        return None, conflict, {}
    canvas = layout["canvas"]
    box = layout["box"]
    scale_x = rect["width"] / canvas["width"]
    scale_y = rect["height"] / canvas["height"]
    node_id = "generated-" + str(task_id)
    objects = data.setdefault("objects", {})
    order = data.setdefault("order", [])
    previous = objects.get(node_id, {})
    objects[node_id] = {**previous, "id": node_id, "kind": "text", "layerRole": "text",
        "x": rect["x"] + box["x"] * scale_x, "y": rect["y"] + box["y"] * scale_y,
        "width": max(1, box["width"] * scale_x), "height": max(1, box["height"] * scale_y),
        "text": layout["copy"], "fontSize": max(1, round(layout["size"] * scale_y)),
        "fontFamily": layout["font_id"], "fill": layout["color"], "textAlign": layout["alignment"],
        "region": layout["region"], "lineBoxes": [{
            "text": line["text"], "x": (line["x"] - box["x"]) * scale_x, "y": (line["y"] - box["y"]) * scale_y,
            "width": max(1, line["width"] * scale_x) if line["text"] else 0,
            "height": max(1, line["height"] * scale_y),
        } for line in layout["lines"]],
        "assetVersion": int(previous.get("assetVersion", 0)) + 1}
    if node_id not in order:
        order.append(node_id)
    return node_id, conflict, rect


async def place_text_leaf(doc, task_id, placement, settings, layout):
    data = copy.deepcopy(doc.data)
    node, conflict, rect = await asyncio.to_thread(insert_text_leaf, data, task_id, placement, settings, layout)
    if node is None:
        return None, conflict, {}
    doc.data, doc.revision = data, doc.revision + 1
    return node, conflict, rect


async def place(doc, task_id, asset, placement, settings, manifest=None):
    data = copy.deepcopy(doc.data)
    placeholder = "generated-" + str(task_id)
    if data.get("objects", {}).get(placeholder, {}).get("corePlaceholder"):
        data["objects"].pop(placeholder)
    if manifest:
        node, conflict, rect = await asyncio.to_thread(place_layered, data, task_id, asset, placement, settings, manifest)
        if node is None:
            return None, conflict, {}
        doc.data, doc.revision = data, doc.revision + 1
        return node, conflict, rect
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
