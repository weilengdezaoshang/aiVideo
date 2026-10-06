"""Durable canvas Agent records. PostgreSQL, not broker delivery, owns progress."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.infrastructure.orm import Base, JSONVariant


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("documents.id", ondelete="SET NULL"))
    source_document_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("uq_agent_session_document", "workspace_id", "source_document_id", unique=True),)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_sessions.id"), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    client_request_id: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    principal: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(24), default="queued")
    phase: Mapped[str] = mapped_column(String(16), default="model")
    message: Mapped[str] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict | None] = mapped_column(JSONVariant)
    route: Mapped[dict] = mapped_column(JSONVariant)
    snapshot: Mapped[dict] = mapped_column(JSONVariant)
    assets: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    history: Mapped[list] = mapped_column(JSONVariant, default=list)
    model_call_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    execution_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claims: Mapped[dict | None] = mapped_column(JSONVariant)
    counters: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("uq_agent_run_request", "session_id", "client_request_id", unique=True),
        Index("uq_agent_run_active", "session_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running')"),
              sqlite_where=text("status IN ('queued', 'running')")),
        Index("ix_agent_runs_due", "status", "next_run_at", "lease_until"),
    )


class AgentModelCall(Base):
    __tablename__ = "agent_model_calls"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="sending")
    execution_epoch: Mapped[int] = mapped_column(Integer)
    messages: Mapped[list] = mapped_column(JSONVariant)
    response: Mapped[dict | None] = mapped_column(JSONVariant)
    raw_response: Mapped[dict | None] = mapped_column(JSONVariant)
    usage: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(256))
    error: Mapped[dict | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("uq_agent_model_ordinal", "run_id", "ordinal", unique=True),)


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id"), index=True)
    model_call_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_model_calls.id"))
    tool_call_id: Mapped[str] = mapped_column(String(256))
    ordinal: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[dict] = mapped_column(JSONVariant)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    kind: Mapped[str] = mapped_column(String(16), default="read")
    tool_version: Mapped[int | None] = mapped_column(Integer)
    dedupe_of: Mapped[str | None] = mapped_column(String(32))
    execution_epoch: Mapped[int | None] = mapped_column(Integer)
    output: Mapped[dict | None] = mapped_column(JSONVariant)
    error: Mapped[dict | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("uq_agent_tool_call", "model_call_id", "tool_call_id", unique=True),)


class AgentEvent(Base):
    __tablename__ = "agent_events"
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_sessions.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id"), index=True)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
