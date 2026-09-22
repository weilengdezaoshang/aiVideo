"""Independent batch children; parents are presentation-only.
Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("parent_id", sa.Uuid(), sa.ForeignKey("jobs.id")))
    op.create_index("ix_jobs_parent_id", "jobs", ["parent_id"])


def downgrade():
    op.drop_index("ix_jobs_parent_id", table_name="jobs")
    op.drop_column("jobs", "parent_id")
