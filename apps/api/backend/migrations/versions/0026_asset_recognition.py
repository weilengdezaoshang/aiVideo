"""Durable recognition jobs for ingest-time image analysis.

Revision ID: 0026
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "core_recognition_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("stored_asset_id", sa.Uuid(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("requested_by", sa.String(200), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("core_asset_id", sa.Uuid(), nullable=True),
        sa.Column("core_asset_version_id", sa.Uuid(), nullable=True),
        sa.Column("result", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("request_key", name="ux_core_recognition_key"),
        sa.CheckConstraint("source IN ('upload','generation')", name="ck_core_recognition_source"),
        sa.CheckConstraint("status IN ('pending','claimed','succeeded','failed','skipped')", name="ck_core_recognition_status"),
        sa.CheckConstraint("attempt >= 0", name="ck_core_recognition_attempts"),
    )
    op.create_index("ix_core_recognition_due", "core_recognition_jobs", ["status", "next_at"])
    op.create_index("ix_core_recognition_asset", "core_recognition_jobs", ["stored_asset_id"])


def downgrade():
    op.drop_index("ix_core_recognition_asset", table_name="core_recognition_jobs")
    op.drop_index("ix_core_recognition_due", table_name="core_recognition_jobs")
    op.drop_table("core_recognition_jobs")
