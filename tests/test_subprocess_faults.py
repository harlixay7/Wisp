"""Real subprocess-fault integration tests (audit #39).

The main bridge suite uses ``ScriptedLauncher`` fakes; these tests launch real
OS processes through ``launch_contained`` to prove the containment and capture
machinery under fault conditions — no real ``agy`` and no quota involved:

* pipe flooding far past the 64 KB OS pipe buffer (#13 capture path),
* a child that spawns its own child (tree containment on timeout, #1),
* interleaved stdout/stderr concurrency with full capture on both streams,
* partial-output preservation on forced termination.

Each fault case is deterministic and time-bounded.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.antigravity_bridge import launch_contained, sanitize_environment

PYTHON = sys.executable


def _pid_alive(pid: int) -> bool:
    """True while ``pid`` runs; a zombie awaiting its reaper counts as dead."""
    if Path("/proc/self/stat").exists():
        stat = Path(f"/proc/{pid}/stat")
        try:
            # The state letter follows the parenthesized command name.
            state = stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state not in ("Z", "X")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run(script: str, timeout: int, tmp_path: Path):
    return launch_contained(
        [PYTHON, "-c", script],
        cwd=tmp_path,
        env=sanitize_environment(),
        hard_timeout_seconds=timeout,
    )


class TestPipeFlooding:
    def test_large_stdout_is_captured_untruncated(self, tmp_path: Path) -> None:
        # Script file (not -c): a 5 MB argv would exceed the Windows
        # CreateProcess command-line limit.
        writer = tmp_path / "flood.py"
        payload = "F" * (5 * 1024 * 1024)
        writer.write_text(f"print({payload!r})", encoding="utf-8")
        result = launch_contained(
            [PYTHON, str(writer)],
            cwd=tmp_path,
            env=sanitize_environment(),
            hard_timeout_seconds=60,
        )
        assert result.exit_code == 0
        assert len(result.stdout) >= 5 * 1024 * 1024
        stripped = result.stdout.strip()
        assert set(stripped) == {"F"}
        assert len(stripped) == 5 * 1024 * 1024

    def test_interleaved_streams_both_captured(self, tmp_path: Path) -> None:
        script = (
            "import sys\n"
            "for i in range(2000):\n"
            "    sys.stdout.write(f'out-{i}\\n')\n"
            "    sys.stderr.write(f'err-{i}\\n')\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
        )
        result = _run(script, timeout=60, tmp_path=tmp_path)
        assert result.exit_code == 0
        for index in (0, 1, 999, 1999):
            assert f"out-{index}" in result.stdout
            assert f"err-{index}" in result.stderr


class TestTreeContainment:
    @pytest.mark.skipif(os.name != "nt", reason="Job Object tree-kill semantics")
    def test_grandchild_dies_with_the_tree_on_timeout(self, tmp_path: Path) -> None:
        heartbeat = tmp_path / "heartbeat.txt"
        heartbeat.write_text("0", encoding="utf-8")
        grandchild = tmp_path / "grandchild.py"
        grandchild.write_text(
            "import time\n"
            f"handle = open({str(heartbeat)!r}, 'w')\n"
            "end = time.time() + 60\n"
            "while time.time() < end:\n"
            "    handle.write(str(time.time()))\n"
            "    handle.flush()\n"
            "    time.sleep(0.1)\n",
            encoding="utf-8",
        )
        parent = tmp_path / "parent.py"
        parent.write_text(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, "
            f"{str(grandchild)!r}], creationflags=0)\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        result = launch_contained(
            [PYTHON, str(parent)],
            cwd=tmp_path,
            env=sanitize_environment(),
            hard_timeout_seconds=3,
        )
        assert result.timed_out is True
        time.sleep(1.5)
        before = heartbeat.stat().st_mtime
        time.sleep(0.8)
        after = heartbeat.stat().st_mtime
        assert before == after, "grandchild survived the tree kill"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
    def test_group_member_left_behind_on_clean_exit_is_killed(self, tmp_path: Path) -> None:
        # The child exits 0 right away but leaves a sleeper in its process
        # group (like a stdio server agy spawned). Nothing may outlive the run.
        pid_file = tmp_path / "grandchild.pid"
        parent = tmp_path / "parent.py"
        parent.write_text(
            "import subprocess, sys\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            ")\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "print('parent-done', flush=True)\n",
            encoding="utf-8",
        )
        result = launch_contained(
            [PYTHON, str(parent)],
            cwd=tmp_path,
            env=sanitize_environment(),
            hard_timeout_seconds=30,
        )
        assert result.exit_code == 0
        assert "parent-done" in result.stdout
        grandchild = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while _pid_alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        alive = _pid_alive(grandchild)
        if alive:
            os.kill(grandchild, 9)
        assert not alive, "process-group member outlived launch_contained"

    def test_partial_output_survives_timeout_kill(self, tmp_path: Path) -> None:
        script = "print('early-lines', flush=True)\nimport time\ntime.sleep(120)\n"
        result = _run(script, timeout=2, tmp_path=tmp_path)
        assert result.timed_out is True
        assert "early-lines" in result.stdout


class TestContainmentReporting:
    def test_containment_mode_is_populated_for_real_processes(
        self, tmp_path: Path
    ) -> None:
        result = _run("print('ok')", timeout=30, tmp_path=tmp_path)
        assert result.containment in ("job-object", "taskkill-fallback", "process-group")

    def test_report_write_is_atomic_under_real_result(self, tmp_path: Path) -> None:
        from tools.antigravity_bridge import (
            AttemptResult,
            BridgeResult,
            REPORT_SCHEMA_VERSION,
            write_report,
        )

        result = BridgeResult(
            success=True,
            model_used="m",
            failover_used=False,
            timed_out=False,
            rate_limited=False,
            exit_code=0,
            attempts=[
                AttemptResult(
                    model="m", exit_code=0, stdout="{}" * 1000, containment="process-group"
                )
            ],
            critique_markdown="x",
        )
        path = write_report(tmp_path, result, keep_reports=DEFAULT_KEEP())
        data = __import__("json").loads(path.read_text(encoding="utf-8"))
        assert data["schema_version"] == REPORT_SCHEMA_VERSION
        assert data["attempts"][0]["containment"] == "process-group"


def DEFAULT_KEEP() -> int:
    from tools.antigravity_bridge import DEFAULT_KEEP_REPORTS

    return DEFAULT_KEEP_REPORTS
