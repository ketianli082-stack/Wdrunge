"""External services the user configures once for every project.

The user connects a service (manually or imported from Docker) in the app settings.
For each project the platform then:
- creates the project's own database and account on a MySQL/MariaDB connection,
  using the admin account it was given, so projects never share tables or root;
- passes connection details to project processes as environment variables, so code
  reads them and no agent, file, log or model prompt ever holds a password;
- gives projects without a database a SQLite file under <project>/.wdrunge-data/,
  outside task snapshots, so data is never merged as a file.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, insert, select, update

from app.resources import kinds
from app.storage import project_resources, projects, resource_connections, utcnow

NAME = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,39}$')
ENV_NAME = re.compile(r'^[A-Z_][A-Z0-9_]{0,63}$')
DATA_DIR = '.wdrunge-data'
# Variables the platform sets itself; a custom field cannot replace them.
RESERVED = {'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'VIRTUAL_ENV', 'PYTHONPATH', 'NODE_OPTIONS', 'LD_PRELOAD',
            'DYLD_INSERT_LIBRARIES', 'WDRUNGE_DATA_DIR', 'WDRUNGE_DATABASE_PATH', 'WDRUNGE_DATABASE_URL'}
# A failed preparation (service down) is retried at most this often.
RETRY_SECONDS = 60


class ExtraField(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=64)
    # None keeps the saved value of a secret field.
    value: str | None = Field(default=None, max_length=4000)
    secret: bool = False

    @field_validator('key')
    @classmethod
    def env_name(cls, value):
        if not ENV_NAME.match(value) or value in RESERVED:
            raise ValueError(f'{value} 不能作为变量名：请使用大写字母、数字和下划线，且不要使用系统变量名')
        return value


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    label: str = Field(default='', max_length=80)
    type: str = Field(min_length=1, max_length=40)
    host: str = Field(default='127.0.0.1', max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = Field(default='', max_length=200)
    # None keeps the saved password; clear_password removes it.
    password: str | None = Field(default=None, max_length=4000)
    clear_password: bool = False
    database: str = Field(default='', max_length=200)
    scheme: str = Field(default='', max_length=40)
    env_prefix: str = Field(default='', max_length=40)
    provision: bool = True
    # None: every project may use it.
    projects: list[str] | None = Field(default=None, max_length=500)
    extra: list[ExtraField] = Field(default_factory=list, max_length=30)
    note: str = Field(default='', max_length=300)

    @field_validator('name', 'type')
    @classmethod
    def slug(cls, value):
        value = value.strip().lower()
        if not NAME.match(value):
            raise ValueError('只能使用小写字母、数字、"."、"_" 或 "-"，最多 40 个字符')
        return value

    @field_validator('host')
    @classmethod
    def plain_host(cls, value):
        value = value.strip()
        if not value or re.search(r'[\s/@]', value):
            raise ValueError('地址只填主机名或 IP，例如 127.0.0.1')
        return value

    @field_validator('scheme')
    @classmethod
    def plain_scheme(cls, value):
        value = value.strip().lower()
        if value and not re.fullmatch(r'[a-z][a-z0-9+.-]{0,39}', value):
            raise ValueError('连接协议只能包含字母、数字和 + . -')
        return value

    @field_validator('env_prefix')
    @classmethod
    def prefix(cls, value):
        value = value.strip().upper()
        if value and (not re.fullmatch(r'[A-Z][A-Z0-9_]{0,30}', value) or value in {'DB', 'DATABASE', 'WDRUNGE'}):
            raise ValueError('变量前缀只能用大写字母、数字和下划线，并且不能是 DB、DATABASE 或 WDRUNGE')
        return value


def distinctive(value) -> bool:
    """A credential unlikely to occur in ordinary text: a weak password such as
    'password' or '123456' would block every delivery that mentions the word."""
    return bool(value) and len(value) >= 8 and not value.isalpha() and not value.isdigit()


def default_prefix(name: str) -> str:
    prefix = re.sub(r'[^A-Z0-9]+', '_', name.upper()).strip('_')
    return prefix if prefix and prefix[0].isalpha() else f'SVC_{prefix}'


@dataclass
class Provided:
    """What a project process receives: variables, the values to mask in its output,
    the loopback ports it may reach without general network access and the data
    directory it may write."""
    env: dict = field(default_factory=dict)
    secrets: tuple = ()
    ports: tuple = ()
    data_dir: Path | None = None
    names: tuple = ()


class Resources:
    def __init__(self, store):
        self.store = store
        self.locks: dict[str, asyncio.Lock] = {}

    # --- connections ---------------------------------------------------------------

    async def _rows(self):
        async with self.store.engine.connect() as db:
            return [dict(row) for row in (await db.execute(select(resource_connections).order_by(resource_connections.c.name))).mappings()]

    @staticmethod
    def public(row) -> dict:
        data, saved = row['data'], row['secrets']
        extras = [{'key': item['key'], 'secret': item['secret'],
                   'value': '' if item['secret'] else saved.get('extra', {}).get(item['key'], item.get('value', '')),
                   'has_value': bool(saved.get('extra', {}).get(item['key']) if item['secret'] else item.get('value'))}
                  for item in data.get('extra', [])]
        return {**data, 'id': row['id'], 'name': row['name'], 'extra': extras, 'has_password': bool(saved.get('password')),
                'env_prefix': data.get('env_prefix') or default_prefix(row['name']),
                'type_label': kinds.info(data['type'])['label'], 'can_provision': kinds.provisionable(data['type']),
                'updated_at': row['updated_at'].isoformat() if row.get('updated_at') else None}

    async def connections(self) -> list[dict]:
        return [self.public(row) for row in await self._rows()]

    async def _get(self, identifier):
        row = next((row for row in await self._rows() if row['id'] == identifier), None)
        if row is None:
            raise LookupError('连接不存在或已删除')
        return row

    def _merge(self, body: ConnectionInput, existing: dict | None):
        saved = (existing or {}).get('secrets', {}) if existing else {}
        password = '' if body.clear_password else (body.password if body.password is not None else saved.get('password', ''))
        extra_values, extras = {}, []
        for item in body.extra:
            if item.secret:
                value = item.value if item.value not in (None, '') else saved.get('extra', {}).get(item.key, '')
                extra_values[item.key] = value
                extras.append({'key': item.key, 'secret': True})
            else:
                extras.append({'key': item.key, 'secret': False, 'value': item.value or ''})
        port = body.port or kinds.info(body.type).get('port')
        data = {'label': body.label.strip() or kinds.info(body.type)['label'], 'type': body.type, 'host': body.host, 'port': port,
                'username': body.username, 'database': body.database, 'scheme': body.scheme, 'env_prefix': body.env_prefix,
                'provision': body.provision and kinds.provisionable(body.type), 'projects': body.projects,
                'extra': extras, 'note': body.note, 'source': (existing or {}).get('data', {}).get('source', {'kind': 'manual'})}
        return data, {'password': password, 'extra': extra_values}

    async def _check_unique(self, body, data, identifier):
        rows = [row for row in await self._rows() if row['id'] != identifier]
        if any(row['name'] == body.name for row in rows):
            raise ValueError(f'已经有名为 {body.name} 的连接')
        prefix = data['env_prefix'] or default_prefix(body.name)
        for row in rows:
            other = row['data'].get('env_prefix') or default_prefix(row['name'])
            if other == prefix and self._overlap(row['data'].get('projects'), data['projects']):
                raise ValueError(f'变量前缀 {prefix} 已被连接 {row["name"]} 使用，请换一个前缀')
        keys = [item['key'] for item in data['extra']]
        if len(keys) != len(set(keys)):
            raise ValueError('自定义字段的变量名不能重复')

    @staticmethod
    def _overlap(left, right):
        return left is None or right is None or bool(set(left) & set(right))

    async def save(self, body: ConnectionInput, identifier=None, source=None) -> dict:
        existing = await self._get(identifier) if identifier else None
        data, saved = self._merge(body, existing)
        if source:
            data['source'] = source
        await self._check_unique(body, data, identifier)
        now = utcnow()
        async with self.store.write() as db:
            if existing:
                await db.execute(update(resource_connections).where(resource_connections.c.id == identifier).values(
                    name=body.name, data=data, secrets=saved, updated_at=now))
            else:
                identifier = uuid4().hex
                await db.execute(insert(resource_connections).values(id=identifier, name=body.name, data=data, secrets=saved,
                                                                     created_at=now, updated_at=now))
        return self.public(await self._get(identifier))

    async def delete(self, identifier):
        await self._get(identifier)
        async with self.store.write() as db:
            await db.execute(delete(project_resources).where(project_resources.c.connection_id == identifier))
            await db.execute(delete(resource_connections).where(resource_connections.c.id == identifier))
        return {'deleted': identifier}

    async def test(self, body: ConnectionInput | None = None, identifier=None) -> dict:
        if body is None:
            row = await self._get(identifier)
            data, saved = row['data'], row['secrets']
        else:
            data, saved = self._merge(body, await self._get(identifier) if identifier else None)
        return await kinds.test(data['type'], data['host'], data['port'], data.get('username', ''), saved.get('password', ''),
                                data.get('database', ''))

    # --- Docker import -------------------------------------------------------------

    async def docker_candidates(self) -> list[dict]:
        from app.resources import docker
        imported = {row['data'].get('source', {}).get('container') for row in await self._rows()}
        result = []
        for item in await docker.containers():
            password = item.pop('password')
            result.append({**item, 'has_password': bool(password), 'imported': item['container'] in imported,
                           'type_label': kinds.info(item['type'])['label'], 'known_type': item['type'] in kinds.TYPES})
        return result

    async def docker_import(self, container: str, name: str | None = None) -> dict:
        from app.resources import docker
        item = next((c for c in await docker.containers() if container in {c['container_id'], c['container']}), None)
        if item is None:
            raise LookupError('没有找到这个正在运行的容器')
        if not item['port']:
            raise ValueError('这个容器没有把端口映射到本机，无法从本机连接')
        taken = {row['name'] for row in await self._rows()}
        base = (name or re.sub(r'[^a-z0-9_.-]', '-', item['type'])[:36].strip('-') or 'service').lower()
        chosen = base
        counter = 2
        while chosen in taken:
            chosen, counter = f'{base[:36]}-{counter}', counter + 1
        body = ConnectionInput(name=chosen, type=item['type'] if NAME.match(item['type']) else 'custom', host=item['host'],
                               port=item['port'], username=item['username'], password=item['password'], database=item['database'],
                               label=f"{kinds.info(item['type'])['label']}（Docker：{item['container']}）"[:80])
        saved = await self.save(body, source={'kind': 'docker', 'container': item['container'], 'image': item['image']})
        # Image variables only apply when a container is first created: a password
        # changed later makes the import look fine but fail, so say so at once.
        result = await self.test(identifier=saved['id'])
        if not result['ok'] and '密码' in result['detail']:
            result['detail'] += '。容器启动参数里的密码可能已被修改过，请编辑这个连接填写当前密码。'
        return {**saved, 'test': result}

    # --- projects ------------------------------------------------------------------

    async def project_by_root(self, root) -> dict | None:
        root = str(Path(root).resolve())
        async with self.store.engine.connect() as db:
            row = (await db.execute(select(projects).where(projects.c.workspace_path == root))).mappings().first()
        return dict(row) if row else None

    async def _project(self, project_id) -> dict | None:
        async with self.store.engine.connect() as db:
            row = (await db.execute(select(projects).where(projects.c.id == project_id))).mappings().first()
        return dict(row) if row else None

    async def _bindings(self, project_id):
        async with self.store.engine.connect() as db:
            rows = (await db.execute(select(project_resources).where(project_resources.c.project_id == project_id))).mappings()
            return {row['connection_id']: dict(row) for row in rows}

    async def _bind(self, project_id, connection_id, data, saved):
        async with self.store.write() as db:
            await db.execute(delete(project_resources).where(project_resources.c.project_id == project_id,
                                                             project_resources.c.connection_id == connection_id))
            await db.execute(insert(project_resources).values(project_id=project_id, connection_id=connection_id, data=data,
                                                              secrets=saved, updated_at=utcnow()))

    async def ensure(self, project_id, *, retry=False) -> list[dict]:
        """Connections available to a project with the credentials it uses, preparing
        its own database where the platform manages one. Never raises for a service
        that is down: the entry carries state 'error' and the reason."""
        project = await self._project(project_id)
        if project is None:
            return []
        lock = self.locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            rows = [row for row in await self._rows() if row['data'].get('projects') is None or project_id in row['data']['projects']]
            bindings = await self._bindings(project_id)
            entries = []
            for row in rows:
                data, saved = row['data'], row['secrets']
                entry = {'id': row['id'], 'name': row['name'], 'type': data['type'], 'label': data.get('label') or row['name'],
                         'host': data['host'], 'port': data.get('port'), 'scheme': data.get('scheme') or None,
                         'prefix': data.get('env_prefix') or default_prefix(row['name']),
                         'user': data.get('username', ''), 'password': saved.get('password', ''), 'database': data.get('database', ''),
                         'extra': [{**item, 'value': saved.get('extra', {}).get(item['key'], '') if item['secret'] else item.get('value', '')}
                                   for item in data.get('extra', [])],
                         'own_database': bool(data.get('provision')), 'state': 'ready'}
                if data.get('provision'):
                    # The admin account stays with the platform; the project gets its own.
                    entry.update(user='', password='', database='')
                    entry.update(await self._prepare(project, row, bindings.get(row['id']), retry))
                entries.append(entry)
            return entries

    async def _prepare(self, project, row, binding, retry) -> dict:
        data, saved = row['data'], row['secrets']
        version = row['updated_at'].isoformat() if row.get('updated_at') else ''
        current = (binding or {}).get('data', {})
        if current.get('state') == 'ready' and current.get('version') == version:
            return {'user': current['username'], 'password': binding['secrets']['password'], 'database': current['database'], 'state': 'ready'}
        if current.get('state') == 'error' and current.get('version') == version and not retry and time.time() - current.get('at', 0) < RETRY_SECONDS:
            return {'state': 'error', 'error': current.get('error')}
        name = current.get('database') or kinds.project_identifier(project['name'], project['id'])
        password = (binding or {}).get('secrets', {}).get('password') or secrets.token_urlsafe(18)
        try:
            await kinds.provision(data['type'], data['host'], data['port'], data.get('username', ''), saved.get('password', ''),
                                  name, name, password)
        except Exception as error:
            message = kinds.friendly(error)
            await self._bind(project['id'], row['id'], {'state': 'error', 'error': message, 'database': name, 'username': name,
                                                        'version': version, 'at': time.time()}, {'password': password})
            return {'state': 'error', 'error': message}
        await self._bind(project['id'], row['id'], {'state': 'ready', 'database': name, 'username': name, 'version': version,
                                                    'at': time.time()}, {'password': password})
        return {'user': name, 'password': password, 'database': name, 'state': 'ready'}

    # --- what project processes receive ----------------------------------------------

    @staticmethod
    def data_dir(root) -> Path:
        directory = Path(root).resolve() / DATA_DIR
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            # Runtime data is not source: keep it out of the user's version control.
            (directory / '.gitignore').write_text('*\n')
        return directory

    @staticmethod
    def assemble(entries, data_dir: Path) -> dict:
        env = {'WDRUNGE_DATA_DIR': str(data_dir)}
        ready = [entry for entry in entries if entry['state'] == 'ready']
        for entry in ready:
            link = kinds.url(entry['type'], entry['host'], entry['port'], entry['user'], entry['password'], entry['database'], entry['scheme'])
            for suffix, value in (('HOST', entry['host']), ('PORT', str(entry['port'] or '')), ('USER', entry['user']),
                                  ('PASSWORD', entry['password']), ('DATABASE', entry['database']), ('URL', link)):
                if value:
                    env[f"{entry['prefix']}_{suffix}"] = value
            for item in entry['extra']:
                if item['value']:
                    env[item['key']] = item['value']
        primary = next((entry for entry in ready if kinds.relational(entry['type'])), None)
        if primary:
            info = kinds.info(primary['type'])
            env.update(DATABASE_URL=kinds.url(primary['type'], primary['host'], primary['port'], primary['user'], primary['password'], primary['database']),
                       SQLALCHEMY_DATABASE_URL=kinds.url(primary['type'], primary['host'], primary['port'], primary['user'], primary['password'],
                                                         primary['database'], info['sqlalchemy']),
                       DB_ENGINE=primary['type'], DB_HOST=primary['host'], DB_PORT=str(primary['port']), DB_NAME=primary['database'],
                       DB_USER=primary['user'], DB_PASSWORD=primary['password'])
        else:
            path = data_dir / 'app.db'
            env.update(DATABASE_URL=f'sqlite:///{path}', SQLALCHEMY_DATABASE_URL=f'sqlite:///{path}', DB_ENGINE='sqlite',
                       DB_NAME=str(path), SQLITE_PATH=str(path))
        return {key: value for key, value in env.items() if value}

    @staticmethod
    def secret_values(entries) -> tuple:
        values = {entry['password'] for entry in entries if entry['state'] == 'ready'}
        values |= {item['value'] for entry in entries if entry['state'] == 'ready' for item in entry['extra'] if item['secret']}
        # Short values would mask ordinary output; generated and real passwords are longer.
        return tuple(sorted((value for value in values if value and len(value) >= 4), key=len, reverse=True))

    async def provided(self, root) -> Provided:
        project = await self.project_by_root(root)
        if project is None:
            return Provided()
        entries = await self.ensure(project['id'])
        data_dir = self.data_dir(project['workspace_path'])
        ports = tuple(sorted({entry['port'] for entry in entries if entry['state'] == 'ready' and entry['port']
                              and kinds.loopback(entry['host'])}))
        return Provided(self.assemble(entries, data_dir), self.secret_values(entries), ports, data_dir,
                        tuple(entry['name'] for entry in entries))

    async def known_secrets(self, project_id) -> dict[str, str]:
        """Every credential value a project could hold, by the connection it belongs to:
        its own account and the connection's (an admin password the user may also have
        pasted into a request). Reads only, so it is safe inside other transactions."""
        bindings = await self._bindings(project_id)
        found = {}
        for row in await self._rows():
            if row['data'].get('projects') is not None and project_id not in row['data']['projects']:
                continue
            values = [row['secrets'].get('password'), *row['secrets'].get('extra', {}).values(),
                      (bindings.get(row['id']) or {}).get('secrets', {}).get('password')]
            for value in values:
                if distinctive(value):
                    found[value] = row['name']
        return found

    async def names(self, project_id) -> dict[str, dict]:
        """Resources a project can use by name, without preparing anything."""
        rows = [row for row in await self._rows() if row['data'].get('projects') is None or project_id in row['data']['projects']]
        return {row['name']: {'type': row['data']['type'], 'host': row['data']['host'], 'port': row['data'].get('port')} for row in rows}

    async def brief(self, project_id) -> dict:
        """What agents are told: names, variables and state; never a value."""
        project = await self._project(project_id)
        if project is None:
            return {}
        entries = await self.ensure(project_id)
        env = self.assemble(entries, Path(project['workspace_path']) / DATA_DIR)
        resources = []
        for entry in entries:
            item = {'name': entry['name'], 'type': entry['type'], 'state': entry['state'],
                    'env': sorted(key for key in env if key.startswith(entry['prefix'] + '_'))
                           + [extra['key'] for extra in entry['extra'] if extra['key'] in env],
                    'driver': kinds.DRIVERS.get(entry['type'])}
            if entry['own_database']:
                item['database'] = entry.get('database') or None
                item['own_database'] = True
            if entry.get('error'):
                item['error'] = entry['error']
            resources.append({key: value for key, value in item.items() if value not in (None, [], '')})
        primary = env.get('DB_ENGINE', 'sqlite')
        return {
            'resources': resources,
            'database': {'engine': primary, 'env': [key for key in ('DATABASE_URL', 'SQLALCHEMY_DATABASE_URL', 'DB_ENGINE', 'DB_HOST',
                                                                    'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASSWORD', 'SQLITE_PATH') if key in env],
                         **({'name': env.get('DB_NAME')} if primary != 'sqlite' else {'file': f'{DATA_DIR}/app.db'})},
            'data_dir': f'{DATA_DIR}/ (absolute path in WDRUNGE_DATA_DIR; shared by every task, never part of a snapshot or delivery)',
            'rule': ('The platform runs these resources and passes their settings to every command and service you start as '
                     'environment variables. Read connection settings from those variables in code (e.g. '
                     "os.environ['DATABASE_URL']); never write values into files, never hard-code hosts, ports or passwords, "
                     'never start, register or declare them as services; a task that needs one simply uses it. '
                     'Keep runtime data (databases, uploads) under WDRUNGE_DATA_DIR, never in source folders. '
                     'Schema changes go through migrations registered as the "migrate" project command, which the platform '
                     'applies to the project database after delivery.'),
        }

    async def _database(self, project_id):
        """The project's data directory and its database entry (None: built-in SQLite)."""
        project = await self._project(project_id)
        if project is None:
            raise LookupError('项目不存在')
        entries = await self.ensure(project_id)
        entry = next((entry for entry in entries if entry['state'] == 'ready' and kinds.relational(entry['type'])), None)
        return self.data_dir(project['workspace_path']), entry

    async def backup_project(self, project_id) -> dict | None:
        from app.resources import backup
        data_dir, entry = await self._database(project_id)
        return await backup.backup(data_dir, entry)

    async def restore_project(self, project_id, name) -> dict:
        from app.resources import backup
        data_dir, entry = await self._database(project_id)
        return await backup.restore(data_dir, name, entry)

    async def project_status(self, project_id, *, retry=False) -> dict:
        """What the user sees for one project."""
        project = await self._project(project_id)
        if project is None:
            raise LookupError('项目不存在')
        entries = await self.ensure(project_id, retry=retry)
        from app.resources import backup
        return {'project_id': project_id, 'data_dir': str(Path(project['workspace_path']) / DATA_DIR),
                'backups': backup.list_backups(Path(project['workspace_path']) / DATA_DIR)[:5],
                'database': next((entry['name'] for entry in entries if entry['state'] == 'ready' and kinds.relational(entry['type'])), 'sqlite'),
                'resources': [{'id': entry['id'], 'name': entry['name'], 'label': entry['label'], 'type': entry['type'],
                               'state': entry['state'], 'error': entry.get('error'), 'own_database': entry['own_database'],
                               'database': entry['database'] or None, 'user': entry['user'] or None} for entry in entries]}


current: Resources | None = None


def resources() -> Resources | None:
    return current
