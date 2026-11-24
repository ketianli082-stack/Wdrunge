"""Record what the platform did to a tool call's arguments (ignored or repaired)."""
from alembic import op
import sqlalchemy as sa

revision = '0011_tool_call_notes'
down_revision = '0010_reasoning_tokens'
branch_labels = None
depends_on = None


def upgrade():
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('tool_calls')}
    if 'notes' not in columns:
        op.add_column('tool_calls', sa.Column('notes', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('tool_calls') as batch:
        batch.drop_column('notes')
