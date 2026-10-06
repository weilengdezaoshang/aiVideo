"""Errors raised by tool handlers; the gateway turns them into error envelopes."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from backend.agent.contracts import FailureDetails


class ToolError(Exception):
    def __init__(self, code: str, message: str, details: FailureDetails | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
