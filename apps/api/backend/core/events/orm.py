"""Transport state is separate from immutable event envelopes and business Run state."""
import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, Text, UniqueConstraint, Uuid)
from sqlalchemy.orm import Mapped, mapped_column
from backend.infrastructure.orm import Base, JSONVariant
from backend.core.orm import Record


class EventReceipt(Base):
    __tablename__ = "core_event_receipts"
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_runs.id"))
    run_sequence: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    handler_version: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("run_id", "run_sequence"),
        CheckConstraint("status IN ('pending','running','retry','waiting_handler','processed','dead')"),
        CheckConstraint("attempts >= 0 AND generation >= 0"), Index("ix_core_receipt_ready", "status", "ready_at"))


class RunConsumption(Base):
    __tablename__ = "core_run_consumption"
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_runs.id"), primary_key=True)
    completed_sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    blocked_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"))
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("completed_sequence >= 0"), Index("ix_core_stream_ready", "ready_at"))


class EventAttempt(Record, Base):
    __tablename__ = "core_event_attempts"
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"))
    generation: Mapped[int] = mapped_column(Integer)
    number: Mapped[int] = mapped_column(Integer)
    claim_token: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    handler_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("event_id", "generation", "number"),
        CheckConstraint("status IN ('running','succeeded','failed','abandoned','deferred')"))


class EventDeadLetter(Base):
    __tablename__ = "core_event_dead_letters"
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"), primary_key=True)
    error: Mapped[str] = mapped_column(Text)
    handler_version: Mapped[str | None] = mapped_column(String(128))
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventReplay(Record, Base):
    __tablename__ = "core_event_replays"
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"))
    generation: Mapped[int] = mapped_column(Integer)
    operator: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    request_key: Mapped[str] = mapped_column(String(128), unique=True)
    __table_args__ = (UniqueConstraint("event_id", "generation"),)


class EventTimer(Record, Base):
    __tablename__ = "core_event_timers"
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_runs.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    causation_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lock_skip_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_version: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="scheduled", server_default="scheduled")
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"), unique=True)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("run_id", "idempotency_key"),
        ForeignKeyConstraint(["task_id", "run_id"], ["core_tasks.id", "core_tasks.run_id"]),
        CheckConstraint("status IN ('scheduled','fired','cancelled','quarantined') AND version >= 0"),
        CheckConstraint("(status = 'fired') = (event_id IS NOT NULL)"), Index("ix_core_timer_due", "status", "due_at"),
        CheckConstraint("next_fire_at >= due_at AND lock_skip_count >= 0", name="ck_core_timer_schedule"),
        Index("ix_core_timer_next", "status", "next_fire_at", "lease_until"))


class EventAlert(Record, Base):
    __tablename__ = "core_event_alerts"
    context: Mapped[dict] = mapped_column(JSONVariant, default=dict, server_default="{}")
    kind: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"))
    dedupe_key: Mapped[str] = mapped_column(String(256), unique=True)
    occurrences: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_core_alert_open", "kind", "resolved_at"),)


class EventQuarantine(Record, Base):
    """Terminal transport failures must survive even missing referenced records."""
    __tablename__ = "core_event_quarantine"
    source_key: Mapped[str] = mapped_column(String(256), unique=True)
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    timer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    code: Mapped[str] = mapped_column(String(64))
    context: Mapped[dict] = mapped_column(JSONVariant)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_core_quarantine_event", "event_id", "resolved_at"),)
