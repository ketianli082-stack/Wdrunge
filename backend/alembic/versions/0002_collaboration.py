"""Durable project collaboration."""
from alembic import op
from app.collaboration.schema import DDL
revision = '0002_collaboration'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    for statement in DDL:
        op.execute(statement)


def downgrade():
    for table in ('collaboration_receipts', 'collaboration_events', 'collaboration_records'):
        op.execute(f'DROP TABLE IF EXISTS {table}')
