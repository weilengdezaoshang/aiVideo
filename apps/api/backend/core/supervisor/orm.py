"""Persistent supervisor wakeups, not a second event bus."""
import uuid
from datetime import datetime
from sqlalchemy import Uuid, String, Integer, Text, ForeignKey, UniqueConstraint, DateTime, Index, CheckConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from backend.infrastructure.orm import Base, JSONVariant


class Invocation(Base):
    __tablename__ = "core_planner_invocations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_runs.id"))
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    context: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONVariant)
    review: Mapped[dict | None] = mapped_column(JSONVariant)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    failures: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("attempt >= 0 AND failures >= 0"),
        CheckConstraint("status IN ('pending','calling','returned','done')"), UniqueConstraint("event_id", "attempt"), Index("ix_core_planner_due", "status", "next_at", "lease_until"))
