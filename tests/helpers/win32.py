"""Win32 test helpers: user32/kernel32 clipboard fakes and Toolhelp PID utilities.

The fakes simulate just enough of user32/kernel32 to drive every failure path
of ``tools.wisp_capture._set_clipboard_text_with`` and observe
GlobalAlloc/GlobalFree ownership without touching a real clipboard. GlobalLock
returns the address of a real backing buffer, so the production code's
``ctypes.memmove`` writes into memory the test owns. The PID helpers call the
real Windows API and are only usable on Windows.
"""

from __future__ import annotations

import ctypes
from typing import Any

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class FakeKernel32:
    def __init__(self, *, lock_ok: bool = True) -> None:
        self.allocated = 4242
        self.freed: list[int] = []
        self.lock_calls = 0
        self.lock_ok = lock_ok
        self._backing = ctypes.create_string_buffer(256)

    def GlobalAlloc(self, flags: int, size: int) -> int:
        return self.allocated

    def GlobalLock(self, handle: int) -> Any:
        self.lock_calls += 1
        if not self.lock_ok:
            return None
        return ctypes.addressof(self._backing)

    def GlobalUnlock(self, handle: int) -> None:
        return None

    def GlobalFree(self, handle: int) -> None:
        self.freed.append(handle)


class FakeUser32:
    def __init__(
        self,
        *,
        open_clipboard: bool,
        set_succeeds: bool,
    ) -> None:
        self.empty_calls = 0
        self.set_arg: int | None = None
        self.close_calls = 0
        self._open_clipboard = open_clipboard
        self._set_succeeds = set_succeeds

    def OpenClipboard(self, owner: Any) -> int:
        return 1 if self._open_clipboard else 0

    def EmptyClipboard(self) -> None:
        self.empty_calls += 1

    def SetClipboardData(self, fmt: int, handle: int) -> Any:
        self.set_arg = handle
        return handle if self._set_succeeds else None

    def CloseClipboard(self) -> None:
        self.close_calls += 1


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


_KERNEL32 = None


def _kernel32_instance():
    """Lazily loads a PRIVATE kernel32 instance.

    Created on first use (Windows-only paths) so importing this module never
    touches Win32 on POSIX, and so its function prototypes stay private -
    the shared ctypes.windll instances let one module's argtypes break
    another module's calls.
    """
    global _KERNEL32
    if _KERNEL32 is None:
        _KERNEL32 = ctypes.WinDLL("kernel32")
    return _KERNEL32


def pid_alive(pid: int) -> bool:
    """True when a process with this PID still exists (Toolhelp snapshot)."""
    kernel32 = _kernel32_instance()
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.Process32First.restype = ctypes.c_int
    kernel32.Process32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32)]
    kernel32.Process32Next.restype = ctypes.c_int
    kernel32.Process32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32)]
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    # A c_void_p restype yields a pointer-sized int: INVALID_HANDLE_VALUE is
    # 0xFFFFFFFFFFFFFFFF on 64-bit Python, never the 32-bit 0xFFFFFFFF.
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return False
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                if int(entry.th32ProcessID) == pid:
                    return True
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    return False
        return False
    finally:
        kernel32.CloseHandle(snapshot)


def terminate_pids(pids: list[int]) -> None:
    """Best-effort TerminateProcess for every PID in ``pids``."""
    kernel32 = _kernel32_instance()
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    for pid in pids:
        handle = kernel32.OpenProcess(_PROCESS_TERMINATE, 0, pid)
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
