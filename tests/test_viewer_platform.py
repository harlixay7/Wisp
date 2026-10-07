"""Process and account helpers behave honestly on every operating system."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest

from tools import antigravity_viewer as viewer

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process inspection")
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows account switching")


def _sleeper(*extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", *extra],
        stdout=subprocess.DEVNULL,
    )


@POSIX_ONLY
class TestPosixProcessHelpers:
    def test_command_line_and_image_name_are_read(self) -> None:
        proc = _sleeper("marker-argument")
        try:
            command_line = viewer.pid_command_line(proc.pid) or ""
            assert "marker-argument" in command_line
            assert "python" in (viewer.pid_image_name(proc.pid) or "")
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_unrelated_python_process_is_not_a_viewer(self) -> None:
        proc = _sleeper()
        try:
            image = viewer.pid_image_name(proc.pid)
            assert viewer.looks_like_viewer_process(image, pid=proc.pid) is False
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_viewer_command_line_is_recognized_and_terminated(self) -> None:
        proc = _sleeper("antigravity_viewer")
        try:
            image = viewer.pid_image_name(proc.pid)
            assert viewer.looks_like_viewer_process(image, pid=proc.pid) is True
            assert viewer.terminate_process(proc.pid) is True
            assert proc.wait(timeout=10) != 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_missing_pid_has_no_name(self) -> None:
        proc = _sleeper()
        proc.kill()
        proc.wait(timeout=10)
        assert viewer.pid_command_line(proc.pid) is None

    def test_work_area_has_a_sane_default(self) -> None:
        left, top, right, bottom = viewer.work_area()
        assert right > left and bottom > top


class TestAccountSwitch:
    @POSIX_ONLY
    def test_switch_is_reported_unsupported_off_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def forbidden(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("no process may be spawned off Windows")

        monkeypatch.setattr(subprocess, "run", forbidden)
        monkeypatch.setattr(subprocess, "Popen", forbidden)
        assert viewer.switch_account() == {
            "status": "unsupported",
            "output": "Account switching is Windows-only.",
        }

    @WINDOWS_ONLY
    def test_sign_in_launches_without_a_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        launched: dict[str, Any] = {}

        def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        def fake_popen(args: Any, **kwargs: Any) -> object:
            launched["args"] = args
            launched["kwargs"] = kwargs
            return object()

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr(viewer, "resolve_agy_executable", lambda: r"C:\a b\agy & x.exe")
        result = viewer.switch_account()
        assert result["status"] == "switching"
        assert launched["args"] == [
            "cmd.exe",
            "/c",
            "start",
            "Antigravity Sign-In",
            "cmd.exe",
            "/k",
            r"C:\a b\agy & x.exe",
        ]
        assert not launched["kwargs"].get("shell")
