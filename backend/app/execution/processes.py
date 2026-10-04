"""Task-owned process groups with bounded logs, lifetime and concurrent services."""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import signal
import tempfile
import time
from uuid import uuid4

from app.execution.permissions import permissions, scope
from app.execution import sandbox
from app.agents.tools._workspace import workspace_root, resolve_allowed, source_workspace, active_workspace
from app.agents.tools.validation import command_failure
from app.execution.ports import port_open

logger = logging.getLogger(__name__)

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


class Processes:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()

    async def start(self, argv, cwd='.', timeout=120, name='command', network=False,
                    reason='', *, extra_reads=(), write_paths=None, browser=False, keep=False, before_spawn=None):
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
        # Snapshot dependencies may only be shared read-only with their own source
        # project. Never authorize arbitrary symlink targets implicitly.
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
        operation = {'kind': 'command', 'argv': list(argv), 'cwd': str(directory), 'network': network,
                     'browser_graphics': browser, **({'keep_running': True} if keep else {}),
                     'read_paths': [str(p) for p in extra_reads], 'timeout': timeout, 'reason': reason,
                     'write_paths': [str(p) for p in write_paths] if write_paths is not None else [str(workspace_root().resolve())]}
        await permissions().authorize(operation)
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
                    network=network, read_paths=extra_reads, write_paths=write_paths, browser=browser, lifeline=fd)
                process = await asyncio.create_subprocess_exec(*command, cwd=directory,
                    env=sandbox.clean_environment(scratch, directory), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                    pass_fds=(fd,))
            except BaseException:
                temporary.cleanup()
                raise
            job = Job(uuid4().hex, scope(), str(workspace_root().resolve()), name, list(argv), str(directory), process, temporary,
                      keep=keep, network=network, reason=reason)
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
        """The running process serving a registry service, by name or port."""
        return next((job for job in self.project_services(project)
                     if (name and job.service == name) or (port and port in job.ports)), None)

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

    async def relaunch(self, job, root):
        """Start a running kept service again from the project root with the same
        command. A service started in a task snapshot only sees that snapshot; once the
        task's delivery is merged, colleagues and the user expect the project itself."""
        root = Path(root).resolve()
        try:
            cwd = Path(job.cwd).resolve().relative_to(Path(job.workspace).resolve()).as_posix()
        except ValueError:
            job.relaunch = 'failed'
            return None
        ports = sorted(job.ports)
        job.relaunch = 'pending'

        async def release():
            # The new command is reviewed like any other; the old copy keeps serving
            # until it is approved, then gives up its ports.
            await self.stop(job)
            for port in ports:  # the old process may release its socket a moment later
                for _ in range(20):
                    if not await port_open(port, timeout=.2):
                        break
                    await asyncio.sleep(.25)
        tokens = active_workspace.set(root), source_workspace.set(root)
        try:
            reason = (f'{job.reason}；' if job.reason else '') + '任务已交付，同一命令改从项目根目录运行'
            new = await self.start(job.argv, cwd, name=job.name, network=job.network, reason=reason, keep=True,
                                   before_spawn=release)
        except BaseException:
            job.relaunch = 'failed'
            raise
        finally:
            source_workspace.reset(tokens[1])
            active_workspace.reset(tokens[0])
        new.service, new.started_by, new.request, new.conversation = job.service, job.started_by, job.request, job.conversation
        for port in ports:
            await self.wait_ready(new, port, 60)
        return new

    async def relaunch_delivered(self, workspace, root):
        """Move the kept services a delivered task started in its snapshot to the
        project root, where its delivery was just merged."""
        workspace = str(Path(workspace).resolve())
        jobs = [job for job in self.jobs.values() if job.workspace == workspace and job.keep
                and job.process.returncode is None and Path(job.workspace).resolve() != Path(root).resolve()]
        relaunched = []
        for job in jobs:
            try:
                if new := await self.relaunch(job, root):
                    relaunched.append(new)
            except Exception:
                logger.warning('Could not restart kept service %s from the project root', job.service or job.name, exc_info=True)
        return relaunched

    def refresh_after_publish(self, project, workspace, paths):
        """A publish changed project files. Services that no longer match them are marked
        stale; kept services running from the project root also restart (in the
        background) to load them, which clears the mark once the new copy runs."""
        root = Path(project).resolve()
        restart = [job for job in self.project_services(project) if job.workspace != str(workspace)
                   and job.keep and not job.from_snapshot and self.changed_paths(job, paths)]
        self.mark_stale(project, workspace, paths)
        for job in restart:
            self.background(self.relaunch(job, root), f'restart kept service {job.service or job.name}')

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
        data = b''.join(job.logs)[start-job.offset:start-job.offset+limit]
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
