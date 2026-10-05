"""Persistent project, conversation, and team-event storage.

Relationships intentionally use logical foreign keys. The service validates parents
inside the same transaction and every relation column is indexed, while SQLite is
not asked to maintain physical FOREIGN KEY constraints.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from platformdirs import user_data_path
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    event,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import StaticPool


metadata = MetaData()

execution_permissions = Table(
    'execution_permissions', metadata,
    Column('id', String, primary_key=True),
    Column('kind', String, nullable=False),
    Column('scope', String, nullable=False),
    Column('state', String, nullable=False),
    Column('data', JSON, nullable=False),
    Index('ix_execution_permissions_kind_state', 'kind', 'state'),
)

projects = Table(
    "projects",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(160), nullable=False),
    Column("workspace_path", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("workspace_path", name="uq_projects_workspace_path"),
)

conversations = Table(
    "conversations",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("project_id", String(36), nullable=False),
    Column("title", String(240), nullable=False),
    Column("status", String(24), nullable=False, default="active"),
    Column("next_sequence", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("archived_at", DateTime(timezone=True)),
    Index("ix_conversations_project_updated", "project_id", "updated_at"),
)

messages = Table(
    "messages",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("conversation_id", String(36), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("sender_id", String(64), nullable=False),
    Column("role", String(16)),
    Column("kind", String(32), nullable=False),
    Column("content", Text, nullable=False),
    Column("status", String(24), nullable=False),
    Column("metadata_json", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    Index("ix_messages_conversation_sequence", "conversation_id", "sequence", unique=True),
)

conversation_events = Table(
    "conversation_events",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("conversation_id", String(36), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("event_type", String(40), nullable=False),
    Column("entity_id", String(80)),
    Column("payload_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_events_conversation_sequence", "conversation_id", "sequence", unique=True),
    Index("ix_events_entity", "entity_id"),
)

message_receipts = Table(
    "message_receipts",
    metadata,
    Column("message_id", String(80), nullable=False),
    Column("reader_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("message_id", "reader_id", name="uq_message_receipt"),
    Index("ix_receipts_message", "message_id"),
)

message_reactions = Table(
    "message_reactions",
    metadata,
    Column("message_id", String(80), nullable=False),
    Column("worker_id", String(64), nullable=False),
    Column("reaction", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("message_id", "worker_id", "reaction", name="uq_message_reaction"),
    Index("ix_reactions_message", "message_id"),
)

activities = Table(
    "activities",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("conversation_id", String(36), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("agent_id", String(64), nullable=False),
    Column("tool", String(120), nullable=False),
    Column("description", Text, nullable=False),
    Column("phase", String(24), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_activities_conversation_sequence", "conversation_id", "sequence", unique=True),
)

leader_checkpoints = Table(
    "leader_checkpoints",
    metadata,
    Column("conversation_id", String(36), primary_key=True),
    Column("history_cutoff", Integer, nullable=False),
    Column("handoff_json", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

agent_sessions = Table(
    "agent_sessions",
    metadata,
    Column("conversation_id", String(36), nullable=False),
    Column("agent_id", String(64), nullable=False),
    Column("thread_id", String(80), nullable=False),
    Column("context_tokens", Integer),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("conversation_id", "agent_id", name="uq_agent_session"),
    Index("ix_agent_sessions_agent", "agent_id"),
)


# Leader tool calls are not kept in LangGraph checkpoints like worker calls; this
# bounded diagnostic log keeps their arguments and results for root-cause analysis.
tool_calls = Table(
    "tool_calls", metadata,
    Column("conversation_id", String(36), primary_key=True),
    Column("tool_call_id", String(120), primary_key=True),
    Column("agent_id", String(64), nullable=False),
    Column("tool", String(120), nullable=False),
    Column("status", String(16), nullable=False),
    Column("arguments", Text, nullable=False),
    Column("result", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
)

agent_usage = Table(
    "agent_usage", metadata,
    Column("id", String(80), primary_key=True),
    Column("agent_id", String(64), nullable=False, index=True),
    Column("total_tokens", Integer, nullable=False),
    Column("context_tokens", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Provider-reported prompt cache hits; NULL when the provider omitted them.
    Column("cached_tokens", Integer),
    # Session attribution and call details (NULL for calls recorded before them).
    Column("project_id", String(36)),
    Column("conversation_id", String(36), index=True),
    Column("request_id", String(200)),
    Column("attempt", Integer),
    Column("purpose", String(32)),
    Column("model", String(80)),
    Column("output_tokens", Integer),
    Column("latency_ms", Integer),
    Column("retries", Integer),
    # Part of output_tokens the model spent reasoning (thinking), when reported.
    Column("reasoning_tokens", Integer),
)

USAGE_COLUMNS = {
    "cached_tokens": "INTEGER", "project_id": "VARCHAR(36)", "conversation_id": "VARCHAR(36)",
    "request_id": "VARCHAR(200)", "attempt": "INTEGER", "purpose": "VARCHAR(32)", "model": "VARCHAR(80)",
    "output_tokens": "INTEGER", "latency_ms": "INTEGER", "retries": "INTEGER", "reasoning_tokens": "INTEGER",
}

context_compactions = Table(
    "context_compactions", metadata,
    Column("id", String(80), primary_key=True),
    Column("scope", String(200), nullable=False, index=True),
    Column("stage", String(24), nullable=False),
    Column("record_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

context_memory = Table(
    "context_memory", metadata,
    Column("scope", String(200), primary_key=True),
    Column("state_json", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# External services the user connects once (databases, caches, queues...). Secrets
# live in their own column so no listing can return them by accident; the app
# database itself is masked from every project process (sandbox.protected_paths).
resource_connections = Table(
    "resource_connections", metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(40), nullable=False),
    Column("data", JSON, nullable=False),
    Column("secrets", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("name", name="uq_resource_connections_name"),
)

# What the platform prepared for one project on one connection (its own database
# and account), with the generated credentials.
project_resources = Table(
    "project_resources", metadata,
    Column("project_id", String(36), primary_key=True),
    Column("connection_id", String(36), primary_key=True),
    Column("data", JSON, nullable=False),
    Column("secrets", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_project_resources_connection_id", "connection_id"),
)

# Global configuration survives a record reset; what was prepared for projects does not.
SETTINGS_TABLES = {"resource_connections"}


class StorageNotFoundError(LookupError):
    pass


class StorageConflictError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


async def complete(awaitable, release=None):
    """Await a database operation to completion even if the caller is cancelled, then
    deliver the cancellation. Cancelling an in-flight aiosqlite statement makes
    SQLAlchemy invalidate the connection without closing it, so a write transaction
    it had begun keeps the SQLite write lock until garbage collection and every other
    writer fails with "database is locked". `release` frees a result (such as a new
    connection) that the cancelled caller will never receive."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
        if not task.cancelled() and task.exception() is None and release is not None:
            await complete(release(task.result()))
        raise


class CancelSafeConnection:
    """AsyncConnection whose statements finish before a cancellation is delivered."""
    COMPLETE = {'execute', 'exec_driver_sql', 'scalar', 'scalars', 'run_sync', 'commit', 'rollback'}

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        value = getattr(self._connection, name)
        if name in self.COMPLETE:
            return lambda *args, **kwargs: complete(value(*args, **kwargs))
        return value


@asynccontextmanager
async def write_transaction(engine: AsyncEngine, begin: str | None = None):
    """A write transaction that always ends in commit or rollback, never in an
    invalidated connection still holding the write lock (see complete())."""
    connection = await complete(engine.connect(), release=lambda connection: connection.close())
    try:
        if begin:
            await complete(connection.exec_driver_sql(begin))
        try:
            yield CancelSafeConnection(connection)
            await complete(connection.commit())
        except BaseException:
            await complete(connection.rollback())
            raise
    finally:
        await complete(connection.close())


def default_database_path() -> Path:
    configured = os.getenv("WDRUNGE_DATABASE_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return user_data_path("Wdrunge", "Wdrunge") / "wdrunge.db"


def default_database_url() -> str:
    configured = os.getenv("WDRUNGE_DATABASE_URL")
    if configured:
        return configured
    return URL.create(
        drivername="sqlite+aiosqlite", database=str(default_database_path())
    ).render_as_string(hide_password=False)


class ChatStore:
    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or default_database_url()
        engine_options: dict[str, Any] = {"pool_pre_ping": True}
        if self.database_url.endswith(":memory:"):
            engine_options["poolclass"] = StaticPool
        self.engine: AsyncEngine = create_async_engine(self.database_url, **engine_options)
        if self.database_url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", self._configure_sqlite)
        self._initialized = False

    def write(self):
        return write_transaction(self.engine)

    @staticmethod
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    @property
    def sqlite_path(self) -> Path | None:
        url = make_url(self.database_url)
        if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
            return None
        return Path(url.database)

    async def initialize(self) -> None:
        if self._initialized:
            return
        path = self.sqlite_path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
        async with self.write() as connection:
            await connection.run_sync(metadata.create_all)
            if path:
                # create_all does not add columns to databases created before them.
                columns = {row[1] for row in await connection.execute(text("PRAGMA table_info(agent_usage)"))}
                for name, kind in USAGE_COLUMNS.items():
                    if name not in columns:
                        await connection.execute(text(f"ALTER TABLE agent_usage ADD COLUMN {name} {kind}"))
                if "conversation_id" not in columns:
                    await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_usage_conversation_id ON agent_usage (conversation_id)"))
        self._initialized = True

    async def clear_records(self) -> None:
        """Clear app-owned rows atomically; never remove workspace files or schema."""
        await self.initialize()
        async with self.write() as connection:
            existing = set(await connection.scalars(text("SELECT name FROM sqlite_master WHERE type='table'")))
            # These SQLite tables belong to collaboration and LangGraph, not Alembic.
            extra = ('collaboration_receipts', 'collaboration_events', 'collaboration_sections',
                     'collaboration_records', 'writes', 'checkpoints')
            for name in extra:
                if name in existing:
                    await connection.execute(text(f'DELETE FROM "{name}"'))
            for table in reversed(metadata.sorted_tables):
                if table.name not in SETTINGS_TABLES:
                    await connection.execute(delete(table))

    async def close(self) -> None:
        await self.engine.dispose()
        self._initialized = False

    async def _project_exists(self, connection, project_id: str) -> bool:
        return await connection.scalar(select(projects.c.id).where(projects.c.id == project_id)) is not None

    async def _conversation_exists(self, connection, conversation_id: str) -> bool:
        return await connection.scalar(select(conversations.c.id).where(conversations.c.id == conversation_id)) is not None

    async def _next_sequence(self, connection, conversation_id: str) -> int:
        result = await connection.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(next_sequence=conversations.c.next_sequence + 1, updated_at=utcnow())
            .returning(conversations.c.next_sequence)
        )
        value = result.scalar_one_or_none()
        if value is None:
            raise StorageNotFoundError("Conversation not found")
        return int(value)

    async def create_project(self, name: str, workspace_path: str) -> dict[str, Any]:
        await self.initialize()
        project_id = str(uuid4())
        now = utcnow()
        async with self.write() as connection:
            existing = await connection.scalar(
                select(projects.c.id).where(projects.c.workspace_path == workspace_path)
            )
            if existing:
                raise StorageConflictError("A project already uses this workspace")
            await connection.execute(insert(projects).values(
                id=project_id, name=name, workspace_path=workspace_path,
                created_at=now, updated_at=now,
            ))
        return await self.get_project(project_id)

    async def get_project(self, project_id: str) -> dict[str, Any]:
        await self.initialize()
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(projects).where(projects.c.id == project_id))).mappings().first()
        if not row:
            raise StorageNotFoundError("Project not found")
        return dict(row)

    async def list_projects(self) -> list[dict[str, Any]]:
        await self.initialize()
        async with self.engine.connect() as connection:
            project_rows = (await connection.execute(select(projects).order_by(projects.c.updated_at.desc()))).mappings().all()
            conversation_rows = (await connection.execute(
                select(conversations)
                .where(conversations.c.archived_at.is_(None))
                .order_by(conversations.c.updated_at.desc())
            )).mappings().all()
        by_project: dict[str, list[dict[str, Any]]] = {}
        for row in conversation_rows:
            by_project.setdefault(row["project_id"], []).append(dict(row))
        return [{**dict(row), "conversations": by_project.get(row["id"], [])} for row in project_rows]

    async def create_conversation(self, project_id: str, title: str = "新对话") -> dict[str, Any]:
        await self.initialize()
        conversation_id = str(uuid4())
        now = utcnow()
        async with self.write() as connection:
            if not await self._project_exists(connection, project_id):
                raise StorageNotFoundError("Project not found")
            await connection.execute(insert(conversations).values(
                id=conversation_id, project_id=project_id, title=title, status="active",
                next_sequence=0, created_at=now, updated_at=now,
            ))
            await connection.execute(update(projects).where(projects.c.id == project_id).values(updated_at=now))
        return await self.get_conversation_summary(conversation_id)

    async def get_conversation_summary(self, conversation_id: str) -> dict[str, Any]:
        await self.initialize()
        async with self.engine.connect() as connection:
            row = (await connection.execute(
                select(conversations).where(conversations.c.id == conversation_id)
            )).mappings().first()
        if not row:
            raise StorageNotFoundError("Conversation not found")
        return dict(row)

    async def conversation_workspace(self, conversation_id: str) -> str:
        await self.initialize()
        async with self.engine.connect() as connection:
            row = (await connection.execute(
                select(projects.c.workspace_path)
                .select_from(conversations.join(projects, conversations.c.project_id == projects.c.id))
                .where(conversations.c.id == conversation_id)
            )).first()
        if not row:
            raise StorageNotFoundError("Conversation or project not found")
        return str(row[0])

    async def model_history(self, conversation_id: str) -> list[dict[str, str]]:
        await self.initialize()
        async with self.engine.connect() as connection:
            rows = (await connection.execute(
                select(messages.c.role, messages.c.content)
                .where(messages.c.conversation_id == conversation_id, messages.c.role.is_not(None))
                .order_by(messages.c.sequence)
            )).all()
        return [{"role": row.role, "content": row.content} for row in rows]

    async def add_user_message(self, conversation_id: str, content: str, message_id: str) -> dict[str, Any]:
        await self.initialize()
        async with self.write() as connection:
            return await self.add_user_message_in_transaction(connection, conversation_id, content, message_id)

    async def add_user_message_in_transaction(self, connection, conversation_id, content, message_id):
        now = utcnow()
        if not await self._conversation_exists(connection, conversation_id):
            raise StorageNotFoundError("Conversation not found")
        duplicate = await connection.scalar(select(messages.c.id).where(messages.c.id == message_id))
        if duplicate:
            raise StorageConflictError("This message was already submitted")
        sequence = await self._next_sequence(connection, conversation_id)
        await connection.execute(insert(messages).values(
            id=message_id, conversation_id=conversation_id, sequence=sequence,
            sender_id="user", role="user", kind="user", content=content,
            status="completed", metadata_json={}, created_at=now, completed_at=now,
        ))
        return {"id": message_id, "sequence": sequence, "created_at": now}

    async def set_initial_title(self, conversation_id: str, title: str) -> None:
        await self.initialize()
        now = utcnow()
        async with self.write() as connection:
            await connection.execute(
                update(conversations)
                .where(conversations.c.id == conversation_id, conversations.c.title == "新对话")
                .values(title=title, updated_at=now)
            )

    async def add_chat_message(self, conversation_id: str, payload: dict[str, Any]) -> None:
        await self.initialize()
        async with self.write() as connection:
            await self.add_chat_message_in_transaction(connection, conversation_id, payload)

    async def add_chat_message_in_transaction(self, connection, conversation_id, payload):
        """Shared transaction makes a collaboration notification and its chat atomic."""
        if await connection.scalar(select(messages.c.id).where(messages.c.id == payload['id'])):
            return
        now = utcnow()
        sequence = await self._next_sequence(connection, conversation_id)
        await connection.execute(insert(messages).values(
            id=payload["id"], conversation_id=conversation_id, sequence=sequence,
            sender_id=payload["sender"], role=None, kind="progress" if payload.get("kind") == "progress" else "team", content=payload["content"],
            status="completed", metadata_json={"mention": payload.get("mention"), "mentions": payload.get("mentions") or []},
            created_at=now, completed_at=now,
        ))
        await self._insert_event(connection, conversation_id, sequence, "chat_message", payload["id"], payload, now)

    async def start_assistant_message(self, conversation_id: str, message_id: str, content: str) -> None:
        await self.initialize()
        now = utcnow()
        async with self.write() as connection:
            sequence = await self._next_sequence(connection, conversation_id)
            await connection.execute(insert(messages).values(
                id=message_id, conversation_id=conversation_id, sequence=sequence,
                sender_id="leader", role="assistant", kind="assistant", content=content,
                status="streaming", metadata_json={}, created_at=now,
            ))

    async def update_assistant_message(self, message_id: str, content: str, status: str = "streaming") -> None:
        values: dict[str, Any] = {"content": content, "status": status}
        if status != "streaming":
            values["completed_at"] = utcnow()
        async with self.write() as connection:
            await connection.execute(update(messages).where(messages.c.id == message_id).values(**values))

    async def add_receipt(self, conversation_id: str, payload: dict[str, Any]) -> None:
        await self.initialize()
        async with self.write() as connection:
            await self.add_receipt_in_transaction(connection, conversation_id, payload)

    async def add_receipt_in_transaction(self, connection, conversation_id, payload):
        now = utcnow()
        if not await connection.scalar(select(messages.c.id).where(messages.c.id == payload["message_id"], messages.c.conversation_id == conversation_id)):
            raise StorageNotFoundError("Message not found")
        existing = await connection.scalar(select(message_receipts.c.message_id).where(
            message_receipts.c.message_id == payload["message_id"], message_receipts.c.reader_id == payload["reader"]
        ))
        if existing:
            return
        if not existing:
            await connection.execute(insert(message_receipts).values(
                message_id=payload["message_id"], reader_id=payload["reader"], created_at=now,
            ))
        sequence = await self._next_sequence(connection, conversation_id)
        await self._insert_event(connection, conversation_id, sequence, "read_receipt", payload["message_id"], payload, now)

    async def add_reaction(self, conversation_id: str, payload: dict[str, Any]) -> None:
        await self.initialize()
        async with self.write() as connection:
            await self.add_reaction_in_transaction(connection, conversation_id, payload)

    async def add_reaction_in_transaction(self, connection, conversation_id, payload):
        now = utcnow()
        if not await connection.scalar(select(messages.c.id).where(messages.c.id == payload["message_id"], messages.c.conversation_id == conversation_id)):
            raise StorageNotFoundError("Message not found")
        existing = await connection.scalar(select(message_reactions.c.message_id).where(
            message_reactions.c.message_id == payload["message_id"],
            message_reactions.c.worker_id == payload["worker"],
            message_reactions.c.reaction == payload["reaction"],
        ))
        if not existing:
            await connection.execute(insert(message_reactions).values(
                message_id=payload["message_id"], worker_id=payload["worker"],
                reaction=payload["reaction"], created_at=now,
            ))
        sequence = await self._next_sequence(connection, conversation_id)
        await self._insert_event(connection, conversation_id, sequence, "message_reaction", payload["message_id"], payload, now)

    async def upsert_activity(self, conversation_id: str, payload: dict[str, Any]) -> None:
        await self.initialize()
        now = utcnow()
        async with self.write() as connection:
            existing = await connection.scalar(select(activities.c.id).where(activities.c.id == payload["id"]))
            if existing:
                await connection.execute(update(activities).where(activities.c.id == payload["id"]).values(
                    agent_id=payload["agent"], tool=payload["tool"], description=payload["description"],
                    phase=payload["phase"], updated_at=now,
                ))
            else:
                sequence = await self._next_sequence(connection, conversation_id)
                await connection.execute(insert(activities).values(
                    id=payload["id"], conversation_id=conversation_id, sequence=sequence,
                    agent_id=payload["agent"], tool=payload["tool"], description=payload["description"],
                    phase=payload["phase"], created_at=now, updated_at=now,
                ))
            event_sequence = await self._next_sequence(connection, conversation_id)
            await self._insert_event(connection, conversation_id, event_sequence, "activity", payload["id"], payload, now)

    async def add_event(self, conversation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        await self.initialize()
        now = utcnow()
        async with self.write() as connection:
            sequence = await self._next_sequence(connection, conversation_id)
            await self._insert_event(connection, conversation_id, sequence, event_type, payload.get("id"), payload, now)

    async def _insert_event(self, connection, conversation_id: str, sequence: int, event_type: str, entity_id: str | None, payload: dict[str, Any], now: datetime) -> None:
        await connection.execute(insert(conversation_events).values(
            id=str(uuid4()), conversation_id=conversation_id, sequence=sequence,
            event_type=event_type, entity_id=entity_id, payload_json=payload, created_at=now,
        ))

    async def conversation_event_page(self, conversation_id: str, after: int = 0, limit: int = 200):
        await self.initialize()
        async with self.engine.connect() as connection:
            if not await self._conversation_exists(connection, conversation_id):
                raise StorageNotFoundError("Conversation not found")
            rows = (await connection.execute(select(conversation_events).where(
                conversation_events.c.conversation_id == conversation_id,
                conversation_events.c.sequence > after,
            ).order_by(conversation_events.c.sequence).limit(limit + 1))).mappings().all()
        page = rows[:limit]
        return {"events": [{"sequence": r["sequence"], "type": r["event_type"],
                            "payload": r["payload_json"], "created_at": r["created_at"]} for r in page],
                "cursor": page[-1]["sequence"] if page else after, "has_more": len(rows) > limit}

    async def get_conversation_detail(self, conversation_id: str) -> dict[str, Any]:
        await self.initialize()
        async with self.engine.connect() as connection:
            conversation = (await connection.execute(
                select(conversations).where(conversations.c.id == conversation_id)
            )).mappings().first()
            if not conversation:
                raise StorageNotFoundError("Conversation not found")
            project = (await connection.execute(
                select(projects).where(projects.c.id == conversation["project_id"])
            )).mappings().first()
            message_rows = (await connection.execute(
                select(messages).where(messages.c.conversation_id == conversation_id).order_by(messages.c.sequence)
            )).mappings().all()
            activity_rows = (await connection.execute(
                select(activities).where(activities.c.conversation_id == conversation_id).order_by(activities.c.sequence)
            )).mappings().all()
            message_ids = [row["id"] for row in message_rows]
            receipts = (await connection.execute(
                select(message_receipts).where(message_receipts.c.message_id.in_(message_ids))
            )).mappings().all() if message_ids else []
            reactions = (await connection.execute(
                select(message_reactions).where(message_reactions.c.message_id.in_(message_ids))
            )).mappings().all() if message_ids else []
        readers: dict[str, list[str]] = {}
        for row in receipts:
            readers.setdefault(row["message_id"], []).append(row["reader_id"])
        completed: dict[str, list[str]] = {}
        for row in reactions:
            if row["reaction"] == "done":
                completed.setdefault(row["message_id"], []).append(row["worker_id"])
        visible_messages = []
        for row in message_rows:
            item = dict(row)
            metadata_value = item.pop("metadata_json") or {}
            visible_messages.append({
                "id": item["id"], "sender": item["sender_id"], "content": item["content"],
                "kind": item["kind"], "status": item["status"], "sequence": item["sequence"],
                "mention": metadata_value.get("mention"), "mentions": metadata_value.get("mentions") or [],
                "read_by": readers.get(item["id"], []), "completed_by": completed.get(item["id"], []),
                "created_at": item["created_at"],
            })
        visible_activities = [{
            "id": row["id"], "agent": row["agent_id"], "tool": row["tool"],
            "description": row["description"], "phase": row["phase"],
            "sequence": row["sequence"], "created_at": row["created_at"],
        } for row in activity_rows]
        return {
            **dict(conversation),
            "project_name": project["name"] if project else "Unknown project",
            "workspace_path": project["workspace_path"] if project else "",
            "messages": visible_messages,
            "activities": visible_activities,
        }

    async def record_tool_calls(self, conversation_id: str, agent_id: str, calls: list[dict], keep: int = 5000) -> None:
        """Insert once per (conversation, call ID); texts are bounded and the log is capped."""
        if not calls:
            return
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        await self.initialize()
        rows = [{'conversation_id': conversation_id, 'tool_call_id': str(call['id'])[:120], 'agent_id': agent_id,
                 'tool': str(call['tool'])[:120], 'status': call['status'], 'arguments': call['arguments'][:8000],
                 'result': call['result'][:8000], 'created_at': utcnow()} for call in calls]
        async with self.write() as connection:
            await connection.execute(sqlite_insert(tool_calls).values(rows).on_conflict_do_nothing())
            newest = select(tool_calls.c.created_at).order_by(tool_calls.c.created_at.desc()).offset(keep).limit(1)
            await connection.execute(delete(tool_calls).where(tool_calls.c.created_at <= newest.scalar_subquery()))

    async def load_tool_calls(self, conversation_id: str) -> list[dict]:
        await self.initialize()
        async with self.engine.connect() as connection:
            rows = await connection.execute(select(tool_calls).where(tool_calls.c.conversation_id == conversation_id)
                                            .order_by(tool_calls.c.created_at))
            return [dict(row._mapping) for row in rows]

    async def record_agent_usage(self, agent_id: str, total: int, context: int, cached: int | None = None, *,
                                 project_id=None, conversation_id=None, request_id=None, attempt=None, purpose=None,
                                 model=None, output_tokens=None, latency_ms=None, retries=None, reasoning_tokens=None) -> None:
        await self.initialize()
        async with self.write() as connection:
            await connection.execute(insert(agent_usage).values(
                id=uuid4().hex, agent_id=agent_id, total_tokens=total,
                context_tokens=context, cached_tokens=cached, created_at=utcnow(),
                project_id=project_id, conversation_id=conversation_id, request_id=(request_id or None) and str(request_id)[:200],
                attempt=attempt, purpose=purpose, model=(model or None) and str(model)[:80], output_tokens=output_tokens,
                latency_ms=int(latency_ms) if isinstance(latency_ms, (int, float)) else None, retries=retries,
                reasoning_tokens=reasoning_tokens if isinstance(reasoning_tokens, int) else None,
            ))

    async def conversation_tool_counts(self, conversation_id: str) -> dict[str, int]:
        from sqlalchemy import func, case
        await self.initialize()
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(func.count(), func.sum(case((activities.c.phase == 'failed', 1), else_=0)))
                                            .where(activities.c.conversation_id == conversation_id))).one()
        return {'total': int(row[0] or 0), 'failed': int(row[1] or 0)}

    async def conversation_usage(self, conversation_id: str) -> dict:
        """Token, cache and call totals of one session, by agent, purpose and task."""
        from sqlalchemy import func, case
        await self.initialize()
        cached_input = func.sum(case((agent_usage.c.cached_tokens.is_not(None), agent_usage.c.context_tokens), else_=0))
        fields = (func.count().label('calls'), func.sum(agent_usage.c.total_tokens).label('total_tokens'),
                  func.sum(agent_usage.c.context_tokens).label('input_tokens'), func.sum(agent_usage.c.output_tokens).label('output_tokens'),
                  func.sum(agent_usage.c.reasoning_tokens).label('reasoning_tokens'),
                  func.sum(agent_usage.c.cached_tokens).label('cached_tokens'), cached_input.label('cache_input_tokens'),
                  func.avg(agent_usage.c.latency_ms).label('latency_ms'), func.sum(agent_usage.c.retries).label('retries'))
        def shape(row) -> dict:
            data = {key: (int(value) if value is not None else 0) for key, value in row._mapping.items()
                    if key not in {'group', 'latency_ms'}}
            data['latency_ms'] = round(row._mapping['latency_ms']) if row._mapping['latency_ms'] is not None else None
            data['cache_percent'] = round(100 * data['cached_tokens'] / data['cache_input_tokens'], 1) if data['cache_input_tokens'] else None
            return data
        where = agent_usage.c.conversation_id == conversation_id
        async with self.engine.connect() as connection:
            total = shape((await connection.execute(select(*fields).where(where))).one())
            groups = {}
            for name, column in (('by_agent', agent_usage.c.agent_id), ('by_purpose', agent_usage.c.purpose),
                                 ('by_task', agent_usage.c.request_id), ('by_model', agent_usage.c.model)):
                rows = await connection.execute(select(column.label('group'), *fields).where(where).group_by(column)
                                                .order_by(func.sum(agent_usage.c.total_tokens).desc()))
                groups[name] = [{'key': row._mapping['group'], **shape(row)} for row in rows]
        return {'conversation_id': conversation_id, 'totals': total, **groups}

    async def load_agent_cache_usage(self) -> dict[str, tuple[int, int]]:
        """Input and cached tokens per agent, over calls that reported cache data."""
        from sqlalchemy import func
        await self.initialize()
        async with self.engine.connect() as connection:
            rows = await connection.execute(select(
                agent_usage.c.agent_id, func.sum(agent_usage.c.context_tokens), func.sum(agent_usage.c.cached_tokens)
            ).where(agent_usage.c.cached_tokens.is_not(None)).group_by(agent_usage.c.agent_id))
            return {row[0]: (int(row[1]), int(row[2])) for row in rows}

    async def load_agent_usage(self) -> dict[str, int]:
        from sqlalchemy import func
        await self.initialize()
        async with self.engine.connect() as connection:
            rows = await connection.execute(select(
                agent_usage.c.agent_id, func.sum(agent_usage.c.total_tokens)
            ).group_by(agent_usage.c.agent_id))
            return {row[0]: int(row[1]) for row in rows}

    async def save_compaction(self, scope: str, stage: str, record: dict) -> None:
        await self.initialize()
        async with self.write() as connection:
            await connection.execute(insert(context_compactions).values(
                id=record['id'], scope=scope, stage=stage, record_json=record, created_at=utcnow(),
            ))

    async def load_compactions(self, scope: str) -> list[dict]:
        await self.initialize()
        async with self.engine.connect() as connection:
            rows = await connection.scalars(select(context_compactions.c.record_json)
                .where(context_compactions.c.scope == scope).order_by(context_compactions.c.created_at))
            return list(rows)

    async def find_compacted_message(self, scope: str, message_id: str) -> dict | None:
        """The serialized message as first recorded by a compaction, i.e. its original."""
        from sqlalchemy import cast, String
        await self.initialize()
        async with self.engine.connect() as connection:
            rows = await connection.scalars(select(context_compactions.c.record_json)
                .where(context_compactions.c.scope == scope,
                       cast(context_compactions.c.record_json, String).contains(message_id))
                .order_by(context_compactions.c.created_at))
            for record in rows:
                for message in record.get('source', []):
                    if message.get('data', {}).get('id') == message_id:
                        return message
        return None

    async def save_context_memory(self, scope: str, state: dict) -> None:
        await self.initialize()
        async with self.write() as connection:
            existing = await connection.scalar(select(context_memory.c.scope).where(context_memory.c.scope == scope))
            values = dict(state_json=state, updated_at=utcnow())
            if existing:
                await connection.execute(update(context_memory).where(context_memory.c.scope == scope).values(**values))
            else:
                await connection.execute(insert(context_memory).values(scope=scope, **values))

    async def load_context_memory(self, scope: str) -> dict | None:
        await self.initialize()
        async with self.engine.connect() as connection:
            return await connection.scalar(select(context_memory.c.state_json).where(context_memory.c.scope == scope))

    async def save_leader_checkpoint(self, conversation_id: str, history_cutoff: int, handoff: dict[str, Any]) -> None:
        await self.initialize()
        now = utcnow()
        async with self.write() as connection:
            if not await self._conversation_exists(connection, conversation_id):
                return
            existing = await connection.scalar(select(leader_checkpoints.c.conversation_id).where(leader_checkpoints.c.conversation_id == conversation_id))
            values = {"history_cutoff": history_cutoff, "handoff_json": handoff, "updated_at": now}
            if existing:
                await connection.execute(update(leader_checkpoints).where(leader_checkpoints.c.conversation_id == conversation_id).values(**values))
            else:
                await connection.execute(insert(leader_checkpoints).values(conversation_id=conversation_id, **values))

    async def load_leader_checkpoints(self) -> list[dict[str, Any]]:
        await self.initialize()
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(leader_checkpoints))).mappings().all()
        return [dict(row) for row in rows]

    async def save_agent_session(self, conversation_id: str, agent_id: str, thread_id: str, context_tokens: int | None) -> None:
        await self.initialize()
        now = utcnow()
        async with self.write() as connection:
            if not await self._conversation_exists(connection, conversation_id):
                return
            existing = await connection.scalar(select(agent_sessions.c.thread_id).where(
                agent_sessions.c.conversation_id == conversation_id, agent_sessions.c.agent_id == agent_id
            ))
            values = {"thread_id": thread_id, "context_tokens": context_tokens, "updated_at": now}
            if existing:
                await connection.execute(update(agent_sessions).where(
                    agent_sessions.c.conversation_id == conversation_id, agent_sessions.c.agent_id == agent_id
                ).values(**values))
            else:
                await connection.execute(insert(agent_sessions).values(
                    conversation_id=conversation_id, agent_id=agent_id, **values,
                ))

    async def delete_agent_session(self, conversation_id: str, agent_id: str) -> None:
        await self.initialize()
        async with self.write() as connection:
            await connection.execute(delete(agent_sessions).where(
                agent_sessions.c.conversation_id == conversation_id, agent_sessions.c.agent_id == agent_id
            ))

    async def load_agent_sessions(self, agent_id: str) -> list[dict[str, Any]]:
        await self.initialize()
        async with self.engine.connect() as connection:
            rows = (await connection.execute(
                select(agent_sessions).where(agent_sessions.c.agent_id == agent_id)
            )).mappings().all()
        return [dict(row) for row in rows]


_store: ChatStore | None = None


def get_store() -> ChatStore:
    global _store
    if _store is None:
        _store = ChatStore()
    return _store
