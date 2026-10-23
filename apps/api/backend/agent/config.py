"""Deployment-owned Agent routes; credentials never enter route snapshots."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True,
                              hide_input_in_errors=True)

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    label: str = Field(min_length=1, max_length=100)
    provider: Literal["deepseek", "bailian"]
    model: str = Field(min_length=1, max_length=128)
    base_url: str = Field(alias="baseUrl")
    credential_ref: str = Field(alias="credentialRef", min_length=1, max_length=128)
    persona_id: str | None = Field(default=None, alias="personaId", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    timeout_s: float = Field(default=60, alias="timeoutSeconds", ge=1, le=180)
    max_tokens: int = Field(default=2048, alias="maxTokens", ge=128, le=8192)
    context_max_bytes: int = Field(default=131072, alias="contextMaxBytes", ge=4096, le=524288)
    tool_output_max_bytes: int = Field(default=16384, alias="toolOutputMaxBytes", ge=1024, le=65536)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Agent baseUrl requires HTTPS without credentials/query/fragment")
        if parsed.path.endswith("/chat/completions"):
            raise ValueError("baseUrl must exclude /chat/completions")
        return value.rstrip("/")

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "provider": self.provider, "model": self.model}

    def snapshot(self) -> dict:
        return self.model_dump(by_alias=True)

    @classmethod
    def from_snapshot(cls, value: dict) -> AgentRoute:
        return cls.model_validate(value)


def load_routes(path: Path | str | None) -> list[AgentRoute]:
    """No deployment config means unavailable; malformed explicit config fails closed."""
    if not path:
        return []
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and set(raw) == {"routes"}:
            raw = raw["routes"]
        if not isinstance(raw, list):
            raise ValueError("Agent route file must contain a routes array")
        routes = [AgentRoute.model_validate(item) for item in raw]
        if len({route.id for route in routes}) != len(routes):
            raise ValueError("Duplicate Agent route id")
        # Stable default without changing deployment model IDs.
        return sorted(routes, key=lambda route: route.provider != "deepseek")
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("Invalid Agent route configuration") from error


def load_credentials(path: Path | str | None) -> dict:
    """Shared deployment registry reader. Never log file contents or credentials."""
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        import logging
        logging.getLogger(__name__).error("Agent credentials registry is unreadable or invalid")
        return {}
    if not isinstance(raw, dict):
        import logging
        logging.getLogger(__name__).error("Agent credentials registry must be an object")
        return {}
    return raw
