"""Bounded search/listing and explicit authorization for external file reads."""
import os
from pathlib import Path
import shutil
from itertools import chain
from typing import Annotated, Literal

from pydantic import Field
from app.agents.tools.validation import tool
from app.agents.context_policy import RECOMPUTABLE
from app.agents.tools._workspace import resolve_allowed, workspace_root, effective_roots, is_control_path, control_paths, published_documents, published_hint
from app.execution.permissions import permissions
from app.execution.file_ops import read_regular, move_no_replace

EXCLUDED = {'.git', '.venv', 'node_modules', 'dist', 'target', '__pycache__', '.wdrunge-runs', '.aws', '.ssh', '.codex', '.agents'}


def visible(path):
    return not any(part in EXCLUDED or part == '.env' or part.startswith('.env.') and part not in {'.env.example', '.env.sample'} for part in path.parts)


def walk_files(root):
    count = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if visible(Path(name)) and not (Path(directory) / name).is_symlink() and not is_control_path(Path(directory) / name))
        for name in sorted(names):
            path = Path(directory) / name
            if not visible(Path(name)) or path.is_symlink() or not path.is_file() or is_control_path(path):
                continue
            count += 1
            if count > 10000:
                raise ValueError('目录超过 10,000 个文件，请缩小检索范围')
            yield path


def make_file_management(read_roots, write_roots):
    def roots_for(path):
        paths = effective_roots(read_roots) if path == '.' else (path,)
        return [resolve_allowed(value, read_roots) for value in paths]

    @tool(context_policy=RECOMPUTABLE)
    def browse_directory(path: str = '.', offset: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=200)] = 100) -> dict:
        """Page immediate children of a project directory (including folders).
        Symlinks, credentials, build outputs and dependency trees are omitted.
        """
        roots = roots_for(path)
        if path == '.' and effective_roots(read_roots) != ('.',):
            entries = [{'name': str(root.relative_to(workspace_root().resolve())), 'type': 'directory' if root.is_dir() else 'file'}
                       for root in roots if root.exists()]
            entries.sort(key=lambda item: item['name'])
            return {'entries': entries[offset:offset+limit], 'next_offset': offset+limit if len(entries) > offset+limit else None}
        target = roots[0]
        if not target.is_dir():
            if target.exists():
                raise ValueError(f'Not a directory: {path}; read it with read_project_file')
            documents = published_documents(path)
            raise FileNotFoundError(f'Directory does not exist in this workspace: {path}'
                                    + ('.' + published_hint(documents) if documents else ''))
        entries = []
        with os.scandir(target) as children:
            for child in children:
                if len(entries) >= 10000:
                    raise ValueError('目录过大，请选择更小的目录')
                if visible(Path(child.name)) and not child.is_symlink() and not is_control_path(Path(child.path)):
                    entries.append({'name': child.name, 'type': 'directory' if child.is_dir() else 'file'})
        entries.sort(key=lambda item: (item['type'] != 'directory', item['name']))
        return {'entries': entries[offset:offset+limit], 'next_offset': offset+limit if len(entries) > offset+limit else None}

    @tool(context_policy=RECOMPUTABLE)
    def search_code(query: Annotated[str, Field(min_length=1, max_length=500)], path: str = '.',
                    offset: Annotated[int, Field(ge=0)] = 0,
                    limit: Annotated[int, Field(ge=1, le=200)] = 50) -> dict:
        """Literal code search with line numbers and pagination. Skips symlinks,
        credentials, dependency trees, binary files and files larger than 2 MiB.
        Query a narrower directory when the 10,000-file/32 MiB scan budget is reached.
        """
        targets = roots_for(path)
        hits, total, scanned = [], 0, 0
        for candidate in chain.from_iterable(walk_files(target) if target.is_dir() else [target] if target.is_file() else [] for target in targets):
            if not visible(candidate.relative_to(workspace_root().resolve())):
                continue
            size = candidate.stat().st_size
            if size > 2 * 1024**2:
                continue
            scanned += size
            if scanned > 32 * 1024**2:
                return {'matches': hits, 'incomplete': True, 'reason': 'Scan budget exceeded; narrow path'}
            try:
                data = read_regular(candidate, 2 * 1024**2)
                if len(data) > 2 * 1024**2:
                    continue
                content = data.decode('utf-8')
            except UnicodeError:
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if query in line:
                    if total >= offset:
                        hits.append({'path': str(candidate.relative_to(workspace_root().resolve())), 'line': number, 'text': line[:1000]})
                    total += 1
                    if len(hits) > limit:
                        return {'matches': hits[:limit], 'next_offset': offset+limit, 'incomplete': False}
        return {'matches': hits, 'next_offset': None, 'incomplete': False}

    @tool(context_policy=RECOMPUTABLE)
    async def read_external_file(path: str, reason: str,
                                  offset: Annotated[int, Field(ge=0)] = 0,
                                  limit: Annotated[int, Field(ge=1, le=30000)] = 10000) -> dict:
        """Read a configuration/text file outside the role/project scope after explicit
        AI review or single-use/persistent human permission. Absolute paths or ~/ are supported.
        Permission covers this exact resolved file, never its parent directory or writes.
        UTF-8 only, at most 2 MiB. offset/limit count characters.
        """
        original = Path(path).expanduser()
        if not original.is_absolute():
            raise ValueError('Use an absolute external file path')
        target = original.resolve(strict=True)
        from platformdirs import user_data_path
        private_data = user_data_path('Wdrunge', 'Wdrunge').resolve()
        manager = permissions()
        database = getattr(getattr(manager, 'store', None), 'sqlite_path', None)
        if target == private_data or target.is_relative_to(private_data) or database and str(target) in {str(database) + suffix for suffix in ('', '-wal', '-shm')}:
            raise PermissionError('Agent tools cannot read Wdrunge control-plane credentials or databases')
        if not target.is_file() or target.stat().st_size > 2 * 1024**2:
            raise ValueError('Only regular text files up to 2 MiB can be read')
        await manager.authorize({'kind': 'read_external_file', 'path': str(target), 'reason': reason})
        if original.resolve(strict=True) != target:
            raise ValueError('授权期间路径已改变')
        data = read_regular(target, 2 * 1024**2)
        if len(data) > 2 * 1024**2:
            raise ValueError('File exceeds 2 MiB')
        text = data.decode('utf-8')
        return {'path': str(target), 'text': text[offset:offset+limit], 'next_offset': offset+limit if len(text) > offset+limit else None}

    @tool
    async def manage_file(action: Literal['move', 'delete'], path: str, destination: str = '',
                           recursive: bool = False) -> dict:
        """Move without overwriting, or delete a project file/directory. Destructive
        changes require review. Managed collaboration documents must use
        collaboration publication. Recursive deletion is opt-in and cannot target a root.
        """
        target = resolve_allowed(path, write_roots)
        workspace = workspace_root().resolve()
        if not visible(target.relative_to(workspace)) or target == workspace or target in [(workspace / p).resolve() for p in effective_roots(write_roots)]:
            raise ValueError('Cannot move/delete a protected path or assigned root')
        if not target.exists():
            raise FileNotFoundError(path)
        if any(root.is_relative_to(target) for root in control_paths()):
            raise PermissionError('Cannot move/delete an ancestor of Wdrunge control-plane data')
        dest = resolve_allowed(destination, write_roots) if action == 'move' and destination else None
        if action == 'move' and (dest is None or dest.exists() or dest.is_relative_to(target) or not visible(dest.relative_to(workspace))):
            raise ValueError('Move needs an unused destination outside the source tree')
        from app.collaboration.tools import active
        ctx = active.get()
        candidates = [target, *([dest] if dest else [])]
        if ctx:
            documents = await ctx.service.catalog(ctx.project)
            managed = [workspace / document['path'] for document in documents]
            if any(p == q or q.is_relative_to(p) for p in candidates for q in managed) or any(p == workspace/'docs' or p.is_relative_to(workspace/'docs') for p in candidates):
                raise ValueError('Managed/shared documents require collaboration publication')
        if target.is_dir() and action == 'delete' and not recursive:
            raise ValueError('Directory deletion requires recursive=true')
        await permissions().authorize({'kind': action, 'path': str(target), 'destination': str(dest) if dest else None,
                                       'recursive': recursive})
        if resolve_allowed(path, write_roots) != target or dest and resolve_allowed(destination, write_roots) != dest:
            raise ValueError('授权期间路径已改变')
        if action == 'move':
            if dest.exists():
                raise ValueError('Destination now exists; refusing overwrite')
            move_no_replace(target, dest)
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return {'action': action, 'path': str(target), 'destination': str(dest) if dest else None}

    return browse_directory, search_code, read_external_file, manage_file
