"""Process containment primitives for the Antigravity bridge.

Windows: Win32 Job Objects (``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``), suspended
launch and resume, and process-tree termination. POSIX: process-group
signalling for children started in their own session.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
from typing import Any

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


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


_KERNEL32: Any = None


def _get_kernel32() -> Any:
    global _KERNEL32
    if _KERNEL32 is None:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        _KERNEL32 = kernel32
    return _KERNEL32


def _create_job_object() -> int | None:
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
    except Exception:
        return None


def _close_job_object(job_handle: int | None) -> None:
    if os.name != "nt" or not job_handle:
        return
    try:
        _get_kernel32().CloseHandle(job_handle)
    except Exception:
        pass


def _terminate_job_object(job_handle: int | None) -> None:
    if os.name != "nt" or not job_handle:
        return
    try:
        _get_kernel32().TerminateJobObject(job_handle, 1)
    except Exception:
        pass


def _taskkill_process_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass


def _descendant_pids(root_pid: int) -> list[int]:
    """Returns all live descendant PIDs of ``root_pid`` via the process snapshot.

    Used because taskkill /T cannot walk a tree whose root has already exited
    (GATE-4 FL-004): a child that terminates while its own children live
    orphans them to any snapshot-based walk that starts from the dead PID.
    The BFS below starts from the *recorded* root while it is still alive and
    follows the parent->child chains present in the snapshot.
    """
    if os.name != "nt":
        return []
    kernel32 = _get_kernel32()
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.Process32First.restype = ctypes.c_int
    kernel32.Process32First.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.Process32Next.restype = ctypes.c_int
    kernel32.Process32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

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

    _TH32CS_SNAPPROCESS = 0x00000002
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or int(snapshot) == _INVALID_WINDOWS_HANDLE:
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
    descendants: list[int] = []
    frontier = [root_pid]
    seen = {root_pid}
    while frontier:
        current = frontier.pop()
        for child in parent_to_children.get(current, ()):  # BFS over live chains
            if child in seen:
                continue
            seen.add(child)
            descendants.append(child)
            frontier.append(child)
    return descendants


def _terminate_process_tree(proc: subprocess.Popen, job_handle: int | None) -> None:
    if os.name == "nt":
        # Belt and braces (verified empirically 2026-10): in some environments
        # (e.g. venv interpreters that re-exec through the WindowsApps Store
        # alias) child processes DO NOT inherit Job Object membership, so
        # TerminateJobObject alone leaves grandchildren alive. Three
        # mechanisms, ordered: (1) explicit descendant walk via the process
        # snapshot — works even when the direct child has already exited
        # (GATE-4 FL-004); (2) taskkill /T while the direct child is alive;
        # (3) TerminateJobObject for whatever the job did contain.
        for pid in _descendant_pids(proc.pid):
            try:
                handle = _get_kernel32().OpenProcess(0x0001, 0, pid)  # PROCESS_TERMINATE
                if handle:
                    try:
                        _get_kernel32().TerminateProcess(handle, 1)
                    finally:
                        _get_kernel32().CloseHandle(handle)
            except Exception:
                pass
        _taskkill_process_tree(proc.pid)
        _terminate_job_object(job_handle)
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return

    _signal_process_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
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


CONTAINMENT_LABELS: dict[str, str] = {
    "job-object": "Windows Job Object + taskkill tree termination",
    "process-group": "POSIX session process group",
    "taskkill-fallback": "degraded: taskkill tree termination (Job Object unavailable)",
    "none": "none — containment could not be established",
}

_CREATE_SUSPENDED = 0x0004
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
# (HANDLE)-1 as returned by Win32 through c_void_p: 32-bit on 32-bit
# Python, 0xFFFF_FFFF_FFFF_FFFF on 64-bit. Compute, never hardcode
# (fresh audit AST-002: the 32-bit literal never matched on x64).
_INVALID_WINDOWS_HANDLE = ctypes.c_void_p(-1).value
# ResumeThread returns a DWORD, so its failure sentinel is 32 bits wide on
# every architecture and must not be compared against the pointer-sized
# INVALID_HANDLE_VALUE.
_RESUME_THREAD_FAILED = 0xFFFFFFFF  # (DWORD)-1


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


def _resume_primary_thread(pid: int) -> None:
    """Resumes a process created with ``CREATE_SUSPENDED`` (Windows).

    ``subprocess.Popen`` keeps only the process handle, so the primary thread
    is located through the Toolhelp thread snapshot (the first thread of a
    suspended process) and resumed via ``OpenThread`` + ``ResumeThread``.
    Raises ``OSError`` when the thread cannot be found or resumed; the caller
    terminates the still-frozen process.
    """
    kernel32 = _get_kernel32()
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.Thread32First.restype = ctypes.c_int
    kernel32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_THREADENTRY32)]
    kernel32.Thread32Next.restype = ctypes.c_int
    kernel32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_THREADENTRY32)]
    kernel32.OpenThread.restype = ctypes.c_void_p
    kernel32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.ResumeThread.restype = ctypes.c_uint32
    kernel32.ResumeThread.argtypes = [ctypes.c_void_p]

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or int(snapshot) == _INVALID_WINDOWS_HANDLE:
        raise OSError(f"CreateToolhelp32Snapshot failed for pid {pid}")
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        thread_id: int | None = None
        if kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            while True:
                if entry.th32OwnerProcessID == pid:
                    thread_id = int(entry.th32ThreadID)
                    break
                if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
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
