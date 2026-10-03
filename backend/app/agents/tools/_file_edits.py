"""Atomic text edits with a shared lock for each resolved workspace path."""
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from threading import Lock
from weakref import WeakValueDictionary

_locks = WeakValueDictionary()
_registry_lock = Lock()


def file_lock(path):
    with _registry_lock:
        lock = _locks.get(str(path))
        if lock is None:
            lock = Lock()
            _locks[str(path)] = lock
        return lock


def file_hash(data):
    return hashlib.sha256(data).hexdigest()


def update_file(target: Path, content, edits, expected_hash):
    with file_lock(target):
        if target.exists() and not target.is_file():
            raise ValueError('Not a regular file')
        before = target.read_bytes() if target.exists() else None
        if expected_hash is not None and (before is None or file_hash(before) != expected_hash):
            raise ValueError('File changed since reading; reload before retrying')
        if edits is not None:
            if before is None:
                raise ValueError('Partial edits require an existing file')
            content = before.decode('utf-8')
            for edit in edits:
                if content.count(edit.old_text) != 1:
                    raise ValueError('old_text must match exactly once; read the current file and include more context')
                content = content.replace(edit.old_text, edit.new_text, 1)
        if len(content) > 100000:
            raise ValueError('File is too large; keep content under 100000 characters')
        after = content.encode('utf-8')
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(after)
                output.flush()
                os.fsync(output.fileno())
            if before is not None:
                os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
            # Detect edits made by external writers while we prepared the file.
            current = target.read_bytes() if target.exists() else None
            if current != before:
                raise ValueError('File changed during the edit; reload before retrying')
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return len(content), file_hash(after)
