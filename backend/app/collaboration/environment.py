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

from app.execution.ports import port_open

SERVICE_NAME = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,39}$')
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

SKIP = {'node_modules', '.venv', '.git', 'dist', 'build', 'target', '.wdrunge-runs', '__pycache__'}


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


async def service_runtime(root, service) -> dict:
    """Whether a registry service is serving now. `status: verified` only records that
    it once served; this is the live state."""
    from app.execution.processes import processes
    job = processes.service_job(root, name=service['name'], port=service.get('port'))
    if job:
        return job.runtime()
    if service.get('port') and await port_open(service['port'], timeout=.2):
        return {'state': 'listening', 'note': 'the port answers, but no team process serves it'}
    return {'state': 'stopped'}


def runtime_label(runtime: dict) -> str:
    if runtime['state'] != 'running':
        return runtime['state']
    label = f"yes (process {runtime['process_id']}, started by {runtime.get('started_by') or 'a task'}"
    label += ', kept after its task' if runtime.get('kept') else ''
    if runtime.get('stale'):
        label += f", STALE: newer project changes in {', '.join(runtime['stale'][:3])} — restart it"
    return label + ')'


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
                'rule': 'Use these service ports and commands. Never use reserved ports. Register a new service with '
                        'collaboration_register_service to get a free port; refer to services by name. A running '
                        'service is already serving the project: use it instead of starting another copy; restart '
                        'it (keep_running=true) only when you changed its code or it is marked stale.'}

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
                               health_path=None, command=None, cwd='.', note=None, source=None):
        """Create or update a service. Without a port, keep its port or allocate a free one."""
        if not SERVICE_NAME.match(name):
            raise ValueError('Service name must be 1-40 lowercase letters, digits, ".", "_" or "-"')
        if kind not in KINDS:
            raise ValueError(f'kind must be one of {KINDS}')
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
        if command:
            service['command'] = {'argv': list(command), 'cwd': cwd or '.'}
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
        if port in platform_ports():
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

    async def register_command(self, project, actor, name, argv, cwd='.', purpose=None, verified=False):
        if not SERVICE_NAME.match(name):
            raise ValueError('Command name must be 1-40 lowercase letters, digits, ".", "_" or "-"')
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError('argv must be a non-empty list of strings')
        record = {'name': name, 'argv': list(argv)[:40], 'cwd': cwd or '.', 'purpose': (purpose or '')[:200],
                  'status': 'verified' if verified else 'declared', 'updated': time.time(), 'updated_by': actor}
        async with self.transaction() as c:
            await self._project(c, project)
            await self._put(c, 'command', project, name, record)
        return record

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
