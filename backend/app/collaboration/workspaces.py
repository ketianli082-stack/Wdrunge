"""Per-request file snapshots and conservative three-way publication.

No git checkout or user commit is altered. Run snapshots survive blocked tasks and
conflicts. Binary conflicts, overlapping text edits, symlinks and protected paths
are never silently merged.
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from app.storage import StorageConflictError

EXCLUDED = {'.git', '.wdrunge-runs', 'node_modules', '.venv', '__pycache__', '.pytest_cache', 'dist', 'target', '.env', '.aws', '.codex', '.agents'}
MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 10000


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.journal-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


# browser_verify evidence: named by content hash, so identical screenshots share a path.
SCREENSHOT = re.compile(r'^docs/testing/browser-[0-9a-f]{16,32}\.png$')


def unreferenced_screenshots(changes, source: Path, references: str) -> list[str]:
    """New browser screenshots that no delivered text, result or evidence names: failed
    attempts and retakes stay in the task snapshot instead of the project."""
    corpus = [references]
    for item in changes:
        if item['content'] is not None and not SCREENSHOT.match(item['path']):
            try:
                corpus.append(item['content'].decode('utf-8'))
            except UnicodeDecodeError:
                pass
    text = '\n'.join(corpus)
    return [item['path'] for item in changes
            if item['content'] is not None and SCREENSHOT.match(item['path']) and not (source / item['path']).exists()
            and Path(item['path']).name not in text]


def merge_text(base: str, current: str, proposed: str) -> str:
    if proposed == base:
        return current
    if current == base or current == proposed:
        return proposed
    original = base.splitlines(keepends=True)
    def changes(value):
        lines = value.splitlines(keepends=True)
        return [(a, b, lines[x:y]) for op, a, b, x, y in difflib.SequenceMatcher(None, original, lines, autojunk=False).get_opcodes() if op != 'equal']
    left, right = changes(current), changes(proposed)
    for a, b, replacement in left:
        for x, y, other in right:
            if (a,b,replacement) == (x,y,other):
                continue
            # Boundary insertions are deliberately treated as conflicts.
            overlap = max(a,x) < min(b,y) or (a == b and x <= a <= y) or (x == y and a <= x <= b)
            if overlap:
                raise StorageConflictError('Overlapping edits require explicit reconciliation')
    unique = []
    for change in left+right:
        if change not in unique:
            unique.append(change)
    for a,b,replacement in sorted(unique, key=lambda change: (change[0],change[1]), reverse=True):
        original[a:b] = replacement
    return ''.join(original)


def files(root):
    result = {}
    total = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in EXCLUDED and not (Path(directory)/name).is_symlink()]
        for name in names:
            path = Path(directory)/name
            # Secret .env files never sync; non-secret templates are ordinary project files.
            if (name in EXCLUDED or name.startswith('.env.') and name not in {'.env.example', '.env.sample'}
                    or path.is_symlink()):
                continue
            if not path.is_file():
                continue
            total += path.stat().st_size
            if total > MAX_BYTES or len(result) >= MAX_FILES:
                raise ValueError('Workspace snapshot exceeds 128 MiB or 10,000 files; narrow the project workspace')
            result[str(path.relative_to(root))] = path
    return result


class IsolatedWorkspace:
    def __init__(self, source, instance):
        self.source = Path(source).resolve()
        if not instance.isalnum():
            raise ValueError('Invalid workspace instance')
        self.root = self.source/'.wdrunge-runs'/instance
        if (self.source/'.wdrunge-runs').is_symlink() or self.root.is_symlink():
            raise ValueError('Run workspace must not be a symlink')
        self.base = self.root/'base'
        self.work = self.root/'work'
        self.manifest_path = self.root/'manifest.json'

    def prepare(self):
        if self.manifest_path.exists():
            return self.work
        if self.base.exists() or self.work.exists():
            raise StorageConflictError('Incomplete snapshot retained for inspection; create a fresh request workspace')
        source_files = files(self.source)
        self.base.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        manifest = {}
        for name, source in source_files.items():
            data = source.read_bytes()
            for destination in (self.base/name, self.work/name):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                shutil.copymode(source, destination)
            manifest[name] = hash_bytes(data)
        for directory, dirs, _ in os.walk(self.source, followlinks=False):
            if 'node_modules' in dirs:
                source = Path(directory)/'node_modules'
                target = self.work/source.relative_to(self.source)
                if not source.is_symlink() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(source, target_is_directory=True)
            dirs[:] = [name for name in dirs if name not in EXCLUDED and not (Path(directory)/name).is_symlink()]
        # Shared dependencies are excluded from publication, and the normal file
        # tools reject writes through links outside the isolated workspace.
        save_json(self.manifest_path, manifest)
        return self.work

    def plan(self, managed_paths=()):
        manifest = json.loads(self.manifest_path.read_text())
        working = files(self.work)
        changes = []
        for name in sorted(manifest.keys() | working.keys()):
            base = (self.base/name).read_bytes() if name in manifest else None
            proposed = working[name].read_bytes() if name in working else None
            if hash_bytes(proposed) == manifest.get(name):
                continue
            if name in managed_paths:
                raise StorageConflictError(f'Managed document {name} must be updated with publish')
            target = self.source/name
            if target.is_symlink() or not target.resolve().is_relative_to(self.source):
                raise ValueError('Unsafe merge target')
            current = target.read_bytes() if target.is_file() else None
            if current == proposed:
                continue
            if current != base:
                if None in (base, current, proposed):
                    raise StorageConflictError(f'Concurrent creation or deletion: {name}')
                try:
                    proposed = merge_text(base.decode('utf-8'), current.decode('utf-8'), proposed.decode('utf-8')).encode()
                except UnicodeDecodeError as exc:
                    raise StorageConflictError(f'Binary conflict: {name}') from exc
                except StorageConflictError as exc:
                    raise StorageConflictError(f'{name}: {exc}') from exc
            changes.append({'path': name, 'expected_hash': hash_bytes(current), 'content': proposed})
        return changes

    def publish(self, changes):
        # A disk journal makes an interrupted multi-file merge recoverable.
        import base64
        journal = self.root/'merge.json'
        encoded = [{**item, 'content': base64.b64encode(item['content']).decode() if item['content'] is not None else None} for item in changes]
        save_json(journal, encoded)
        self.recover_merge()

    def recover_merge(self):
        import base64
        journal = self.root/'merge.json'
        if not journal.exists():
            return
        changes = json.loads(journal.read_text())
        decoded = []
        for item in changes:
            target = self.source/item['path']
            if not target.resolve().is_relative_to(self.source) or target.is_symlink():
                raise ValueError('Unsafe journal target')
            content = base64.b64decode(item['content']) if item['content'] is not None else None
            actual = target.read_bytes() if target.is_file() else None
            if hash_bytes(actual) not in {item['expected_hash'], hash_bytes(content)}:
                raise StorageConflictError(f"Merge recovery blocked by external change: {item['path']}")
            decoded.append((target, content))
        for target, content in decoded:
            if content is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=target.parent, prefix='.collab-merge-')
            try:
                mode = target.stat().st_mode if target.exists() else 0o644
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temp, mode)
                os.replace(temp, target)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        journal.unlink()

    def sync(self, names):
        """Copy just-published shared documents into this snapshot as unchanged
        base files, so file tools see them and plan() does not treat them as edits."""
        if not self.manifest_path.exists():
            return
        manifest = json.loads(self.manifest_path.read_text())
        for name in names:
            source = self.source/name
            if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(self.source):
                continue
            local = self.work/name
            if hash_bytes(local.read_bytes() if local.is_file() else None) != manifest.get(name):
                continue  # Never overwrite this request's own edits.
            data = source.read_bytes()
            for target in (self.base/name, local):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            manifest[name] = hash_bytes(data)
        save_json(self.manifest_path, manifest)

    def refresh(self):
        """Pull new dependency artifacts without overwriting this request's edits."""
        manifest = json.loads(self.manifest_path.read_text())
        source_files = files(self.source)
        for name in sorted(manifest.keys() | source_files.keys()):
            local = self.work/name
            local_data = local.read_bytes() if local.is_file() else None
            if hash_bytes(local_data) != manifest.get(name):
                continue
            source = source_files.get(name)
            if source:
                data = source.read_bytes()
                for target in (self.base/name, self.work/name):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                manifest[name] = hash_bytes(data)
            else:
                (self.base/name).unlink(missing_ok=True)
                local.unlink(missing_ok=True)
                manifest.pop(name, None)
        save_json(self.manifest_path, manifest)
