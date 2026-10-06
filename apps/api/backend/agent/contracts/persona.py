"""Persona configuration: identity and voice only, never permissions or tools."""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from .evidence import Contract

Line = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_CONTROL_MARKERS = re.compile(r"\[E\d|\bE\d+\.f\d+\b|submit_plan|complete_step|report_blocker|ask_user"
                              r"|submit_reflection|read_evidence|\bfinish\b|tool_call|function_call"
                              r"|read_canvas|list_canvas_elements|get_canvas_elements|inspect_image")


def _clean(text: str) -> str:
    if _CONTROL_MARKERS.search(text):
        raise ValueError("persona text must not contain tool names or evidence markers")
    return text


class PersonaProfile(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    version: int = Field(ge=1)
    displayName: str = Field(min_length=1, max_length=40)
    identity: str = Field(min_length=1, max_length=300)
    voice: list[Line] = Field(max_length=6)
    principles: list[Line] = Field(min_length=1, max_length=8)
    expression: dict[Literal["refuse", "uncertain", "ask", "partial"], Line] = Field(
        default_factory=dict)

    @field_validator("displayName", "identity")
    @classmethod
    def _scalar(cls, value: str) -> str:
        return _clean(value)

    @field_validator("voice", "principles")
    @classmethod
    def _lines(cls, value: list[str]) -> list[str]:
        return [_clean(item) for item in value]

    @field_validator("expression")
    @classmethod
    def _expression(cls, value: dict[str, str]) -> dict[str, str]:
        return {key: _clean(text) for key, text in value.items()}
