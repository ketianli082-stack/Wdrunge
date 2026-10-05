"""Backups of a project's database, taken by the platform before it applies a migration,
so a failed or unwanted schema change can be undone from the settings page.

Kept in <project>/.wdrunge-data/backups/ (never part of snapshots or deliveries), the
newest KEEP per project. SQLite uses its online backup API; MySQL/MariaDB a logical dump
through the project's own account. Other engines are not backed up.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from pathlib import Path

KEEP = 5
MAX_BYTES = 200 * 1024 * 1024
END = '\n-- wdrunge:end\n'
NAME = re.compile(r'^(sqlite|mysql)-\d{8}-\d{6}(-\d+)?\.(db|sql)$')


class BackupUnavailable(RuntimeError):
    pass


def folder(data_dir: Path) -> Path:
    path = data_dir / 'backups'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _target(data_dir, engine, suffix):
    stamp = time.strftime('%Y%m%d-%H%M%S')
    target = folder(data_dir) / f'{engine}-{stamp}.{suffix}'
    counter = 2
    while target.exists():
        target = folder(data_dir) / f'{engine}-{stamp}-{counter}.{suffix}'
        counter += 1
    return target


def _prune(data_dir):
    backups = sorted(folder(data_dir).iterdir(), key=lambda path: path.stat().st_mtime, reverse=True)
    for old in [path for path in backups if NAME.match(path.name)][KEEP:]:
        old.unlink(missing_ok=True)


def _sqlite_backup(source: Path, target: Path):
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as origin, sqlite3.connect(target) as copy:
        origin.backup(copy)


def _sqlite_restore(backup: Path, live: Path):
    with sqlite3.connect(f'file:{backup}?mode=ro', uri=True) as origin, sqlite3.connect(live) as copy:
        origin.backup(copy)


def _mysql_connect(entry, **options):
    import pymysql
    return pymysql.connect(host=entry['host'], port=entry['port'], user=entry['user'], password=entry['password'],
                           database=entry['database'], connect_timeout=5, charset='utf8mb4', **options)


def _mysql_backup(entry, target: Path):
    connection = _mysql_connect(entry)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables '
                           'WHERE table_schema = DATABASE()')
            if int(cursor.fetchone()[0]) > MAX_BYTES:
                raise BackupUnavailable('数据库超过 200 MB，没有自动备份')
            cursor.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
            tables = [row[0] for row in cursor.fetchall()]
            with target.open('w', encoding='utf-8') as out:
                out.write('SET FOREIGN_KEY_CHECKS=0' + END)
                for table in tables:
                    cursor.execute(f'SHOW CREATE TABLE `{table}`')
                    out.write(cursor.fetchone()[1] + END)
                    cursor.execute(f'SELECT * FROM `{table}`')
                    while rows := cursor.fetchmany(200):
                        values = ','.join('(' + ','.join(connection.literal(value) for value in row) + ')' for row in rows)
                        out.write(f'INSERT INTO `{table}` VALUES {values}' + END)
                out.write('SET FOREIGN_KEY_CHECKS=1' + END)
    finally:
        connection.close()


def _mysql_restore(entry, backup: Path):
    statements = [part for part in backup.read_text(encoding='utf-8').split(END) if part.strip()]
    connection = _mysql_connect(entry, autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET FOREIGN_KEY_CHECKS=0')
            cursor.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
            for (table,) in cursor.fetchall():
                cursor.execute(f'DROP TABLE IF EXISTS `{table}`')
            for statement in statements:
                cursor.execute(statement)
            cursor.execute('SET FOREIGN_KEY_CHECKS=1')
    finally:
        connection.close()


def _engine(entry):
    return 'sqlite' if entry is None else 'mysql' if entry['type'] in {'mysql', 'mariadb'} else None


async def backup(data_dir: Path, entry=None) -> dict | None:
    """Back up the project database: `entry` is its connected database, or None for the
    built-in SQLite file. None when there is nothing to back up."""
    engine = _engine(entry)
    if engine is None:
        raise BackupUnavailable(f"{entry['type']} 数据库暂不支持自动备份")
    if engine == 'sqlite':
        live = data_dir / 'app.db'
        if not live.is_file():
            return None
        target = _target(data_dir, 'sqlite', 'db')
        await asyncio.to_thread(_sqlite_backup, live, target)
    else:
        target = _target(data_dir, 'mysql', 'sql')
        try:
            await asyncio.wait_for(asyncio.to_thread(_mysql_backup, entry, target), 300)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    await asyncio.to_thread(_prune, data_dir)
    return describe(target)


def describe(path: Path) -> dict:
    return {'name': path.name, 'engine': path.name.split('-', 1)[0], 'size': path.stat().st_size,
            'created': path.stat().st_mtime}


def list_backups(data_dir: Path) -> list[dict]:
    path = data_dir / 'backups'
    if not path.is_dir():
        return []
    return sorted((describe(item) for item in path.iterdir() if NAME.match(item.name)), key=lambda item: item['created'], reverse=True)


async def restore(data_dir: Path, name: str, entry=None) -> dict:
    if not NAME.match(name):
        raise ValueError('备份名称无效')
    path = data_dir / 'backups' / name
    if not path.is_file():
        raise LookupError('备份不存在或已被清理')
    engine = _engine(entry)
    if path.name.split('-', 1)[0] != engine:
        raise ValueError('这个备份不属于项目当前使用的数据库')
    if engine == 'sqlite':
        await asyncio.to_thread(_sqlite_restore, path, data_dir / 'app.db')
    else:
        await asyncio.wait_for(asyncio.to_thread(_mysql_restore, entry, path), 300)
    return describe(path)
