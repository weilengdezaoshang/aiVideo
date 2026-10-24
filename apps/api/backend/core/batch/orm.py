"""Durable batch quota, dispatch, and retry links. Run and task enums stay unchanged."""
import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.orm import Record
from backend.infrastructure.orm import Base, JSONVariant


class CoreBatchQuota(Record, Base):
    """One global in-flight row (``quota_key='global'``). Per-run caps are counted from dispatches."""

    __tablename__ = "core_batch_quotas"
    quota_key: Mapped[str] = mapped_column(String(64))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("workspaces.id"), nullable=True)
    inflight: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    limit_value: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("quota_key"),
        CheckConstraint("inflight >= 0 AND limit_value > 0 AND inflight <= limit_value"),
        CheckConstraint("quota_key <> ''"),
    )


class CoreBatchDispatch(Record, Base):
    __tablename__ = "core_batch_dispatches"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_runs.id"))
    task_key: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(24))
    credits: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    __table_args__ = (
        UniqueConstraint("run_id", "task_key"),
        UniqueConstraint("idempotency_key"),
        CheckConstraint("state IN ('reserved','submitted','settled','rejected_budget')"),
        CheckConstraint("credits >= 0"),
        Index("ix_core_batch_dispatch_open", "workspace_id", "state"),
        Index("ix_core_batch_dispatch_run", "run_id", "state"),
    )


class CoreBatchRetry(Record, Base):
    __tablename__ = "core_batch_retries"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    parent_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_runs.id"))
    child_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    request_key: Mapped[str] = mapped_column(String(128))
    quote: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    document: Mapped[dict] = mapped_column(JSONVariant)
    __table_args__ = (
        UniqueConstraint("parent_run_id", "request_key"),
        UniqueConstraint("child_run_id"),
        ForeignKeyConstraint(["child_run_id", "workspace_id"], ["core_runs.id", "core_runs.workspace_id"]),
        ForeignKeyConstraint(["parent_run_id", "workspace_id"], ["core_runs.id", "core_runs.workspace_id"]),
        Index("ix_core_batch_retry_pending", "created_at", "id", postgresql_where=text(
            "child_run_id IS NOT NULL AND COALESCE((document ->> 'settled')::boolean, false) = false")),
        CheckConstraint("quote >= 0"),
        Index("ix_core_batch_retry_parent", "parent_run_id"),
    )
