"""Find services running in local Docker containers and the credentials their images
were started with. Runs as the platform (outside any sandbox) and only when the user
asks; listing never returns a secret, importing reads it once into the app database.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from app.resources.kinds import TYPES

CANDIDATES = ('/usr/local/bin/docker', '/opt/homebrew/bin/docker', '/Applications/Docker.app/Contents/Resources/bin/docker')
# Image name fragment -> type, checked in order.
IMAGES = [('mariadb', 'mariadb'), ('mysql', 'mysql'), ('postgres', 'postgresql'), ('redis', 'redis'), ('mongo', 'mongodb'),
          ('rabbitmq', 'rabbitmq'), ('minio', 'minio'), ('elasticsearch', 'elasticsearch'), ('neo4j', 'neo4j')]


class DockerUnavailable(RuntimeError):
    pass


def executable() -> str:
    found = shutil.which('docker') or next((path for path in CANDIDATES if Path(path).is_file()), None)
    if not found:
        raise DockerUnavailable('没有找到 Docker。请确认已安装 Docker Desktop 并已启动。')
    return found


async def _docker(*args, timeout=15) -> str:
    process = await asyncio.create_subprocess_exec(executable(), *args, stdin=asyncio.subprocess.DEVNULL,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise DockerUnavailable('Docker 没有响应，请确认 Docker Desktop 已启动。')
    if process.returncode:
        message = err.decode(errors='replace').strip()
        if 'Cannot connect' in message or 'daemon' in message:
            raise DockerUnavailable('Docker 没有运行，请先启动 Docker Desktop。')
        raise DockerUnavailable(message[:300] or 'Docker 命令失败')
    return out.decode(errors='replace')


def kind_of(image: str) -> str:
    name = image.lower().split('@')[0].rsplit('/', 1)[-1].split(':')[0]
    return next((kind for fragment, kind in IMAGES if fragment in name), name or 'custom')


def _option(command, flag):
    for index, item in enumerate(command):
        if item == flag and index + 1 < len(command):
            return command[index + 1]
        if item.startswith(flag + '='):
            return item.split('=', 1)[1]
    return None


def credentials(kind: str, env: dict, command: list[str]) -> dict:
    """Admin account and default database from the image's documented variables."""
    if kind in {'mysql', 'mariadb'}:
        password = env.get('MARIADB_ROOT_PASSWORD') or env.get('MYSQL_ROOT_PASSWORD')
        if password:
            return {'username': 'root', 'password': password, 'database': ''}
        if env.get('MYSQL_USER'):
            return {'username': env['MYSQL_USER'], 'password': env.get('MYSQL_PASSWORD', ''), 'database': env.get('MYSQL_DATABASE', '')}
        return {'username': 'root', 'password': '', 'database': ''}
    if kind == 'postgresql':
        user = env.get('POSTGRES_USER', 'postgres')
        return {'username': user, 'password': env.get('POSTGRES_PASSWORD', ''), 'database': env.get('POSTGRES_DB', user)}
    if kind == 'redis':
        return {'username': '', 'password': env.get('REDIS_PASSWORD') or _option(command, '--requirepass') or '', 'database': ''}
    if kind == 'mongodb':
        return {'username': env.get('MONGO_INITDB_ROOT_USERNAME', ''), 'password': env.get('MONGO_INITDB_ROOT_PASSWORD', ''), 'database': ''}
    if kind == 'rabbitmq':
        return {'username': env.get('RABBITMQ_DEFAULT_USER', 'guest'), 'password': env.get('RABBITMQ_DEFAULT_PASS', 'guest'),
                'database': env.get('RABBITMQ_DEFAULT_VHOST', '')}
    if kind == 'minio':
        return {'username': env.get('MINIO_ROOT_USER') or env.get('MINIO_ACCESS_KEY') or 'minioadmin',
                'password': env.get('MINIO_ROOT_PASSWORD') or env.get('MINIO_SECRET_KEY') or 'minioadmin', 'database': ''}
    if kind == 'neo4j':
        user, _, password = (env.get('NEO4J_AUTH') or 'neo4j/').partition('/')
        return {'username': user, 'password': password, 'database': ''}
    if kind == 'elasticsearch':
        return {'username': 'elastic' if env.get('ELASTIC_PASSWORD') else '', 'password': env.get('ELASTIC_PASSWORD', ''), 'database': ''}
    return {'username': '', 'password': '', 'database': ''}


def published_port(kind: str, ports: dict) -> int | None:
    """The host port of the service's main container port, else the first published one."""
    default = TYPES.get(kind, {}).get('port')
    published = []
    for container, bindings in (ports or {}).items():
        for binding in bindings or ():
            if binding.get('HostPort', '').isdigit():
                published.append((container.split('/')[0], int(binding['HostPort'])))
    return next((host for container, host in published if default and container == str(default)),
                published[0][1] if published else None)


def describe(item: dict) -> dict:
    config = item.get('Config') or {}
    env = dict(entry.split('=', 1) for entry in config.get('Env') or () if '=' in entry)
    command = [str(part) for part in (config.get('Cmd') or [])]
    image = config.get('Image') or item.get('Image') or ''
    kind = kind_of(image)
    found = credentials(kind, env, command)
    return {'container_id': (item.get('Id') or '')[:12], 'container': (item.get('Name') or '').lstrip('/'), 'image': image,
            'type': kind, 'host': '127.0.0.1', 'port': published_port(kind, (item.get('NetworkSettings') or {}).get('Ports')),
            **found}


async def containers() -> list[dict]:
    """Running containers with what they publish; the full record includes secrets."""
    identifiers = (await _docker('ps', '-q')).split()
    if not identifiers:
        return []
    data = json.loads(await _docker('inspect', *identifiers))
    return [describe(item) for item in data]
