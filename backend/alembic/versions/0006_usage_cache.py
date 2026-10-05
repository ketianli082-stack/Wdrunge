"""Record provider-reported prompt cache hits per model call."""
from alembic import op
import sqlalchemy as sa

revision = '0006_usage_cache'
down_revision = '0005_execution_permissions'
branch_labels = None
depends_on = None


def upgrade():
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('agent_usage')}
    if 'cached_tokens' not in columns:
        op.add_column('agent_usage', sa.Column('cached_tokens', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('agent_usage') as batch:
        batch.drop_column('cached_tokens')
