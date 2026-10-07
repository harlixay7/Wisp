"""Focused regression tests for specific bridge defects.

Each test pins one behavior that used to be wrong. None of them spawns the
real ``agy`` binary; the Win32 test drives a ctypes stub, so it runs on every
platform.
"""

from __future__ import annotations

import ctypes
import json
import types
from pathlib import Path

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


def _scripted(results: list[bridge.AttemptResult], calls: list[list[str]]):
    """Launcher fake that records each command and replays ``results`` in order."""

    def launcher(command, cwd, env, hard_timeout_seconds, raw_line_sink=None):
        calls.append([str(part) for part in command])
        result = results[min(len(calls), len(results)) - 1]
        result.command = tuple(calls[-1])
        return result

    return launcher


def _config(**overrides) -> bridge.BridgeConfig:
    defaults = dict(
        envelope=bridge.DelegationEnvelope(prompt="review"),
        workspace=Path.cwd(),
        retry_backoff_seconds=0.0,
        executable="agy-test",
    )
    defaults.update(overrides)
    return bridge.BridgeConfig(**defaults)


class TestSuccessIsNotRateLimited:
    def test_quota_text_inside_tool_result_does_not_trigger_failover(self) -> None:
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "step_update": {
                            "step_index": 1,
                            "tool_result": {
                                "path": "docs/quota.md",
                                "content": "On RESOURCE_EXHAUSTED (code 429) the bridge fails over.",
                            },
                        }
                    }
                ),
                json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "Findings: none."},
                    }
                ),
            ]
        )
        attempt = bridge.AttemptResult(exit_code=0, stdout=stdout, duration_seconds=0.01)
        calls: list[list[str]] = []

        result = bridge.run_bridge(_config(), launcher=_scripted([attempt], calls))

        assert result.success
        assert not result.failover_used
        assert not result.rate_limited
        assert len(calls) == 1
        assert result.to_dict()["attempts"][0]["rate_limited"] is False

    def test_failed_attempt_with_quota_text_is_still_rate_limited(self) -> None:
        attempt = bridge.AttemptResult(exit_code=1, stderr="RESOURCE_EXHAUSTED")

        assert attempt.rate_limited

