"""Index current document sections for bounded project search."""
from alembic import op
from app.collaboration.schema import DDL
revision = '0003_collaboration_search'
down_revision = '0002_collaboration'
branch_labels = None
depends_on = None


def upgrade():
    for sql in DDL:
        op.execute(sql)


def downgrade():
    op.execute('DROP TABLE IF EXISTS collaboration_sections')
    op.execute('DROP INDEX IF EXISTS ix_collaboration_project_kind')
