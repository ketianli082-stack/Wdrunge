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

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
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
