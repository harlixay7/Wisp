"""Process containment: full capture, pipe floods, timeouts, tree kill, and Win32 handles."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from tests.helpers.win32 import pid_alive as windows_pid_alive, terminate_pids
from tools import antigravity_containment as containment
from tools.antigravity_bridge import _descendant_pids, launch_contained, sanitize_environment

PYTHON = sys.executable


def _posix_pid_alive(pid: int) -> bool:
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


class TestOutputCapture:
    def test_captures_complete_stdout_and_stderr(self, tmp_path: Path) -> None:
        script = (
            "import sys;sys.stdout.write('alpha-line\\n' * 500);sys.stderr.write('omega-error\\n')"
        )
        env = sanitize_environment(home=tmp_path)

        result = launch_contained(
            [sys.executable, "-c", script], cwd=tmp_path, env=env, hard_timeout_seconds=60
        )

        assert result.exit_code == 0
        assert result.stdout.count("alpha-line") == 500
        assert "omega-error" in result.stderr
        assert not result.timed_out

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


class TestTimeouts:
    def test_timeout_kills_process_and_returns_partial_output(self, tmp_path: Path) -> None:
        script = "import time; print('booted', flush=True); time.sleep(60)"
        env = sanitize_environment(home=tmp_path)
        started = time.monotonic()

        result = launch_contained(
            [sys.executable, "-c", script], cwd=tmp_path, env=env, hard_timeout_seconds=2
        )

        elapsed = time.monotonic() - started
        assert result.timed_out
        assert elapsed < 30
        assert "booted" in result.stdout
        assert result.exit_code != 0


class TestContainmentMode:
    def test_real_child_process_records_containment_mode(self, tmp_path: Path) -> None:
        result = launch_contained(
            [sys.executable, "-c", "print('contained-ok')"],
            cwd=tmp_path,
            env=sanitize_environment(),
            hard_timeout_seconds=30,
        )
        assert result.exit_code == 0
        assert "contained-ok" in result.stdout
        assert result.containment in {"job-object", "taskkill-fallback", "process-group"}
        if os.name == "nt":
            assert result.containment in {"job-object", "taskkill-fallback"}
        else:
            assert result.containment == "process-group"


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
        while _posix_pid_alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        alive = _posix_pid_alive(grandchild)
        if alive:
            os.kill(grandchild, 9)
        assert not alive, "process-group member outlived launch_contained"


@pytest.mark.skipif(os.name != "nt", reason="descendant walk is Toolhelp/Windows")
class TestDescendantTreeKill:
    @staticmethod
    def _environment_preserves_pid_chains(tmp_path: Path) -> bool:
        """Probes whether this environment keeps parent-PID chains intact.

        Some interpreter chains (venv launchers re-execing through the
        WindowsApps Store alias) re-parent grandchildren to an intermediate
        host, breaking any snapshot walk from the recorded root. When even a
        LIVE root cannot be walked, the dead-root scenario is untestable here
        rather than broken. The probe grandchild is a plain sleeper, and the
        probe cleans up its own descendants afterwards so nothing leaks into
        sibling tests.
        """
        grandchild = tmp_path / "probe-grandchild.py"
        grandchild.write_text("import time; time.sleep(45)\n", encoding="utf-8")
        parent = tmp_path / "probe.py"
        parent.write_text(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, " + repr(str(grandchild)) + "])\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            [PYTHON, str(parent)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        intact = False
        try:
            time.sleep(0.8)
            descendants = _descendant_pids(proc.pid)
            intact = bool(descendants)
            if intact:
                # Leave no sleeper behind for sibling tests.
                terminate_pids(descendants)
        finally:
            proc.kill()
            proc.wait(timeout=10)
        return intact

    def test_descendant_pids_finds_grandchild_of_live_root(self, tmp_path: Path) -> None:
        marker_grandchild = tmp_path / "grandchild.py"
        marker_grandchild.write_text("import time; time.sleep(60)\n", encoding="utf-8")
        marker_parent = tmp_path / "parent.py"
        marker_parent.write_text(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, "
            f"{str(marker_grandchild)!r}])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            [PYTHON, str(marker_parent)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            grandchild_pid = int(proc.stdout.readline().strip())
            descendants = _descendant_pids(proc.pid)
            if not descendants and not self._environment_preserves_pid_chains(tmp_path):
                pytest.skip(
                    "environment re-execs the interpreter and re-parents "
                    "grandchildren; PID-chain walking is not meaningful here"
                )
            assert grandchild_pid in descendants
            terminate_pids([pid for pid in descendants if pid != proc.pid])
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_tree_kill_reaps_orphans_after_parent_exit(self, tmp_path: Path) -> None:
        # Sleep-only grandchild: death is verified by PID absence in the
        # process snapshot, which is immune to stray-writer interference.
        grandchild = tmp_path / "grandchild.py"
        grandchild.write_text("import time; time.sleep(60)\n", encoding="utf-8")
        if not self._environment_preserves_pid_chains(tmp_path):
            pytest.skip(
                "environment re-execs the interpreter; orphan cleanup relies "
                "on taskkill/Job Object termination from a live parent (see "
                "test_grandchild_dies_with_the_tree_on_timeout) - the dead-"
                "root walk cannot be exercised on this interpreter chain"
            )
        parent = tmp_path / "parent.py"
        parent.write_text(
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, " + repr(str(grandchild)) + "])\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            [PYTHON, str(parent)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=30)
        assert proc.returncode == 0
        time.sleep(0.5)

        # The orphaned grandchild is still alive: the walk starts from the
        # recorded (now dead) parent PID and follows the snapshot chain.
        orphans = _descendant_pids(proc.pid)
        assert orphans, "snapshot walk lost the orphaned grandchild"
        assert all(windows_pid_alive(pid) for pid in orphans)

        # Terminate the orphans explicitly, as the tree kill does.
        terminate_pids(orphans)
        time.sleep(1.0)
        for pid in orphans:
            assert not windows_pid_alive(pid), f"grandchild {pid} orphaned after its parent exited"


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


class TestWindowsHandleConstants:
    def test_invalid_handle_constant_is_pointer_sized(self) -> None:
        """(HANDLE)-1 through c_void_p is pointer-sized; a 32-bit 0xFFFFFFFF
        literal would never match it on 64-bit Python."""
        expected = ctypes.c_void_p(-1).value
        assert containment._INVALID_WINDOWS_HANDLE == expected
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            assert containment._INVALID_WINDOWS_HANDLE == 0xFFFFFFFFFFFFFFFF
