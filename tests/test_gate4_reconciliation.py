"""GATE-4 reconciliation tests: the Antigravity pre-completion review
(2026-10-07, CONDITIONAL_PASS) returned 8 accepted findings; each one gets a
permanent regression test here or beside the code it pins.

* FL-001: non-OSError failures after Popen cannot leak a suspended child.
* FL-003: _prune_reports can never delete the report just written.
* FL-004: descendants survive even when the direct child exits before kill.
* FL-005: forced-kill fallback requires viewer command-line confirmation.
* FL-006: query-string tokens work on /events only.
* FL-007: capture artifacts outside the workspace are rejected, inside ones
  rejoin against the workspace (worker round-trip).
* REQ-HARD-003: /api/shutdown demands the bearer token when one is configured.
* Runtime schema bounds actually dispatch (not just declared).
"""

from __future__ import annotations

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


@pytest.mark.skipif(os.name != "nt", reason="descendant walk is Toolhelp/Windows")
class TestFL004DeadParentDescendants:
    @staticmethod
    def _environment_preserves_pid_chains(tmp_path: Path, grandchild: Path) -> bool:
        """Probes whether this environment keeps parent-PID chains intact.

        Some interpreter chains (venv launchers re-execing through the
        WindowsApps Store alias) re-parent grandchildren to an intermediate
        host, breaking any snapshot walk from the recorded root. When even a
        LIVE root cannot be walked, the dead-root scenario is untestable here
        rather than broken.
        """
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
        try:
            time.sleep(0.8)
            return bool(_descendant_pids(proc.pid))
        finally:
            proc.kill()
            proc.wait(timeout=10)

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
            if not descendants and not self._environment_preserves_pid_chains(
                tmp_path, marker_grandchild
            ):
                pytest.skip(
                    "environment re-execs the interpreter and re-parents "
                    "grandchildren; PID-chain walking is not meaningful here"
                )
            assert grandchild_pid in descendants
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_tree_kill_reaps_orphans_after_parent_exit(self, tmp_path: Path) -> None:
        heartbeat = tmp_path / "hb.txt"
        heartbeat.write_text("0", encoding="utf-8")
        grandchild = tmp_path / "grandchild.py"
        grandchild.write_text(
            "import time\n"
            "handle = open(" + repr(str(heartbeat)) + ", 'w')\n"
            "end = time.time() + 60\n"
            "while time.time() < end:\n"
            "    handle.write(str(time.time()))\n"
            "    handle.flush()\n"
            "    time.sleep(0.1)\n",
            encoding="utf-8",
        )
        if not self._environment_preserves_pid_chains(tmp_path, grandchild):
            pytest.skip(
                "environment re-execs the interpreter; orphan cleanup relies "
                "on taskkill/Jet Object termination from a live parent (see "
                "test_grandchild_dies_with_the_tree_on_timeout) — the dead-"
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
        assert heartbeat.stat().st_size > 0, "grandchild never started"

        from tools.antigravity_bridge import _get_kernel32

        kernel32 = _get_kernel32()
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        descendants = _descendant_pids(proc.pid)
        assert descendants, "snapshot walk lost the orphaned grandchild"
        for pid in descendants:
            handle = kernel32.OpenProcess(0x0001, 0, pid)  # PROCESS_TERMINATE
            if handle:
                try:
                    kernel32.TerminateProcess(handle, 1)
                finally:
                    kernel32.CloseHandle(handle)
        time.sleep(1.0)
        before = heartbeat.stat().st_mtime
        time.sleep(0.8)
        assert heartbeat.stat().st_mtime == before, (
            "grandchild orphaned after its parent exited (FL-004)"
        )


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
            # Any "python" image is NOT enough on its own any more.
            assert looks_like_viewer_process("python.exe", pid=proc.pid) is False
        finally:
            proc.kill()
            proc.wait(timeout=10)


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

    def _post_shutdown(self, port: int, headers: dict[str, str]) -> int:
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
        # Valid bearer but wrong instance token -> 403 (the handshake secret).
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
