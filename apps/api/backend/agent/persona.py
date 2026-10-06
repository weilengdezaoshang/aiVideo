"""Persona registry: identity and voice are configuration, never authority.

The persona text sits between the immutable rules and the role prompt. It is frozen on
the Run at admission, so editing the deployment file cannot change a running Run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from backend.agent.contracts import PersonaProfile

DEFAULT_PERSONA = PersonaProfile(
    id="canvas-assistant", version=1, displayName="画布助手",
    identity="你是画布助手，帮助用户查看、核对和理解当前画布。",
    voice=["使用简体中文", "简洁，先结论后依据"],
    principles=["没有证据不下结论", "信息不足或工具失败时如实说明", "不夸大能力，不声称执行了未执行的操作"],
    expression={"uncertain": "目前无法确认，需要进一步核实。", "refuse": "这超出了我当前的能力，我只能读取并解释画布。",
                "partial": "我只看到了部分内容，结论仅限于已读取的部分。", "ask": "为了准确回答，我需要你补充一点信息。"})

_EXPRESSION_LABELS = {"refuse": "无法做到时", "uncertain": "不确定时", "ask": "需要提问时", "partial": "只掌握部分信息时"}


def render(profile: PersonaProfile, scope: Literal["full", "principles"]) -> str:
    lines = [f"[人格 {profile.id}@v{profile.version}]"]
    if scope == "full":
        lines.append(profile.identity)
        if profile.voice:
            lines.append("语气：" + "；".join(profile.voice))
    lines.append("工作信条：")
    lines.extend(f"- {item}" for item in profile.principles)
    if scope == "full":
        for key, text in profile.expression.items():
            lines.append(f"{_EXPRESSION_LABELS[key]}：{text}")
    return "\n".join(lines)


def load_personas(path: Path | str | None) -> dict[str, PersonaProfile]:
    """No file means only the built-in persona; a malformed explicit file fails closed."""
    personas = {DEFAULT_PERSONA.id: DEFAULT_PERSONA}
    if not path:
        return personas
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and set(raw) == {"personas"}:
            raw = raw["personas"]
        if not isinstance(raw, list):
            raise ValueError("Agent persona file must contain a personas array")
        loaded = [PersonaProfile.model_validate(item) for item in raw]
        if len({item.id for item in loaded}) != len(loaded):
            raise ValueError("Duplicate Agent persona id")
    except (OSError, ValueError, TypeError, ValidationError) as error:
        raise ValueError("Invalid Agent persona configuration") from error
    personas.update({item.id: item for item in loaded})
    return personas


def resolve(personas: dict[str, PersonaProfile], persona_id: str | None) -> PersonaProfile:
    if persona_id is None:
        return DEFAULT_PERSONA
    try:
        return personas[persona_id]
    except KeyError:
        raise ValueError(f"Unknown Agent persona: {persona_id}") from None
