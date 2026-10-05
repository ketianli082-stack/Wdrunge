"""Record the reasoning (thinking) part of each model call's output tokens."""
from alembic import op
import sqlalchemy as sa

revision = '0010_reasoning_tokens'
down_revision = '0009_resources'
branch_labels = None
depends_on = None


def upgrade():
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('agent_usage')}
    if 'reasoning_tokens' not in columns:
        op.add_column('agent_usage', sa.Column('reasoning_tokens', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('agent_usage') as batch:
        batch.drop_column('reasoning_tokens')
