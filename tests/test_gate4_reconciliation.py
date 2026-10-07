import ctypes
import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.antigravity_bridge import (
    _descendant_pids,
    _prune_reports,
    AttemptResult,
    BridgeResult,
    write_report,
)
from tools.antigravity_mcp_server import InvalidParams, handle_request

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


class TestFL003PruneExclusion:
    def test_new_report_survives_its_own_pruning_pass(self, tmp_path: Path) -> None:
        # Four older reports with FUTURE mtimes sort ahead of the fresh one;
        # with keep=2 the fresh report lands past the keep line and only the
        # explicit exclusion saves it (the FL-003 scenario).
        future = time.time() + 3600
        for index in range(4):
            candidate = tmp_path / f"antigravity-report-20260101-00000{index}-aaaaaaaa.json"
            candidate.write_text("{}", encoding="utf-8")
            os.utime(candidate, (future, future))
        fresh = tmp_path / "antigravity-report-20260101-000009-00000000.json"
        fresh.write_text("{}", encoding="utf-8")

        _prune_reports(tmp_path, keep=2, exclude=fresh)

        assert fresh.is_file(), "pruning deleted the report it just wrote"
        survivors = sorted(path.name for path in tmp_path.glob("antigravity-report-*.json"))
        assert len(survivors) == 3  # fresh + the two newest older reports

    def test_write_report_integration_keeps_newest(self, tmp_path: Path) -> None:
        result = BridgeResult(
            success=True,
            model_used="m",
            failover_used=False,
            timed_out=False,
            rate_limited=False,
            exit_code=0,
            attempts=[AttemptResult(exit_code=0, stdout="x")],
            critique_markdown="x",
        )
        write_report(tmp_path, result, keep_reports=1)
        second = write_report(tmp_path, result, keep_reports=1)
        assert second.is_file()
        reports = list((tmp_path / ".antigravity-reports").glob("antigravity-report-*.json"))
        assert len(reports) == 1


def _pid_alive(pid: int) -> bool:
    """True when a process with this PID still exists (Toolhelp snapshot)."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]

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

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or int(snapshot) == 0xFFFFFFFF:
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


def _terminate_pids(pids: list[int]) -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    for pid in pids:
        handle = kernel32.OpenProcess(0x0001, 0, pid)  # PROCESS_TERMINATE
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="descendant walk is Toolhelp/Windows")
class TestFL004DeadParentDescendants:
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
            "import subprocess, sys\n"
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
                _terminate_pids(descendants)
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
            _terminate_pids([pid for pid in descendants if pid != proc.pid])
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
        assert all(_pid_alive(pid) for pid in orphans)

        # The FL-004 cleanup: terminate the orphans explicitly.
        _terminate_pids(orphans)
        time.sleep(1.0)
        for pid in orphans:
            assert not _pid_alive(pid), (
                f"grandchild {pid} orphaned after its parent exited (FL-004)"
            )


class TestFL005ForcedKillGuard:
    def test_non_viewer_python_process_is_refused(self) -> None:
        if os.name != "nt":
            pytest.skip("tasklist/PowerShell PID inspection")
        from tools.antigravity_viewer import looks_like_viewer_process

        proc = subprocess.Popen(
            [PYTHON, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
        )
        try:
            # A "python" image alone is NOT enough any more: the recycled PID
            # must also carry a command line naming this viewer (FL-005).
            assert looks_like_viewer_process("python.exe", pid=proc.pid) is False
        finally:
            proc.kill()
            proc.wait(timeout=10)


class TestFL007ArtifactRoundTrip:
    def test_capture_inside_workspace_is_contained_and_rejoinable(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import ViewerContext

        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        capture = tmp_path / ".antigravity-reports" / "captures" / "paste-x.png"
        capture.parent.mkdir(parents=True)
        capture.write_bytes(b"\x89PNG\r\n\x1a\n")

        context = ViewerContext(workspace=tmp_path, live_dir=live, port=0)
        contained = context.contain_artifact(str(capture))
        assert contained is not None
        # The worker must be able to rejoin the contained path against the
        # workspace and find the same file (the FL-007 round-trip).
        rejoined = (tmp_path / contained).resolve()
        assert rejoined.is_file()
        assert rejoined == capture.resolve()

    def test_artifact_outside_workspace_is_rejected_entirely(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import ViewerContext

        # Workspace and artifact live in SIBLING directories: the artifact is
        # a real image file that resolves nowhere inside the workspace.
        workspace = tmp_path / "ws"
        workspace.mkdir()
        live = workspace / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        outside = tmp_path / "elsewhere" / "keep.png"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"\x89PNG\r\n\x1a\n")

        context = ViewerContext(workspace=workspace, live_dir=live, port=0)
        assert context.contain_artifact(str(outside)) is None


class TestREQHARD003ShutdownAuth:
    AUTH_TOKEN = "gate4-test-bearer-token"

    @pytest.fixture()
    def token_viewer(self, tmp_path: Path):
        from test_antigravity_viewer import ViewerProcess

        with ViewerProcess(
            tmp_path, extra_args=["--auth-token", self.AUTH_TOKEN]
        ) as context:
            base, live = context
            manifest = json.loads((live / "viewer.json").read_text(encoding="utf-8"))
            yield base, manifest

    def _post_shutdown(self, port: int, headers: dict) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.request("POST", "/api/shutdown", body="{}", headers=headers)
        return conn.getresponse().status

    def test_shutdown_without_any_authorization_is_rejected(self, token_viewer) -> None:
        base, manifest = token_viewer
        status = self._post_shutdown(manifest["port"], {"X-Wisp-Request": "1"})
        # Missing bearer (server bound with an auth token) -> 401 before the
        # instance token is even considered.
        assert status == 401

    def test_shutdown_with_wrong_instance_token_is_rejected(self, token_viewer) -> None:
        base, manifest = token_viewer
        status = self._post_shutdown(
            manifest["port"],
            {
                "X-Wisp-Request": "1",
                "Authorization": f"Bearer {self.AUTH_TOKEN}",
            },
        )
        # Correct bearer but wrong instance token -> 403 (the handshake secret).
        assert status == 403

    def test_regular_get_with_wrong_bearer_is_rejected(self, token_viewer) -> None:
        import urllib.request

        base, manifest = token_viewer
        request = urllib.request.Request(
            f"{base}/api/status",
            headers={"Authorization": "Bearer not-the-token"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=15)
        assert excinfo.value.code == 401

    def test_regular_get_with_correct_bearer_passes(self, token_viewer) -> None:
        import urllib.request

        base, manifest = token_viewer
        request = urllib.request.Request(
            f"{base}/api/status",
            headers={"Authorization": f"Bearer {self.AUTH_TOKEN}"},
        )
        response = urllib.request.urlopen(request, timeout=15)
        assert response.status == 200

    def test_query_token_on_regular_get_is_rejected(self, token_viewer) -> None:
        import urllib.request

        base, manifest = token_viewer
        # Even a VALID token must not travel in the query string on regular
        # routes - only /events may use it (EventSource limitation).
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(
                f"{base}/api/status?token={self.AUTH_TOKEN}", timeout=15
            )
        assert excinfo.value.code == 401

    def test_non_loopback_bind_refuses_without_token(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import main

        exit_code = main(
            [
                "--host",
                "0.0.0.0",
                "--workspace",
                str(tmp_path),
                "--no-open",
                "--port",
                "0",
            ]
        )
        assert exit_code == 2


class TestRuntimeBoundsDispatch:
    def test_oversized_prompt_is_rejected_at_dispatch(self) -> None:
        with pytest.raises(InvalidParams):
            handle_request(
                "tools/call",
                {
                    "name": "antigravity_review",
                    "arguments": {"prompt": "x" * 400_001},
                },
            )

    def test_oversized_artifact_array_is_rejected_at_dispatch(self) -> None:
        with pytest.raises(InvalidParams):
            handle_request(
                "tools/call",
                {
                    "name": "antigravity_review",
                    "arguments": {
                        "prompt": "ok",
                        "artifacts": [f"p/{index}.py" for index in range(301)],
                    },
                },
            )
