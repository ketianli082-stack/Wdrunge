"""General development execution tools. All code runs through the same policy."""
import asyncio
from pathlib import Path
import platform
import re
from typing import Annotated, Literal
from pydantic import Field

from app.agents.tools.validation import tool, ToolFailure, failure_result, command_failure
from app.agents.tools._workspace import resolve_allowed
from app.execution.processes import processes, port_open
from app.execution.permissions import connection_texts, network_targets, argv_ports, LOG_PORT_PATTERN
from app.execution.sandbox import capability
from app.execution.environment import inspect_environment


OUTPUT_LIMIT = 4000
SIGNAL = re.compile(r'error|fail|exception|traceback|warn|denied|not found|cannot|unable|✗|✘|err!', re.I)


def compact_output(result: dict, full: str | None = None) -> dict:
    """Every result is resent on later model calls: keep the head, the error and
    warning lines, and the tail. The full log stays available via manage_process."""
    output = full if full is not None else result.get('output', '')
    size = len(output.encode())
    if size <= OUTPUT_LIMIT:
        return result
    head, tail = output[:800], output[-2400:]
    middle = output[800:-2400].splitlines()
    signals, used = [], 0
    for line in dict.fromkeys(line.strip() for line in middle if SIGNAL.search(line)):
        if used + len(line) > 1200:
            break
        signals.append(line[:300])
        used += len(line)
    omitted = f'\n… [{size} bytes total; middle omitted'
    omitted += (f'; {len(signals)} error/warning lines kept:]\n' + '\n'.join(signals) + '\n…\n') if signals else ']\n'
    return {**result, 'output': head + omitted + tail, 'output_truncated': True,
            'full_log': f"manage_process(action='logs', process_id='{result.get('id')}', offset=..., limit=...)"}


STARTUP_GRACE = 1.5


def automatic_network(argv, background):
    """Background services listen on ports and local HTTP checks connect to them; both
    need network access in the OS sandbox. Non-loopback targets stay an explicit choice."""
    if background:
        return True
    targets = network_targets(connection_texts(argv))
    return bool(targets) and all(target['loopback'] for target in targets)


async def served_ports(job, declared, ready_port):
    """Ports this process is serving: the confirmed ready_port, argv ports that were
    free before it started, and ports it printed in its own log, now accepting."""
    if job.process.returncode is not None:
        return set()
    ports = {ready_port} if ready_port else set()
    log = processes.logs(job, 0, 65536)['output']
    candidates = (declared | {int(p) for p in LOG_PORT_PATTERN.findall(log) if 0 < int(p) < 65536}) - ports
    for port in candidates:
        if await port_open(port, timeout=.3):
            ports.add(port)
    job.ports.update(ports)
    return ports


async def register_observed(job, ports):
    """Record verified project services in the environment registry."""
    from app.collaboration.tools import active
    from app.agents.tools._workspace import workspace_root
    from app.collaboration.environment import service_url
    ctx = active.get()
    if not ctx:
        return []
    try:
        cwd = str(Path(job.cwd).resolve().relative_to(workspace_root().resolve())) or '.'
    except ValueError:
        cwd = '.'
    registered = []
    for port in sorted(ports):
        try:
            service = await ctx.service.observe_service(ctx.project, ctx.member, port, job.name, job.argv, cwd)
        except Exception:
            continue
        if service:
            registered.append({'name': service['name'], 'port': port, 'url': service_url(service), 'status': service['status']})
    return registered


@tool
async def execute_command(command: str, arguments: list[str], cwd: str = '.',
                          timeout: Annotated[int | None, Field(ge=1, le=1800)] = None,
                          background: bool = False, name: str = 'command',
                          network: bool | None = None, reason: str = '',
                          ok_exit_codes: Annotated[list[int] | None, Field(max_length=10)] = None,
                          ready_port: Annotated[int | None, Field(ge=1, le=65535)] = None) -> dict:
    """Run an executable with arguments (string array) in cwd; no shell. Foreground
    returns exit code and output (timeout default 120 s, max 600). background=true
    starts a service (lifetime up to 1800 s) and returns a process ID; ready_port
    waits for its port. network: omitted = automatic for services and loopback-only
    URLs. ok_exit_codes lists expected non-zero exits. Reviewed and sandboxed.
    """
    if not background and timeout is not None and timeout > 600:
        raise ValueError('Foreground timeout cannot exceed 600 seconds; use background=true for longer commands')
    if ready_port is not None and not background:
        raise ValueError('ready_port applies to background=true services')
    argv = [command, *arguments]
    if network is None:
        network = automatic_network(argv, background)
    timeout = timeout or (1800 if background else 120)
    expected = tuple(ok_exit_codes) if ok_exit_codes else (0,)
    if background:
        if ready_port is not None and await port_open(ready_port):
            raise ToolFailure(f'Port {ready_port} already accepts connections before this command started; '
                              'another program (or an earlier process of yours) holds it.',
                              'service_unavailable', 'port_in_use',
                              'Stop your own earlier process via manage_process, or start the service on a free port '
                              'and tell dependent tasks the new endpoint. Do not terminate unowned services.',
                              'after_state_change')
        declared = {port for port in argv_ports(argv) if not await port_open(port, timeout=.2)}
        job = await processes.start(argv, cwd, timeout, name, network, reason)
        if ready_port is not None:
            ready = await processes.wait_ready(job, ready_port, min(60, timeout))
        else:
            ready = None
            try:
                await asyncio.wait_for(asyncio.shield(job.watcher), STARTUP_GRACE)
            except TimeoutError:
                pass
        result = {**job.snapshot(), 'network': network, **processes.logs(job, 0, 2000)}
        if job.process.returncode is not None and (job.process.returncode not in expected or job.timed_out):
            message = f"Background process exited during startup with code {job.process.returncode}"
            return {**result, **command_failure(message, processes.logs(job, 0, 128 * 1024)['output'])}
        served = await served_ports(job, declared, ready_port if ready else None)
        if served:
            result['ports'] = sorted(served)
            registered = await register_observed(job, served)
            if registered:
                result['registered_services'] = registered
        if ready is not None:
            result['ready'] = ready
            if not ready:
                running = job.process.returncode is None
                result.update(failure_result(f'Port {ready_port} did not accept connections '
                    + (f'within {min(60, timeout)} seconds' if running else 'before the process exited'),
                    'service_unavailable', 'service_not_ready',
                    'Read manage_process logs for the listening address/port. '
                    + ('The process is still running; wait again or stop it and correct the command.' if running
                       else 'Correct the command or port before starting it again.'), 'after_service_ready'))
        return result
    result = await processes.run(argv, cwd, timeout, name=name, network=network, reason=reason, ok_exit_codes=expected)
    result['network'] = network
    job = processes.jobs.get(result.get('id'))
    # Scan the whole retained log; run() already cut the output to its last 30 KB.
    return compact_output(result, processes.logs(job, 0, 128 * 1024)['output'] if job else None)


@tool
async def manage_process(action: Literal['list', 'status', 'logs', 'stop'], process_id: str = '',
                         offset: Annotated[int, Field(ge=0)] = 0,
                         limit: Annotated[int, Field(ge=1, le=30000)] = 10000) -> dict:
    """Manage only this task workspace's processes. process_id accepts the process ID
    or the name given at start (the newest such process). Log offsets count bytes and
    are returned as next_offset. truncated=true means older log bytes were evicted.
    """
    if action == 'list':
        return {'processes': [{**job.snapshot(), 'ports': sorted(job.ports)} for job in processes.owned_jobs()]}
    job = processes.owned(process_id)
    if action == 'stop':
        return await processes.stop(job)
    return {**job.snapshot(), **(processes.logs(job, offset, limit) if action == 'logs' else {})}


HOST_NOTES = {
    'Darwin': {'os': 'macOS', 'userland': 'BSD', 'notes': [
        'lsof -nP -iTCP:<port> -sTCP:LISTEN lists listeners; exit code 1 means none',
        'ls -lT shows full timestamps; GNU options such as --time-style are unsupported',
        'sed -i needs a suffix argument: sed -i "" ...',
        'localhost may resolve to ::1 before 127.0.0.1']},
    'Linux': {'os': 'Linux', 'userland': 'GNU', 'notes': [
        'ss -ltnp lists listening TCP sockets', 'lsof may not be installed']},
}


@tool
def detect_environment(cwd: str = '.') -> dict:
    """Manifests, package manager, scripts, virtual environment, sandbox and host
    command conventions (BSD/GNU) for a directory."""
    system = platform.system()
    return {**inspect_environment(resolve_allowed(cwd, ('.',))), 'sandbox': capability(),
            'host': HOST_NOTES.get(system, {'os': system, 'notes': []})}


development_tools = (execute_command, manage_process, detect_environment)
