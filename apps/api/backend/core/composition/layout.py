"""Measure and wrap copy. The returned string is the input, character for character."""

from __future__ import annotations

import re

from PIL import Image, ImageDraw, ImageFont

from .errors import CompositionError
from .fonts import FontRegistry
from .presets import PRESET_VERSION, canvas_size, region_pixels

_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def layout_text(params: dict, registry: FontRegistry, *, min_font_size: int, max_font_size: int,
                max_copy_chars: int, min_edge: int, max_edge: int, line_height: float,
                max_lines: int, presets=None, regions=None) -> dict:
    copy = params.get("copy")
    if not isinstance(copy, str) or not copy or len(copy) > max_copy_chars:
        raise CompositionError("copy_limit")
    if copy != params["copy"]:
        raise CompositionError("copy_limit")
    color = params.get("color")
    if not isinstance(color, str) or _COLOR.fullmatch(color) is None:
        raise CompositionError("invalid_color")
    alignment = params.get("alignment")
    if alignment not in {"left", "center", "right"}:
        raise CompositionError("param_invalid")
    size = params.get("size")
    if type(size) is not int or not (min_font_size <= size <= max_font_size):
        raise CompositionError("canvas_limit")
    preset, width, height = canvas_size(params.get("preset"), params.get("width"), params.get("height"),
                                         min_edge=min_edge, max_edge=max_edge, presets=presets)
    face = registry.get(params.get("font_id"))
    face.require_coverage(copy)
    font = face.load(size)
    region = region_pixels(params.get("region"), width, height, regions)
    lines = _wrap(copy, font, region["width"], size)
    step = max(1, round(size * line_height))
    if not lines or len(lines) > max_lines or len(lines) * step > region["height"]:
        raise CompositionError("overflow")
    placed = []
    for index, text in enumerate(lines):
        text_width = int(round(font.getlength(text)))
        if alignment == "left":
            x = region["x"]
        elif alignment == "right":
            x = region["x"] + region["width"] - text_width
        else:
            x = region["x"] + (region["width"] - text_width) // 2
        line_y = int(region["y"] + index * step)
        placed.append({"text": text, "x": int(x), "y": line_y, "width": text_width, "height": step})
        _require_ink_inside(font, text, int(x), line_y, text_width, step, region)
    flat = "".join(item["text"] for item in placed)
    if flat != copy.replace("\n", ""):
        raise CompositionError("overflow")
    return {"copy": copy, "font_id": face.id, "font_version": face.version, "font_sha256": face.sha256,
            "size": size, "color": color, "alignment": alignment, "region": params["region"],
            "preset": preset, "canvas": {"width": width, "height": height, "preset_version": PRESET_VERSION},
            "box": region, "lines": placed}


def _wrap(copy: str, font: ImageFont.FreeTypeFont, limit: int, size: int) -> list[str]:
    """Break only at newlines or when the next character does not fit. Never drop a character."""
    lines: list[str] = []
    current = ""
    for char in copy:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        if font.getlength(char) > limit:
            raise CompositionError("overflow")
        trial = current + char
        if current and font.getlength(trial) > limit:
            lines.append(current)
            current = char
        else:
            current = trial
    lines.append(current)
    if size < 1:
        raise CompositionError("overflow")
    return lines


def _require_ink_inside(font: ImageFont.FreeTypeFont, text: str, x: int, y: int, width: int, height: int,
                        region: dict) -> None:
    """Pillow anchor lt matches the line box top-left stored for the browser."""
    if text == "":
        if y < region["y"] or y + height > region["y"] + region["height"]:
            raise CompositionError("overflow")
        return
    pad = max(8, font.size)
    image = Image.new("L", (width + pad * 2, height + pad * 2), 0)
    ImageDraw.Draw(image).text((pad, pad), text, font=font, fill=255, anchor="lt")
    pixels = image.load()
    min_x = min_y = 10**9
    max_x = max_y = -1
    for row in range(image.height):
        for col in range(image.width):
            if pixels[col, row]:
                min_x, min_y = min(min_x, col), min(min_y, row)
                max_x, max_y = max(max_x, col), max(max_y, row)
    if max_x < 0:
        raise CompositionError("missing_glyph")
    ink_left = x + (min_x - pad)
    ink_top = y + (min_y - pad)
    ink_right = x + (max_x - pad) + 1
    ink_bottom = y + (max_y - pad) + 1
    if ink_left < region["x"] or ink_top < region["y"] or ink_right > region["x"] + region["width"] or ink_bottom > region["y"] + region["height"]:
        raise CompositionError("overflow")
