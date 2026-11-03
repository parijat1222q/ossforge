"""Trusted lifetime launcher. Invoked by absolute path, never a target module.

The inherited job handle is configured by the parent. Fail before target launch
if Windows cannot associate this process with that job (including nested jobs).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import subprocess
import sys


def main():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.SetEvent.argtypes = [wintypes.HANDLE]
    api.SetEvent.restype = wintypes.BOOL
    handle, ready = map(int, sys.argv[1:3])
    if not api.AssignProcessToJobObject(handle, api.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    # Parent retains the only job handle; child cannot keep it alive on timeout.
    if not api.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())
    if not api.SetEvent(ready):
        raise ctypes.WinError(ctypes.get_last_error())
    if not api.CloseHandle(ready):
        raise ctypes.WinError(ctypes.get_last_error())
    return subprocess.call(sys.argv[3:], close_fds=True)


if __name__ == '__main__':
    sys.exit(main())
