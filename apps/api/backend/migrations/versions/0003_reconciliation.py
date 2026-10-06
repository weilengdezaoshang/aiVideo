"""Independent, bounded reconciliation deadlines.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("reconcile_deadline", sa.DateTime(timezone=True)))
    op.create_index("ix_jobs_recovery_lease", "jobs", ["status", "lease_until"])


def downgrade():
    op.drop_index("ix_jobs_recovery_lease", table_name="jobs")
    op.drop_column("jobs", "reconcile_deadline")
