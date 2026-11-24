"""Executed inside OS sandbox before project code; no project imports.

argv: mode ('command' or 'browser'), lifeline fd ('-' for none), executable, args...
With a lifeline, this process stays as a small supervisor: the backend holds the
pipe's write end for its whole life, so end-of-file means the backend exited (even
killed with SIGKILL) and the command's process group is killed instead of running on
as an orphan. macOS has no parent-death signal; Linux uses bwrap --die-with-parent.
"""
import os
import resource
import signal
import sys
import threading

# Bundlers open every module of a dependency at once (Vite/Rollup on a UI library:
# well over a thousand files); when descriptors run out they report an unrelated
# "failed to resolve import", never the limit.
OPEN_FILES = 4096
# CPU time of one process, all threads summed. A backstop for a process that escapes
# its group's kill, not a work limit: the platform's wall-clock timers (30 minutes for
# a command, the service lifetime for a kept service) end ordinary work first, and a
# parallel build or a long-lived development server must never reach it.
CPU_SECONDS = 4 * 3600


def cap(kind, wanted):
    """Lower a limit to `wanted`, or to the host's own hard limit when that is lower."""
    hard = resource.getrlimit(kind)[1]
    value = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
    resource.setrlimit(kind, (value, value))


cap(resource.RLIMIT_CORE, 0)
cap(resource.RLIMIT_FSIZE, 64 * 1024 * 1024)
cap(resource.RLIMIT_NOFILE, OPEN_FILES)
cap(resource.RLIMIT_CPU, CPU_SECONDS)
if sys.platform == 'linux' and sys.argv[1] != 'browser':
    # V8 needs a large virtual address reservation even with low physical memory
    # use. This is only an address-space ceiling, not a process-tree RAM quota.
    resource.setrlimit(resource.RLIMIT_AS, (64 * 1024**3, 64 * 1024**3))
lifeline = None if sys.argv[2] == '-' else int(sys.argv[2])
command = sys.argv[3:]
if lifeline is None:
    os.execv(command[0], command)

child = os.fork()
if child == 0:
    os.close(lifeline)
    os.execv(command[0], command)


def watch():
    # Blocks until the backend closes its write end, i.e. exits.
    while os.read(lifeline, 1):
        pass
    os.killpg(0, signal.SIGKILL)


threading.Thread(target=watch, daemon=True).start()
# The backend signals the whole group; the supervisor must not outlive the command.
signal.signal(signal.SIGTERM, lambda *_: os.killpg(0, signal.SIGKILL))
_, status = os.waitpid(child, 0)
if os.WIFSIGNALED(status):
    # Report the command's own signal (exit_code -N), not a supervisor exit code.
    signal.signal(os.WTERMSIG(status), signal.SIG_DFL)
    os.kill(os.getpid(), os.WTERMSIG(status))
sys.exit(os.WEXITSTATUS(status))
