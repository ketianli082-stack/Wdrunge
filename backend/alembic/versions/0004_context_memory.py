"""Durable model usage and auditable context compression ranges."""
from alembic import op
from app.storage import agent_usage, context_compactions, context_memory

revision = '0004_context_memory'
down_revision = '0003_collaboration_search'
branch_labels = None
depends_on = None


def upgrade():
    for table in (agent_usage, context_compactions, context_memory):
        table.create(op.get_bind(), checkfirst=True)


def downgrade():
    for table in (context_memory, context_compactions, agent_usage):
        table.drop(op.get_bind(), checkfirst=True)
