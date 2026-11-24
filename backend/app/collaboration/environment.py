"""Project environment registry: how this project runs, recorded by the platform.

Services (any number and kind), their ports and start commands, verified project
commands and the platform's own reserved ports. Agents refer to services by name
and get ports from the registry instead of guessing; the command reviewer treats
claimed project ports as the project's own and platform ports as never the
project's. A port counts as claimed only when it was free when the project
registered it, was observed being served by a project process, or the user set it.
"""
from __future__ import annotations

import json
import os
import platform
import re
import time
from pathlib import Path

from app.execution.environment import INSTALL_INPUTS as RUNTIME_INPUTS
from app.execution.ports import port_open

SERVICE_NAME = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,39}$')
MIGRATION = re.compile(r'migrat|迁移', re.I)
KINDS = ('http', 'tcp', 'other')
# Ports the Wdrunge platform itself serves or is used from (its API and UI dev server).
# Learned from live requests, so no number is hard-coded; WDRUNGE_RESERVED_PORTS adds more.
_platform_ports: set[int] = set()


def note_platform_port(port) -> None:
    try:
        port = int(port)
    except (TypeError, ValueError):
        return
    if 0 < port < 65536:
        _platform_ports.add(port)


class PlatformPortsMiddleware:
    """Learn the platform's own ports from live traffic: the port this API is served
    on and, for loopback browser origins, the UI dev server's port."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') in {'http', 'websocket'}:
            server = scope.get('server')
            if server and len(server) > 1 and str(server[0]) in {'127.0.0.1', 'localhost', '::1', '0.0.0.0', '::'}:
                note_platform_port(server[1])
            for key, value in scope.get('headers') or ():
                if key == b'origin':
                    match = re.match(rb'^https?://(localhost|127\.0\.0\.1|\[::1\]):(\d{1,5})$', value)
                    if match:
                        note_platform_port(match.group(2).decode())
        await self.app(scope, receive, send)


def platform_ports() -> set[int]:
    configured = set()
    for item in os.getenv('WDRUNGE_RESERVED_PORTS', '').split(','):
        if item.strip().isdigit():
            configured.add(int(item))
    return _platform_ports | configured


def host_facts() -> dict:
    system = platform.system()
    return {'os': {'Darwin': 'macOS', 'Linux': 'Linux', 'Windows': 'Windows'}.get(system, system),
            'userland': {'Darwin': 'BSD', 'Linux': 'GNU'}.get(system, 'unknown')}


def service_url(service: dict) -> str | None:
    if service.get('base_url'):
        return service['base_url']
    if service.get('kind', 'http') == 'http' and service.get('port'):
        return f"http://{service.get('host') or '127.0.0.1'}:{service['port']}"
    return None


def trusted(service: dict) -> bool:
    return bool(service.get('port')) and (service.get('status') == 'verified' or service.get('claimed')
                                          or service.get('source') == 'user')


# --- manifest discovery -------------------------------------------------------

SKIP = {'node_modules', '.venv', '.git', 'dist', 'build', 'target', '.wdrunge-runs', '.wdrunge-data', '__pycache__'}


def _candidates(root: Path, depth=2):
    stack = [(root, 0)]
    while stack:
        directory, level = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or entry.name in SKIP:
                continue
            if entry.is_dir() and level < depth and not entry.name.startswith('.'):
                stack.append((entry, level + 1))
            elif entry.is_file() and entry.stat().st_size < 512 * 1024:
                yield entry


def discover(root: str) -> list[dict]:
    """Ports declared by the project's own manifests. Declarations, not facts."""
    root_path = Path(root)
    found = []
    def add(name, port, path, detail, command=None):
        if 0 < port < 65536:
            found.append({'name': re.sub(r'[^a-z0-9_.-]', '-', name.lower())[:40].strip('-') or 'service', 'port': port,
                          'source_file': str(path.relative_to(root_path)), 'detail': detail[:200], 'command': command})
    for path in _candidates(root_path):
        name = path.name
        try:
            text = path.read_text(errors='replace')
        except OSError:
            continue
        folder = path.parent.name if path.parent != root_path else root_path.name
        if name == 'package.json':
            try:
                scripts = json.loads(text).get('scripts', {}) or {}
            except (ValueError, AttributeError):
                continue
            for script, line in scripts.items() if isinstance(scripts, dict) else ():
                match = re.search(r'--port[= ](\d{2,5})', str(line))
                if match:
                    add(folder, int(match.group(1)), path, f'scripts.{script}: {line}',
                        {'argv': ['npm', 'run', script], 'cwd': str(path.parent.relative_to(root_path))})
        elif re.fullmatch(r'vite\.config\.(js|ts|mjs|cjs|mts)', name):
            match = re.search(r'\bport\s*:\s*(\d{2,5})', text)
            if match:
                add(folder, int(match.group(1)), path, 'vite server.port')
        elif re.fullmatch(r'(docker-)?compose(\.[\w-]+)?\.ya?ml', name):
            service = None
            for line in text.splitlines():
                header = re.match(r'^  ([A-Za-z0-9_.-]+):\s*$', line)
                if header:
                    service = header.group(1)
                mapping = re.match(r'^\s*-\s*["\']?(?:[\d.]+:)?(\d{2,5}):\d{2,5}', line)
                if mapping and service:
                    add(service, int(mapping.group(1)), path, f'compose service {service}')
        elif name in {'.env.example', '.env.sample'} or name == 'Procfile':
            for line in text.splitlines():
                match = re.match(r'^\s*([A-Za-z_]*PORT)\s*=\s*(\d{2,5})\s*$', line)
                if match:
                    key = match.group(1).lower().removesuffix('_port').removesuffix('port').strip('_')
                    add(key or folder, int(match.group(2)), path, f'{match.group(1)}={match.group(2)}')
    return found


# --- commands the project root must be able to run ---------------------------

# The dependency folders the platform builds itself at the project root, in the
# command's cwd, from that directory's manifests (processes.prepare_root).
DEPENDENCY_FOLDERS = {'.venv': ('python', 'pyproject.toml or requirements.txt'),
                      'node_modules': ('node', 'package.json')}
# What a root installation reads; a change to any of them calls for a new check.
INSTALL_INPUTS = tuple(name for names in RUNTIME_INPUTS.values() for name in names)


def _manifest_dirs(workspace: Path, runtime: str) -> list[str]:
    names = {'python': ('pyproject.toml', 'requirements.txt'), 'node': ('package.json',)}[runtime]
    return sorted({str(path.parent.relative_to(workspace)) for path in _candidates(workspace) if path.name in names})[:5]


def placement_problem(argv, cwd, workspace) -> str | None:
    """Why a command that runs in `workspace` (a task snapshot) cannot run at the project
    root once the task's files are merged, or None. Dependency folders and build output
    are never merged; the root gets a .venv or node_modules only where the platform
    installs one, in the command's cwd from a manifest there."""
    from app.collaboration.workspaces import Ignore, unmerged_reason
    from app.execution.sandbox import resolve_executable
    workspace = Path(workspace).resolve()
    cwd = os.path.normpath(cwd or '.')
    directory = workspace / cwd
    executable = argv[0]
    if Path(executable).is_absolute():
        if Path(os.path.normpath(executable)).is_relative_to(workspace) or Path(executable).resolve().is_relative_to(workspace):
            return (f'{executable} is an absolute path into your task snapshot, which does not exist at the project root. '
                    f'Give the executable relative to cwd (e.g. ".venv/bin/python") or by name.')
        return None
    found = resolve_executable(executable, directory) if directory.is_dir() else None
    if not found or not Path(os.path.abspath(found)).is_relative_to(workspace):
        return None
    relative = Path(os.path.abspath(found)).relative_to(workspace)
    reason = unmerged_reason(relative.as_posix(), Ignore(workspace))
    if not reason:
        return None
    for folder, (runtime, manifest) in DEPENDENCY_FOLDERS.items():
        if folder not in relative.parts:
            continue
        index = relative.parts.index(folder)
        owner = Path(*relative.parts[:index]).as_posix() if index else '.'
        if owner == cwd and runtime in _runtimes(directory):
            return None
        where = '' if Path(executable) == relative else f' runs from {relative}, which is'
        lead = (f'{executable}{where} inside {folder}/, which is never merged: the project root gets a {folder} only '
                f'where the platform installs one, in the command\'s cwd from the {manifest} there. ')
        if owner != cwd:
            inner = Path(*relative.parts[index:]).as_posix()
            return lead + (f'This {folder} is in "{owner}", not in cwd "{cwd}": register the command with cwd="{owner}" '
                           f'and argv[0]="{inner}"' + ('' if runtime in _runtimes(workspace / owner) else
                                                        f', and keep {manifest} in "{owner}"') + '.')
        places = [place for place in _manifest_dirs(workspace, runtime) if place != cwd]
        return lead + (f'cwd "{cwd}" has no {manifest}. Keep the manifest in the directory the command runs in'
                       + (f' (found in: {", ".join(places)}; set cwd to that directory and create its {folder} there)' if places else '')
                       + '.')
    return (f'{executable} runs from {relative}: {reason}, so it does not exist at the project root after the merge and the '
            'platform does not build it. Register a command that runs from merged source files or builds first.')


def _runtimes(directory: Path) -> set:
    from app.execution.environment import inspect_environment
    try:
        return {item['type'] for item in inspect_environment(directory)['runtimes']}
    except (OSError, ValueError):
        return set()


def install_fingerprint(argv, cwd, workspace) -> str:
    """Identifies what a root check of this command depends on: the command and the
    installation inputs in its cwd."""
    import hashlib
    digest = hashlib.sha256(json.dumps([list(argv), os.path.normpath(cwd or '.')]).encode())
    directory = Path(workspace) / (cwd or '.')
    for name in INSTALL_INPUTS:
        path = directory / name
        digest.update(name.encode() + (path.read_bytes() if path.is_file() else b'-'))
    return digest.hexdigest()[:24]


# Registered commands a delivery is verified with, by name: test, backend-test,
# frontend-build, build.web ...
VERIFICATION = re.compile(r'(^|[-_.])(tests?|build)($|[-_.])')
VERIFICATION_TIMEOUT = 600


def verification_line(result: dict) -> str:
    """One check as the reviewer reads it."""
    if result.get('uncovered'):
        return ('no registered test or build command covers the files this task changed in '
                + ', '.join(name + '/' for name in result['uncovered']) + ', so the platform ran none for them')
    head = f"{result['name']} ({' '.join(result['argv'])[:120]}, cwd {result['cwd']}): "
    if result['ok'] is None:
        return head + result['note']
    if result['ok']:
        return head + f"passed (exit 0, {result.get('seconds')}s, run by the platform at delivery)"
    status = 'timed out' if result.get('timed_out') else f"exit {result.get('exit_code')}"
    return head + f"FAILED ({status}, run by the platform at delivery); output ends: {(result.get('output') or '')[-300:]}"


async def service_runtime(root, service) -> dict:
    """Whether a registry service is serving now. `status: verified` only records that
    it once served; this is the live state."""
    from app.execution.processes import processes
    job = processes.service_job(root, name=service['name'], port=service.get('port'))
    if job:
        return job.runtime()
    if old := processes.restarting(root, name=service['name'], port=service.get('port')):
        # Between the old copy stopping and the new one serving: not stopped.
        return {'state': 'restarting', 'process_id': old.id, 'started_by': old.started_by,
                'note': 'the platform is starting it again from the project root; it serves again within seconds'}
    if service.get('port') and await port_open(service['port'], timeout=.2):
        return {'state': 'listening', 'note': 'the port answers, but no team process serves it'}
    return {'state': 'stopped'}


def runtime_label(runtime: dict) -> str:
    if runtime['state'] == 'restarting':
        return ('restarting: the platform is starting it again from the project root and it serves again '
                'within seconds; no action needed, do not start it yourself')
    if runtime['state'] != 'running':
        return runtime['state']
    label = f"yes (process {runtime['process_id']}, started by {runtime.get('started_by') or 'a task'}"
    label += ', kept after its task' if runtime.get('kept') else ''
    changes = ', '.join(runtime.get('stale', [])[:3])
    # Advise a restart only where one would load the newer files: a copy in a task
    # snapshot sees that snapshot alone, and the platform moves or restarts it itself.
    if runtime.get('relaunch') == 'pending':
        label += ', being restarted from the project root by the platform; no action needed'
    elif runtime.get('from_snapshot') and runtime.get('relaunch') != 'failed':
        label += ", running from its task's snapshot until that task delivers, then from the project root"
        if changes:
            label += f"; it cannot see newer project changes in {changes} before then, and restarting it would not change that"
    elif changes:
        label += f", STALE: newer project changes in {changes} — restart it with keep_running=true"
    return label + ')'


async def connected_brief(project) -> dict | None:
    """Connected services and the project database, for every agent handoff."""
    from app.resources import service
    if service.current is None:
        return None
    try:
        return await service.current.brief(project) or None
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Could not describe connected services', exc_info=True)
        return None


class EnvironmentMixin:
    """Methods of Collaboration; records live in collaboration_records."""

    async def environment(self, project, *, discover_manifests=True) -> dict:
        if discover_manifests:
            await self.discover_services(project)
        async with self.reading() as c:
            root = (await self._project(c, project))['workspace_path']
            services = await self._list(c, 'service', project)
            commands = await self._list(c, 'command', project)
        services = [{**s, 'runtime': await service_runtime(root, s)} for s in sorted(services, key=lambda s: s['name'])]
        return {'host': host_facts(), 'reserved_ports': sorted(platform_ports()), 'services': services,
                'commands': sorted(commands, key=lambda s: s['name'])}

    async def environment_brief(self, project) -> dict:
        """Compact form for every agent handoff."""
        data = await self.environment(project)
        services = [{key: value for key, value in {
            'name': s['name'], 'kind': s.get('kind'), 'url': service_url(s), 'port': s.get('port'), 'status': s.get('status'),
            'running': runtime_label(s['runtime']),
            'owner': s.get('owner'), 'start': ' '.join(s['command']['argv']) if s.get('command') else None,
            'cwd': (s.get('command') or {}).get('cwd'), 'note': s.get('note')}.items() if value} for s in data['services'][:20]]
        commands = [{'name': c['name'], 'run': ' '.join(c['argv']), 'cwd': c.get('cwd', '.'), 'status': c.get('status')}
                    for c in data['commands'][:15]]
        return {'host': data['host'], 'reserved_ports': data['reserved_ports'], 'services': services, 'commands': commands,
                **({'connected': connected} if (connected := await connected_brief(project)) else {}),
                'rule': 'Use these service ports and commands. Never use reserved ports. Register a new service with '
                        'collaboration_register_service to get a free port; refer to services by name. A running '
                        'service is already serving the project: use it instead of starting another copy; restart '
                        'it (keep_running=true) only when you changed its code or it is marked stale.'}

    async def connected_service(self, project, name=None, port=None) -> str | None:
        """The connected service (app.resources) a service name or loopback port refers
        to: the platform provides it, so it is never registered, declared or started."""
        from app.resources import kinds, service
        if service.current is None:
            return None
        try:
            names = await service.current.names(project)
        except Exception:
            return None
        if name in names:
            return name
        return next((key for key, item in names.items() if port and item['port'] == port and kinds.loopback(item['host'])), None)

    async def migration_for(self, project, paths) -> dict | None:
        """The registered migration command a merge of `paths` calls for: the command
        named "migrate" (or whose purpose is migrating), when a merged path is under its
        directory."""
        async with self.reading() as c:
            commands = await self._list(c, 'command', project)
        command = next((c for c in commands if c['name'] == 'migrate'), None) or next(
            (c for c in commands if MIGRATION.search(c.get('purpose') or '')), None)
        if command is None:
            return None
        base = (command.get('cwd') or '.').strip('/')
        if base in {'', '.'} or any(path == base or path.startswith(base + '/') for path in paths):
            return command
        return None

    async def apply_migration(self, project, request_id, root, work, command, conversation='') -> bool:
        """Run the project's migration command at the project root after a merge, with a
        backup of the project database first. False (and the Leader is told) when it fails:
        services then keep running the code they had."""
        from types import SimpleNamespace
        from app.agents.tools._workspace import active_workspace, source_workspace
        from app.execution import sandbox
        from app.execution.processes import processes
        from app.resources import kinds, service
        root, cwd = Path(root).resolve(), command.get('cwd') or '.'
        note = ''
        if service.current is not None:
            try:
                made = await service.current.backup_project(project)
                note = f"迁移前已备份数据库（{made['name']}），可在「服务连接」中恢复。" if made else ''
            except Exception as error:
                note = f'迁移前没能备份数据库：{kinds.friendly(error)}。'
        tokens = active_workspace.set(root), source_workspace.set(root)
        cause = ''
        try:
            problem = await processes.prepare_root(SimpleNamespace(argv=command['argv'], cwd=str(Path(work) / cwd)), root, cwd)
            if problem:
                cause, result = '迁移前准备项目根目录的运行环境失败', {'exit_code': None, 'output': problem}
            else:
                # A command a task already ran successfully was authorized there; replaying it
                # is the platform's step, not a new choice by a model.
                result = await processes.run(
                    command['argv'], cwd, 600, name='migrate', network=sandbox.capability().get('backend') != 'seatbelt',
                    reason='任务交付已合并：平台在项目根目录对项目数据库执行登记的迁移命令',
                    platform='重放任务中已验证的登记迁移命令' if command.get('status') in {'verified', 'ran_in_task'} else None)
        except Exception as error:
            cause, result = f'{type(error).__name__}: {error}', {'exit_code': None, 'output': ''}
        finally:
            source_workspace.reset(tokens[1])
            active_workspace.reset(tokens[0])
        if result.get('exit_code') == 0 and not result.get('timed_out'):
            await self.register_command(project, 'platform', command['name'], command['argv'], cwd, command.get('purpose'), verified=True)
            return True
        if not cause:
            cause = '执行超时（600 秒）' if result.get('timed_out') else f"命令退出码 {result.get('exit_code')}"
        output = str(result.get('output') or '').strip()[-1500:]
        async with self.transaction() as c:
            await self._event(c, project, 'leader', {
                'type': 'migration_failed', 'request_id': request_id, 'cause': cause[:500],
                'cwd': str(root / cwd), 'argv': command['argv'],
                'summary': (f"任务 {request_id} 的交付已合并，但迁移命令 {' '.join(command['argv'])} "
                            f"在 {root / cwd} 执行失败。原因：{cause[:500]}\n"
                            + (f'输出末尾：\n{output}\n' if output else '（命令没有输出）\n')
                            + f'{note}服务没有切换到新代码。先按上面的原因判断是代码问题还是环境/审批问题，'
                            '只有代码问题才需要让后端修复迁移并重新交付。')}, conversation)
        return False

    async def report_service_failure(self, job, detail, stage='start'):
        """A kept service could not restart from the project root after a delivery:
        the Leader hears about it instead of finding the service silently gone, and
        whether a member has anything to fix. `stage`: 'install' (the platform could not
        install dependencies at the root) or 'start' (it did not serve from there)."""
        from sqlalchemy import text
        async with self.reading() as c:
            row = (await c.execute(text('SELECT id FROM projects WHERE workspace_path=:w'), {'w': job.scope})).first()
            record = await self._get(c, 'service', row[0], job.service, False) if row and job.service else None
        if not row:
            return
        name = job.service or job.name
        if stage == 'install':
            # The check before the merge installed from these same manifests in a clean
            # copy: what fails at the root now is the root's environment, not the delivery.
            try:
                cwd = os.path.relpath(job.cwd, job.workspace)
                checked = bool(record) and record.get('root_checked') == install_fingerprint(job.argv, cwd, job.scope)
            except (OSError, ValueError):
                checked = False
            advice = ('这是项目根目录的环境问题，不是交付内容的问题：同一份清单在合并前已经在干净副本里安装成功。'
                      '不要因此退回任务，也不要让成员改代码或手动复制依赖目录；平台在之后每次交付或服务重启时会再安装。'
                      '同样的失败反复出现时请告诉用户。' if checked else
                      '先看上面的输出再决定：清单或锁文件本身的错误（依赖不存在、锁文件和清单不一致）才需要负责的成员改清单后重新交付；'
                      '网络、文件占用、权限这类是环境问题，平台在之后每次交付或服务重启时会再安装，不需要成员改代码。'
                      '不要让成员手动复制依赖目录。')
        else:
            advice = '根目录的依赖已按清单安装，问题在启动：请按上面的信息让负责的成员修复，使项目在根目录可以启动；不要让成员手动复制依赖目录。'
        async with self.transaction() as c:
            await self._event(c, row[0], 'leader', {
                'type': 'service_attention', 'service': name, 'request_id': job.request, 'stage': stage,
                'summary': f'服务 {name} 在交付后没能从项目根目录重新启动：{detail[:1500]}\n{advice}'},
                job.conversation or '')

    async def trusted_ports(self, project) -> dict[int, str]:
        async with self.reading() as c:
            services = await self._list(c, 'service', project)
        return {s['port']: s['name'] for s in services if trusted(s)}

    async def _claimable(self, port, services, name) -> str | None:
        """Why this port cannot be claimed for `name`, or None."""
        if port in platform_ports():
            return f'port {port} is reserved by the Wdrunge platform'
        owner = next((s['name'] for s in services if s.get('port') == port and s['name'] != name), None)
        if owner:
            return f'port {port} is already registered for service {owner}'
        return None

    async def register_service(self, project, actor, name, kind='http', port=None, host='127.0.0.1', base_url=None,
                               health_path=None, argv=None, cwd='.', note=None, source=None, task=None, workspace=None):
        """Create or update a service. Without a port, keep its port or allocate a free one.
        `task`/`workspace`: the running task registering it and its snapshot, where the
        start command is checked against what the project root will have."""
        if not SERVICE_NAME.match(name):
            raise ValueError('Service name must be 1-40 lowercase letters, digits, ".", "_" or "-"')
        if argv and workspace and (problem := placement_problem(argv, cwd, workspace)):
            raise ValueError(f'Service {name} not registered: {problem}')
        if kind not in KINDS:
            raise ValueError(f'kind must be one of {KINDS}')
        if connected := await self.connected_service(project, name, port):
            raise ValueError(f'{connected} is a service the platform connects for this project (see environment.connected): '
                             'it is already running and its settings reach every command as environment variables. Do not '
                             'register, start or declare it; just use it.')
        async with self.reading() as c:
            await self._project(c, project)
            services = await self._list(c, 'service', project)
        existing = next((s for s in services if s['name'] == name), None)
        claimed = existing.get('claimed', False) if existing else False
        warning = None
        if port is None:
            port = existing.get('port') if existing else None
        if port is None:
            port = await self.free_port(project, services, name, kind)
            claimed = True
        elif not existing or existing.get('port') != port:
            reason = await self._claimable(port, services, name)
            if reason:
                raise ValueError(f'{reason}. Omit port to allocate a free one, or choose another.')
            in_use = await port_open(port)
            claimed = not in_use
            if in_use:
                warning = (f'Port {port} is already in use by a process the project has not been seen to start; '
                           'it stays unverified until a project process is observed serving it.')
        source = source or ('user' if actor == 'user' else 'agent')
        service = {**(existing or {}), 'name': name, 'kind': kind, 'port': port, 'host': host or '127.0.0.1',
                   'base_url': base_url, 'health_path': health_path, 'owner': (existing or {}).get('owner') or actor,
                   'claimed': claimed, 'source': source if source == 'user' or not existing else existing.get('source', source),
                   'status': (existing or {}).get('status', 'declared') if existing and existing.get('port') == port else 'declared',
                   'updated': time.time(), 'updated_by': actor}
        if argv:
            service['command'] = {'argv': list(argv), 'cwd': cwd or '.'}
            service.pop('root_checked', None)
            if task:
                service['task'] = task
        if note is not None:
            service['note'] = note[:300]
        service = {key: value for key, value in service.items() if value is not None}
        async with self.transaction() as c:
            await self._put(c, 'service', project, name, service)
        return {**service, 'url': service_url(service), **({'warning': warning} if warning else {})}

    async def free_port(self, project, services, name, kind):
        taken = {s.get('port') for s in services if s['name'] != name} | platform_ports()
        start = {'http': 8100, 'tcp': 9100}.get(kind, 9600)
        for port in range(start, start + 800):
            if port not in taken and not await port_open(port, timeout=.2):
                return port
        raise RuntimeError('No free port found for the service')

    async def remove_service(self, project, actor, name):
        async with self.transaction() as c:
            existing = await self._get(c, 'service', project, name, False)
            if not existing:
                raise ValueError(f'Unknown service {name}')
            if existing.get('source') == 'user' and actor != 'user':
                raise ValueError('Only the user can remove a service the user registered')
            await c.execute(self._delete_sql(), {'k': 'service', 'p': project, 'i': name})
        return {'removed': name}

    @staticmethod
    def _delete_sql():
        from sqlalchemy import text
        return text('DELETE FROM collaboration_records WHERE kind=:k AND project=:p AND id=:i')

    async def observe_service(self, project, actor, port, process_name, argv, cwd):
        """A project process was seen serving `port`: verify the registered service,
        or record an observed one, together with the command that started it."""
        async with self.reading() as c:
            services = await self._list(c, 'service', project)
        if port in platform_ports() or await self.connected_service(project, port=port):
            return None
        service = next((s for s in services if s.get('port') == port), None)
        if service is None:
            name = re.sub(r'[^a-z0-9_.-]', '-', (process_name or 'service').lower())[:40].strip('-') or 'service'
            if any(s['name'] == name for s in services):
                name = f'{name[:33]}-{port}'
            service = {'name': name, 'kind': 'http', 'port': port, 'host': '127.0.0.1', 'owner': actor,
                       'source': 'observed', 'claimed': True}
        service.update(status='verified', verified_at=time.time(), verified_by=actor,
                       command=service.get('command') or {'argv': list(argv)[:30], 'cwd': cwd or '.'}, updated=time.time())
        async with self.transaction() as c:
            await self._put(c, 'service', project, service['name'], service)
        return service

    async def register_command(self, project, actor, name, argv, cwd='.', purpose=None, verified=False, ran=False,
                               task=None, workspace=None):
        """Status: verified (ran at the project root, or its root installation was checked
        when its task delivered), ran_in_task (ran successfully in a task snapshot only),
        declared. `task`/`workspace` as in register_service."""
        if not SERVICE_NAME.match(name):
            raise ValueError('Command name must be 1-40 lowercase letters, digits, ".", "_" or "-"')
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError('argv must be a non-empty list of strings')
        if workspace and (problem := placement_problem(argv, cwd, workspace)):
            raise ValueError(f'Command {name} not registered: {problem}')
        record = {'name': name, 'argv': list(argv)[:40], 'cwd': cwd or '.', 'purpose': (purpose or '')[:200],
                  'status': 'verified' if verified else 'ran_in_task' if ran else 'declared',
                  'updated': time.time(), 'updated_by': actor, **({'task': task} if task else {})}
        async with self.transaction() as c:
            await self._project(c, project)
            await self._put(c, 'command', project, name, record)
        return record

    async def run_verification(self, project, request, root, work) -> list[dict]:
        """Run, in the task's workspace, the registered test and build commands that
        cover what the task changed. A developer's delivery is judged on these: the
        platform's own run, not the member's account of one. Only commands that already
        ran successfully somewhere are replayed (the platform does not make a first run
        that would need the user's review); a command over unchanged code is not run."""
        import asyncio
        from app.agents.tools._workspace import active_workspace, source_workspace
        from app.collaboration.workspaces import IsolatedWorkspace
        from app.execution import sandbox
        from app.execution.processes import processes
        if not request.get('workspace_id'):
            return []
        async with self.reading() as c:
            commands = [c for c in await self._list(c, 'command', project) if VERIFICATION.search(c['name'])]
        changed = [path for path in await asyncio.to_thread(IsolatedWorkspace(root, request['workspace_id']).changed_paths)
                   if Path(path).parts[0] != 'docs']

        def covers(command, path):
            cwd = os.path.normpath(command.get('cwd') or '.')
            return cwd == '.' or path == cwd or path.startswith(cwd + '/')
        results = []
        # Said, not hidden: "every check passed" must not be read from an empty list.
        # Directories only: a file at the root is a manifest or configuration, not code
        # that a member's cases are expected to cover.
        if uncovered := sorted({Path(path).parts[0] for path in changed
                                if len(Path(path).parts) > 1 and not any(covers(command, path) for command in commands)}):
            results.append({'ok': None, 'uncovered': uncovered})
        for command in sorted(commands, key=lambda c: c['name']):
            cwd = os.path.normpath(command.get('cwd') or '.')
            if not any(covers(command, path) for path in changed):
                continue
            entry = {'name': command['name'], 'argv': command['argv'], 'cwd': cwd}
            target = str((Path(work) / cwd).resolve())
            ran_here = any(job.argv == list(command['argv']) and job.cwd == target and job.process.returncode == 0
                           and not job.timed_out for job in list(processes.jobs.values()))
            if command.get('status') not in {'verified', 'ran_in_task'} and not ran_here:
                results.append({**entry, 'ok': None, 'note': 'registered but never run successfully, so the platform did not run it'})
                continue
            if not (Path(work) / cwd).is_dir():
                results.append({**entry, 'ok': False, 'exit_code': None, 'output': f'its cwd {cwd} does not exist in the workspace'})
                continue
            tokens = active_workspace.set(Path(work)), source_workspace.set(Path(root))
            started = time.monotonic()
            try:
                result = await processes.run(
                    command['argv'], cwd, VERIFICATION_TIMEOUT, name=command['name'],
                    network=sandbox.capability().get('backend') != 'seatbelt',
                    reason='任务交付：平台在任务工作区重跑已登记的测试和构建命令',
                    platform='重放任务中已运行通过的登记命令')
            except Exception as error:
                result = {'exit_code': None, 'output': f'{type(error).__name__}: {error}'}
            finally:
                source_workspace.reset(tokens[1])
                active_workspace.reset(tokens[0])
            ok = result.get('exit_code') == 0 and not result.get('timed_out')
            results.append({**entry, 'ok': ok, 'exit_code': result.get('exit_code'), 'seconds': round(time.monotonic() - started, 1),
                            **({'timed_out': True} if result.get('timed_out') else {}),
                            **({} if ok else {'output': str(result.get('output') or '').strip()[-1500:]})})
        return results

    async def check_root_commands(self, project, request, root, work):
        """Before a task's delivery merges: every command and service start command it
        registered, and every service it keeps running, must be able to run at the project
        root, where dependency folders and build output never arrive. Its dependencies are
        installed the way the root gets them (processes.prepare_root) in a clean copy of
        the snapshot without them, and the executable must then resolve. Raises
        ValueError naming each problem; a check that passed is not repeated until the
        command or the installation inputs in its cwd change."""
        from app.collaboration.workspaces import project_runs
        from app.execution.processes import processes
        work = Path(work).resolve()
        async with self.reading() as c:
            commands = await self._list(c, 'command', project)
            services = await self._list(c, 'service', project)
        targets = [('command', item, item['argv'], item.get('cwd') or '.') for item in commands if item.get('task') == request['id']]
        targets += [('service', item, item['command']['argv'], item['command'].get('cwd') or '.') for item in services
                    if item.get('task') == request['id'] and item.get('command')]
        for job in list(processes.jobs.values()):
            if job.keep and job.process.returncode is None and Path(job.workspace) == work:
                record = next((item for item in services if item['name'] == job.service), None)
                targets.append(('service', record or {'name': job.service or job.name}, job.argv, os.path.relpath(job.cwd, work)))
        pending, seen = [], set()
        for kind, record, argv, cwd in targets:
            cwd = os.path.normpath(cwd)
            fingerprint = install_fingerprint(argv, cwd, work)
            if (tuple(argv), cwd) in seen or record.get('root_checked') == fingerprint:
                continue
            seen.add((tuple(argv), cwd))
            pending.append((kind, record, argv, cwd, fingerprint))
        if not pending:
            return
        problems = [f"{kind} {record['name']} ({' '.join(argv)[:120]}, cwd {cwd}): {problem}"
                    for kind, record, argv, cwd, _ in pending if (problem := placement_problem(argv, cwd, work))]
        if not problems:
            problems = await self._install_check(project_runs(root) / f"{request['workspace_id']}-check", work, pending)
        if problems:
            raise ValueError('Not merged: the project root could not run what this task registers or keeps running. '
                             'Dependency folders (.venv, node_modules) and build output are never merged; the platform '
                             'installs dependencies at the root from the manifests in each command\'s cwd. Checked in a '
                             'clean copy of your snapshot without them:\n- ' + '\n- '.join(problems[:6])
                             + '\nFix the manifests or the registration, then deliver again.')
        async with self.transaction() as c:
            for kind, record, _, _, fingerprint in pending:
                stored = await self._get(c, kind, project, record['name'], False)
                if stored:
                    stored['root_checked'] = fingerprint
                    if kind == 'command' and stored.get('status') != 'verified':
                        stored['status'] = 'verified'
                    await self._put(c, kind, project, record['name'], stored)

    async def _install_check(self, clean, work, pending) -> list[str]:
        import asyncio
        import shutil
        from types import SimpleNamespace
        from app.agents.tools._workspace import active_workspace, source_workspace
        from app.collaboration.workspaces import files
        from app.execution.processes import processes
        def copy():
            shutil.rmtree(clean, ignore_errors=True)
            for name, source in files(work).items():
                target = clean / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            clean.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(copy)
        tokens = active_workspace.set(clean), source_workspace.set(clean)
        problems = []
        try:
            for kind, record, argv, cwd, _ in pending:
                if not (clean / cwd).is_dir():
                    problems.append(f"{kind} {record['name']}: its cwd {cwd} has no delivered files")
                    continue
                try:
                    problem = await processes.prepare_root(SimpleNamespace(argv=argv, cwd=str(work / cwd)), clean, cwd)
                except Exception as error:
                    problem = f'{type(error).__name__}: {error}'
                if problem:
                    problems.append(f"{kind} {record['name']} ({' '.join(argv)[:120]}, cwd {cwd}): {problem}")
        finally:
            source_workspace.reset(tokens[1])
            active_workspace.reset(tokens[0])
            await asyncio.to_thread(shutil.rmtree, clean, True)
        return problems

    async def discover_services(self, project):
        """Add manifest-declared ports once; never override agent, user or verified entries."""
        async with self.reading() as c:
            root = (await self._project(c, project))['workspace_path']
            services = await self._list(c, 'service', project)
        known_ports = {s.get('port') for s in services}
        known_names = {s['name'] for s in services}
        added = []
        for item in discover(root):
            if item['port'] in known_ports or item['port'] in platform_ports():
                continue
            name = item['name'] if item['name'] not in known_names else f"{item['name'][:33]}-{item['port']}"
            service = {'name': name, 'kind': 'http', 'port': item['port'], 'host': '127.0.0.1', 'source': 'manifest',
                       'status': 'declared', 'claimed': not await port_open(item['port'], timeout=.2),
                       'note': f"declared in {item['source_file']} ({item['detail']})", 'owner': 'project', 'updated': time.time()}
            if item.get('command'):
                service['command'] = item['command']
            known_ports.add(item['port'])
            known_names.add(name)
            added.append(service)
        if added:
            async with self.transaction() as c:
                for service in added:
                    await self._put(c, 'service', project, service['name'], service)
        return added
