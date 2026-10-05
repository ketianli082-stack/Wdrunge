"""Attribute model usage to sessions, tasks and purposes."""
from alembic import op
import sqlalchemy as sa

revision = '0008_usage_sessions'
down_revision = '0007_tool_calls'
branch_labels = None
depends_on = None

COLUMNS = {
    'project_id': sa.String(36), 'conversation_id': sa.String(36), 'request_id': sa.String(200), 'attempt': sa.Integer(),
    'purpose': sa.String(32), 'model': sa.String(80), 'output_tokens': sa.Integer(), 'latency_ms': sa.Integer(), 'retries': sa.Integer(),
}


def upgrade():
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('agent_usage')}
    for name, kind in COLUMNS.items():
        if name not in columns:
            op.add_column('agent_usage', sa.Column(name, kind, nullable=True))
    indexes = {index['name'] for index in sa.inspect(op.get_bind()).get_indexes('agent_usage')}
    if 'ix_agent_usage_conversation_id' not in indexes:
        op.create_index('ix_agent_usage_conversation_id', 'agent_usage', ['conversation_id'])


def downgrade():
    op.drop_index('ix_agent_usage_conversation_id', table_name='agent_usage')
    with op.batch_alter_table('agent_usage') as batch:
        for name in COLUMNS:
            batch.drop_column(name)
