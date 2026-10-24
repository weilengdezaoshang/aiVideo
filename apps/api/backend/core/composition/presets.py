"""Fixed canvas sizes. Names are slot ids, not a claim about any platform's rules."""

from __future__ import annotations

PRESET_VERSION = "canvas-presets-2026-10-24"

PRESETS: dict[str, tuple[int, int]] = {
    "feed_square": (1080, 1080),
    "story_portrait": (1080, 1920),
    "banner_landscape": (1920, 1080),
}

# Fractions of the canvas. Code owns every pixel derived from these.
REGIONS: dict[str, tuple[float, float, float, float]] = {
    "title": (0.08, 0.06, 0.84, 0.22),
    "body": (0.08, 0.32, 0.84, 0.40),
    "footer": (0.08, 0.76, 0.84, 0.18),
}


def canvas_size(preset: str | None, width: int | None, height: int | None,
                *, min_edge: int, max_edge: int, presets: dict[str, tuple[int, int]] | None = None
                ) -> tuple[str | None, int, int]:
    from .errors import CompositionError
    table = presets or PRESETS
    if preset is not None:
        if width is not None or height is not None:
            raise CompositionError("invalid_preset")
        pair = table.get(preset)
        if pair is None:
            raise CompositionError("invalid_preset")
        return preset, pair[0], pair[1]
    if width is None or height is None:
        raise CompositionError("canvas_limit")
    if not (min_edge <= width <= max_edge and min_edge <= height <= max_edge):
        raise CompositionError("canvas_limit")
    return None, width, height


def region_pixels(name: str, width: int, height: int,
                  regions: dict[str, tuple[float, float, float, float]] | None = None) -> dict[str, int]:
    from .errors import CompositionError
    table = regions or REGIONS
    if name not in table:
        raise CompositionError("invalid_preset")
    x, y, w, h = table[name]
    box = {"x": round(x * width), "y": round(y * height), "width": max(1, round(w * width)),
           "height": max(1, round(h * height))}
    box["width"] = min(box["width"], width - box["x"])
    box["height"] = min(box["height"], height - box["y"])
    return box
