"""Size names resolve through the composition preset table.

This module does not keep a second pixel table. Callers without a published
compose binding use the same composition defaults.
"""
from __future__ import annotations

from backend.core.composition.presets import PRESETS as COMPOSITION_PRESETS
from backend.core.composition.presets import PRESET_VERSION as COMPOSITION_PRESET_VERSION
from backend.core.composition.presets import REGIONS as COMPOSITION_REGIONS

PRESET_VERSION = COMPOSITION_PRESET_VERSION
PRESETS = {key: {"width": pair[0], "height": pair[1]} for key, pair in COMPOSITION_PRESETS.items()}


def default_preset_table() -> tuple[str, dict[str, dict[str, int]], set[str]]:
    return PRESET_VERSION, dict(PRESETS), set(COMPOSITION_REGIONS)
