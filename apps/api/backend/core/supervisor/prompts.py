"""Load the pinned supervisor prompt. Import does not touch a database, model, or secret."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_VERSIONS = {"supervisor-v1": "supervisor-v1.txt", "supervisor-v2": "supervisor-v2.txt"}


@dataclass(frozen=True, slots=True)
class SupervisorPrompt:
    version: str
    text: str
    content_hash: str


def load_prompt(version: str = "supervisor-v1", expected_hash: str | None = None) -> SupervisorPrompt:
    filename = _VERSIONS.get(version)
    if filename is None:
        raise ValueError(f"unsupported supervisor prompt version: {version!r}")
    path = (_PROMPTS_DIR / filename).resolve()
    if path.parent != _PROMPTS_DIR.resolve():
        raise ValueError(f"supervisor prompt path escaped the template directory: {version!r}")
    text = path.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if expected_hash is not None and expected_hash != content_hash:
        raise ValueError(
            f"supervisor prompt hash mismatch for {version}: expected {expected_hash}, got {content_hash}"
        )
    return SupervisorPrompt(version=version, text=text, content_hash=content_hash)
