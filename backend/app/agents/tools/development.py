"""General development execution tools. All code runs through the same policy."""
import asyncio
from pathlib import Path
import platform
import re
import shutil
from typing import Annotated, Literal
from pydantic import Field

from app.agents.tools.validation import tool, ToolFailure, failure_result, command_failure
from app.agents.tools._workspace import resolve_allowed
from app.execution.processes import processes, port_open
from app.execution.permissions import connection_texts, network_targets, argv_ports, LOG_PORT_PATTERN
from app.execution.sandbox import capability, host_path
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
# A sandboxed command without network gets EPERM on any socket, loopback included;
# automatic_network sees only argv, not what a script opens itself.
SOCKET_DENIED = re.compile(r'Errno 1\]|\bEPERM\b|Operation not permitted', re.I)
SOCKET_USE = re.compile(r'socket|\.(bind|connect|listen)\(|\b(listen|connect|bind) EPERM|urlopen|HTTPConnection|'
                        r'create_connection|fetch failed|server_bind', re.I)


def socket_denied(output):
    return bool(SOCKET_DENIED.search(output) and SOCKET_USE.search(output))


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


async def registry_service(name, port):
    """The registry service a start command targets: by name, else by port."""
    from app.collaboration.tools import active
    ctx = active.get()
    if not ctx:
        return None
    try:
        services = (await ctx.service.environment(ctx.project, discover_manifests=False))['services']
    except Exception:
        return None
    return (next((s for s in services if s['name'] == name), None)
            or next((s for s in services if port and s.get('port') == port), None))


async def declared_services():
    """Services some task of the project delivers, or None outside a collaboration."""
    from app.collaboration.tools import active
    ctx = active.get()
    if not ctx:
        return None
    try:
        return await ctx.service.declared_services(ctx.project)
    except Exception:
        return None


def tag_job(job, service):
    from app.collaboration.tools import active
    ctx = active.get()
    if service:
        job.service = service['name']
    if ctx:
        job.started_by, job.request, job.conversation = ctx.member, ctx.request, ctx.conversation or None


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
                          ready_port: Annotated[int | None, Field(ge=1, le=65535)] = None,
                          keep_running: bool = False) -> dict:
    """Run an executable with arguments (string array) in cwd; no shell. Foreground
    returns exit code and output (timeout default 120 s, max 600). background=true
    starts a service (lifetime up to 1800 s) and returns a process ID; ready_port
    waits for its port. keep_running=true replaces a running copy; services a task
    declares stay running after delivery. network: omitted = automatic for services and loopback-only URLs.
    ok_exit_codes lists expected non-zero exits. Reviewed and sandboxed.
    """
    if not background and timeout is not None and timeout > 600:
        raise ValueError('Foreground timeout cannot exceed 600 seconds; use background=true for longer commands')
    if ready_port is not None and not background:
        raise ValueError('ready_port applies to background=true services')
    if keep_running and not background:
        raise ValueError('keep_running applies to background=true services')
    argv = [command, *arguments]
    if network is None:
        network = automatic_network(argv, background)
    timeout = timeout or (1800 if background else 120)
    # Expected non-zero exits are added to success, never replace it: a probe that
    # may exit 7 (closed port) also succeeds when the port answers.
    expected = (0, *(code for code in ok_exit_codes or () if code != 0))
    if background:
        target = await registry_service(name, ready_port)
        current = processes.service_job(None, name=target and target['name'], port=ready_port)
        replaced = None
        if current is not None and keep_running:
            # Restarting a service (e.g. after changing its code) replaces the running one.
            await processes.stop(current)
            replaced = current.id
            for _ in range(20):
                if ready_port is None or not await port_open(ready_port, timeout=.2):
                    break
                await asyncio.sleep(.25)
        elif current is not None:
            label = current.service or current.name
            raise ToolFailure(f'Service {label} is already running (process {current.id}, started by '
                              f'{current.started_by or "another task"}'
                              + (', kept after its task' if current.keep else '') + ').',
                              'service_unavailable', 'service_already_running',
                              f'Use the running service as it is. If you changed its code, restart it with '
                              f'keep_running=true (that replaces it), or stop it with manage_process(action="stop", '
                              f'process_id="{label}") first.', 'after_state_change')
        if ready_port is not None and await port_open(ready_port):
            raise ToolFailure(f'Port {ready_port} already accepts connections before this command started; '
                              'another program (or an earlier process of yours) holds it.',
                              'service_unavailable', 'port_in_use',
                              'Stop your own earlier process via manage_process, or start the service on a free port '
                              'and tell dependent tasks the new endpoint. Do not terminate unowned services.',
                              'after_state_change')
        declared = {port for port in argv_ports(argv) if not await port_open(port, timeout=.2)}
        job = await processes.start(argv, cwd, timeout, name, network, reason, keep=keep_running)
        tag_job(job, target)
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
                job.service = job.service or registered[0]['name']
        if replaced:
            result['replaced_process'] = replaced
        if keep_running and job.process.returncode is None:
            declared = await declared_services()
            if declared is not None and job.service not in declared:
                result['warning'] = ('It runs until your task ends: only services a task declares stay running '
                                     'after delivery, so a helper (a preview, a mock) stops with your task.')
            else:
                result['kept'] = True
                if not job.service:
                    result['warning'] = ('Kept, but it serves no registered service yet; register its port with '
                                         'collaboration_register_service so colleagues can find it.')
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
    full = processes.logs(job, 0, 128 * 1024)['output'] if job else None
    if not network and socket_denied(full if full is not None else result.get('output', '')):
        # Also when an expected exit code made the run count as a success.
        result['warning'] = ('Socket access was denied: with network=false the sandbox blocks every socket the '
                             'command opens, 127.0.0.1 included. If its code serves or connects locally, run it '
                             'again with network=true; this output does not show the code failing.')
    return compact_output(result, full)


@tool
async def manage_process(action: Literal['list', 'status', 'logs', 'stop'], process_id: str = '',
                         offset: Annotated[int, Field(ge=0)] = 0,
                         limit: Annotated[int, Field(ge=1, le=30000)] = 10000) -> dict:
    """Manage this task's processes and the project's running services. process_id
    accepts the process ID, start name or service name (newest first). Log offsets count
    bytes (next_offset); truncated=true means older bytes were evicted.
    """
    if action == 'list':
        # Finished one-off commands (ls, curl, ...) are only counted: their output was
        # already returned, and listing them all buries the services.
        listed, finished = [], 0
        owned = processes.owned_jobs()
        for job in owned:
            snapshot = job.snapshot()
            if snapshot['state'] == 'running' or job.name != 'command':
                listed.append({**snapshot, 'ports': sorted(job.ports), **({'service': job.service} if job.service else {})})
            else:
                finished += 1
        shared = [{**job.snapshot(), 'ports': sorted(job.ports), 'service': job.service, 'started_by': job.started_by}
                  for job in processes.project_services() if job not in owned]
        return {'processes': listed, 'finished_commands': finished, **({'project_services': shared} if shared else {})}
    job = project_process(process_id)
    if action == 'stop':
        return await processes.stop(job)
    return {**job.snapshot(), **(processes.logs(job, offset, limit) if action == 'logs' else {})}


def project_process(identifier):
    """An owned process, else a running project service by ID, service or process name."""
    try:
        return processes.owned(identifier)
    except ValueError:
        job = next((job for job in processes.project_services()
                    if identifier in {job.id, job.service, job.name}), None)
        if job is None:
            raise
        return job


HOST_EXECUTABLES = ('python3', 'node', 'npm', 'uv')
HOST_NOTES = {
    'Darwin': {'os': 'macOS', 'userland': 'BSD', 'notes': [
        'lsof -nP -iTCP:<port> -sTCP:LISTEN lists listeners; exit code 1 means none',
        'ls -lT shows full timestamps; GNU options such as --time-style are unsupported',
        'sed -i needs a suffix argument: sed -i "" ...',
        'localhost may resolve to ::1 before 127.0.0.1',
        '/usr/bin/python3 is an Xcode shim the sandbox blocks; run python3 from PATH (host.executables)',
        '/tmp is not writable; $TMPDIR is private to one command and deleted when it ends, '
        'so files a later call needs belong in the workspace',
        'network=false blocks every socket, 127.0.0.1 included: a script that serves or connects locally '
        '(a test server, an HTTP client) needs network=true; loopback URLs in the arguments are detected']},
    'Linux': {'os': 'Linux', 'userland': 'GNU', 'notes': [
        'ss -ltnp lists listening TCP sockets', 'lsof may not be installed',
        '/tmp and $TMPDIR are private to one command and discarded when it ends, '
        'so files a later call needs belong in the workspace',
        'network=false gives a command its own loopback: it can serve and connect to itself, but reaching '
        'project services needs network=true; loopback URLs in the arguments are detected']},
}


@tool
async def detect_environment(cwd: str = '.') -> dict:
    """Manifests, package manager, scripts, virtual environment, sandbox, host command
    conventions (BSD/GNU) and project services with their live runtime state."""
    system = platform.system()
    # An empty project has no manifests; name the interpreters commands will actually run
    # so the model does not probe for them one call at a time.
    executables = {name: shutil.which(name, path=host_path()) for name in HOST_EXECUTABLES}
    result = {**inspect_environment(resolve_allowed(cwd, ('.',))), 'sandbox': capability(),
              'host': {**HOST_NOTES.get(system, {'os': system, 'notes': []}),
                       'executables': {name: path for name, path in executables.items() if path}}}
    services = await project_services_view()
    return {**result, 'services': services} if services else result


async def project_services_view() -> list[dict]:
    from app.collaboration.tools import active
    from app.collaboration.environment import service_url
    ctx = active.get()
    if not ctx:
        return []
    try:
        services = (await ctx.service.environment(ctx.project, discover_manifests=False))['services']
    except Exception:
        return []
    return [{key: value for key, value in {
        'name': s['name'], 'url': service_url(s), 'status': s.get('status'), 'runtime': s['runtime'],
        'start': ' '.join(s['command']['argv']) if s.get('command') else None,
        'cwd': (s.get('command') or {}).get('cwd')}.items() if value} for s in services[:20]]


development_tools = (execute_command, manage_process, detect_environment)
