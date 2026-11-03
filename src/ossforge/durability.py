"""Flush local state without pretending Windows has POSIX directory fsync.

On Windows file contents are flushed before an atomic same-volume replacement
with MoveFileExW WRITE_THROUGH. This is not a power-loss guarantee for parent
or ancestor directory entries. State must live on a local filesystem.
"""
from __future__ import annotations

import os
from pathlib import Path


def replace(source: Path, destination: Path) -> None:
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        if not move(str(source.absolute()), str(destination.absolute()), 1 | 8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        source.replace(destination)
        sync_directory(destination.parent)


def sync_directory(path: Path) -> None:
    if os.name == 'nt':
        # Namespace durability differs; file data has already been fsynced.
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
