from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys


IS_WINDOWS = os.name == "nt"
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
GATE_WAIT_TIMEOUT_MS = 30_000


if IS_WINDOWS:
    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]


    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]


    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


    KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    KERNEL32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    KERNEL32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    KERNEL32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
    KERNEL32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    KERNEL32.TerminateJobObject.restype = wintypes.BOOL
    KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    KERNEL32.CloseHandle.restype = wintypes.BOOL
    KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    KERNEL32.OpenProcess.restype = wintypes.HANDLE
    KERNEL32.CreateEventW.argtypes = [
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    KERNEL32.CreateEventW.restype = wintypes.HANDLE
    KERNEL32.SetEvent.argtypes = [wintypes.HANDLE]
    KERNEL32.SetEvent.restype = wintypes.BOOL
    KERNEL32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    KERNEL32.WaitForSingleObject.restype = wintypes.DWORD
    KERNEL32.WaitForMultipleObjects.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    KERNEL32.WaitForMultipleObjects.restype = wintypes.DWORD


def windows_error(prefix: str, error: int | None = None) -> OSError:
    if error is None:
        error = ctypes.get_last_error()
    return OSError(error, f"{prefix}: {ctypes.FormatError(error)}")


class WindowsJob:
    """A Windows Job Object whose whole process tree dies with this handle."""

    def __init__(self) -> None:
        if not IS_WINDOWS:
            raise RuntimeError("Windows Job Objects are only available on Windows")
        handle = KERNEL32.CreateJobObjectW(None, None)
        if not handle:
            raise windows_error("CreateJobObjectW failed")
        self._handle: int | None = int(handle)
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not KERNEL32.SetInformationJobObject(
            wintypes.HANDLE(self._handle),
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = windows_error("SetInformationJobObject failed")
            KERNEL32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None
            raise error

    def launch(self, command: list[str], **popen_kwargs: object) -> subprocess.Popen:
        if self._handle is None:
            raise RuntimeError("cannot launch into a closed Windows Job Object")
        if not command:
            raise ValueError("command must not be empty")
        security = SECURITY_ATTRIBUTES(
            nLength=ctypes.sizeof(SECURITY_ATTRIBUTES),
            lpSecurityDescriptor=None,
            bInheritHandle=True,
        )
        gate_handle = KERNEL32.CreateEventW(
            ctypes.byref(security), True, False, None
        )
        if not gate_handle:
            raise windows_error("CreateEventW failed")
        parent_handle = KERNEL32.OpenProcess(SYNCHRONIZE, True, os.getpid())
        if not parent_handle:
            error = windows_error("OpenProcess for launcher failed")
            KERNEL32.CloseHandle(gate_handle)
            raise error
        process: subprocess.Popen | None = None
        try:
            wrapper_command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--gate-handle",
                str(int(gate_handle)),
                "--parent-handle",
                str(int(parent_handle)),
                "--",
                *(str(part) for part in command),
            ]
            # The inherited gate keeps the wrapper blocked until assignment.
            # The real launcher-process handle lets it escape if the launcher
            # dies in the narrow interval before AssignProcessToJobObject.
            popen_kwargs["close_fds"] = False
            process = subprocess.Popen(wrapper_command, **popen_kwargs)
            process_handle = getattr(process, "_handle", None)
            if process_handle is None:
                raise RuntimeError("Popen did not expose a Windows process handle")
            assigned = KERNEL32.AssignProcessToJobObject(
                wintypes.HANDLE(self._handle),
                wintypes.HANDLE(int(process_handle)),
            )
            if not assigned:
                raise windows_error(
                    "AssignProcessToJobObject failed", ctypes.get_last_error()
                )
            if not KERNEL32.SetEvent(gate_handle):
                raise windows_error("SetEvent failed")
            return process
        except BaseException:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            raise
        finally:
            KERNEL32.CloseHandle(gate_handle)
            KERNEL32.CloseHandle(parent_handle)

    def terminate(self, exit_code: int = 1) -> bool:
        if self._handle is None:
            return True
        return bool(
            KERNEL32.TerminateJobObject(
                wintypes.HANDLE(self._handle),
                wintypes.UINT(exit_code),
            )
        )

    def close(self) -> None:
        if self._handle is None:
            return
        # KILL_ON_JOB_CLOSE is the final guarantee even if explicit termination
        # was unnecessary or failed.
        KERNEL32.CloseHandle(wintypes.HANDLE(self._handle))
        self._handle = None

    def __enter__(self) -> WindowsJob:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def wrapper_main(arguments: list[str]) -> int:
    if not IS_WINDOWS:
        raise RuntimeError("the Windows Job wrapper cannot run on this platform")
    if (
        len(arguments) < 6
        or arguments[0] != "--gate-handle"
        or arguments[2] != "--parent-handle"
        or arguments[4] != "--"
    ):
        raise SystemExit(
            "usage: windows_process_job.py --gate-handle HANDLE "
            "--parent-handle HANDLE -- COMMAND [ARG ...]"
        )
    try:
        gate_handle = wintypes.HANDLE(int(arguments[1]))
        parent_handle = wintypes.HANDLE(int(arguments[3]))
    except ValueError as exc:
        raise SystemExit("invalid gate or parent-process handle") from exc
    command = arguments[5:]
    if not command:
        raise SystemExit("missing wrapped command")
    try:
        wait_handles = (wintypes.HANDLE * 2)(gate_handle, parent_handle)
        wait_result = KERNEL32.WaitForMultipleObjects(
            len(wait_handles), wait_handles, False, GATE_WAIT_TIMEOUT_MS
        )
        if wait_result == WAIT_OBJECT_0 + 1:
            # The launcher died before assigning this wrapper to its Job.
            return 1
        if wait_result == WAIT_TIMEOUT:
            raise RuntimeError("timed out waiting for Windows Job assignment gate")
        if wait_result == WAIT_FAILED:
            raise windows_error("WaitForMultipleObjects failed")
        if wait_result != WAIT_OBJECT_0:
            raise RuntimeError(f"unexpected Windows gate wait result: {wait_result}")
    finally:
        KERNEL32.CloseHandle(gate_handle)
        KERNEL32.CloseHandle(parent_handle)

    child = subprocess.Popen(command, close_fds=False)
    try:
        return child.wait()
    except BaseException:
        if child.poll() is None:
            try:
                child.kill()
                child.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise


if __name__ == "__main__":
    raise SystemExit(wrapper_main(sys.argv[1:]))
