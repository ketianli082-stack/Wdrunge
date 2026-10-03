"""OS isolation: fail closed when the installed backend cannot enforce a policy."""
import json
import os
from pathlib import Path
import platform
import shutil
import sys


class SandboxUnavailable(RuntimeError):
    pass


def capability():
    system = platform.system()
    if system == 'Darwin':
        executable = '/usr/bin/sandbox-exec'
        available = Path(executable).is_file()
        return {'platform': system, 'backend': 'seatbelt', 'available': available,
                'detail': '使用系统 Seatbelt；sandbox-exec 已弃用，实际启动失败时不会降级为裸执行。'}
    if system == 'Linux':
        return {'platform': system, 'backend': 'bubblewrap', 'available': bool(shutil.which('bwrap')),
                'detail': '需要 Bubblewrap 和可用的用户命名空间；这是内核隔离机制，但 bwrap 并非所有发行版预装。'}
    return {'platform': system, 'backend': None, 'available': False,
            'detail': 'Windows Sandbox 需要受支持的 Windows 版本、可选功能和虚拟化；目前没有可验证的 AppContainer/Job Object 执行适配器，因此拒绝执行。'}


PROTECTED = {'.git', '.aws', '.ssh', '.codex', '.agents', '.wdrunge-runs'}


def protected_paths(root):
    """Mask credentials and app metadata even inside a writable workspace."""
    from platformdirs import user_data_path
    # The human control plane and app database are never available to project code,
    # even when the selected workspace is an ancestor such as the user's HOME.
    found = [user_data_path('Wdrunge', 'Wdrunge')]
    from app.execution.permissions import current
    if current and current.store.sqlite_path:
        found += [Path(str(current.store.sqlite_path) + suffix) for suffix in ('', '-wal', '-shm')]
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            if name in PROTECTED or name == '.env' or name.startswith('.env.') and name not in {'.env.example', '.env.sample'}:
                found.append(Path(directory) / name)
        dirs[:] = [name for name in dirs if name not in PROTECTED | {'node_modules', '.venv', 'dist', 'target'}
                   and not (Path(directory) / name).is_symlink()]
    return found


def host_path() -> str:
    """The host PATH without the platform's own virtual environment. Project commands
    run with the interpreters the user has; Wdrunge's dependencies (installed only in
    its venv) would otherwise make missing project dependencies look satisfied."""
    bin_dir = 'Scripts' if os.name == 'nt' else 'bin'
    own = {Path(sys.prefix, bin_dir).resolve()} if sys.prefix != sys.base_prefix else set()
    if os.environ.get('VIRTUAL_ENV'):
        own.add(Path(os.environ['VIRTUAL_ENV'], bin_dir).resolve())
    return os.pathsep.join(entry for entry in os.environ.get('PATH', '').split(os.pathsep)
                           if entry and Path(entry).resolve() not in own)


def clean_environment(scratch, directory):
    # Never forward provider credentials, Python injection variables or host HOME.
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR') if key in os.environ}
    if 'PATH' in environment:
        environment['PATH'] = host_path()
    environment.update(HOME=str(scratch), TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch),
        XDG_CACHE_HOME=str(scratch / 'cache'), npm_config_cache=str(scratch / 'npm-cache'),
        PIP_CACHE_DIR=str(scratch / 'pip-cache'), UV_CACHE_DIR=str(scratch / 'uv-cache'),
        PYTHONNOUSERSITE='1', PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1', PIP_REQUIRE_VIRTUALENV='true', UV_PYTHON_DOWNLOADS='never', GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_GLOBAL=os.devnull, npm_config_userconfig=os.devnull,
        WDRUNGE_DATABASE_PATH=str(scratch / 'service.db'),
        WDRUNGE_DATABASE_URL=f'sqlite+aiosqlite:///{scratch / "service.db"}')
    venv = directory / '.venv'
    if venv.is_dir() and not venv.is_symlink():
        environment['VIRTUAL_ENV'] = str(venv)
        environment['PATH'] = str(venv / ('Scripts' if os.name == 'nt' else 'bin')) + os.pathsep + environment.get('PATH', '')
    return environment


def command_line(argv, workspace, directory, scratch, *, network=False, read_paths=(), write_paths=None, browser=False,
                 lifeline=None):
    status = capability()
    if not status['available']:
        raise SandboxUnavailable(status['detail'])
    requested = str(directory / argv[0]) if not Path(argv[0]).is_absolute() and '/' in argv[0] else argv[0]
    executable = shutil.which(requested, path=clean_environment(scratch, directory).get('PATH'))
    if not executable:
        raise FileNotFoundError(f'Executable not found: {argv[0]}')
    invocation = str(Path(executable).absolute())
    executable = Path(executable).resolve()
    runtime = [Path(p) for p in ('/usr', '/bin', '/sbin', '/lib', '/lib64', '/System/Library',
        '/System/Cryptexes/OS', '/System/Volumes/Preboot/Cryptexes/OS', '/private/var/db/dyld',
        '/Library/Apple', '/opt/homebrew', '/usr/local') if Path(p).exists()]
    runtime += [Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()]
    # Version-manager toolchains (e.g. nvm, uv) need their own install tree, never all HOME.
    if not any(executable.is_relative_to(root) for root in runtime + [workspace]):
        homes = [Path.home() / '.nvm/versions/node', Path.home() / '.local/share/uv/python', Path.home() / '.pyenv/versions']
        for base in homes:
            if executable.is_relative_to(base) and len(executable.relative_to(base).parts) > 1:
                runtime.append(base / executable.relative_to(base).parts[0])
                break
        else:
            raise SandboxUnavailable('该可执行文件位于未授权的工具链目录；请使用项目或系统安装的运行时')
    reads = list(dict.fromkeys([*runtime, workspace, *read_paths]))
    writes = [scratch, *(write_paths if write_paths is not None else [workspace])]
    protected = protected_paths(workspace)
    launcher = Path(__file__).with_name('limit_exec.py').resolve()
    reads += [launcher, Path(sys.executable).resolve()]
    # The lifeline (an inherited pipe fd) stops the command when the backend dies;
    # bwrap --die-with-parent already does that on Linux.
    watched = str(lifeline) if lifeline is not None and status['backend'] == 'seatbelt' else '-'
    payload = [sys.executable, '-I', str(launcher), 'browser' if browser else 'command', watched, invocation, *argv[1:]]
    if status['backend'] == 'seatbelt':
        quote = lambda path: json.dumps(str(path))
        rules = ['(version 1)', '(deny default)', '(allow process*)', '(allow sysctl-read)',
                 '(allow mach-lookup)', '(allow signal (target same-sandbox))',
                 '(allow file-read-metadata)', '(allow file-read* (literal "/") (subpath "/dev") (subpath "/private/etc"))',
                 '(allow file-write* (literal "/dev/null") (literal "/dev/tty"))']
        rules += [f'(allow file-read* file-map-executable (subpath {quote(path)}))' for path in reads + writes]
        rules += [f'(allow file-write* (subpath {quote(path)}))' for path in writes]
        rules += [f'(deny file-read* file-write* (subpath {quote(path)}))' for path in protected]
        # Prevent newly-created sensitive paths, including credential files.
        rules += ['(deny file-write* (regex #"/(\\.git|\\.aws|\\.ssh|\\.codex|\\.agents)(/|$)"))']
        if network:
            rules.append('(allow network*)')
        if browser:
            # Only the fixed browser evidence runner gets graphics-device access
            # and Chromium's IPC service namespace; arbitrary commands do not.
            rules += ['(allow iokit-open)', '(allow mach-register (global-name-regex #"^org[.]chromium[.]"))']
        profile = scratch / 'sandbox.sb'
        profile.write_text('\n'.join(rules))
        return ['/usr/bin/sandbox-exec', '-f', str(profile), *payload]
    args = ['bwrap', '--die-with-parent', '--new-session', '--unshare-all']
    if network:
        args.append('--share-net')
    args += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    for path in dict.fromkeys(reads):
        args += ['--ro-bind', str(path), str(path)]
    for path in writes:
        args += ['--bind', str(path), str(path)]
    for path in ('/etc/ld.so.cache', '/etc/ssl', '/etc/resolv.conf', '/etc/hosts', '/etc/passwd', '/etc/localtime'):
        if Path(path).exists():
            args += ['--ro-bind', path, path]
    for path in protected:
        if path.exists():
            args += ['--tmpfs', str(path)] if path.is_dir() else ['--ro-bind', '/dev/null', str(path)]
    return [*args, '--chdir', str(directory), '--', *payload]
