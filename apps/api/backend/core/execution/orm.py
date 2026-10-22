"""Execution receipts, not another queue or credit account."""
import uuid
from datetime import datetime
from sqlalchemy import (String, Text, Integer, BigInteger, DateTime, ForeignKey, UniqueConstraint,
                        CheckConstraint, Index, Uuid, func)
from sqlalchemy.orm import Mapped, mapped_column
from backend.infrastructure.orm import Base, JSONVariant


class ExecutionAdmission(Base):
    __tablename__ = "core_execution_admissions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_tasks.id"), unique=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_plans.id"))
    review_ref: Mapped[str] = mapped_column(String(128))
    task_hash: Mapped[str] = mapped_column(String(64))
    catalog_hash: Mapped[str] = mapped_column(String(64))
    checks: Mapped[dict] = mapped_column(JSONVariant)
    placement: Mapped[dict] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskExecution(Base):
    __tablename__ = "core_task_executions"
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_tasks.id"), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("jobs.id"), unique=True)
    causation_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"))
    version: Mapped[int] = mapped_column(Integer, default=0)
    artifact_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    phase: Mapped[str] = mapped_column(String(32), default="ready")
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("core_task_attempts.id"))
    prepared: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    resolved_parameters: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    ticket: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    artifact: Mapped[dict | None] = mapped_column(JSONVariant)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("assets.id"))
    result_credits: Mapped[int] = mapped_column(BigInteger, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint("version >= 0 AND retries >= 0 AND result_credits >= 0"),
        CheckConstraint("phase IN ('ready','preparing','sending','poll','download','publish','unknown','done')"),
        Index("ix_core_execution_due", "phase", "next_at", "lease_until"))


class ProviderReceipt(Base):
    __tablename__ = "core_provider_receipts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_ref: Mapped[str] = mapped_column(String(128))
    provider_task_id: Mapped[str] = mapped_column(String(256))
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_tasks.id"))
    result_hash: Mapped[str] = mapped_column(String(64))
    ticket: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("provider_ref", "provider_task_id"),)


class CanvasWrite(Base):
    __tablename__ = "core_canvas_writes"
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_tasks.id"), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("documents.id"))
    node_id: Mapped[str | None] = mapped_column(String(200))
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id"))
    conflict: Mapped[bool] = mapped_column(default=False)
    history_only: Mapped[bool] = mapped_column(default=False)
    rect: Mapped[dict] = mapped_column(JSONVariant)
