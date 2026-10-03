"""OS primitives for non-overwriting moves and race-resistant authorized reads."""
import ctypes
import os
from pathlib import Path
import stat
import sys


def read_regular(path: Path, maximum: int) -> bytes:
    if os.name == 'posix':
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_BINARY)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Not a regular file')
        return stream.read(maximum + 1)


def move_no_replace(source: Path, destination: Path):
    if os.name == 'nt':
        # Windows os.rename rejects existing destinations, including directories.
        os.rename(source, destination)
        return
    library = ctypes.CDLL(None, use_errno=True)
    left, right = os.fsencode(source), os.fsencode(destination)
    if sys.platform == 'darwin':
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(left, right, 0x00000004)  # RENAME_EXCL
    elif hasattr(library, 'renameat2'):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, left, -100, right, 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise RuntimeError('This host lacks an atomic non-overwriting directory move primitive')
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))
