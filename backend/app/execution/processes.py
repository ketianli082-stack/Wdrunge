"""Task-owned process groups with bounded logs, lifetime and concurrent services."""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import shutil
import signal
import tempfile
import time
from uuid import uuid4
import weakref

from app.execution.permissions import permissions, scope
from app.execution import sandbox
from app.agents.tools._workspace import workspace_root, resolve_allowed, source_workspace, active_workspace
from app.agents.tools.validation import command_failure
from app.execution.ports import port_open

logger = logging.getLogger(__name__)


def brings_own_python(executable: str, directory: Path) -> bool:
    """An absolute Python interpreter outside `directory/.venv` (a venv in the data
    directory, a system Python): the command does not use a project .venv."""
    path = Path(executable)
    return (path.is_absolute() and path.name.startswith('python') and path.exists()
            and not path.resolve().is_relative_to((directory / '.venv').resolve()))


def snapshot_python(venv: Path) -> str | None:
    """The interpreter a task built its own .venv from (pyvenv.cfg), so the project root
    gets the same Python version rather than whatever runs the platform."""
    try:
        config = dict(line.split('=', 1) for line in (venv / 'pyvenv.cfg').read_text().splitlines() if '=' in line)
    except OSError:
        return None
    config = {key.strip(): value.strip() for key, value in config.items()}
    candidates = [config.get('executable')]
    if home := config.get('home'):
        version = config.get('version_info') or config.get('version') or ''
        minor = '.'.join(version.split('.')[:2])
        candidates += [str(Path(home) / f'python{minor}') if minor else None, str(Path(home) / 'python3')]
    return next((item for item in candidates if item and Path(item).is_file()), None)
# Dependent tasks wait at most this long for a kept service to move to the project root
# (its restart is reviewed and may wait for the user).
MOVE_WAIT = 120
# One dependency installation command at the project root may take this long.
INSTALL_TIMEOUT = 900
# Inside a project directory, holds its previous dependency folder while the platform
# installs a new one (the folder keeps its name, so nothing lists or merges it).
PREVIOUS = '.wdrunge-previous'

@dataclass
class Job:
    id: str
    scope: str
    workspace: str
    name: str
    argv: list[str]
    cwd: str
    process: asyncio.subprocess.Process
    temporary: object
    logs: deque = field(default_factory=deque)
    log_size: int = 0
    offset: int = 0
    end: int = 0
    timed_out: bool = False
    # Ports confirmed by a readiness check; used to recognize the task's own services.
    ports: set = field(default_factory=set)
    reader: asyncio.Task | None = None
    watcher: asyncio.Task | None = None
    # A kept service outlives the task that started it (see keep_service).
    keep: bool = False
    service: str | None = None
    started_by: str | None = None
    request: str | None = None
    conversation: str | None = None
    started_at: float = field(default_factory=time.time)
    stale: list = field(default_factory=list)
    timeout_task: asyncio.TimerHandle | None = None
    # Kept so a reviewed service command can be started again from the project root.
    network: bool = False
    reason: str = ''
    # The platform starting it again from the project root: 'pending' (may wait for
    # review), 'failed' (it keeps running where it was).
    relaunch: str | None = None
    # The process the platform started in its place (a restart from the project root,
    # or the same copy started again after it was stopped).
    successor: object | None = None
    # Values of connected-service credentials it received, masked in its output.
    secrets: tuple = ()

    @property
    def from_snapshot(self):
        """Runs from a task snapshot rather than the project root it belongs to."""
        return self.workspace != self.scope

    def snapshot(self):
        return {'id': self.id, 'name': self.name, 'argv': self.argv, 'cwd': self.cwd,
                'state': 'running' if self.process.returncode is None else 'exited',
                'exit_code': self.process.returncode, 'timed_out': self.timed_out,
                **({'kept': True} if self.keep else {})}

    def runtime(self):
        """Live state of a service process, as shown beside its registry entry."""
        return {'state': 'running' if self.process.returncode is None else 'stopped', 'process_id': self.id,
                'kept': self.keep, 'started_by': self.started_by, 'task': self.request,
                'since': round(self.started_at), **({'stale': self.stale[:5]} if self.stale else {}),
                **({'from_snapshot': True} if self.from_snapshot else {}),
                **({'relaunch': self.relaunch} if self.relaunch else {})}


_lifeline: tuple[int, int] | None = None


def lifeline() -> int:
    """Read end of a pipe whose write end this backend holds until it exits. Commands
    inherit the read end; when the backend dies, even by SIGKILL, the kernel closes the
    write end and each command's supervisor kills its process group (limit_exec.py)."""
    global _lifeline
    if _lifeline is None:
        _lifeline = os.pipe()  # both ends non-inheritable; only pass_fds shares the read end
    return _lifeline[0]


def service_lifetime() -> int:
    """Seconds a kept service may run after its task ends (default 4 hours)."""
    try:
        return max(60, int(os.getenv('WDRUNGE_SERVICE_LIFETIME_SECONDS', '14400')))
    except ValueError:
        return 14400


async def kill_group(process):
    # Kill the group even if its parent exited but grandchildren kept pipes open.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS returns EPERM, not ESRCH, when the group holds only an unreaped
        # zombie leader. Signal the leader directly; wait() then reaps it.
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def provided_resources(root):
    """Connected services for the project at `root` (app.resources); none if the
    settings are unavailable, so a command never fails for a missing database."""
    from app.resources import service
    if service.current is None:
        return service.Provided()
    try:
        return await service.current.provided(root)
    except Exception:
        logger.warning('Could not prepare connected services for %s', root, exc_info=True)
        return service.Provided()


def mask(data: bytes, secrets) -> bytes:
    # Same-length masks keep log offsets stable.
    for secret in secrets:
        data = data.replace(secret, b'*' * len(secret))
    return data


class Processes:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        # Kept services the platform is moving to (or restarting at) the project root,
        # by old process ID: (old job, monotonic time until which it counts). The old copy
        # stops before the new one serves; in between the service is restarting, not stopped.
        self.moving: dict[str, tuple[Job, float]] = {}
        # async (job, detail, stage): tells the team a service could not restart (set by
        # the app). stage: 'install' (dependencies at the root) or 'start'.
        self.failure_handler = None
        # One lock per project root, alive while someone holds or waits for it.
        self.roots = weakref.WeakValueDictionary()
        # (project root, request) -> background tasks still acting on that delivery's merge.
        self.merges: dict = {}

    async def start(self, argv, cwd='.', timeout=120, name='command', network=False,
                    reason='', *, extra_reads=(), write_paths=None, browser=False, keep=False, before_spawn=None,
                    platform=None):
        if not argv or len(argv) > 200 or any(not isinstance(arg, str) or '\0' in arg or len(arg) > 16000 for arg in argv):
            raise ValueError('Use an executable plus at most 199 string arguments, without NUL bytes')
        if keep:
            timeout = service_lifetime()
        elif not 1 <= timeout <= 1800:
            raise ValueError('timeout must be between 1 and 1800 seconds')
        directory = resolve_allowed(cwd, ('.',))
        if not directory.is_dir():
            raise ValueError('工作目录不存在')
        status = sandbox.capability()
        if not status['available']:
            raise sandbox.SandboxUnavailable(status['detail'])
        # A snapshot has its own copy of the project's node_modules but no .venv: the
        # project's .venv in this directory (and a node_modules link left by a snapshot
        # taken before copies) is shared read-only. Never authorize other link targets.
        reads = list(extra_reads)
        source = source_workspace.get()
        if source:
            original = source / directory.relative_to(workspace_root())
            for dependency_name in ('.venv', 'node_modules'):
                dependency = original / dependency_name
                local = directory / dependency_name
                if dependency.is_dir() and not dependency.is_symlink() and (not local.exists() or local.is_symlink() and local.resolve() == dependency.resolve()):
                    reads.append(dependency.resolve())
        extra_reads = tuple(dict.fromkeys(reads))
        provided = await provided_resources(scope())
        operation = {'kind': 'command', 'argv': list(argv), 'cwd': str(directory), 'network': network,
                     'browser_graphics': browser, **({'keep_running': True} if keep else {}),
                     'read_paths': [str(p) for p in extra_reads], 'timeout': timeout, 'reason': reason,
                     'write_paths': [str(p) for p in write_paths] if write_paths is not None else [str(workspace_root().resolve())]}
        if provided.names or provided.data_dir:
            # For the reviewer only (not part of the approval key): what the platform provides.
            operation['resources'] = {'connected_services': list(provided.names), 'service_ports': list(provided.ports),
                                      'data_dir': str(provided.data_dir) if provided.data_dir else None}
        await permissions().authorize(operation, platform=platform)
        # Re-resolve after HITL: symlinks may have changed while the dialog was open.
        if resolve_allowed(cwd, ('.',)) != directory:
            raise ValueError('工作目录在审核期间发生变化，请重新执行')
        if before_spawn is not None:
            await before_spawn()
        async with self.lock:
            running = [job for job in self.jobs.values() if job.process.returncode is None and job.keep == keep]
            if len(running) >= 8:
                raise RuntimeError('最多同时运行 8 个保留服务；先停止不需要的服务' if keep else
                                   '最多同时运行 8 个任务进程；先停止不需要的服务')
            for key in list(self.jobs):
                if len(self.jobs) < 100:
                    break
                if self.jobs[key].watcher.done():
                    del self.jobs[key]
            # Short paths also keep browser Unix-domain socket names below the
            # platform length limit; the private subdirectory is the only grant.
            temporary = tempfile.TemporaryDirectory(prefix='wdrunge-exec-', dir='/tmp' if os.name == 'posix' else None)
            scratch = Path(temporary.name).resolve()
            try:
                fd = lifeline()
                command = sandbox.command_line(argv, workspace_root().resolve(), directory, scratch,
                    network=network, read_paths=extra_reads, write_paths=write_paths, browser=browser, lifeline=fd,
                    data_paths=[provided.data_dir] if provided.data_dir else (), service_ports=provided.ports)
                process = await asyncio.create_subprocess_exec(*command, cwd=directory,
                    env=sandbox.clean_environment(scratch, directory, provided.env, network=network), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                    pass_fds=(fd,))
            except BaseException:
                temporary.cleanup()
                raise
            job = Job(uuid4().hex, scope(), str(workspace_root().resolve()), name, list(argv), str(directory), process, temporary,
                      keep=keep, network=network, reason=reason, secrets=tuple(value.encode() for value in provided.secrets))
            self.jobs[job.id] = job
            job.reader = asyncio.create_task(self._read(job))
            # A timer rather than wait_for, so keep_service can extend a running lifetime;
            # created here so it exists as soon as start() returns.
            job.timeout_task = asyncio.get_running_loop().call_later(timeout, self._expire, job)
            job.watcher = asyncio.create_task(self._watch(job))
        return job

    async def _read(self, job):
        while chunk := await job.process.stdout.read(4096):
            job.logs.append(chunk)
            job.log_size += len(chunk)
            job.end += len(chunk)
            while job.log_size > 128 * 1024:
                removed = job.logs.popleft()
                job.log_size -= len(removed)
                job.offset += len(removed)

    async def _watch(self, job):
        try:
            await job.process.wait()
        finally:
            job.timeout_task.cancel()
            await kill_group(job.process)
            await job.reader
            job.temporary.cleanup()

    @staticmethod
    def _expire(job):
        job.timed_out = True
        try:
            os.killpg(job.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                job.process.kill()
            except ProcessLookupError:
                pass

    def owned_jobs(self):
        return [job for job in self.jobs.values()
                if job.scope == scope() and job.workspace == str(workspace_root().resolve())]

    def owned(self, identifier):
        """Find an owned process by ID, or by the name it was started with
        (the newest one, preferring a running process)."""
        owned = self.owned_jobs()
        job = next((job for job in owned if job.id == identifier), None)
        if job is None:
            named = [job for job in owned if job.name == identifier]
            named.sort(key=lambda job: job.process.returncode is None)
            job = named[-1] if named else None
        if job is None:
            known = ', '.join(f"{job.id} ({job.name}, {'running' if job.process.returncode is None else 'exited'})"
                              for job in owned[-8:]) or 'none'
            raise ValueError(f'找不到当前工作区拥有的进程: {identifier}. Use an ID or name from: {known}')
        return job

    def project_services(self, project=None):
        """Running service processes of a project (kept ones and those tied to a registry
        service), newest first; other tasks of the project may inspect and stop them."""
        project = str(Path(project).resolve()) if project else scope()
        jobs = [job for job in self.jobs.values()
                if job.scope == project and job.process.returncode is None and (job.keep or job.service)]
        return sorted(jobs, key=lambda job: job.started_at, reverse=True)

    def service_job(self, project, name=None, port=None):
        """The running process serving a registry service: by name, else by port."""
        jobs = self.project_services(project)
        return (next((job for job in jobs if name and job.service == name), None)
                or next((job for job in jobs if port and port in job.ports), None))

    def restarting(self, project, name=None, port=None):
        """The old process of a service the platform is starting again from the project
        root and that has no serving successor yet, by name or port."""
        project = str(Path(project).resolve()) if project else scope()
        now = time.monotonic()
        return next((job for job, until in self.moving.values() if job.scope == project and now < until
                     and ((name and job.service == name) or (port and port in job.ports))), None)

    def moving_for(self, project, request):
        """Whether kept services a delivered task started are still on their way to the
        project root: still running in its snapshot, or being restarted there. Tasks
        depending on it start once they serve from the root."""
        project = str(Path(project).resolve())
        now = time.monotonic()
        if any(job.scope == project and job.request == request and now < until
               for job, until in self.moving.values()):
            return True
        return any(job.scope == project and job.request == request and job.keep and job.from_snapshot
                   and job.relaunch is None and job.process.returncode is None for job in self.jobs.values())

    def keep_service(self, job, service=None):
        """Let a service outlive its task: lifetime extends to service_lifetime()."""
        job.keep = True
        job.service = service or job.service
        if job.process.returncode is None and job.timeout_task is not None and not job.timed_out:
            job.timeout_task.cancel()
            job.timeout_task = asyncio.get_running_loop().call_later(service_lifetime(), self._expire, job)

    def settle_delivery(self, task_services, project_services):
        """At delivery, decide which of this task's processes outlive it. Services are
        deliverables only when a task declares them: the task's own are kept; one it
        (re)started with keep_running stays only if some task delivers it, e.g. a
        colleague's service it restored. Anything else (a preview, a mock) stops."""
        for job in self.owned_jobs():
            if job.process.returncode is not None:
                continue
            if job.service and job.service in task_services:
                self.keep_service(job)
            elif job.keep and job.service not in project_services:
                job.keep = False

    def mark_stale(self, project, workspace, paths, skip=()):
        """A publish from another workspace changed files a kept service serves from its
        own snapshot; the service keeps running but no longer matches the project."""
        for job in self.project_services(project):
            if job.workspace == str(workspace) or job.id in skip:
                continue
            if changed := self.changed_paths(job, paths):
                job.stale = list(dict.fromkeys([*job.stale, *changed]))[:20]

    def track_merge(self, project, request, task):
        """Count a background task (a migration, a service restart or move) as work the
        platform still does for a delivery's merge, until the task ends."""
        key = (str(Path(project).resolve()), request)
        self.merges.setdefault(key, set()).add(task)

        def finished(task):
            group = self.merges.get(key)
            if group is not None:
                group.discard(task)
                if not group:
                    self.merges.pop(key, None)
        task.add_done_callback(finished)
        return task

    def merge_settling(self, project, request) -> bool:
        """Whether the platform is still acting on a delivery's merge: migrating the
        database, restarting root services to load it, or moving the task's own kept
        services to the project root. What a reviewer sees of the services meanwhile
        (a refused connection, the old code) says nothing about the delivery."""
        return bool(self.merges.get((str(Path(project).resolve()), request))) or self.moving_for(project, request)

    def hold(self, job, seconds=MOVE_WAIT):
        """Count a service as restarting, not stopped, for up to `seconds` from now."""
        self.moving[job.id] = (job, time.monotonic() + seconds)

    async def settled(self, project):
        """Wait while the platform moves or restarts kept services of the project, which
        may reinstall dependencies at its root: a task snapshot taken meanwhile would
        copy a dependency folder that is being replaced."""
        project = str(Path(project).resolve())
        while any(job.scope == project and time.monotonic() < until for job, until in self.moving.values()):
            await asyncio.sleep(.25)

    def root_lock(self, root) -> asyncio.Lock:
        """One at a time per project root: a dependency installation there with the
        service restarts around it, and the copy a new task snapshot takes of its
        dependency folders."""
        key = str(Path(root).resolve())
        lock = self.roots.get(key)
        if lock is None:
            lock = self.roots[key] = asyncio.Lock()
        return lock

    @staticmethod
    def latest(job):
        """The newest process started in place of this one."""
        while job.successor is not None:
            job = job.successor
        return job

    @staticmethod
    def adopt(old, new):
        """`new` takes the place of `old`: same service, task and conversation."""
        new.service, new.started_by, new.request, new.conversation = old.service, old.started_by, old.request, old.conversation
        old.successor = new

    def serving_note(self, job) -> str:
        """Where the service of `job` stands after a failed restart."""
        current = self.latest(job)
        if current.process.returncode is not None:
            return '服务目前已停止'
        return '服务仍由任务快照里的交付前副本提供' if current.from_snapshot else '服务已在项目根目录用原来的依赖重新启动'

    async def relaunch(self, job, root):
        """Start a running kept service again from the project root with the same
        command. A service started in a task snapshot only sees that snapshot; once the
        task's delivery is merged, colleagues and the user expect the project itself.
        Dependencies at the root are reinstalled first when its manifests changed."""
        asked = time.time()
        root = Path(root).resolve()
        lock, current = self.root_lock(root), job
        try:
            while lock.locked():  # another restart or installation at this root, maybe a long one
                self.hold(job)
                await asyncio.sleep(.25)
            async with lock:
                self.hold(job)
                current = self.latest(job)
                if (current is not job and current.process.returncode is None and not current.from_snapshot
                        and current.started_at >= asked):
                    # Started again at the root (beside an installation another restart
                    # needed) after this restart was asked for: it already runs the
                    # project as merged.
                    return current
                return await self._relaunch(current, root)
        finally:
            self.moving.pop(job.id, None)
            self.moving.pop(current.id, None)

    async def _relaunch(self, job, root):
        try:
            cwd = Path(job.cwd).resolve().relative_to(Path(job.workspace).resolve()).as_posix()
        except ValueError:
            job.relaunch = 'failed'
            return None
        # Only ports this process serves itself: one it merely connects to (a proxy's
        # upstream) stays open and would hold up the restart.
        others = {port for other in self.jobs.values() if other is not job and other.process.returncode is None
                  for port in other.ports}
        ports = sorted(job.ports - others)
        job.relaunch = 'pending'

        async def release():
            # The new command is reviewed like any other; the old copy keeps serving
            # until it is approved, then gives up its ports.
            await self.quiesce([job], ports)
        reason = (f'{job.reason}；' if job.reason else '') + '任务已交付，同一命令改从项目根目录运行'
        detail, stage, fresh, rebuilt = '', 'start', False, False
        tokens = active_workspace.set(root), source_workspace.set(root)
        try:
            # The second round rebuilds the dependency folders from the manifests: a copy
            # that served in the task snapshot and does not at the root most often lacks
            # what an installation there provides.
            for rebuild in (False, True):
                if rebuild and fresh:
                    break  # installed from the manifests a moment ago: nothing to rebuild
                # Before the old copy gives way: a snapshot copy serves through the
                # installation, and a root copy stops only for the installation itself
                # and comes back on the folder it had when that fails.
                problem, installed = await self._prepare_root(job, root, cwd, rebuild)
                if problem:
                    detail, stage = problem, 'install'
                    break
                if problem := self.launch_problem(job, root / cwd):
                    detail = problem
                    break
                if rebuild and not installed:
                    break  # nothing to rebuild: the same start would fail the same way
                fresh, rebuilt = installed, rebuild
                self.hold(job)
                try:
                    new = await self.start(job.argv, cwd, name=job.name, network=job.network, reason=reason, keep=True,
                                           before_spawn=release, platform='重启已授权运行的保留服务：同一命令改从项目根目录运行')
                except Exception as error:
                    detail = f'启动命令没有运行（{type(error).__name__}: {error}）'
                    break
                self.adopt(job, new)
                # Until it serves, colleagues see it being restarted rather than stopped.
                new.relaunch = 'pending'
                try:
                    ready = True
                    for port in ports:
                        ready = await self.wait_ready(new, port, 60) and ready
                finally:
                    new.relaunch = None
                if ready and new.process.returncode is None:
                    return new
                output = self.logs(new, max(0, new.end - 1500), 1500)['output'].strip()
                await self.stop(new)
                detail = '从项目根目录重启后没有正常提供服务' + (f'。最后的日志：\n{output[-1200:]}' if output else '')
        except BaseException:
            job.relaunch = 'failed'
            raise
        finally:
            source_workspace.reset(tokens[1])
            active_workspace.reset(tokens[0])
        job.relaunch = 'failed'
        # Whatever served before serves again where it can: the snapshot copy still runs
        # after a failed installation, and starts again after a failed start.
        restored = await self.restore(job) if self.latest(job).process.returncode is not None else None
        await self.report_failure(job, detail + ('\n（平台按清单重装依赖后又启动了一次，仍然失败）' if rebuilt else '')
                                  + f'\n{self.serving_note(job)}。', stage)
        return restored

    async def quiesce(self, jobs, ports=None):
        """Stop services and wait until their ports are free (a process may release its
        socket a moment after it exits)."""
        for job in jobs:
            await self.stop(job)
        for port in sorted({port for job in jobs for port in job.ports}) if ports is None else ports:
            for _ in range(20):
                if not await port_open(port, timeout=.2):
                    break
                await asyncio.sleep(.25)

    async def prepare_root(self, job, root, cwd) -> str | None:
        """Make the project root able to run a delivered command (see _prepare_root).
        Returns why it cannot run, or None."""
        root = Path(root).resolve()
        async with self.root_lock(root):
            return (await self._prepare_root(job, root, cwd))[0] or self.launch_problem(job, root / cwd)

    @staticmethod
    def launch_problem(job, directory) -> str | None:
        if sandbox.resolve_executable(job.argv[0], directory) is None:
            return (f'项目根目录下找不到启动命令 {job.argv[0]}；它可能只存在于任务快照里'
                    '（依赖和虚拟环境不随交付合并），需要让项目能在根目录安装和启动。')
        return None

    async def _prepare_root(self, job, root, cwd, rebuild=False) -> tuple[str | None, bool]:
        """Dependency folders are never merged, so the platform installs them at the root
        from the manifests in the command's cwd, and keeps them current: a folder is
        installed again whenever it is not the one the platform installed from the
        manifests as they are now (`rebuild`: even then). Returns (why an installation
        failed or None, whether anything was installed)."""
        from app.execution.environment import dependency_fingerprint, inspect_environment, installed_fingerprint
        directory = root / cwd
        try:
            runtimes = {item['type'] for item in inspect_environment(directory)['runtimes']}
        except (OSError, ValueError):
            runtimes = set()
        snapshot = Path(job.cwd)
        missing = sandbox.resolve_executable(job.argv[0], directory) is None
        installed = False
        for runtime, folder in (('python', '.venv'), ('node', 'node_modules')):
            if runtime not in runtimes:
                continue
            if runtime == 'python' and brings_own_python(job.argv[0], directory):
                continue  # an absolute interpreter elsewhere never loads this directory's .venv
            target = directory / folder
            own = target.is_dir() and not target.is_symlink()
            had_own = (snapshot / folder).is_dir() and not (snapshot / folder).is_symlink() and snapshot != directory
            # The command uses the folder when the root already has one, the task ran with
            # its own, or its executable is found nowhere else.
            if not (own or had_own or missing):
                continue
            if own and not rebuild and installed_fingerprint(target) == dependency_fingerprint(directory, runtime):
                continue
            if problem := await self._install(job, root, cwd, runtime, folder):
                return problem, installed
            installed = True
        return None, installed

    async def _install(self, job, root, cwd, runtime, folder) -> str | None:
        """Build the dependency folder of `root/cwd` from its manifests, starting from an
        empty one as a clean checkout does (what the check before a merge verified). The
        previous folder is set aside first and comes back when the installation fails.
        Services running from the root at or below this directory load the folder: they
        stop for the installation, never during it (a development server writing its
        cache into node_modules made `npm ci` fail with ENOTEMPTY), and start again after
        it. `job`, the service being restarted, is left for the caller to start once the
        installation succeeds. Returns why it failed, or None."""
        from app.execution.environment import INSTALLED, dependency_fingerprint, installation_commands
        directory = root / cwd
        target, held = directory / folder, directory / PREVIOUS / folder
        python = snapshot_python(Path(job.cwd) / folder)
        try:
            installation_commands(directory, runtime, python=python)
        except ValueError as error:
            return f'无法在项目根目录安装依赖：{error}'
        users = [other for other in self.jobs.values() if other.process.returncode is None and (other.keep or other.service)
                 and Path(other.workspace) == root and Path(other.cwd).is_relative_to(directory)]

        def set_aside():
            if target.exists():
                shutil.rmtree(held, ignore_errors=True)
                held.parent.mkdir(exist_ok=True)
                os.rename(target, held)

        def finish(failed):
            if failed:
                shutil.rmtree(target, ignore_errors=True)
                if held.exists():
                    os.rename(held, target)
            else:
                target.mkdir(exist_ok=True)
                (target / INSTALLED).write_text(dependency_fingerprint(directory, runtime))
                shutil.rmtree(held, ignore_errors=True)
            try:
                held.parent.rmdir()
            except OSError:
                pass

        def waiting():
            for other in [*users, *([job] if isinstance(job, Job) else [])]:
                self.hold(other, INSTALL_TIMEOUT + MOVE_WAIT)
        waiting()
        await self.quiesce(users)
        try:
            await asyncio.to_thread(set_aside)
            problem, retry = await self._run_install(directory, cwd, runtime, python)
            if problem and retry:
                # Once more from an empty folder: registry and network failures pass.
                await asyncio.to_thread(shutil.rmtree, target, True)
                waiting()
                again, _ = await self._run_install(directory, cwd, runtime, python)
                problem = again and f'{again}\n（平台已自动重试 1 次，两次都失败）'
        except BaseException:
            finish(True)
            for other in users:
                self.moving.pop(other.id, None)
            raise
        await asyncio.to_thread(finish, bool(problem))
        for other in users:
            if problem or other is not job:
                await self.respawn(other, '平台在项目根目录重装依赖期间暂停了服务，安装结束后用同一命令重新启动',
                                   '重启已授权运行的保留服务：依赖安装期间暂停')
                self.moving.pop(other.id, None)
        return problem

    async def _run_install(self, directory, cwd, runtime, python) -> tuple[str | None, bool]:
        """Run the installation commands of one runtime in `directory`. Returns (why it
        failed or None, whether another attempt may pass)."""
        from app.execution.environment import installation_commands
        try:
            commands = installation_commands(directory, runtime, python=python)
        except ValueError as error:
            return f'无法在项目根目录安装依赖：{error}', False
        for argv in commands:
            where = f"{' '.join(argv[:4])}，目录 {cwd}"
            try:
                result = await self.run(argv, cwd, INSTALL_TIMEOUT, name=f'install {runtime} dependencies', network=True,
                                        reason='任务已交付；依赖目录不随交付合并，平台在项目根目录按清单安装依赖后再重启服务',
                                        platform='平台按项目清单在项目根目录安装依赖')
            except (OSError, PermissionError, RuntimeError) as error:
                return f'在项目根目录安装依赖失败（{where}）：{type(error).__name__}: {error}', False
            if result.get('timed_out'):
                return f"在项目根目录安装依赖失败（{where}，超时）：\n" + str(result.get('output', ''))[-1200:], False
            if result.get('exit_code') != 0:
                return (f"在项目根目录安装依赖失败（{where}，退出码 {result.get('exit_code')}）：\n"
                        + str(result.get('output', ''))[-1200:]), True
        return None, False

    async def respawn(self, job, reason, platform):
        """Start a stopped kept service again where it ran, with the same command."""
        workspace = Path(job.workspace)
        try:
            cwd = Path(job.cwd).resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            return None
        if not workspace.is_dir():
            return None
        tokens = active_workspace.set(workspace), source_workspace.set(Path(job.scope))
        try:
            new = await self.start(job.argv, cwd, name=job.name, network=job.network, keep=True, reason=reason, platform=platform)
        except Exception:
            logger.warning('Could not start service %s again', job.service or job.name, exc_info=True)
            return None
        finally:
            source_workspace.reset(tokens[1])
            active_workspace.reset(tokens[0])
        self.adopt(job, new)
        for port in job.ports:
            await self.wait_ready(new, port, 60)
        return new

    async def restore(self, job):
        """Start the pre-delivery copy again from its task snapshot after a failed move."""
        if not job.from_snapshot:
            return None
        old = await self.respawn(job, '从项目根目录重启失败，恢复交付前的运行副本', '恢复已授权运行的保留服务的交付前副本')
        if old:
            old.relaunch = 'failed'
        return old

    async def report_failure(self, job, detail, stage):
        logger.warning('Kept service %s could not restart from the project root: %s', job.service or job.name, detail[:300])
        if self.failure_handler is None:
            return
        try:
            await self.failure_handler(job, detail, stage)
        except Exception:
            logger.warning('Could not report the service failure', exc_info=True)

    def relaunch_delivered(self, workspace, root, before=None):
        """Move the kept services a delivered task started in its snapshot to the
        project root, where its delivery was just merged. They count as moving from this
        call on, before the returned coroutine runs, so dependent tasks wait for them.
        `before`: the delivery's migration (an awaitable result); if it fails the services
        stay where they run, on the code and schema they had."""
        workspace = str(Path(workspace).resolve())
        jobs = [job for job in self.jobs.values() if job.workspace == workspace and job.keep
                and job.process.returncode is None and Path(job.workspace).resolve() != Path(root).resolve()]
        for job in jobs:
            job.relaunch = 'pending'
            self.hold(job)

        async def move():
            relaunched = []
            try:
                if before is not None:
                    try:
                        migrated = await before
                    except Exception:
                        migrated = False
                    if not migrated:
                        for job in jobs:
                            job.relaunch = 'failed'
                        return relaunched
                for job in jobs:
                    try:
                        if new := await self.relaunch(job, root):
                            relaunched.append(new)
                    except Exception:
                        logger.warning('Could not restart kept service %s from the project root', job.service or job.name, exc_info=True)
            finally:
                for job in jobs:
                    self.moving.pop(job.id, None)
            return relaunched
        return move()

    def refresh_after_publish(self, project, workspace, paths, request=None):
        """A publish changed project files. Services that no longer match them are marked
        stale; kept services running from the project root also restart (in the
        background) to load them, which clears the mark once the new copy runs.
        `request`: the delivery that published, whose review waits for these restarts."""
        root = Path(project).resolve()
        restart = [job for job in self.project_services(project) if job.workspace != str(workspace)
                   and job.keep and not job.from_snapshot and self.changed_paths(job, paths)]
        self.mark_stale(project, workspace, paths)
        for job in restart:
            self.hold(job)
            task = self.background(self.relaunch(job, root), f'restart kept service {job.service or job.name}')
            if request:
                self.track_merge(project, request, task)

    def background(self, coroutine, label):
        """Run platform-initiated service work without blocking the caller: a restart is
        reviewed like any command and may wait for the user."""
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def finished(task):
            self.tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logger.warning('Could not %s', label, exc_info=task.exception())
        task.add_done_callback(finished)
        return task

    @staticmethod
    def changed_paths(job, paths):
        try:
            base = Path(job.cwd).resolve().relative_to(Path(job.workspace).resolve()).as_posix()
        except ValueError:
            base = '.'
        return [path for path in paths if base in {'', '.'} or path == base or path.startswith(base + '/')]

    async def close_services(self, project, conversation=None):
        """Stop kept services of a project (or of one conversation)."""
        jobs = [job for job in self.project_services(project) if conversation is None or job.conversation == conversation]
        await asyncio.gather(*(self.stop(job) for job in jobs), return_exceptions=True)
        return [job.service or job.name for job in jobs]

    def logs(self, job, offset=0, limit=10000):
        start = max(offset, job.offset)
        data = mask(b''.join(job.logs), job.secrets)[start-job.offset:start-job.offset+limit]
        return {'output': data.decode(errors='replace'), 'next_offset': start+len(data),
                'truncated': offset < job.offset, 'available_from': job.offset}

    async def stop(self, job):
        if job.watcher.done():
            return job.snapshot()
        await kill_group(job.process)
        await job.watcher
        return job.snapshot()

    async def wait_ready(self, job, port, timeout):
        """Wait until the port accepts loopback connections while the job runs."""
        deadline = asyncio.get_running_loop().time() + timeout
        while job.process.returncode is None and asyncio.get_running_loop().time() < deadline:
            if await port_open(port):
                # The job may have died while we connected to someone else's listener.
                if job.process.returncode is None:
                    job.ports.add(port)
                    return True
                break
            await asyncio.sleep(.25)
        return False

    async def run(self, *args, ok_exit_codes=(0,), **kwargs):
        job = await self.start(*args, **kwargs)
        try:
            await asyncio.shield(job.watcher)
            result = {**job.snapshot(), **self.logs(job, max(0, job.end-30000), 30000)}
            if result['exit_code'] not in ok_exit_codes or result['timed_out']:
                result.update(command_failure('Command timed out' if result['timed_out'] else f"Command exited {result['exit_code']}",
                                              result['output'], timed_out=result['timed_out']))
            return result
        except asyncio.CancelledError:
            await self.stop(job)
            raise

    async def close(self, workspace=None, *, keep_services=False):
        """Stop processes (of one workspace) when a turn or task ends; keep_services
        spares kept services of a delivered task. A kept service running from the project
        root belongs to the project, not to whichever turn or task last ran there: only
        an explicit stop, its lifetime or shutdown (workspace=None) ends it."""
        await asyncio.gather(*(self.stop(job) for job in list(self.jobs.values())
            if workspace is None or (job.workspace == str(workspace) and not (job.keep and (keep_services or not job.from_snapshot)))),
            return_exceptions=True)


processes = Processes()
