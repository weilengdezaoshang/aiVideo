"""Pinned self-check template. Dynamic values travel as user-role JSON, never spliced into system text."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .contracts import TEMPLATE_VERSION

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_VERSIONS = {TEMPLATE_VERSION: "selfcheck-v1.txt"}
_SLOTS = ("user_message", "invariants", "refs", "plan_summary")


@dataclass(frozen=True, slots=True)
class SelfCheckPrompt:
    version: str
    text: str
    content_hash: str


def load_prompt(version: str = TEMPLATE_VERSION, expected_hash: str | None = None) -> SelfCheckPrompt:
    filename = _VERSIONS.get(version)
    if filename is None:
        raise ValueError(f"unsupported selfcheck prompt version: {version!r}")
    path = (_PROMPTS_DIR / filename).resolve()
    if path.parent != _PROMPTS_DIR.resolve():
        raise ValueError(f"selfcheck prompt path escaped the template directory: {version!r}")
    text = path.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if expected_hash is not None and expected_hash != content_hash:
        raise ValueError(
            f"selfcheck prompt hash mismatch for {version}: expected {expected_hash}, got {content_hash}"
        )
    return SelfCheckPrompt(version=version, text=text, content_hash=content_hash)


def system_instructions(prompt: SelfCheckPrompt) -> str:
    """Fixed checker instructions. Slot names point at user JSON; user bytes never enter this string."""

    text = prompt.text
    for name in _SLOTS:
        text = text.replace("{" + name + "}", "$data." + name)
    return text


def user_payload(*, user_message: str, invariants, refs, plan_summary) -> str:
    return json.dumps(
        {
            "user_message": user_message,
            "invariants": invariants,
            "refs": refs,
            "plan_summary": plan_summary,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )


def render_messages(
    prompt: SelfCheckPrompt,
    *,
    user_message: str,
    invariants,
    refs,
    plan_summary,
) -> list[dict]:
    return [
        {"role": "system", "content": system_instructions(prompt)},
        {"role": "user", "content": user_payload(
            user_message=user_message, invariants=invariants, refs=refs, plan_summary=plan_summary,
        )},
    ]
