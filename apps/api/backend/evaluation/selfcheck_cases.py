"""Frozen plan self-check rows. Labels stay in the dataset and never enter a prompt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DriftKind = Literal["count", "modality", "ratio", "object", "omission", "extra_paid", "none"]
CaughtBy = Literal["invariants", "semantic", "neither"]

FIXTURE_PATH = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "plan-selfcheck-cases.jsonl"
# SHA-256 of the curated JSONL bytes. build_cases() loads this file; it does not expand templates.
FROZEN_DATASET_SHA256 = "a195b375fadce54537e4e6abaac0b26ea726e5b94e4524af0fe71f96856dedbf"
EXPECTED_COUNTS = {"total": 80, "normal": 50, "drift": 30}


class SelfCheckCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    kind: DriftKind
    caught_by: CaughtBy
    user_message: str
    invariants: dict = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list)
    plan: dict
    label_pass: bool
    notes: str = ""

    @property
    def drift(self) -> bool:
        return self.label_pass is False


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_CANONICAL_CONTENT_HASH: str | None = None


def _content_hash(cases: list[SelfCheckCase]) -> str:
    """Canonical identity of these case objects, including labels. Not the file SHA."""

    payload = [case.model_dump(mode="json") for case in cases]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def dataset_hash(cases: list[SelfCheckCase] | None = None) -> str:
    """Content identity. None means the frozen 80-case fixture, cached after the first load.

    FROZEN_DATASET_SHA256 remains the raw file integrity check and is not this value.
    """

    global _CANONICAL_CONTENT_HASH
    if cases is None:
        if _CANONICAL_CONTENT_HASH is None:
            _CANONICAL_CONTENT_HASH = _content_hash(build_cases())
        return _CANONICAL_CONTENT_HASH
    return _content_hash(cases)


def load_cases(path: Path | None = None, *, verify_frozen: bool = True) -> list[SelfCheckCase]:
    source = path or FIXTURE_PATH
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if verify_frozen and digest != FROZEN_DATASET_SHA256:
        raise ValueError(
            f"plan self-check fixture hash mismatch: expected {FROZEN_DATASET_SHA256}, got {digest}"
        )
    cases: list[SelfCheckCase] = []
    seen: set[str] = set()
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        if " #" in line or line.strip().startswith("#"):
            raise ValueError(f"line {line_no}: template marker is not a case")
        try:
            case = SelfCheckCase.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(f"line {line_no}: {exc}") from None
        if case.case_id in seen:
            raise ValueError(f"duplicate case_id {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    if verify_frozen:
        normal = sum(1 for case in cases if case.label_pass)
        drift = sum(1 for case in cases if not case.label_pass)
        if (len(cases), normal, drift) != (80, 50, 30):
            raise ValueError(f"frozen split must be 50 normal + 30 drift, got {normal}/{drift}/{len(cases)}")
        if any(case.kind == "none" and not case.label_pass for case in cases):
            raise ValueError("kind none must be a normal plan")
        if any(case.kind != "none" and case.label_pass for case in cases):
            raise ValueError("drift kind must be label_pass false")
    return cases


def build_cases() -> list[SelfCheckCase]:
    """Compatibility loader for the curated fixture. Does not synthesize rows."""

    return load_cases(FIXTURE_PATH)


def write_fixture(path: Path = FIXTURE_PATH) -> str:
    raise RuntimeError(
        f"refusing to regenerate {path}; tests/fixtures/plan-selfcheck-cases.jsonl is the frozen source"
    )
