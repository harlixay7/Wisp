"""Process containment primitives for the Antigravity bridge.

Windows: the child is created suspended, assigned to a Win32 Job Object
configured with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, then resumed, and the
whole tree is killed on timeout or cancellation. POSIX: the child runs in its
own session and is killed through its process group.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
from typing import Any

CONTAINMENT_LABELS: dict[str, str] = {
    "job-object": "Windows Job Object + taskkill tree termination",
    "process-group": "POSIX session process group",
    "taskkill-fallback": "degraded: taskkill tree termination (Job Object unavailable)",
    "none": "none — containment could not be established",
}

_TERMINATE_WAIT_SECONDS = 5
_TASKKILL_TIMEOUT_SECONDS = 15

# ---------------------------------------------------------------- Win32 API

_CREATE_SUSPENDED = 0x0004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JOBOBJECTINFOCLASS value
_PROCESS_TERMINATE = 0x0001
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPPROCESS = 0x00000002
_TH32CS_SNAPTHREAD = 0x00000004

# INVALID_HANDLE_VALUE ((HANDLE)-1) as returned through a c_void_p restype.
# Its width follows the pointer size, so derive it rather than hardcode it.
# Compare it only against pointer-typed return values.
_INVALID_WINDOWS_HANDLE = ctypes.c_void_p(-1).value
# ResumeThread returns a DWORD, so its failure value is 32 bits wide on every
# architecture and never equals the pointer-sized INVALID_HANDLE_VALUE.
_RESUME_THREAD_FAILED = 0xFFFFFFFF  # (DWORD)-1

# With argtypes/restype declared, a ctypes call fails only by raising one of
# these; Win32 errors themselves come back as return values.
_WIN32_ERRORS = (OSError, ctypes.ArgumentError)


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryLimit", ctypes.c_size_t),
        ("PeakJobMemoryLimit", ctypes.c_size_t),
    ]


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_char * 260),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
    ]


_KERNEL32: Any = None


def _get_kernel32() -> Any:
    """Returns kernel32 with every function this module calls fully prototyped.

    Declaring argtypes/restype once keeps handles pointer-sized on 64-bit
    Python (the ctypes default restype is a 32-bit int, which truncates them).
    """
    global _KERNEL32
    if _KERNEL32 is None:
        kernel32 = ctypes.windll.kernel32
        handle = ctypes.c_void_p
        dword = ctypes.c_uint32
        prototypes: dict[str, tuple[Any, list[Any]]] = {
            "CreateJobObjectW": (handle, [ctypes.c_void_p, ctypes.c_wchar_p]),
            "SetInformationJobObject": (
                ctypes.c_int,
                [handle, ctypes.c_int, ctypes.c_void_p, dword],
            ),
            "AssignProcessToJobObject": (ctypes.c_int, [handle, handle]),
            "TerminateJobObject": (ctypes.c_int, [handle, dword]),
            "CloseHandle": (ctypes.c_int, [handle]),
            "OpenProcess": (handle, [dword, ctypes.c_int, dword]),
            "TerminateProcess": (ctypes.c_int, [handle, dword]),
            "CreateToolhelp32Snapshot": (handle, [dword, dword]),
            "Process32First": (ctypes.c_int, [handle, ctypes.POINTER(_PROCESSENTRY32)]),
            "Process32Next": (ctypes.c_int, [handle, ctypes.POINTER(_PROCESSENTRY32)]),
            "Thread32First": (ctypes.c_int, [handle, ctypes.POINTER(_THREADENTRY32)]),
            "Thread32Next": (ctypes.c_int, [handle, ctypes.POINTER(_THREADENTRY32)]),
            "OpenThread": (handle, [dword, ctypes.c_int, dword]),
            "ResumeThread": (dword, [handle]),
        }
        for name, (restype, argtypes) in prototypes.items():
            function = getattr(kernel32, name)
            function.restype = restype
            function.argtypes = argtypes
        _KERNEL32 = kernel32
    return _KERNEL32


def _is_valid_handle(handle: int | None) -> bool:
    return bool(handle) and handle != _INVALID_WINDOWS_HANDLE


# ------------------------------------------------------------- Job Objects


def _create_job_object() -> int | None:
    """Creates a kill-on-close Job Object, or returns None when unavailable.

    Containment is best-effort: without a job the caller falls back to
    taskkill-based tree termination and records the degraded mode.
    """
    if os.name != "nt":
        return None
    try:
        kernel32 = _get_kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not configured:
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except _WIN32_ERRORS:
        return None


def _assign_to_job_object(job_handle: int, process_handle: int) -> bool:
    """Adds a process to the job; False when the OS refuses the assignment."""
    try:
        return bool(_get_kernel32().AssignProcessToJobObject(job_handle, process_handle))
    except _WIN32_ERRORS:
        return False


def _close_job_object(job_handle: int | None) -> None:
    """Closes the job handle; with KILL_ON_JOB_CLOSE this kills its members."""
    if os.name != "nt" or not job_handle:
        return
    try:
        _get_kernel32().CloseHandle(job_handle)
    except _WIN32_ERRORS:
        pass


def _terminate_job_object(job_handle: int | None) -> None:
    if os.name != "nt" or not job_handle:
        return
    try:
        _get_kernel32().TerminateJobObject(job_handle, 1)
    except _WIN32_ERRORS:
        pass


# --------------------------------------------------------- process trees


def _taskkill_process_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _descendant_pids(root_pid: int) -> list[int]:
    """Return the PIDs of every live descendant of root_pid.

    taskkill /T walks the tree from the root, so it finds nothing once the
    root has exited, even while grandchildren still run. Windows does not
    reparent orphans, so their recorded parent PID still names the dead root.
    Walking a parent-to-child map built from a process snapshot reaches them
    either way.
    """
    if os.name != "nt":
        return []
    kernel32 = _get_kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not _is_valid_handle(snapshot):
        return []
    parent_to_children: dict[int, list[int]] = {}
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            parent = int(entry.th32ParentProcessID)
            child = int(entry.th32ProcessID)
            parent_to_children.setdefault(parent, []).append(child)
            ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    # Depth-first walk; ``seen`` guards against PID reuse creating a cycle.
    descendants: list[int] = []
    frontier = [root_pid]
    seen = {root_pid}
    while frontier:
        current = frontier.pop()
        for child in parent_to_children.get(current, ()):
            if child in seen:
                continue
            seen.add(child)
            descendants.append(child)
            frontier.append(child)
    return descendants


def _terminate_pid(pid: int) -> None:
    kernel32 = _get_kernel32()
    try:
        handle = kernel32.OpenProcess(_PROCESS_TERMINATE, 0, pid)
        if not handle:
            return
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)
    except _WIN32_ERRORS:
        pass


def _terminate_process_tree(proc: subprocess.Popen, job_handle: int | None) -> None:
    """Kills ``proc`` and everything it spawned, then waits briefly for it."""
    if os.name == "nt":
        # Job Object membership alone is unreliable: some launchers (e.g. venv
        # interpreters that re-exec through the WindowsApps Store alias) start
        # children outside the job, so TerminateJobObject can leave
        # grandchildren alive. Kill in three layers: (1) every descendant in a
        # process snapshot, which works even after the direct child exited;
        # (2) taskkill /T, which catches anything spawned since the snapshot;
        # (3) TerminateJobObject for whatever the job did capture.
        for pid in _descendant_pids(proc.pid):
            _terminate_pid(pid)
        _taskkill_process_tree(proc.pid)
        _terminate_job_object(job_handle)
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=_TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        return

    _signal_process_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=_TERMINATE_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=_TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _signal_process_group(pgid: int, sig: int) -> None:
    """Signals every member of a POSIX process group, tolerating an empty group.

    The child is started with ``start_new_session=True``, so its PID is also
    its process-group ID. Signalling that ID directly keeps working after the
    group leader has been reaped, as long as any member survives (os.getpgid
    would fail at that point). ProcessLookupError means nothing is left, and
    PermissionError means the remaining members are no longer ours to signal.
    """
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _resume_primary_thread(pid: int) -> None:
    """Resumes a process created with ``CREATE_SUSPENDED`` (Windows).

    ``subprocess.Popen`` keeps only the process handle, so the primary thread
    is located through the Toolhelp thread snapshot (the first thread of a
    suspended process) and resumed via ``OpenThread`` + ``ResumeThread``.
    Raises ``OSError`` when the thread cannot be found or resumed; the caller
    terminates the still-frozen process.
    """
    kernel32 = _get_kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not _is_valid_handle(snapshot):
        raise OSError(f"CreateToolhelp32Snapshot failed for pid {pid}")
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        thread_id: int | None = None
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread_id = int(entry.th32ThreadID)
                break
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        if thread_id is None:
            raise OSError(f"primary thread of pid {pid} not found in snapshot")
        thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, 0, thread_id)
        if not thread:
            raise OSError(f"OpenThread failed for tid {thread_id} (pid {pid})")
        try:
            if kernel32.ResumeThread(thread) == _RESUME_THREAD_FAILED:
                raise OSError(f"ResumeThread failed for tid {thread_id} (pid {pid})")
        finally:
            kernel32.CloseHandle(thread)
    finally:
        kernel32.CloseHandle(snapshot)
