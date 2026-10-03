import asyncio
from urllib.error import URLError
import pytest

from app.agents.tools.run_service_check import check_service, probe
from app.agents.tools._workspace import active_workspace


@pytest.fixture(autouse=True)
def approve_test_service(monkeypatch):
    """Deterministic human approval; the real OS sandbox still executes the service."""
    import importlib
    import sys
    from types import SimpleNamespace
    from app.execution import permissions
    from app.execution.processes import processes
    class Approval:
        store = SimpleNamespace(sqlite_path=None)
        async def authorize(self, *args, **kwargs):
            return None
    monkeypatch.setattr(permissions, 'current', Approval())
    monkeypatch.setattr(processes, 'jobs', {})
    module = importlib.import_module('app.agents.tools.run_service_check')
    monkeypatch.setattr(module, 'verification_python', lambda directory: sys.executable)


def test_service_check_starts_probes_and_stops_only_its_temporary_service(tmp_path):
    app = tmp_path/'backend'/'app'
    app.mkdir(parents=True)
    (app/'main.py').write_text('from fastapi import FastAPI\napp=FastAPI()\n@app.get("/health")\ndef health(): return {"ready":True}\n')
    async def scenario():
        token = active_workspace.set(tmp_path)
        try:
            success = await check_service('backend', '/health')
            assert success['passed'] and success['status'] == 200, success
            assert success['stopped']
            assert 'true' in success['body']
            with pytest.raises((URLError, OSError)):
                await asyncio.to_thread(probe, success['url'])
            missing = await check_service('backend', '/missing')
            assert not missing['passed'] and missing['status'] == 404
            (app/'main.py').write_text('raise RuntimeError("startup broken")\n')
            failure = await check_service('backend', '/', timeout=5)
            assert failure.get('error') and failure['stopped']
            assert 'startup broken' in failure['logs']
        finally:
            active_workspace.reset(token)
    asyncio.run(scenario())


def test_service_check_rejects_nonlocal_paths_before_startup(tmp_path):
    async def scenario():
        for path in ['https://example.com', '//example.com', '/bad\r\nHeader:x']:
            with pytest.raises(ValueError):
                await check_service('backend', path)
    asyncio.run(scenario())


def test_cancelling_service_check_stops_its_process(tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module('app.agents.tools.run_service_check')
    app = tmp_path/'backend'/'app'
    app.mkdir(parents=True)
    (app/'main.py').write_text('import time\ntime.sleep(60)\n')
    started = asyncio.Event()
    processes = []
    spawn = asyncio.create_subprocess_exec
    async def track(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        started.set()
        return process
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', track)
    async def scenario():
        token = active_workspace.set(tmp_path)
        try:
            task = asyncio.create_task(check_service('backend', '/'))
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert processes[0].returncode is not None
        finally:
            active_workspace.reset(token)
    asyncio.run(scenario())
