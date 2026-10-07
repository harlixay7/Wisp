"""Focused regression tests for specific bridge defects.

Each test pins one behavior that used to be wrong. None of them spawns the
real ``agy`` binary; the Win32 test drives a ctypes stub, so it runs on every
platform.
"""

from __future__ import annotations

import ctypes
import inspect
import json
import os
import sys
import types
import typing
from pathlib import Path

import pytest

from tools import antigravity_bridge as bridge
from tools import antigravity_containment as containment


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
        monkeypatch.setattr(containment, "_get_kernel32", lambda: stub)

        with pytest.raises(OSError, match="ResumeThread failed"):
            containment._resume_primary_thread(4242)

        assert stub.closed == [0x2000, 0x1000], "thread and snapshot handles must close"

    def test_previous_suspend_count_is_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _kernel32_stub(pid=4242, resume_result=1)
        monkeypatch.setattr(containment, "_get_kernel32", lambda: stub)

        containment._resume_primary_thread(4242)

    def test_sentinel_is_not_the_pointer_sized_handle_on_64_bit(self) -> None:
        assert containment._RESUME_THREAD_FAILED == 0xFFFFFFFF
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            assert containment._RESUME_THREAD_FAILED != containment._INVALID_WINDOWS_HANDLE


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
                                "content": "On RESOURCE_EXHAUSTED (code 429) we fail over.",
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

    def test_clean_exit_with_only_a_quota_message_fails_over(self) -> None:
        quota = bridge.AttemptResult(
            exit_code=0,
            stdout="Error: RESOURCE_EXHAUSTED (code 429): Individual quota reached",
            duration_seconds=0.01,
        )
        recovered = bridge.AttemptResult(
            exit_code=0,
            stdout=json.dumps({"event": "result", "result": "Findings: none."}),
            duration_seconds=0.01,
        )
        calls: list[list[str]] = []

        result = bridge.run_bridge(_config(), launcher=_scripted([quota, recovered], calls))

        assert quota.rate_limited
        assert result.success
        assert result.failover_used
        assert len(calls) == 2

    def test_failed_attempt_with_quota_text_is_still_rate_limited(self) -> None:
        attempt = bridge.AttemptResult(exit_code=1, stderr="RESOURCE_EXHAUSTED")

        assert attempt.rate_limited


class TestDryRunMatchesDispatch:
    SKILLS = ("adversarial-plan-hardening-engine",)
    RECOMMENDED = ("zero-trust-ast-wiring-verifier",)

    def test_dry_run_prints_the_command_run_bridge_executes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The shipped two-skill render exceeds the real Windows command line;
        # this test is about parity, not the size preflight.
        monkeypatch.setattr(bridge, "_WINDOWS_COMMAND_LINE_LIMIT", 10**9)
        exit_code = bridge.main(
            [
                "--prompt",
                "Review the plan",
                "--workspace",
                str(tmp_path),
                "--skills",
                ",".join(self.SKILLS),
                "--recommended-skills",
                ",".join(self.RECOMMENDED),
                "--executable",
                "agy-test",
                "--dry-run",
            ]
        )
        assert exit_code == 0
        dry_run = json.loads(capsys.readouterr().out)

        calls: list[list[str]] = []
        success = bridge.AttemptResult(exit_code=0, stdout="CRITIQUE")
        config = _config(
            envelope=bridge.DelegationEnvelope(prompt="Review the plan"),
            workspace=tmp_path,
            skills=self.SKILLS,
            recommended_skills=self.RECOMMENDED,
        )
        assert bridge.run_bridge(config, launcher=_scripted([success], calls)).success

        dispatched = calls[0]
        assert dry_run["command"] == dispatched
        assert dry_run["payload"] == dispatched[dispatched.index("-p") + 1]
        assert "## ADVERSARIAL SKILL REGISTRY MANIFEST" in dry_run["payload"]
        # The workspace has no Skills/, so the shipped registry must be mounted.
        assert dry_run["command"].count("--add-dir") == 2

    @pytest.mark.skipif(os.name != "nt", reason="Windows CreateProcess argv limit")
    def test_dry_run_applies_the_windows_argv_preflight(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(bridge, "_WINDOWS_COMMAND_LINE_LIMIT", 100)
        exit_code = bridge.main(
            ["--prompt", "x" * 200, "--workspace", str(tmp_path), "--dry-run"]
        )
        assert exit_code == 2
        assert "command line" in capsys.readouterr().err


class TestQuotaHookScope:
    def test_hook_does_not_run_after_a_non_rate_limit_failure(self, tmp_path: Path) -> None:
        # The quota wait re-runs the primary model; that rerun fails for an
        # unrelated reason. Rotating credentials cannot help, so the operator
        # hook must not run.
        marker = tmp_path / "hook-ran"
        hook = f'"{sys.executable}" -c "open({str(marker)!r}, \'w\').close()"'
        rate_limited = bridge.AttemptResult(
            exit_code=1, stderr="RESOURCE_EXHAUSTED: quota reached. Resets in 0s."
        )
        fatal = bridge.AttemptResult(exit_code=1, stdout="boom", stderr="fatal: bad flag")
        calls: list[list[str]] = []

        result = bridge.run_bridge(
            _config(quota_wait_seconds=10, quota_hook=hook),
            launcher=_scripted([rate_limited, fatal], calls),
        )

        assert not result.success
        assert not result.quota_hook_used
        assert result.quota_hook_output is None
        assert not marker.exists()
        assert len(calls) == 2
        assert not result.failover_used

    def test_hook_runs_when_still_rate_limited_after_the_wait(self, tmp_path: Path) -> None:
        hook = f'"{sys.executable}" -c "print(1)"'
        rate_limited = bridge.AttemptResult(
            exit_code=1, stderr="RESOURCE_EXHAUSTED: quota reached. Resets in 0s."
        )
        success = bridge.AttemptResult(exit_code=0, stdout="CRITIQUE")
        calls: list[list[str]] = []

        result = bridge.run_bridge(
            _config(quota_wait_seconds=10, quota_hook=hook),
            launcher=_scripted([rate_limited, rate_limited, success], calls),
        )

        assert result.success
        assert result.quota_hook_used
        assert len(calls) == 3


class TestLaunchFnContract:
    def test_launch_fn_declares_the_raw_line_sink_parameter(self) -> None:
        # run_bridge calls the launcher with five positional arguments; the
        # type must describe the same contract launch_contained implements.
        parameters, returns = typing.get_args(bridge.LaunchFn)
        assert len(parameters) == 5
        assert parameters[4] == (bridge.RawLineSink | None)
        assert returns is bridge.AttemptResult
        signature = inspect.signature(bridge.launch_contained)
        assert len(signature.parameters) == len(parameters)
