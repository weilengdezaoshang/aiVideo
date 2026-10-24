"""Durable canvas version-switch commands.

Revision ID: 0025
Revises: 0024
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "core_version_switch_commands",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", sa.String(200), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("core_asset_id", sa.Uuid(), nullable=False),
        sa.Column("core_asset_version_id", sa.Uuid(), nullable=False),
        sa.Column("expected_document_revision", sa.Integer(), nullable=False),
        sa.Column("expected_anchor_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("outbox_id", sa.Uuid(), nullable=True),
        sa.Column("result", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "user_id", "idempotency_key", name="ux_version_switch_key"),
        sa.CheckConstraint("status IN ('pending','leased','applied','conflict','rejected')"),
        sa.CheckConstraint("expected_document_revision >= 0 AND expected_anchor_version >= 0 AND attempt >= 0"),
    )
    op.create_index("ix_version_switch_due", "core_version_switch_commands", ["status", "next_at"])
    op.create_index(
        "ix_version_switch_recover", "core_version_switch_commands", ["status", "lease_until"],
        postgresql_where=sa.text("status = 'leased'"),
    )


def downgrade():
    op.drop_index("ix_version_switch_recover", table_name="core_version_switch_commands")
    op.drop_index("ix_version_switch_due", table_name="core_version_switch_commands")
    op.drop_table("core_version_switch_commands")
