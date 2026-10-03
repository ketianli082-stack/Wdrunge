"""Persist exact-operation execution approvals and revocable grants."""
from alembic import op
from app.storage import execution_permissions

revision = '0005_execution_permissions'
down_revision = '0004_context_memory'
branch_labels = None
depends_on = None


def upgrade():
    execution_permissions.create(op.get_bind(), checkfirst=True)


def downgrade():
    execution_permissions.drop(op.get_bind(), checkfirst=True)
