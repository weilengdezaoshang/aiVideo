"""Skill contract and Markdown parsing. Frontmatter is data; the body is the playbook.

A skill is Run-scoped: the allowlist tightens ``scope.tools`` (the reviewer's
gate-① then rejects anything else), phases and confirmation points compile into
review intent rules, and the body is injected into the supervisor context.
"""
from __future__ import annotations

import hashlib
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from backend.core.contracts import Contract

NAME_MAX = 120
ROLE_MAX = 200
SCENE_MAX = 200
BODY_MAX = 20000


class SkillPhase(Contract):
    name: str = Field(min_length=1, max_length=64)
    tools: list[str] = Field(min_length=1)
    required: bool = True
    confirm: bool = False


class SkillRouting(Contract):
    ask_first: list[str] = Field(default_factory=list, max_length=10)
    act: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("ask_first", "act")
    @classmethod
    def bounded_lines(cls, value: list[str]) -> list[str]:
        for line in value:
            if not isinstance(line, str) or not line.strip() or len(line) > SCENE_MAX:
                raise ValueError("routing lines must be short non-empty strings")
        return value


class SkillSpec(Contract):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    version: str = Field(min_length=1, max_length=64)
    scenes: list[str] = Field(min_length=1, max_length=8)
    role: str = Field(min_length=1, max_length=ROLE_MAX)
    body: str = Field(default="", max_length=BODY_MAX)
    allowlist: list[str] = Field(min_length=1)
    phases: list[SkillPhase] = Field(min_length=1, max_length=8)
    routing: SkillRouting = Field(default_factory=SkillRouting)
    boundaries: list[str] = Field(default_factory=list, max_length=10)
    defaults: dict = Field(default_factory=dict)
    source: Literal["official", "user"] = "official"

    @field_validator("scenes")
    @classmethod
    def bounded_scenes(cls, value: list[str]) -> list[str]:
        for line in value:
            if not isinstance(line, str) or not line.strip() or len(line) > SCENE_MAX:
                raise ValueError("scenes must be short non-empty strings")
        return [line.strip() for line in value]

    @field_validator("allowlist")
    @classmethod
    def unique_tools(cls, value: list[str]) -> list[str]:
        for tool in value:
            if not isinstance(tool, str) or not tool or len(tool) > 64:
                raise ValueError("allowlist entries must be tool ids")
        if len(set(value)) != len(value):
            raise ValueError("allowlist entries must be unique")
        return value

    @field_validator("boundaries")
    @classmethod
    def bounded_boundaries(cls, value: list[str]) -> list[str]:
        for line in value:
            if not isinstance(line, str) or not line.strip() or len(line) > SCENE_MAX:
                raise ValueError("boundaries must be short non-empty strings")
        return value

    @model_validator(mode="after")
    def phases_inside_allowlist(self):
        known = set(self.allowlist)
        for phase in self.phases:
            outside = set(phase.tools) - known
            if outside:
                raise ValueError(f"phase {phase.name} uses tools outside the allowlist: {sorted(outside)}")
        if not any(phase.required for phase in self.phases):
            raise ValueError("at least one phase must be required")
        return self

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"source"})
        import json
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()


def parse_skill_markdown(text: str) -> SkillSpec:
    """YAML frontmatter plus a Markdown playbook body. Malformed input is a hard error."""
    if not isinstance(text, str) or not text.startswith("---"):
        raise ValueError("skill markdown must start with a YAML frontmatter block")
    parts = text.split("\n---", 2)
    if len(parts) < 2:
        raise ValueError("skill frontmatter is not closed")
    try:
        raw = yaml.safe_load(parts[0][3:].strip("\n"))
    except yaml.YAMLError as exc:
        raise ValueError(f"skill frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("skill frontmatter must be a mapping")
    body = parts[1].lstrip("\n") if len(parts) > 1 else ""
    if len(parts) > 2:  # A stray "---" inside the body; keep the document verbatim.
        body = "\n---".join([body, *parts[2:]])
    raw["body"] = body
    try:
        return SkillSpec.model_validate(raw)
    except ValueError as exc:
        raise ValueError(f"invalid skill definition: {exc}") from exc
