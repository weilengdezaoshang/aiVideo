"""Durable canvas Agent records. PostgreSQL, not broker delivery, owns progress."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.infrastructure.orm import Base, JSONVariant
from backend.core import orm as core_orm  # noqa: F401 — register credit association targets


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("documents.id", ondelete="SET NULL"))
    source_document_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    principal: Mapped[str] = mapped_column(String(200), default="", server_default="")
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("uq_agent_session_document", "workspace_id", "source_document_id", "principal", unique=True),)


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
    execution: Mapped[dict | None] = mapped_column(JSONVariant)
    persona: Mapped[dict | None] = mapped_column(JSONVariant)
    stage: Mapped[str] = mapped_column(String(16), default="plan")
    plan: Mapped[dict | None] = mapped_column(JSONVariant)
    waiting: Mapped[dict | None] = mapped_column(JSONVariant)
    counters: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("uq_agent_run_request", "session_id", "client_request_id", unique=True),
        Index("uq_agent_run_active", "session_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running') AND phase <> 'media'"),
              sqlite_where=text("status IN ('queued', 'running') AND phase <> 'media'")),
        Index("ix_agent_runs_due", "status", "next_run_at", "lease_until"),
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id"), index=True)
    plan_version: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(80))
    intent: Mapped[str] = mapped_column(Text)
    allowed_tools: Mapped[list] = mapped_column(JSONVariant, default=list)
    depends_on: Mapped[list] = mapped_column(JSONVariant, default=list)
    done_when: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    execution: Mapped[dict | None] = mapped_column(JSONVariant)
    summary: Mapped[str | None] = mapped_column(Text)
    evidence_refs: Mapped[list] = mapped_column(JSONVariant, default=list)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("uq_agent_step_key", "run_id", "plan_version", "key", unique=True),)


class AgentReflection(Base):
    __tablename__ = "agent_reflections"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id"), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_sessions.id"), index=True)
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("agent_model_calls.id"))
    round: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16))
    hard_checks: Mapped[dict | None] = mapped_column(JSONVariant)
    verdict: Mapped[str] = mapped_column(String(16))
    issues: Mapped[list] = mapped_column(JSONVariant, default=list)
    question: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentModelCall(Base):
    __tablename__ = "agent_model_calls"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="sending")
    execution_epoch: Mapped[int] = mapped_column(Integer)
    messages: Mapped[list] = mapped_column(JSONVariant)
    manifest: Mapped[dict | None] = mapped_column(JSONVariant)
    stage: Mapped[str | None] = mapped_column(String(16))
    step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("agent_steps.id"))
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
    step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("agent_steps.id"))
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


class HostCreditAccount(Base):
    __tablename__ = "host_credit_accounts"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"), primary_key=True)
    available: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (CheckConstraint("available >= 0", name="ck_host_credit_nonnegative"),)


class HostCreditGrant(Base):
    __tablename__ = "host_credit_grants"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    principal: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("amount > 0", name="ck_host_credit_grant_positive"),)


class HostCreditLedger(Base):
    __tablename__ = "host_credit_ledger"
    core_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("core_runs.id", name="fk_credit_core_run"))
    core_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("agent_runs.id"), index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("agent_steps.id"))
    child_index: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("jobs.id"))
    quoted: Mapped[int] = mapped_column(BigInteger)
    charged: Mapped[int] = mapped_column(BigInteger, default=0)
    refunded: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String(24), default="reserved")
    details: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("(run_id IS NOT NULL AND step_id IS NOT NULL AND core_run_id IS NULL) OR (run_id IS NULL AND step_id IS NULL AND core_run_id IS NOT NULL)", name="ck_credit_owner"),
        Index("uq_credit_core_task", "core_task_id", unique=True),
        ForeignKeyConstraint(["core_run_id", "workspace_id"], ["core_runs.id", "core_runs.workspace_id"], name="fk_credit_core_workspace"),
        ForeignKeyConstraint(["core_task_id", "core_run_id"], ["core_tasks.id", "core_tasks.run_id"], name="fk_credit_core_task"),
        CheckConstraint("(core_run_id IS NULL) = (core_task_id IS NULL)", name="ck_credit_core_pair"),
        Index("ix_credit_core_run_task", "core_run_id", "core_task_id"),
        Index("uq_host_credit_step_attempt", "run_id", "step_id", "child_index", "attempt", unique=True),
        CheckConstraint("quoted >= 0 AND charged >= 0 AND refunded >= 0 AND charged + refunded <= quoted",
                        name="ck_host_credit_conservation"))


class HostNodeLease(Base):
    __tablename__ = "host_node_leases"
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id"), index=True)
    step_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_steps.id"))
    expected_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentPlanVersion(Base):
    __tablename__ = "agent_plan_versions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    writer: Mapped[str] = mapped_column(String(16))
    proposal: Mapped[dict] = mapped_column(JSONVariant)
    content_hash: Mapped[str] = mapped_column(String(64))
    intent: Mapped[dict] = mapped_column(JSONVariant)
    bindings: Mapped[dict] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("uq_agent_plan_version", "run_id", "version", unique=True),)


class AgentPlanReview(Base):
    __tablename__ = "agent_plan_reviews"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_plan_versions.id"), unique=True)
    result: Mapped[dict] = mapped_column(JSONVariant)
    input_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentExecutionAdmission(Base):
    __tablename__ = "agent_execution_admissions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_plan_reviews.id"), unique=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_plan_versions.id"), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
