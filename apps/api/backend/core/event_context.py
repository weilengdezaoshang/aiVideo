"""Causal context shared by state transitions and registered event handlers."""
from contextvars import ContextVar
from uuid import UUID

current_event_id: ContextVar[UUID | None] = ContextVar("core_current_event", default=None)
