"""Flatten a preview and return a real layer manifest. The preview is not a text layer."""

from __future__ import annotations

import hashlib
import io
import re

from PIL import Image, ImageDraw, UnidentifiedImageError

from .errors import CompositionError
from .presets import PRESET_VERSION, canvas_size

_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")

MANIFEST_SCHEMA = "composition-layers-v1"


def _require_cutout_mask(subject: Image.Image, proof: dict | None, mask_png: bytes | None) -> None:
    """A fully opaque bitmap is a subject only with a same-size cutout mask that has ink."""
    if not proof or proof.get("source") != "cutout" or not mask_png:
        raise CompositionError("need_subject")
    digest = proof.get("mask_sha256")
    if not isinstance(digest, str) or hashlib.sha256(mask_png).hexdigest() != digest:
        raise CompositionError("need_subject")
    mask = _open_rgba(mask_png, max_pixels=subject.width * subject.height)
    if mask.size != subject.size:
        raise CompositionError("need_subject")
    if mask.convert("L").getextrema()[1] == 0:
        raise CompositionError("need_subject")


def _contain(src_w: int, src_h: int, frame: dict) -> dict:
    """Fit the whole source inside the frame. Contain may scale up; it never crops."""
    scale = min(frame["width"] / src_w, frame["height"] / src_h)
    if scale <= 0:
        raise CompositionError("canvas_limit")
    width = max(1, int(round(src_w * scale)))
    height = max(1, int(round(src_h * scale)))
    width = min(width, frame["width"])
    height = min(height, frame["height"])
    return {"x": frame["x"] + (frame["width"] - width) // 2,
            "y": frame["y"] + (frame["height"] - height) // 2,
            "width": width, "height": height, "scale": scale}


def _open_rgba(raw: bytes, *, max_pixels: int) -> Image.Image:
    try:
        with Image.open(io.BytesIO(raw)) as incoming:
            if incoming.width < 1 or incoming.height < 1 or incoming.width * incoming.height > max_pixels:
                raise CompositionError("canvas_limit")
            return incoming.convert("RGBA")
    except CompositionError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise CompositionError("param_invalid") from exc


def _alpha_kind(image: Image.Image) -> str:
    lo, hi = image.getchannel("A").getextrema()
    if hi == 0:
        return "empty"
    if lo == 255:
        return "opaque"
    return "cutout"


def _color(value: object) -> str:
    if not isinstance(value, str) or _COLOR.fullmatch(value) is None:
        raise CompositionError("invalid_color")
    return value


def compose_layers(*, width: int | None, height: int | None, preset: str | None, background: dict,
                   texts: list[dict], registry, min_edge: int, max_edge: int,
                   subject_margin_ratio: float, max_pixels: int, presets=None,
                   subject_png: bytes | None = None, subject_asset_id: str | None = None,
                   subject_sha256: str | None = None, subject_proof: dict | None = None,
                   mask_png: bytes | None = None,
                   background_asset_id: str | None = None, background_sha256: str | None = None,
                   background_png: bytes | None = None) -> tuple[bytes, dict]:
    preset_id, canvas_w, canvas_h = canvas_size(preset, width, height, min_edge=min_edge, max_edge=max_edge,
                                                presets=presets)
    if canvas_w * canvas_h > max_pixels:
        raise CompositionError("canvas_limit")
    subject_image = None
    if subject_png is not None:
        if not subject_sha256 or hashlib.sha256(subject_png).hexdigest() != subject_sha256:
            raise CompositionError("param_invalid")
        subject_image = _open_rgba(subject_png, max_pixels=max_pixels)
        kind = _alpha_kind(subject_image)
        if kind == "empty":
            raise CompositionError("need_subject")
        if kind == "opaque":
            _require_cutout_mask(subject_image, subject_proof, mask_png)
    margin_x = int(canvas_w * subject_margin_ratio)
    margin_y = int(canvas_h * subject_margin_ratio)
    frame = {"x": margin_x, "y": margin_y, "width": canvas_w - 2 * margin_x, "height": canvas_h - 2 * margin_y}
    if frame["width"] < 1 or frame["height"] < 1:
        raise CompositionError("canvas_limit")
    bg_image = None
    color = None
    if background.get("kind") == "color":
        color = _color(background.get("color"))
    elif background.get("kind") == "image":
        if not background_png or not background_sha256:
            raise CompositionError("param_invalid")
        if hashlib.sha256(background_png).hexdigest() != background_sha256:
            raise CompositionError("param_invalid")
        if "pad" in background:
            color = _color(background.get("pad"))
        bg_image = _open_rgba(background_png, max_pixels=max_pixels)
    else:
        raise CompositionError("param_invalid")
    canvas = Image.new("RGBA", (canvas_w, canvas_h), color or (0, 0, 0, 0))
    bg_box = {"x": 0, "y": 0, "width": canvas_w, "height": canvas_h}
    if bg_image is not None:
        placed = _contain(*bg_image.size, bg_box)
        resized = bg_image.resize((placed["width"], placed["height"]), Image.Resampling.NEAREST)
        canvas.alpha_composite(resized, (placed["x"], placed["y"]))
        bg_box = {key: placed[key] for key in ("x", "y", "width", "height")}
    layers = [{
        "id": "background", "role": "background",
        "kind": "image" if bg_image is not None else "color", "z": 0, "opacity": 1,
        "transform": bg_box, "color": color, "fit": "contain",
        "asset_id": background_asset_id, "source_sha256": background_sha256, "source_version_id": None,
    }]
    if subject_image is not None:
        placed_subject = _contain(*subject_image.size, frame)
        resized_subject = subject_image.resize((placed_subject["width"], placed_subject["height"]), Image.Resampling.NEAREST)
        canvas.alpha_composite(resized_subject, (placed_subject["x"], placed_subject["y"]))
        layers.append({
            "id": "subject", "role": "subject", "kind": "image", "z": 1, "opacity": 1, "fit": "contain",
            "transform": {key: placed_subject[key] for key in ("x", "y", "width", "height")},
            "asset_id": subject_asset_id, "source_sha256": subject_sha256, "source_version_id": None,
        })
    draw = ImageDraw.Draw(canvas)
    text_ids = []
    for index, item in enumerate(texts):
        if item.get("canvas", {}).get("width") not in {None, canvas_w} or item.get("canvas", {}).get("height") not in {None, canvas_h}:
            raise CompositionError("canvas_limit")
        if item.get("canvas", {}).get("width") != canvas_w or item.get("canvas", {}).get("height") != canvas_h:
            raise CompositionError("canvas_limit")
        copy = item["copy"]
        if "".join(line["text"] for line in item["lines"]) != copy.replace("\n", ""):
            raise CompositionError("param_invalid")
        face = registry.get(item["font_id"])
        if face.sha256 != item["font_sha256"] or face.version != item["font_version"]:
            raise CompositionError("font_hash_mismatch")
        font = face.load(item["size"])
        for line in item["lines"]:
            if line["text"]:
                draw.text((line["x"], line["y"]), line["text"], fill=item["color"], font=font, anchor="lt")
        box = item["box"]
        layers.append({
            "id": f"text-{index}", "role": "text", "kind": "text", "z": 2 + index, "opacity": 1,
            "text": copy, "font_id": item["font_id"], "font_version": item["font_version"],
            "font_sha256": item["font_sha256"], "font_size": item["size"], "color": item["color"],
            "align": item["alignment"], "region": item["region"],
            "asset_id": item.get("asset_id"), "source_sha256": item.get("sha256"),
            "source_version_id": item.get("core_asset_version_id"),
            "transform": {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]},
            "lines": item["lines"],
        })
        if item.get("asset_id"):
            text_ids.append(item["asset_id"])
    manifest = {"schema": MANIFEST_SCHEMA, "preset_version": PRESET_VERSION, "preset": preset_id,
                "canvas": {"width": canvas_w, "height": canvas_h}, "fit": "contain",
                "lineage": {"background_asset_id": background_asset_id, "background_sha256": background_sha256,
                            "subject_asset_id": subject_asset_id, "subject_sha256": subject_sha256,
                            "text_asset_ids": text_ids},
                "layers": layers}
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue(), manifest
