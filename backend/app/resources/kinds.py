"""Connection types: defaults, connection URLs, connection tests and per-project
provisioning. Any other type name is allowed (a custom service); it gets host, port,
credentials and its own fields as environment variables, and a TCP reachability test.
"""
from __future__ import annotations

import asyncio
import re
import socket
from urllib.parse import quote

# label, default port, URL scheme, SQLAlchemy driver scheme, relational database.
TYPES = {
    'mysql': {'label': 'MySQL', 'port': 3306, 'scheme': 'mysql', 'sqlalchemy': 'mysql+pymysql', 'relational': True, 'provision': True},
    'mariadb': {'label': 'MariaDB', 'port': 3306, 'scheme': 'mysql', 'sqlalchemy': 'mysql+pymysql', 'relational': True, 'provision': True},
    'postgresql': {'label': 'PostgreSQL', 'port': 5432, 'scheme': 'postgresql', 'sqlalchemy': 'postgresql+psycopg', 'relational': True, 'provision': True},
    'redis': {'label': 'Redis', 'port': 6379, 'scheme': 'redis'},
    'mongodb': {'label': 'MongoDB', 'port': 27017, 'scheme': 'mongodb'},
    'rabbitmq': {'label': 'RabbitMQ', 'port': 5672, 'scheme': 'amqp'},
    'minio': {'label': 'MinIO / S3', 'port': 9000, 'scheme': 'http'},
    'elasticsearch': {'label': 'Elasticsearch', 'port': 9200, 'scheme': 'http'},
    'neo4j': {'label': 'Neo4j', 'port': 7687, 'scheme': 'bolt'},
}
# Driver advice for agents; the platform's own environment is not the project's.
DRIVERS = {
    'mysql': 'Python: PyMySQL plus cryptography (MySQL 8 caching_sha2_password); Node: mysql2',
    'mariadb': 'Python: PyMySQL; Node: mysql2',
    'postgresql': 'Python: psycopg[binary]; Node: pg',
    'redis': 'Python: redis; Node: ioredis or redis',
    'mongodb': 'Python: pymongo; Node: mongodb',
    'rabbitmq': 'Python: pika; Node: amqplib',
    'minio': 'Python: minio or boto3; Node: minio or @aws-sdk/client-s3',
    'neo4j': 'Python: neo4j; Node: neo4j-driver',
}
LOOPBACK = {'localhost', '127.0.0.1', '::1', '0.0.0.0'}


def info(kind: str) -> dict:
    return TYPES.get(kind, {'label': kind, 'port': None, 'scheme': None})


def relational(kind: str) -> bool:
    return bool(info(kind).get('relational'))


def provisionable(kind: str) -> bool:
    """Types whose per-project database and account the platform can create itself."""
    return kind in {'mysql', 'mariadb', 'postgresql'}


def url(kind, host, port, user='', password='', database='', scheme=None) -> str | None:
    scheme = scheme or info(kind).get('scheme')
    if not scheme or not host:
        return None
    auth = ''
    if user or password:
        auth = quote(user or '', safe='') + (':' + quote(password, safe='') if password else '') + '@'
    location = f'[{host}]' if ':' in host else host
    return f"{scheme}://{auth}{location}{f':{port}' if port else ''}{'/' + quote(database, safe='') if database else ''}"


def loopback(host: str) -> bool:
    return (host or '').lower() in LOOPBACK or (host or '').startswith('127.')


# --- connection tests ------------------------------------------------------------

def _tcp(host, port, timeout=3):
    with socket.create_connection((host, port), timeout=timeout):
        pass


def _mysql(host, port, user, password, database, timeout=5):
    import pymysql
    connection = pymysql.connect(host=host, port=port, user=user, password=password or '', database=database or None,
                                 connect_timeout=timeout, read_timeout=timeout, write_timeout=timeout)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT VERSION()')
            return f'已连接，服务器版本 {cursor.fetchone()[0]}'
    finally:
        connection.close()


def _postgres(host, port, user, password, database, timeout=5):
    import psycopg
    with psycopg.connect(host=host, port=port, user=user, password=password or None, dbname=database or 'postgres',
                         connect_timeout=timeout) as connection:
        version = connection.execute('SHOW server_version').fetchone()[0]
    return f'已连接，服务器版本 {version}'


def _redis(host, port, user, password, timeout=3):
    def command(stream, *parts):
        payload = f'*{len(parts)}\r\n' + ''.join(f'${len(p.encode())}\r\n{p}\r\n' for p in parts)
        stream.sendall(payload.encode())
        return stream.recv(512).decode(errors='replace')
    with socket.create_connection((host, port), timeout=timeout) as stream:
        if password:
            reply = command(stream, 'AUTH', *([user] if user else []), password)
            if not reply.startswith('+OK'):
                raise ConnectionError(f'认证失败：{reply.strip()[:120]}')
        reply = command(stream, 'PING')
        if not reply.startswith('+PONG'):
            raise ConnectionError(reply.strip()[:120] or '没有响应')
    return '已连接（PING 正常）'


def _test(kind, host, port, user, password, database):
    if not host or not port:
        raise ValueError('需要填写地址和端口')
    if kind in {'mysql', 'mariadb'}:
        return _mysql(host, port, user, password, database)
    if kind == 'postgresql':
        return _postgres(host, port, user, password, database)
    if kind == 'redis':
        return _redis(host, port, user, password)
    _tcp(host, port)
    return '端口可以连接（这种类型只检查端口，不校验账号密码）'


def friendly(error: Exception) -> str:
    text = str(error)
    if isinstance(error, (ConnectionRefusedError, socket.timeout, TimeoutError)) or 'Errno 61' in text or "Can't connect" in text:
        return '连接不上：服务可能没有启动，或者地址、端口不对'
    if 'Access denied' in text or '1045' in text or 'password authentication failed' in text:
        return '账号或密码不正确'
    if 'cryptography' in text:
        return '服务器使用 caching_sha2_password 认证，平台缺少 cryptography 依赖'
    return text[:300] or error.__class__.__name__


async def test(kind, host, port, user='', password='', database='') -> dict:
    try:
        detail = await asyncio.wait_for(asyncio.to_thread(_test, kind, host, port, user, password, database), 12)
        return {'ok': True, 'detail': detail}
    except Exception as error:  # every failure is shown to the user, never raised
        return {'ok': False, 'detail': friendly(error)}


# --- per-project provisioning ----------------------------------------------------

def project_identifier(name: str, project_id: str) -> str:
    """Database and account name: readable, unique per project, within MySQL limits."""
    slug = re.sub(r'[^a-z0-9]+', '_', (name or '').lower()).strip('_')[:18] or 'project'
    return f"wd_{slug}_{project_id.replace('-', '')[:6]}"


def _provision_mysql(host, port, admin, admin_password, database, user, password):
    import pymysql
    connection = pymysql.connect(host=host, port=port, user=admin, password=admin_password or '', connect_timeout=5,
                                 read_timeout=10, write_timeout=10, autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
            # Host '%': containers see host connections from their bridge gateway, not
            # localhost. Written '%%' where parameters make PyMySQL %-format the statement.
            cursor.execute(f"CREATE USER IF NOT EXISTS '{user}'@'%%' IDENTIFIED BY %s", (password,))
            cursor.execute(f"ALTER USER '{user}'@'%%' IDENTIFIED BY %s", (password,))
            cursor.execute(f"GRANT ALL PRIVILEGES ON `{database}`.* TO '{user}'@'%'")
            cursor.execute('FLUSH PRIVILEGES')
    finally:
        connection.close()


def _provision_postgres(host, port, admin, admin_password, database, user, password):
    import psycopg
    from psycopg import sql
    with psycopg.connect(host=host, port=port, user=admin, password=admin_password or None, dbname='postgres',
                         connect_timeout=5, autocommit=True) as connection:
        exists = connection.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', (user,)).fetchone()
        statement = 'ALTER ROLE {} WITH LOGIN PASSWORD {}' if exists else 'CREATE ROLE {} WITH LOGIN PASSWORD {}'
        connection.execute(sql.SQL(statement).format(sql.Identifier(user), sql.Literal(password)))
        if not connection.execute('SELECT 1 FROM pg_database WHERE datname = %s', (database,)).fetchone():
            # CREATE DATABASE cannot run inside a transaction: autocommit.
            connection.execute(sql.SQL('CREATE DATABASE {} OWNER {} ENCODING {}').format(
                sql.Identifier(database), sql.Identifier(user), sql.Literal('UTF8')))
        connection.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(database)))
        connection.execute(sql.SQL('GRANT ALL PRIVILEGES ON DATABASE {} TO {}').format(sql.Identifier(database), sql.Identifier(user)))


async def provision(kind, host, port, admin, admin_password, database, user, password) -> None:
    if not re.fullmatch(r'[a-z0-9_]{1,32}', database) or not re.fullmatch(r'[a-z0-9_]{1,32}', user):
        raise ValueError('Invalid database or account name')
    if kind in {'mysql', 'mariadb'}:
        create = _provision_mysql
    elif kind == 'postgresql':
        create = _provision_postgres
    else:
        raise ValueError(f'{kind} 暂不支持自动创建项目数据库')
    await asyncio.wait_for(asyncio.to_thread(create, host, port, admin, admin_password, database, user, password), 20)
