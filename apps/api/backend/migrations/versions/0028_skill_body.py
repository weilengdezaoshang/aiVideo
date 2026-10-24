"""Skill playbook body column.

Revision ID: 0028
Revises: 0027
"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("core_skill_versions",
        sa.Column("body", sa.Text(), nullable=False, server_default=""))


def downgrade():
    op.drop_column("core_skill_versions", "body")
