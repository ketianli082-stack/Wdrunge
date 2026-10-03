"""Task-owned process groups with bounded logs, lifetime and concurrent services."""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import tempfile
from uuid import uuid4

from app.execution.permissions import permissions, scope
from app.execution import sandbox
from app.agents.tools._workspace import workspace_root, resolve_allowed, source_workspace
from app.agents.tools.validation import command_failure
from app.execution.ports import port_open


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

    def snapshot(self):
        return {'id': self.id, 'name': self.name, 'argv': self.argv, 'cwd': self.cwd,
                'state': 'running' if self.process.returncode is None else 'exited',
                'exit_code': self.process.returncode, 'timed_out': self.timed_out}


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

    async def start(self, argv, cwd='.', timeout=120, name='command', network=False,
                    reason='', *, extra_reads=(), write_paths=None, browser=False):
        if not argv or len(argv) > 200 or any(not isinstance(arg, str) or '\0' in arg or len(arg) > 16000 for arg in argv):
            raise ValueError('Use an executable plus at most 199 string arguments, without NUL bytes')
        if not 1 <= timeout <= 1800:
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
                     'browser_graphics': browser,
                     'read_paths': [str(p) for p in extra_reads], 'timeout': timeout, 'reason': reason,
                     'write_paths': [str(p) for p in write_paths] if write_paths is not None else [str(workspace_root().resolve())]}
        await permissions().authorize(operation)
        # Re-resolve after HITL: symlinks may have changed while the dialog was open.
        if resolve_allowed(cwd, ('.',)) != directory:
            raise ValueError('工作目录在审核期间发生变化，请重新执行')
        async with self.lock:
            running = [job for job in self.jobs.values() if job.process.returncode is None]
            if len(running) >= 8:
                raise RuntimeError('最多同时运行 8 个任务进程；先停止不需要的服务')
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
                command = sandbox.command_line(argv, workspace_root().resolve(), directory, scratch,
                    network=network, read_paths=extra_reads, write_paths=write_paths, browser=browser)
                process = await asyncio.create_subprocess_exec(*command, cwd=directory,
                    env=sandbox.clean_environment(scratch, directory), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            except BaseException:
                temporary.cleanup()
                raise
            job = Job(uuid4().hex, scope(), str(workspace_root().resolve()), name, list(argv), str(directory), process, temporary)
            self.jobs[job.id] = job
            job.reader = asyncio.create_task(self._read(job))
            job.watcher = asyncio.create_task(self._watch(job, timeout))
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

    async def _watch(self, job, timeout):
        try:
            await asyncio.wait_for(job.process.wait(), timeout)
        except TimeoutError:
            job.timed_out = True
        finally:
            await kill_group(job.process)
            await job.reader
            job.temporary.cleanup()

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

    async def close(self, workspace=None):
        await asyncio.gather(*(self.stop(job) for job in list(self.jobs.values())
            if workspace is None or job.workspace == str(workspace)), return_exceptions=True)


processes = Processes()
