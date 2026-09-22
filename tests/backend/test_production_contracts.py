from pathlib import Path

from backend.tools.public_types import render


def test_production_types_match_pydantic_schema():
    root = Path(__file__).resolve().parents[2]
    assert (root / "apps/web/shared/production-contracts.ts").read_text() == render()
