"""Keep test control-plane credentials out of the user's live application data."""
import pytest


@pytest.fixture(autouse=True)
def private_execution_token_directory(tmp_path, monkeypatch):
    from app.execution import api
    monkeypatch.setattr(api, 'user_data_path', lambda *args: tmp_path / 'app-data')


@pytest.fixture(autouse=True)
def healthy_provider():
    """The provider circuit breaker is process-wide; start every test healthy."""
    from app.agents import resilience
    resilience._failures.clear()
    yield
    resilience._failures.clear()
