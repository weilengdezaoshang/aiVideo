"""Non-secret composition snapshot stored on the published tool binding."""

from __future__ import annotations

from backend.core.tools.contracts import ToolSettings

from .fonts import load_registry
from .presets import PRESET_VERSION, PRESETS, REGIONS


def composition_snapshot(settings: ToolSettings | None = None) -> dict:
    """Numbers, preset fractions and font id/version/sha. No filesystem path."""
    settings = settings or ToolSettings()
    registry = load_registry(settings.font_registry_path, settings.max_font_bytes)
    return {
        "preset_version": PRESET_VERSION,
        "presets": {key: {"width": value[0], "height": value[1]} for key, value in PRESETS.items()},
        "regions": {key: {"x": value[0], "y": value[1], "width": value[2], "height": value[3]}
                    for key, value in REGIONS.items()},
        "fonts": [{"id": face.id, "version": face.version, "sha256": face.sha256, "face_index": face.face_index}
                  for face in (registry.get(font_id) for font_id in registry.ids())],
        "min_font_size": settings.min_font_size,
        "max_font_size": settings.max_font_size,
        "min_canvas_edge": settings.min_canvas_edge,
        "max_canvas_edge": settings.max_canvas_edge,
        "text_line_height": settings.text_line_height,
        "max_text_lines": settings.max_text_lines,
        "max_copy_chars": settings.max_copy_chars,
        "max_text_layers": settings.max_text_layers,
        "subject_margin_ratio": settings.subject_margin_ratio,
        "text_overflow": settings.text_overflow,
        "max_image_pixels": settings.max_image_pixels,
        "max_font_bytes": settings.max_font_bytes,
    }


def snapshot_presets(snapshot: dict) -> dict[str, tuple[int, int]]:
    return {key: (int(value["width"]), int(value["height"])) for key, value in snapshot["presets"].items()}


def snapshot_regions(snapshot: dict) -> dict[str, tuple[float, float, float, float]]:
    return {key: (float(value["x"]), float(value["y"]), float(value["width"]), float(value["height"]))
            for key, value in snapshot["regions"].items()}
