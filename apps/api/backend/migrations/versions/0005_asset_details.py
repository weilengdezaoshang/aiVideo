"""Asset metadata for the database-backed public API.
Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assets", sa.Column("details", JSONB(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("assets", "details")
