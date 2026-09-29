from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pathlib import Path

from app import main
from app.agents.team import Team


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
