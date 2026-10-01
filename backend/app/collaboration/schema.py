"""Additive SQLite collaboration schema, shared by startup and Alembic."""
DDL = [
    """CREATE TABLE IF NOT EXISTS collaboration_records (
        kind TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        data TEXT NOT NULL, state TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
        updated REAL NOT NULL, PRIMARY KEY(kind, project, id))""",
    "CREATE INDEX IF NOT EXISTS ix_collaboration_queue ON collaboration_records(kind, state, owner, updated)",
    """CREATE TABLE IF NOT EXISTS collaboration_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
        recipient TEXT NOT NULL, conversation TEXT NOT NULL DEFAULT '',
        data TEXT NOT NULL, created REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_collaboration_inbox ON collaboration_events(project,recipient,seq)",
    """CREATE TABLE IF NOT EXISTS collaboration_receipts (
        project TEXT NOT NULL, context TEXT NOT NULL, seq INTEGER NOT NULL,
        PRIMARY KEY(project,context,seq))""",
]
