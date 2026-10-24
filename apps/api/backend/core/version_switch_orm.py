"""Version-switch commands. Not part of core/orm.py."""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.infrastructure.orm import Base, JSONVariant


class VersionSwitchCommand(Base):
    __tablename__ = "core_version_switch_commands"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    user_id: Mapped[str] = mapped_column(String(200))
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("documents.id"))
    node_id: Mapped[str] = mapped_column(String(128))
    core_asset_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    core_asset_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    expected_document_revision: Mapped[int] = mapped_column(Integer)
    expected_anchor_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    outbox_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    result: Mapped[dict] = mapped_column(JSONVariant, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "idempotency_key", name="ux_version_switch_key"),
        CheckConstraint("status IN ('pending','leased','applied','conflict','rejected')"),
        CheckConstraint("expected_document_revision >= 0 AND expected_anchor_version >= 0 AND attempt >= 0"),
        Index("ix_version_switch_due", "status", "next_at"),
        Index("ix_version_switch_recover", "status", "lease_until",
              postgresql_where=text("status = 'leased'")),
    )
