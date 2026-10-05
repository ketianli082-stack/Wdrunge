"""Bounded diagnostic log of Leader tool call arguments and results."""
from alembic import op
from app.storage import tool_calls

revision = '0007_tool_calls'
down_revision = '0006_usage_cache'
branch_labels = None
depends_on = None


def upgrade():
    tool_calls.create(op.get_bind(), checkfirst=True)


def downgrade():
    tool_calls.drop(op.get_bind(), checkfirst=True)
