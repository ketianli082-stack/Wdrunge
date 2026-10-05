"""Global external-service connections and what the platform prepared per project."""
from alembic import op
from app.storage import project_resources, resource_connections

revision = '0009_resources'
down_revision = '0008_usage_sessions'
branch_labels = None
depends_on = None


def upgrade():
    resource_connections.create(op.get_bind(), checkfirst=True)
    project_resources.create(op.get_bind(), checkfirst=True)


def downgrade():
    project_resources.drop(op.get_bind(), checkfirst=True)
    resource_connections.drop(op.get_bind(), checkfirst=True)
