"""Focused regression tests for specific bridge defects.

Each test pins one behavior that used to be wrong. None of them spawns the
real ``agy`` binary; the Win32 test drives a ctypes stub, so it runs on every
platform.
"""

from __future__ import annotations

import ctypes
import types

import pytest

from tools import antigravity_bridge as bridge


def _kernel32_stub(pid: int, resume_result: int) -> types.SimpleNamespace:
    """Minimal kernel32 fake: one thread owned by ``pid``; ResumeThread scripted."""
    closed: list[int] = []

    def create_snapshot(flags, process_id):
        return 0x1000

    def thread32_first(snapshot, entry_ref):
        entry = entry_ref._obj
        entry.th32OwnerProcessID = pid
        entry.th32ThreadID = 77
        return 1

    def thread32_next(snapshot, entry_ref):
        return 0

    def open_thread(access, inherit, thread_id):
        return 0x2000

    def resume_thread(handle):
        return resume_result

    def close_handle(handle):
        closed.append(handle)
        return 1

    return types.SimpleNamespace(
        CreateToolhelp32Snapshot=create_snapshot,
        Thread32First=thread32_first,
        Thread32Next=thread32_next,
        OpenThread=open_thread,
        ResumeThread=resume_thread,
        CloseHandle=close_handle,
        closed=closed,
    )


class TestResumeThreadFailureDetection:
    def test_dword_failure_sentinel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ResumeThread returns (DWORD)-1 on failure. On 64-bit Python the
        # pointer-sized INVALID_HANDLE_VALUE is 0xFFFFFFFFFFFFFFFF, so the
        # failure must be recognized by its 32-bit value.
        stub = _kernel32_stub(pid=4242, resume_result=0xFFFFFFFF)
        monkeypatch.setattr(bridge, "_get_kernel32", lambda: stub)

        with pytest.raises(OSError, match="ResumeThread failed"):
            bridge._resume_primary_thread(4242)

        assert stub.closed == [0x2000, 0x1000], "thread and snapshot handles must close"

    def test_previous_suspend_count_is_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _kernel32_stub(pid=4242, resume_result=1)
        monkeypatch.setattr(bridge, "_get_kernel32", lambda: stub)

        bridge._resume_primary_thread(4242)

    def test_sentinel_is_not_the_pointer_sized_handle_on_64_bit(self) -> None:
        assert bridge._RESUME_THREAD_FAILED == 0xFFFFFFFF
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            assert bridge._RESUME_THREAD_FAILED != bridge._INVALID_WINDOWS_HANDLE
