"""Fake Win32 modules for clipboard ownership tests (GATE-4 FL-008).

Simulates just enough of user32/kernel32 to execute every failure path of
``tools.wisp_capture._set_clipboard_text_with`` deterministically and observe
GlobalAlloc/GlobalFree ownership without touching a real clipboard.
GlobalLock returns the address of a real backing buffer so the production
code's ctypes.memmove writes into memory the test owns.
"""

from __future__ import annotations

import ctypes
from typing import Any


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
