"""Start a task-owned local service, probe it, and always stop that process group."""
import asyncio
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.tools.validation import tool
from app.tools._workspace import resolve_allowed


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(url):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(Request(url, method='GET'), timeout=1)
    except HTTPError as error:
        response = error  # An HTTP error is still useful evidence, not success.
    with response:
        return {'status': response.code, 'body': response.read(4000).decode(errors='replace')}


async def stop_process(process):
    if process.returncode is not None:
        return
    try:
        if os.name != 'nt':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
    except ProcessLookupError:
        await process.wait()


async def check_service(service, path, timeout=20):
    if not path.startswith('/') or path.startswith('//') or any(c in path for c in '\r\n\\'):
        raise ValueError('Use an HTTP path beginning with a single slash')
    directory = resolve_allowed(service, (service,))
    required = directory / ('app/main.py' if service == 'backend' else 'package.json')
    if not required.is_file():
        return {'error': f'{service} standard entrypoint unavailable; inspect the project startup instructions.'}
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    command = (sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)) if service == 'backend' else (
        'npm', 'run', 'dev', '--', '--host', '127.0.0.1', '--port', str(port), '--strictPort')
    url = f'http://127.0.0.1:{port}{path}'
    # Test database and output never enter the delivered project snapshot.
    with tempfile.TemporaryDirectory(prefix='wdrunge-service-check-') as temporary:
        env = dict(os.environ)
        db = str(Path(temporary) / 'test.db')
        env.update(WDRUNGE_DATABASE_PATH=db, WDRUNGE_DATABASE_URL=f'sqlite+aiosqlite:///{db}')
        log_path = Path(temporary) / 'service.log'
        with log_path.open('wb') as log:
            process = await asyncio.create_subprocess_exec(*command, cwd=directory, env=env,
                stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=os.name != 'nt')
            result = {'url': url, 'error': 'Service did not become ready before timeout'}
            try:
                deadline = asyncio.get_running_loop().time() + timeout
                while asyncio.get_running_loop().time() < deadline:
                    if process.returncode is not None:
                        result = {'url': url, 'error': f'Service exited {process.returncode} during startup'}
                        break
                    try:
                        response = await asyncio.to_thread(probe, url)
                        # Do not report another service as ours after a port collision.
                        await asyncio.sleep(.1)
                        if process.returncode is None:
                            result = {'url': url, **response, 'passed': 200 <= response['status'] < 300}
                            break
                    except (URLError, OSError, TimeoutError):
                        pass
                    await asyncio.sleep(.2)
            finally:
                await stop_process(process)
        with log_path.open('rb') as log:
            log.seek(max(0, log_path.stat().st_size - 10000))
            result['logs'] = log.read().decode(errors='replace')
        return {**result, 'stopped': True, 'scope': 'temporary local HTTP smoke check; not browser or production verification'}


@tool
async def run_service_check(service: Literal['backend', 'frontend'], path: str = '/') -> dict:
    """Temporarily start standard FastAPI/Vite on an isolated localhost port, GET path,
    report HTTP status/body/startup logs, and stop only this tool's process group.
    Uses a temporary Wdrunge database. This is an HTTP smoke test, not UI interaction,
    dependency installation or deployment. Unsupported project layouts need coordination.
    """
    return await check_service(service, path)
