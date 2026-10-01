"""Add project-scoped persistent conversations.

Revision ID: 0001
Revises:
"""

from alembic import op

from app.storage import metadata


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The imported metadata intentionally contains indexes and unique constraints,
    # but no physical foreign keys; relationships are checked by ChatStore.
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
