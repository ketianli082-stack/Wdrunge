from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pathlib import Path
from uuid import uuid4

from app import main
from app.conversation_title import clean_title
from app.agents.team import Team
from app.storage import ChatStore


client = TestClient(main.app)
WORKSPACE = str(Path(__file__).resolve().parents[2])


def test_openapi_and_validation(monkeypatch):
    schema = client.get("/openapi.json").json()
    assert "/api/chat/stream" in schema["paths"]
    assert "/api/team" in schema["paths"]
    assert "text/event-stream" in schema["paths"]["/api/chat/stream"]["post"]["responses"]["200"]["content"]
    assert client.get("/api/health").json() == {"status": "ok"}
    agents = client.get("/api/team").json()["agents"]
    usage = client.get("/api/team").json()["usage"]
    assert [agent["id"] for agent in agents] == [
        "leader", "product", "backend", "designer", "frontend", "testing"
    ]
    assert all(agent["status"] == "idle" for agent in agents)
    assert len(usage["agents"]) == 6
    assert usage["total_tokens"] >= 0
    response = client.post("/api/chat/stream", json={"messages": []})
    assert response.status_code == 422
    response = client.post(
        "/api/chat/stream",
        json={"messages": [{"role": "assistant", "content": "hello"}], "workspace_path": WORKSPACE},
    )
    assert response.status_code == 422
    response = client.post(
        "/api/chat/stream",
        json={"messages": [{"role": "user", "content": "hello"}], "conversation_id": "", "workspace_path": WORKSPACE},
    )
    assert response.status_code == 422
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    response = client.post(
        "/api/chat/stream",
        json={"messages": [{"role": "user", "content": "hello"}], "workspace_path": WORKSPACE},
    )
    assert response.status_code == 503


def test_workspace_validation(tmp_path):
    response = client.post("/api/workspaces/validate", json={"path": str(tmp_path)})
    assert response.status_code == 200
    assert response.json() == {"path": str(tmp_path.resolve())}
    assert client.post("/api/workspaces/validate", json={"path": "relative/path"}).status_code == 422
    assert client.post("/api/workspaces/validate", json={"path": str(tmp_path / "missing")}).status_code == 422
    response = client.post("/api/chat/stream", json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 422


def test_stream_emits_team_events_and_done(monkeypatch):
    class FakeTeam:
        async def stream(self, history, *, conversation_id=None, workspace_path=None):
            assert history[-1].content == "hello"
            assert conversation_id == "chat-1"
            assert workspace_path == Path(WORKSPACE)
            yield "status", {"id": "leader", "status": "running"}
            yield "chat_message", {"id": "task1", "sender": "leader", "content": "@后端开发 请接手这部分工作。", "mentions": [{"worker": "backend", "name": "后端开发", "summary": "请接手这部分工作。"}]}
            yield "read_receipt", {"message_id": "task1", "reader": "backend"}
            yield "message_reaction", {"message_id": "task1", "worker": "backend", "reaction": "done"}
            yield "activity", {"id": "tool1", "agent": "backend", "tool": "read_project_file", "description": "读取项目文件", "phase": "completed"}
            yield "delta", {"role": "leader", "content": "任务已完成"}
            yield "status", {"id": "backend", "status": "idle"}

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(main, "get_team", lambda: FakeTeam())
    response = client.post(
        "/api/chat/stream",
        json={"messages": [{"role": "user", "content": "hello"}], "conversation_id": "chat-1", "workspace_path": WORKSPACE},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'event: status\ndata: {"id": "leader", "status": "running"}\n\n' in response.text
    assert 'event: read_receipt\ndata: {"message_id": "task1", "reader": "backend"}\n\n' in response.text
    assert 'event: message_reaction\ndata: {"message_id": "task1", "worker": "backend", "reaction": "done"}\n\n' in response.text
    assert 'event: delta\ndata: {"role": "leader", "content": "任务已完成"}\n\n' in response.text
    assert "event: activity" in response.text
    assert "event: done\ndata: {}\n\n" in response.text


def test_greeting_returns_direct_reply_over_sse(monkeypatch):
    team = Team()

    async def reply(_messages, *, allow_delegation=True):
        assert allow_delegation
        return AIMessage(content="你好！")

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(team.leader, "next_message", reply)
    monkeypatch.setattr(main, "get_team", lambda: team)
    response = client.post(
        "/api/chat/stream",
        json={"messages": [{"role": "user", "content": "你好"}], "workspace_path": WORKSPACE},
    )
    assert response.status_code == 200
    assert 'event: delta\ndata: {"role": "leader", "content": "你好！"}\n\n' in response.text
    assert "event: chat_message" not in response.text
    assert "event: done\ndata: {}\n\n" in response.text


def test_project_conversation_stream_is_persisted(monkeypatch, tmp_path):
    class FakeTeam:
        async def stream(self, history, *, conversation_id=None, workspace_path=None):
            assert workspace_path == tmp_path.resolve()
            if history[-1].content == "继续优化":
                assert [message.content for message in history] == ["实现持久化", "已经完成", "继续优化"]
                yield "delta", {"role": "leader", "content": "继续完成"}
                return
            assert [message.content for message in history] == ["实现持久化"]
            yield "chat_message", {
                "id": "task-1", "sender": "leader", "content": "@后端开发 开始实现",
                "mentions": [{"worker": "backend", "name": "后端开发", "summary": "开始实现"}],
            }
            yield "read_receipt", {"message_id": "task-1", "reader": "backend"}
            yield "activity", {
                "id": "activity-1", "agent": "backend", "tool": "read_project_file",
                "description": "读取项目文件", "phase": "running",
            }
            yield "activity", {
                "id": "activity-1", "agent": "backend", "tool": "read_project_file",
                "description": "读取项目文件", "phase": "completed",
            }
            yield "message_reaction", {"message_id": "task-1", "worker": "backend", "reaction": "done"}
            yield "delta", {"role": "leader", "content": "已经完成"}

    test_store = ChatStore(f"sqlite+aiosqlite:///{tmp_path / 'wdrunge-test.db'}")
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_runtime_store_identity", None)
    monkeypatch.setattr(main, "_checkpoint_connection", None)
    monkeypatch.setattr(main, "get_team", lambda: FakeTeam())
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    title_calls = []

    async def fake_title(user_message, assistant_reply):
        title_calls.append((user_message, assistant_reply))
        return "实现持久化对话标题超过十五个字"

    monkeypatch.setattr(main.conversation_title, "generate_title", fake_title)

    with TestClient(main.app) as persistent_client:
        project_response = persistent_client.post(
            "/api/projects", json={"name": "Wdrunge", "workspace_path": str(tmp_path)}
        )
        assert project_response.status_code == 201
        project = project_response.json()
        conversation_response = persistent_client.post(
            f"/api/projects/{project['id']}/conversations", json={"title": "新对话"}
        )
        assert conversation_response.status_code == 201
        conversation = conversation_response.json()

        message_id = str(uuid4())
        response = persistent_client.post(
            f"/api/conversations/{conversation['id']}/messages/stream",
            json={"content": "实现持久化", "client_message_id": message_id},
        )
        assert response.status_code == 200
        assert 'event: delta\ndata: {"role": "leader", "content": "已经完成", "message_id":' in response.text
        assert response.text.index("event: done") < response.text.index("event: title")
        assert title_calls == [("实现持久化", "已经完成")]

        projects = persistent_client.get("/api/projects").json()
        assert projects[0]["id"] == project["id"]
        assert projects[0]["conversations"][0]["title"] == "实现持久化对话标题超过十五个字"[:15]
        detail = persistent_client.get(f"/api/conversations/{conversation['id']}").json()
        assert [message["sender"] for message in detail["messages"]] == ["user", "leader", "leader"]
        task_message = detail["messages"][1]
        assert task_message["read_by"] == ["backend"]
        assert task_message["completed_by"] == ["backend"]
        assert detail["messages"][2]["content"] == "已经完成"
        assert detail["activities"][0]["phase"] == "completed"

        second = persistent_client.post(
            f"/api/conversations/{conversation['id']}/messages/stream",
            json={"content": "继续优化", "client_message_id": str(uuid4())},
        )
        assert second.status_code == 200
        assert "event: title" not in second.text
        assert len(title_calls) == 1


def test_project_conversation_relationships_are_logical(monkeypatch, tmp_path):
    class FakeTeam:
        async def configure_persistence(self, _store, _checkpointer):
            return None

    database_path = tmp_path / "Application Support" / "logical-keys.db"
    test_store = ChatStore(f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_runtime_store_identity", None)
    monkeypatch.setattr(main, "_checkpoint_connection", None)
    monkeypatch.setattr(main, "get_team", lambda: FakeTeam())

    with TestClient(main.app) as persistent_client:
        missing = persistent_client.post(
            "/api/projects/missing/conversations", json={"title": "不会创建"}
        )
        assert missing.status_code == 404

        import sqlite3
        connection = sqlite3.connect(database_path)
        try:
            tables = ["conversations", "messages", "conversation_events", "activities", "agent_sessions"]
            assert all(connection.execute(f"PRAGMA foreign_key_list({table})").fetchall() == [] for table in tables)
        finally:
            connection.close()
