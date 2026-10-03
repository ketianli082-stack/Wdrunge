"""Start a task-owned local service, probe it, and always stop that process group."""
import asyncio
import socket
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.agents.tools.validation import tool, failure_result, command_failure
from app.agents.tools._workspace import resolve_allowed
from app.execution.processes import processes
from app.execution.environment import verification_python


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


async def check_service(service, path, timeout=20):
    if not path.startswith('/') or path.startswith('//') or any(c in path for c in '\r\n\\'):
        raise ValueError('Use an HTTP path beginning with a single slash')
    directory = resolve_allowed(service, (service,))
    required = directory / ('app/main.py' if service == 'backend' else 'package.json')
    if not required.is_file():
        return {**failure_result(f'{service} standard entrypoint unavailable; inspect project startup instructions.',
            'environment_unavailable', 'unsupported_project_layout',
            'Inspect project startup instructions and use an authorized execute_command background service for this layout.',
            'after_environment_change'), 'passed': False}
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    command = [str(verification_python(directory)), '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)] if service == 'backend' else [
        'npm', 'run', 'dev', '--', '--host', '127.0.0.1', '--port', str(port), '--strictPort']
    url = f'http://127.0.0.1:{port}{path}'
    job = await processes.start(command, service, timeout + 5, name=f'{service} smoke check',
        network=True, reason='Start a temporary loopback HTTP smoke test')
    result = {'url': url, **failure_result('Service did not become ready before timeout',
        'service_unavailable', 'service_not_ready', 'Inspect startup logs and dependencies, correct the startup problem, then recheck readiness.',
        'after_service_ready'), 'passed': False}
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if job.process.returncode is not None:
                result = {'url': url, **command_failure(f'Service exited {job.process.returncode} during startup',
                    processes.logs(job)['output']), 'passed': False}
                break
            try:
                response = await asyncio.to_thread(probe, url)
                await asyncio.sleep(.1)
                if job.process.returncode is None:
                    result = {'url': url, **response, 'passed': 200 <= response['status'] < 300}
                    if not result['passed']:
                        result.update(failure_result(f"HTTP check returned {response['status']}",
                            'service_unavailable' if response['status'] in {502, 503, 504} else 'verification_failed',
                            'http_error', 'Inspect the HTTP status/body and server logs; confirm the requested route and expected response before rechecking.',
                            'after_service_ready' if response['status'] in {502, 503, 504} else 'inspect_before_retry'))
                    break
            except (URLError, OSError, TimeoutError):
                pass
            await asyncio.sleep(.2)
    finally:
        await processes.stop(job)
    return {**result, 'logs': processes.logs(job, max(0, job.end-10000))['output'],
            'stopped': True, 'scope': 'sandboxed temporary HTTP smoke check; not browser verification'}


@tool
async def run_service_check(service: Literal['backend', 'frontend'], path: str = '/') -> dict:
    """Temporarily start standard FastAPI/Vite on an isolated localhost port, GET path,
    report HTTP status/body/startup logs, and stop only this tool's process group.
    Uses a temporary Wdrunge database. This is an HTTP smoke test, not UI interaction,
    dependency installation or deployment. Unsupported project layouts need coordination.
    """
    return await check_service(service, path)
