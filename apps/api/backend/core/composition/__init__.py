"""Deterministic poster layout. Models supply copy and a region, never canvas coordinates."""

from .compose import compose_layers
from .errors import CompositionError
from .fonts import FontRegistry, bundled_registry
from .layout import layout_text
from .presets import PRESET_VERSION, PRESETS

__all__ = ["PRESET_VERSION", "PRESETS", "CompositionError", "FontRegistry", "bundled_registry",
           "compose_layers", "layout_text"]
