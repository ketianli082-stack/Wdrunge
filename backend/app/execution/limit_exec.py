"""Executed inside OS sandbox before project code; no project imports."""
import os
import resource
import sys

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
if sys.platform == 'linux' and sys.argv[1] != 'browser':
    # V8 needs a large virtual address reservation even with low physical memory
    # use. This is only an address-space ceiling, not a process-tree RAM quota.
    resource.setrlimit(resource.RLIMIT_AS, (64 * 1024**3, 64 * 1024**3))
os.execv(sys.argv[2], sys.argv[2:])
