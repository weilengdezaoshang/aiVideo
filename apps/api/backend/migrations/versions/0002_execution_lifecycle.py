"""Durable phase deadlines and execution ownership.

Revision ID: 0002
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("phase", sa.String(32), nullable=False, server_default="prepare"))
    op.add_column("jobs", sa.Column("lease_owner", sa.String(64)))
    for name in ("lease_until", "dispatched_until", "queue_deadline", "execution_deadline", "download_deadline", "cancel_requested_at"):
        op.add_column("jobs", sa.Column(name, sa.DateTime(timezone=True)))
    op.add_column("jobs", sa.Column("phase_failures", sa.Integer(), nullable=False, server_default="0"))
    for name in ("provider_snapshot", "phase_payload"):
        op.add_column("jobs", sa.Column(name, postgresql.JSONB(), nullable=False, server_default="{}"))
    op.alter_column("jobs", "slot_scope", type_=sa.String(128), existing_type=sa.String(32))
    op.add_column("outbox", sa.Column("claim_token", sa.String(64)))


def downgrade():
    op.drop_column("outbox", "claim_token")
    # PostgreSQL rejects a rollback if wider scope identifiers are still present.
    op.alter_column("jobs", "slot_scope", type_=sa.String(32), existing_type=sa.String(128))
    for name in ("phase_payload", "provider_snapshot", "phase_failures", "cancel_requested_at",
                 "download_deadline", "execution_deadline", "queue_deadline", "dispatched_until", "lease_until", "lease_owner", "phase"):
        op.drop_column("jobs", name)
