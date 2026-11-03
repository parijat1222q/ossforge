"""Local-filesystem exclusive locks shared by every state writer.

Lock files are dedicated sentinels, never removed or replaced while in use.
Windows participants lock byte zero, including when the file is still empty.
"""
from __future__ import annotations

import errno
import os
import time

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


def acquire(file, *, blocking: bool = False) -> None:
    if os.name != 'nt':
        fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    while True:
        position = file.tell()
        try:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if not blocking:
                raise BlockingIOError(exc.errno, str(exc)) from exc
        finally:
            file.seek(position)
        # LK_LOCK stops after ten seconds, unlike the POSIX budget lock.
        time.sleep(0.05)


def release(file) -> None:
    if os.name != 'nt':
        fcntl.flock(file, fcntl.LOCK_UN)
        return
    position = file.tell()
    try:
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        file.seek(position)
