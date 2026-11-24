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
import sys
import tempfile
import threading
from pathlib import Path

from app.storage import StorageConflictError

# A task's own throwaway helpers (mock servers, probe scripts) live here in its
# snapshot: usable across its commands, never merged into the project.
TASK_SCRATCH = '.task-scratch'
# Where sync() leaves the project's version of a file the task also changed.
MERGED_COPIES = f'{TASK_SCRATCH}/project-versions'
# The project's runtime data (its SQLite database, uploads) lives in DATA_DIR at the
# project root, shared by every task and never copied into a snapshot or merged.
DATA_DIR = '.wdrunge-data'
LEGACY_RUNS = '.wdrunge-runs'
EXCLUDED = {'.git', LEGACY_RUNS, 'node_modules', '.venv', '__pycache__', '.pytest_cache', 'dist', 'target', '.env', '.aws', '.codex', '.agents',
            TASK_SCRATCH, DATA_DIR}
# Live database files anywhere in the project: binary state that changes while a
# service runs, so a three-way merge could only conflict or lose writes.
RUNTIME_DATA = re.compile(r'\.(db|sqlite3?|db3)(-wal|-shm|-journal)?$', re.I)
MAX_BYTES = 128 * 1024 * 1024
# A snapshot's manifest is updated by its own task (rebase, refresh) and by colleagues'
# merges (sync) from different threads; one read-modify-write at a time.
MANIFEST_LOCK = threading.Lock()
MAX_FILES = 10000
# Task snapshots live outside every project tree: tools that look upward for their
# configuration (uv and pytest for pyproject.toml, Node for node_modules, tsconfig
# "extends") must find the snapshot's files, never the project root's, which the
# sandbox lets a command see but not read. Snapshots taken before this lived in
# LEGACY_RUNS inside the project.


def runs_root() -> Path:
    """Where task snapshots are kept: WDRUNGE_RUNS_DIR, or a folder beside (not inside)
    the application data, which project commands and file tools may never touch."""
    configured = os.environ.get('WDRUNGE_RUNS_DIR')
    if configured:
        return Path(configured).expanduser().resolve()
    from platformdirs import user_data_path
    return user_data_path('Wdrunge', 'Wdrunge').resolve().with_name('Wdrunge Runs')


def project_runs(source) -> Path:
    """The snapshots folder of one project, named after it for people browsing the disk."""
    source = Path(source).resolve()
    name = re.sub(r'[^A-Za-z0-9_-]+', '-', source.name).strip('-')[:40] or 'project'
    return runs_root() / f"{name}-{hashlib.sha256(str(source).encode()).hexdigest()[:12]}"


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
# Screenshots browser_verify saves: shot-<page>-<4 or more hex of the content> (earlier: browser-<16 hex>).
SCREENSHOT = re.compile(r'^docs/testing/(?:browser-[0-9a-f]{16,32}|shot-[a-z0-9-]{1,40}-[0-9a-f]{4,16})\.png$')
# A screenshot as text cites it: its path, or its name alone (a report beside it).
CITED_SCREENSHOT = re.compile(r'(?:docs/testing/([A-Za-z0-9._-]+\.png)|(?<![A-Za-z0-9._/-])((?:browser|shot)-[A-Za-z0-9-]+\.png))')


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


def secret_leaks(changes, secrets: dict[str, str]) -> list[tuple[str, str]]:
    """Delivered files containing a credential value, with the connection it belongs to."""
    if not secrets:
        return []
    needles = [(value.encode(), name) for value, name in secrets.items()]
    found = []
    for item in changes:
        if item['content'] is None:
            continue
        name = next((name for needle, name in needles if needle in item['content']), None)
        if name:
            found.append((item['path'], name))
    return found


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


def _glob(pattern: str) -> str:
    """A .gitignore pattern as a regular expression over a slash-separated path."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == '*':
            j = i
            while j < n and pattern[j] == '*':
                j += 1
            whole = j - i >= 2 and (i == 0 or pattern[i - 1] == '/')
            if whole and pattern[j:j + 1] == '/':
                out.append('(?:.*/)?')  # "**/": any number of directories
                i = j + 1
            elif whole and j == n:
                out.append('.*')  # "/**": everything inside
                i = j
            else:
                out.append('[^/]*')
                i = j
        elif c == '?':
            out.append('[^/]')
            i += 1
        elif c == '[' and (found := _class(pattern, i)):
            out.append(found[0])
            i = found[1]
        elif c == '\\' and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    return ''.join(out)


def _class(pattern: str, start: int):
    """The bracket expression opening at `start` as (regular expression, index after
    it), or None when it never closes (the bracket is then an ordinary character)."""
    i, n = start + 1, len(pattern)
    negated = pattern[i:i + 1] in ('!', '^')
    i += negated
    members, first = [], True
    while i < n and (pattern[i] != ']' or first):
        if pattern[i] == '\\' and i + 1 < n:
            i += 1
        elif pattern[i] == '-' and members and i + 1 < n and pattern[i + 1] != ']':
            members.append('-')  # a range between its neighbours
            i += 1
            first = False
            continue
        members.append(re.escape(pattern[i]))
        i += 1
        first = False
    if i >= n:
        return None
    # A path separator is never matched by a bracket expression.
    return ('(?!/)[' + ('^' if negated else '') + ''.join(members) + ']', i + 1)


class Ignore:
    """The project's own .gitignore files. What a project keeps out of version control
    (tool caches, generated output) is not source: such paths are neither copied into
    task snapshots nor merged back. Rules apply as git applies them: per directory,
    the last matching rule wins, deeper files override, and nothing inside an ignored
    directory can be included again. Only .gitignore files are read (no global or
    .git/info excludes)."""

    def __init__(self, root):
        self.root = Path(root)
        self.loaded: dict[str, list] = {}

    def rules(self, directory: str) -> list:
        if directory in self.loaded:
            return self.loaded[directory]
        rules = self.loaded[directory] = []
        path = self.root / directory / '.gitignore'
        try:
            if path.is_symlink() or not path.is_file():
                return rules
            text = path.read_bytes()[:256 * 1024].decode('utf-8', 'replace')
        except OSError:
            return rules
        for raw in text.splitlines():
            line = raw.rstrip(' ')
            if raw != line and line.endswith('\\'):
                line += ' '  # an escaped trailing space is part of the name
            if not line or line.startswith('#'):
                continue
            negated = line.startswith('!')
            line = line[1:] if negated else line
            directory_only = line.endswith('/')
            line = line[:-1] if directory_only else line
            if not line or '' in line.removeprefix('/').split('/'):
                continue  # an empty path segment matches nothing
            # A slash at the start or in the middle ties the pattern to this directory.
            anchored = '/' in line
            try:
                regex = re.compile(('' if anchored else '(?:.*/)?') + _glob(line.removeprefix('/')))
            except re.error:
                continue
            rules.append((regex, negated, directory_only, raw.strip()))
        return rules

    def decided(self, relative: str, is_dir: bool):
        """(directory of the .gitignore, pattern) that ignores this path itself, or None."""
        parts = relative.split('/')
        verdict = None
        for depth in range(len(parts)):
            base, rest = '/'.join(parts[:depth]), '/'.join(parts[depth:])
            for regex, negated, directory_only, text in self.rules(base):
                if (is_dir or not directory_only) and regex.fullmatch(rest):
                    verdict = None if negated else (base, text)
        return verdict

    def reason(self, relative: str) -> str | None:
        """Why a path is ignored (itself or through a parent directory), or None."""
        parts = Path(relative).parts
        for end in range(1, len(parts) + 1):
            if found := self.decided('/'.join(parts[:end]), end < len(parts) or (self.root / relative).is_dir()):
                return f'it is {self.why(found)}; remove the pattern if this is a deliverable'
        return None

    @staticmethod
    def why(found) -> str:
        return f'ignored by {found[0] + "/" if found[0] else ""}.gitignore (pattern "{found[1]}")'


def unmerged_reason(path: str, ignore: Ignore | None = None) -> str | None:
    """Why a snapshot path never reaches the project, or None for an ordinary project
    file. `ignore`: the snapshot's .gitignore rules, to consult them as well."""
    parts = Path(path).parts
    name = parts[-1] if parts else ''
    if TASK_SCRATCH in parts:
        return f'{TASK_SCRATCH}/ holds task-private helpers that are never merged'
    if DATA_DIR in parts:
        return f'{DATA_DIR}/ is project runtime data that is never merged'
    if hidden := next((part for part in parts if part in EXCLUDED), None):
        return f'{hidden} is never merged (dependencies, build output, caches and secrets are excluded)'
    if name.startswith('.env.') and name not in {'.env.example', '.env.sample'}:
        return 'secret .env files are never merged; deliver a .env.example template instead'
    if RUNTIME_DATA.search(name):
        return 'live database files are runtime data that is never merged'
    if ignore is not None and parts and '..' not in parts and not Path(path).is_absolute():
        return ignore.reason(Path(path).as_posix())
    return None


def clone_tree(source: Path, target: Path):
    """Copy a directory tree to a path that does not exist yet, links kept as links.
    On macOS the files are cloned copy-on-write (APFS): a node_modules of 14,000 files
    takes under two seconds and no disk space until either copy changes. Where cloning
    is not available the data is copied."""
    if sys.platform == 'darwin':
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.copyfile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
        # copyfile(3): COPYFILE_RECURSIVE | COPYFILE_NOFOLLOW | COPYFILE_CLONE (clone, else copy).
        if libc.copyfile(os.fsencode(source), os.fsencode(target), None, 1 << 15 | 1 << 18 | 1 << 19 | 1 << 24) == 0:
            return
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, symlinks=True)


def files(root):
    result = {}
    total = 0
    ignore = Ignore(root)
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory).relative_to(root).as_posix()
        base = '' if base == '.' else base + '/'
        dirs[:] = [name for name in dirs if name not in EXCLUDED and not (Path(directory)/name).is_symlink()
                   and not ignore.decided(base + name, True)]
        for name in names:
            path = Path(directory)/name
            # Secret .env files never sync; non-secret templates are ordinary project files.
            if (name in EXCLUDED or name.startswith('.env.') and name not in {'.env.example', '.env.sample'}
                    or RUNTIME_DATA.search(name) or path.is_symlink()):
                continue
            if not path.is_file() or ignore.decided(base + name, False):
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
        self.root = project_runs(self.source)/instance
        # A snapshot taken inside the project before snapshots moved out of it; prepare()
        # moves it out when its task runs again.
        self.legacy = self.source/LEGACY_RUNS/instance
        if not (self.root/'manifest.json').exists() and (self.legacy/'manifest.json').exists():
            self.root = self.legacy
        elif self.root.is_relative_to(self.source):
            raise ValueError(f'Task snapshots ({runs_root()}) must be outside the project; set WDRUNGE_RUNS_DIR elsewhere')
        if (self.source/LEGACY_RUNS).is_symlink() or self.root.is_symlink():
            raise ValueError('Run workspace must not be a symlink')
        self.base = self.root/'base'
        self.work = self.root/'work'
        self.manifest_path = self.root/'manifest.json'

    def prepare(self, relocate=True):
        """`relocate`: move a legacy snapshot out of the project (false while a process
        still runs from it)."""
        if self.manifest_path.exists():
            if relocate and self.root == self.legacy:
                self.relocate()
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
                    # The task's own copy, not a link to the project's: package managers
                    # and bundlers write inside node_modules (caches, temporary files, a
                    # new dependency) and resolve packages from parent directories, which
                    # a read-only link into the project cannot serve. It is as the project
                    # root had it when the task started, and never merged back.
                    clone_tree(source, target)
            dirs[:] = [name for name in dirs if name not in EXCLUDED and not (Path(directory)/name).is_symlink()]
        save_json(self.manifest_path, manifest)
        return self.work

    def changed_paths(self) -> list[str]:
        """Project files this task has added, changed or deleted so far."""
        manifest = json.loads(self.manifest_path.read_text())
        working = files(self.work)
        return sorted(name for name in manifest.keys() | working.keys()
                      if hash_bytes(working[name].read_bytes() if name in working else None) != manifest.get(name))

    def missing_screenshots(self, texts) -> tuple[list[str], list[str]]:
        """Screenshot paths the texts cite that exist neither in the task's workspace nor
        in the project, and the screenshots the workspace does hold (Blog10: one mistyped
        character in a cited name cost a 24-minute rework that changed no code)."""
        cited = dict.fromkeys(f'docs/testing/{full or bare}' for text in texts for full, bare in CITED_SCREENSHOT.findall(text or ''))
        missing = [path for path in cited if not (self.work / path).is_file() and not (self.source / path).is_file()]
        folder = self.work / 'docs' / 'testing'
        present = sorted(f'docs/testing/{path.name}' for path in folder.glob('*.png')) if missing and folder.is_dir() else []
        return missing, present

    def left_out(self, limit=8) -> list[str]:
        """For whoever reviews a delivery: what the task's workspace holds that a merge
        leaves behind, and why: paths the project's .gitignore ignores, and build
        output. Not listed, because every task has them: dependency folders (the
        platform installs them at the project root), tool caches, the scratch area."""
        quiet = EXCLUDED - {'dist', 'target'}
        ignore, found = Ignore(self.work), []

        def count(directory):
            total = 0
            for _, _, names in os.walk(directory):
                total += len(names)
                if total > 2000:
                    return 'over 2000 files'
            return f'{total} file' + ('' if total == 1 else 's')
        for directory, dirs, names in os.walk(self.work, followlinks=False):
            base = Path(directory).relative_to(self.work).as_posix()
            base = '' if base == '.' else base + '/'
            kept = []
            for name in sorted(dirs):
                path = Path(directory) / name
                if name in quiet or path.is_symlink():
                    continue
                if name in EXCLUDED:
                    found.append(f'{base}{name}/ ({count(path)}): build output is never merged')
                elif rule := ignore.decided(base + name, True):
                    found.append(f'{base}{name}/ ({count(path)}): {ignore.why(rule)}')
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(names):
                if name not in quiet and (rule := ignore.decided(base + name, False)):
                    found.append(f'{base}{name}: {ignore.why(rule)}')
        return found[:limit] + ([f'… and {len(found) - limit} more ignored paths'] if len(found) > limit else [])

    def relocate(self):
        target = project_runs(self.source)/self.legacy.name
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.legacy), str(target))
        self.root, self.base, self.work, self.manifest_path = target, target/'base', target/'work', target/'manifest.json'
        try:
            (self.source/LEGACY_RUNS).rmdir()
        except OSError:
            pass  # other legacy snapshots remain

    def plan(self, managed_paths=()):
        manifest = json.loads(self.manifest_path.read_text())
        working = files(self.work)
        ignore = Ignore(self.work)
        changes = []
        for name in sorted(manifest.keys() | working.keys()):
            if RUNTIME_DATA.search(name):
                continue  # recorded by snapshots taken before such files were excluded
            if name not in working and (self.work/name).is_file() and ignore.reason(name):
                # A project file the task now ignores and still has: left as it is in
                # the project. Deleting it in the task still deletes it there.
                continue
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

    def rebase(self, changes):
        """Record merged content as this snapshot's base, so a later delivery from the
        same snapshot merges only what changed after this merge."""
        with MANIFEST_LOCK:
            self._rebase(changes)

    def _rebase(self, changes):
        manifest = json.loads(self.manifest_path.read_text())
        for item in changes:
            for target in (self.base/item['path'], self.work/item['path']):
                if item['content'] is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(item['content'])
            if item['content'] is None:
                manifest.pop(item['path'], None)
            else:
                manifest[item['path']] = hash_bytes(item['content'])
        save_json(self.manifest_path, manifest)

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
        """Bring files the project just received (a colleague's merged delivery, shared
        documents just published) into this snapshot as unchanged base files, so the
        task sees them and plan() does not treat them as its own edits. A path this task
        changed itself is never overwritten: it is returned under 'kept', and merging the
        task's version later will meet the project's."""
        result = {'pulled': [], 'kept': []}
        if not self.manifest_path.exists():
            return result
        with MANIFEST_LOCK:
            manifest = json.loads(self.manifest_path.read_text())
            ignore = Ignore(self.source)
            for name in dict.fromkeys(names):
                source = self.source/name
                if (unmerged_reason(name, ignore) or source.is_symlink() or not source.resolve().is_relative_to(self.source)
                        or source.exists() and not source.is_file()):
                    continue
                data = source.read_bytes() if source.is_file() else None
                if hash_bytes(data) == manifest.get(name):
                    continue
                local = self.work/name
                local_data = local.read_bytes() if local.is_file() else None
                if hash_bytes(local_data) != manifest.get(name) and local_data != data:
                    # Never overwrite this request's own edits; the project's version is
                    # left beside them in the task scratch area for comparison.
                    result['kept'].append(name)
                    if data is not None:
                        copy = self.work/MERGED_COPIES/name
                        copy.parent.mkdir(parents=True, exist_ok=True)
                        copy.write_bytes(data)
                    continue
                for target in (self.base/name, local):
                    if data is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                if data is None:
                    manifest.pop(name, None)
                else:
                    manifest[name] = hash_bytes(data)
                if local_data != data:
                    result['pulled'].append(name)
            save_json(self.manifest_path, manifest)
        return result

    def refresh(self):
        """Pull new dependency artifacts without overwriting this request's edits."""
        with MANIFEST_LOCK:
            self._refresh()

    def _refresh(self):
        manifest = json.loads(self.manifest_path.read_text())
        source_files = files(self.source)
        ignore = Ignore(self.source)
        for name in sorted(manifest.keys() | source_files.keys()):
            local = self.work/name
            if name not in source_files and (self.source/name).is_file() and ignore.reason(name):
                # The project still has the file and now ignores it: no longer a project
                # file to track, and the task's copy (a cache it may be using) stays.
                (self.base/name).unlink(missing_ok=True)
                manifest.pop(name, None)
                continue
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
