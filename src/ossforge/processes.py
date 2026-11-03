"""Native subprocess trees, with cleanup completed before source restoration.

POSIX uses sessions. Windows uses a non-breakaway Job Object. A trusted launcher
joins the job BEFORE starting the requested command; no assign-after-spawn race.
This controls lifetime, not file/network access or hostile-code isolation.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ASSIGNMENT_TIMEOUT = 15


class CleanupUnconfirmed(RuntimeError):
    """Source restoration must stop because native workers may still be alive."""


class Job:
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes = ctypes
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)

        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', w.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ['ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount']]

        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in
                        ['TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime']]
            _fields_ += [(name, w.DWORD) for name in
                         ['TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses']]

        self.Accounting = Accounting
        for name, args, result in [
            ('CreateJobObjectW', [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            ('CreateEventW', [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
            ('WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD),
            ('SetInformationJobObject', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            ('QueryInformationJobObject', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            ('TerminateJobObject', [w.HANDLE, w.UINT], w.BOOL),
            ('CloseHandle', [w.HANDLE], w.BOOL),
        ]:
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.ready = self.api.CreateEventW(None, True, False, None)
        if not self.ready:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            self.api.CloseHandle(self.ready)
            self.ready = None
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway flags
        try:
            self.checked(self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def await_assignment(self, proc):
        deadline = time.monotonic() + ASSIGNMENT_TIMEOUT
        while True:
            result = self.api.WaitForSingleObject(self.ready, 20)
            if result == 0:
                return
            if result != 258:
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            if proc.poll() is not None:
                raise OSError('Windows job launcher exited before assignment; target not confirmed started')
            if time.monotonic() >= deadline:
                raise OSError('Windows job assignment readiness timed out; target not confirmed started')

    def checked(self, result):
        if not result:
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def _terminate(self):
        self.checked(self.api.TerminateJobObject(self.handle, 124))
        # Termination is asynchronous. Do not restore source under dying workers.
        deadline = time.monotonic() + 30
        while True:
            accounting = self.Accounting()
            self.checked(self.api.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(accounting),
                                                           self.ctypes.sizeof(accounting), None))
            if accounting.ActiveProcesses == 0:
                return
            if time.monotonic() >= deadline:
                raise CleanupUnconfirmed('native process tree cleanup unconfirmed; preserve source for inspection')
            time.sleep(0.01)

    def terminate(self):
        try:
            self._terminate()
        except OSError as exc:
            raise CleanupUnconfirmed('native process tree cleanup failed; preserve source for inspection') from exc

    def close(self):
        if self.ready:
            self.checked(self.api.CloseHandle(self.ready))
            self.ready = None
        if self.handle:
            self.checked(self.api.CloseHandle(self.handle))
            self.handle = None


def kill_tree(proc, job=None):
    if job is not None:
        job.terminate()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@contextlib.contextmanager
def launched(command, **kwargs):
    job = None
    proc = None
    assigned = False
    try:
        if os.name == 'nt':
            job = Job()
            info = subprocess.STARTUPINFO()
            info.lpAttributeList = {'handle_list': [job.handle, job.ready]}
            os.set_handle_inheritable(job.handle, True)
            os.set_handle_inheritable(job.ready, True)
            try:
                proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name('windows_launcher.py')),
                                         str(job.handle), str(job.ready), *command], startupinfo=info, close_fds=True, **kwargs)
            finally:
                os.set_handle_inheritable(job.handle, False)
                os.set_handle_inheritable(job.ready, False)
            job.await_assignment(proc)
            assigned = True
        else:
            proc = subprocess.Popen(command, start_new_session=True, **kwargs)
        yield proc, job
    finally:
        try:
            if proc is not None and proc.poll() is None:
                if job is not None and not assigned:
                    # A not-yet-assigned launcher must not join/spawn after a
                    # zero-active-process cleanup query. Stop it first.
                    proc.kill()
                kill_tree(proc, job)
                proc.wait()
        finally:
            if job is not None:
                # Also remove any workers left behind by a normally exiting command.
                try:
                    job.terminate()
                finally:
                    job.close()
