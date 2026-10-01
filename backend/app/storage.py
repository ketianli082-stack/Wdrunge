"""Persistent project, conversation, and team-event storage.

Relationships intentionally use logical foreign keys. The service validates parents
inside the same transaction and every relation column is indexed, while SQLite is
not asked to maintain physical FOREIGN KEY constraints.
"""

from __future__ import annotations

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


class StorageNotFoundError(LookupError):
    pass


class StorageConflictError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


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
        async with self.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        self._initialized = True

    async def clear_records(self) -> None:
        """Clear app-owned rows atomically; never remove workspace files or schema."""
        await self.initialize()
        async with self.engine.begin() as connection:
            existing = set(await connection.scalars(text("SELECT name FROM sqlite_master WHERE type='table'")))
            # These SQLite tables belong to collaboration and LangGraph, not Alembic.
            extra = ('collaboration_receipts', 'collaboration_events', 'collaboration_sections',
                     'collaboration_records', 'writes', 'checkpoints')
            for name in extra:
                if name in existing:
                    await connection.execute(text(f'DELETE FROM "{name}"'))
            for table in reversed(metadata.sorted_tables):
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
        async with self.engine.begin() as connection:
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
        async with self.engine.begin() as connection:
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
        now = utcnow()
        async with self.engine.begin() as connection:
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
        async with self.engine.begin() as connection:
            await connection.execute(
                update(conversations)
                .where(conversations.c.id == conversation_id, conversations.c.title == "新对话")
                .values(title=title, updated_at=now)
            )

    async def add_chat_message(self, conversation_id: str, payload: dict[str, Any]) -> None:
        await self.initialize()
        async with self.engine.begin() as connection:
            await self.add_chat_message_in_transaction(connection, conversation_id, payload)

    async def add_chat_message_in_transaction(self, connection, conversation_id, payload):
        """Shared transaction makes a collaboration notification and its chat atomic."""
        if await connection.scalar(select(messages.c.id).where(messages.c.id == payload['id'])):
            return
        now = utcnow()
        sequence = await self._next_sequence(connection, conversation_id)
        await connection.execute(insert(messages).values(
            id=payload["id"], conversation_id=conversation_id, sequence=sequence,
            sender_id=payload["sender"], role=None, kind="team", content=payload["content"],
            status="completed", metadata_json={"mention": payload.get("mention"), "mentions": payload.get("mentions") or []},
            created_at=now, completed_at=now,
        ))
        await self._insert_event(connection, conversation_id, sequence, "chat_message", payload["id"], payload, now)

    async def start_assistant_message(self, conversation_id: str, message_id: str, content: str) -> None:
        await self.initialize()
        now = utcnow()
        async with self.engine.begin() as connection:
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
        async with self.engine.begin() as connection:
            await connection.execute(update(messages).where(messages.c.id == message_id).values(**values))

    async def add_receipt(self, conversation_id: str, payload: dict[str, Any]) -> None:
        await self.initialize()
        async with self.engine.begin() as connection:
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
        async with self.engine.begin() as connection:
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
        async with self.engine.begin() as connection:
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
        async with self.engine.begin() as connection:
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

    async def save_leader_checkpoint(self, conversation_id: str, history_cutoff: int, handoff: dict[str, Any]) -> None:
        await self.initialize()
        now = utcnow()
        async with self.engine.begin() as connection:
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
        async with self.engine.begin() as connection:
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
        async with self.engine.begin() as connection:
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
