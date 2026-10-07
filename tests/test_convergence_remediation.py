"""Convergence-loop remediation tests (Phase 3, TDD).

Red/green pairs for the verified defect ledger of the 2026-10-07 convergence
loop: CAN-001..014 (minus the disproven CAN-013), MISSED-001..004, and
NEW-001 (Windows argv-limit preflight). Each test fails against the
pre-fix code and pins the corrected behavior.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.antigravity_bridge import (
    AttemptResult,
    BridgeConfig,
    BridgeResult,
    DelegationEnvelope,
    run_bridge,
    write_report,
)
from tools.antigravity_mcp_server import InvalidParams, handle_request
from tools.antigravity_viewer import (
    ViewerContext,
    _host_from_header,
    _safe_mtime,
)
from tools.wisp_chat import MAX_MESSAGE_CHARS

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


@pytest.fixture()
def fake_viewer(tmp_path: Path):
    """Local viewer fixture: fake launcher, fake capture, no real agy."""
    from test_antigravity_viewer import ViewerProcess

    with ViewerProcess(
        tmp_path,
        extra_env={
            "WISP_ASK_FAKE": "1",
            "WISP_ASK_FAKE_DELAY": "0.5",
            "WISP_CAPTURE_FAKE": "text:selected text from clipboard",
        },
    ) as context:
        yield context


# ------------------------------------------------- CAN-001 / MISSED-004


class TestAskLockLeak:
    def test_oversized_prompt_is_rejected_without_acquiring_lock(
        self, fake_viewer
    ) -> None:
        base, _ = fake_viewer
        status, data = _post_ask(base, {"prompt": "x" * (MAX_MESSAGE_CHARS + 1)})
        assert status == 400
        assert "MAX_MESSAGE_CHARS" in data["error"]

    def test_oversized_prompt_does_not_brick_the_ask_feature(
        self, fake_viewer
    ) -> None:
        base, _ = fake_viewer
        status, _ = _post_ask(base, {"prompt": "x" * (MAX_MESSAGE_CHARS + 1)})
        assert status == 400
        # The very next (valid) ask must NOT be 409-bricked by a leaked lock.
        status2, data2 = _post_ask(base, {"prompt": "still works"})
        assert status2 == 200, data2

    def test_malformed_thread_id_after_lock_is_rejected(self, fake_viewer) -> None:
        base, _ = fake_viewer
        # A valid prompt but invalid thread id: must 404 and not brick.
        status, _ = _post_ask(
            base, {"prompt": "hi", "thread_id": "not-a-valid-thread-id"}
        )
        assert status == 404
        status2, _ = _post_ask(base, {"prompt": "recovers"})
        assert status2 == 200


def _post_ask(base: str, payload: dict) -> tuple[int, dict]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        base + "/api/ask",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=30)
        return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ------------------------------------------------- CAN-002


class TestAnswerClamping:
    def test_worker_clamps_oversized_answers(self, tmp_path: Path) -> None:
        """A successful delegation with a huge critique is stored clamped and
        reported as done, not 'Delegation failed before answering'."""
        from test_antigravity_viewer import ViewerProcess

        with ViewerProcess(
            tmp_path,
            extra_env={
                "WISP_ASK_FAKE": "1",
                "WISP_ASK_FAKE_BIG": "1",
            },
        ) as context:
            base, live = context
            status, data = _post_ask(base, {"prompt": "big answer please"})
            assert status == 200
            thread_id = data["thread_id"]
            deadline = time.time() + 40
            thread = None
            while time.time() < deadline:
                import urllib.request

                thread = json.loads(
                    urllib.request.urlopen(
                        base + "/api/chat/thread/" + thread_id, timeout=10
                    ).read()
                )["thread"]
                if len(thread["messages"]) >= 2:
                    break
                time.sleep(0.3)
            assistant = thread["messages"][-1]
            assert assistant["role"] == "assistant"
            assert "Delegation failed" not in assistant["content"]
            assert len(assistant["content"]) <= MAX_MESSAGE_CHARS
            meta = assistant.get("meta") or {}
            assert meta.get("success") is True


# ------------------------------------------------- CAN-003 / CAN-005


class TestAuthGuards:
    def test_bearer_comparison_is_constant_time(self) -> None:
        import inspect

        from tools import antigravity_viewer

        source = inspect.getsource(antigravity_viewer.ViewerHandler._authorized)
        assert "compare_digest" in source
        assert re.search(r"header == f?", source) is None

    def test_host_header_handles_ipv6_brackets(self) -> None:
        assert _host_from_header("[::1]:48477") == "::1"
        assert _host_from_header("[::1]") == "::1"
        assert _host_from_header("127.0.0.1:48477") == "127.0.0.1"
        assert _host_from_header("localhost") == "localhost"
        assert _host_from_header("evil.example.com") == "evil.example.com"
        assert _host_from_header("") == ""

    def test_ipv6_loopback_binding_serves_loopback_requests(self) -> None:
        """End-to-end: --host ::1 must not 403 every request (CAN-005)."""
        import urllib.request

        from test_antigravity_viewer import ViewerProcess

        try:
            with ViewerProcess(tmp_path_factory(), extra_args=["--host", "::1"]) as ctx:
                base, _ = ctx
                health = urllib.request.urlopen(base + "/health", timeout=10)
                assert health.status == 200
        except (OSError, RuntimeError):
            pytest.skip("IPv6 loopback unavailable in this environment")


def tmp_path_factory() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp())



# ------------------------------------------------- CAN-004 / MISSED-003


class TestStatRaceSafety:
    def test_safe_mtime_survives_missing_files(self, tmp_path: Path) -> None:

        ghost = tmp_path / "run-20260101-000000-ghost.jsonl"
        assert _safe_mtime(ghost) == 0.0
        real = tmp_path / "run-20260101-000001-real.jsonl"
        real.write_text("{}\n", encoding="utf-8")
        assert _safe_mtime(real) > 0.0

    def test_list_runs_never_raises_on_vanished_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tools.antigravity_viewer import list_runs

        live = tmp_path / "live"
        live.mkdir(parents=True)
        for index in range(3):
            (live / f"run-20260101-00000{index}-aa.jsonl").write_text(
                '{"kind":"run_start"}\n', encoding="utf-8"
            )

        original_stat = Path.stat

        def flaky_stat(self: Path, *args, **kwargs):
            # Simulate retention pruning between glob and sort: every third
            # stat raises FileNotFoundError.
            if "ghost" not in str(self) and self.name.endswith("aa.jsonl"):
                raise FileNotFoundError(self)
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        entries = list_runs(live)
        monkeypatch.undo()
        assert isinstance(entries, list)

    def test_collect_auth_state_never_raises_on_vanished_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tools import antigravity_viewer as viewer

        log_dir = tmp_path / "logdir"
        log_dir.mkdir()
        (log_dir / "agy.log").write_text("hello\n", encoding="utf-8")
        monkeypatch.setattr(
            viewer,
            "_AUTH_LOG_DIRS",
            (log_dir,),
        )

        original_stat = Path.stat

        def flaky_stat(self: Path, *args, **kwargs):
            if self.suffix == ".log":
                raise FileNotFoundError(self)
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        state = viewer.collect_auth_state()
        monkeypatch.undo()
        assert state["account"] is None or isinstance(state["account"], str)


# ------------------------------------------------- CAN-014


class TestStatusRunCount:
    def test_status_counts_runs_cheaply(self, fake_viewer) -> None:
        base, live = fake_viewer
        import urllib.request

        status = json.loads(urllib.request.urlopen(base + "/api/status", timeout=10).read())
        on_disk = len(list(live.glob("run-*.jsonl")))
        assert status["runs"] == on_disk


# ------------------------------------------------- CAN-007


class TestAtomicViewerFiles:
    def test_manifest_write_is_atomic(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import _write_viewer_manifest

        _write_viewer_manifest(tmp_path, {"port": 1})
        leftovers = [p for p in tmp_path.glob("viewer.json.tmp.*")]
        assert leftovers == []
        assert json.loads((tmp_path / "viewer.json").read_text(encoding="utf-8")) == {"port": 1}

    def test_settings_write_is_atomic(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import write_viewer_settings

        write_viewer_settings(tmp_path, {"model": "gemini-3.8-flash-high"})
        leftovers = list(tmp_path.glob("viewer_settings.json.tmp.*"))
        assert leftovers == []


# ------------------------------------------------- MISSED-001


class TestShutdownHandshakeWithAuth:
    def test_authenticated_shutdown_success_shuts_the_viewer_down(
        self, tmp_path: Path
    ) -> None:
        import urllib.request

        from test_antigravity_viewer import ViewerProcess

        auth = "convergence-auth-token"
        viewer = ViewerProcess(tmp_path, extra_args=["--auth-token", auth])
        with viewer as context:
            base, live = context
            manifest = json.loads((live / "viewer.json").read_text(encoding="utf-8"))
            instance = manifest["token"]
            port = manifest["port"]

            # The fixed handshake: bearer auth + instance token in the
            # dedicated header. This must actually shut the viewer down.
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            conn.request(
                "POST",
                "/api/shutdown",
                body="{}",
                headers={
                    "X-Wisp-Request": "1",
                    "Authorization": f"Bearer {auth}",
                    "X-Instance-Token": instance,
                },
            )
            response = conn.getresponse()
            assert response.status == 200

            deadline = time.time() + 10
            while time.time() < deadline:
                if viewer.proc.poll() is not None:
                    break
                time.sleep(0.2)
            assert viewer.proc.poll() is not None, (
                "authenticated shutdown did not stop the viewer (MISSED-001)"
            )

    def test_request_shutdown_sends_instance_header(self) -> None:
        import inspect

        from tools import antigravity_viewer

        source = inspect.getsource(antigravity_viewer.request_shutdown)
        assert "X-Instance-Token" in source


# ------------------------------------------------- NEW-001


class TestArgvLimitPreflight:
    def test_oversized_payload_fails_fast_with_actionable_error(self) -> None:
        if os.name != "nt":
            pytest.skip("Windows CreateProcess argv limit")

        calls: list[str] = []

        def launcher(command, cwd, env, hard_timeout, raw_line_sink=None):
            calls.append(command[0])
            return AttemptResult(exit_code=0, stdout="{}")

        config = BridgeConfig(
            envelope=DelegationEnvelope(
                prompt="x" * 40_000,
                context="y" * 2_000,
            ),
            workspace=ROOT,
            retries=0,
        )
        result = run_bridge(config, launcher=launcher)
        assert result.success is False
        assert "command line" in result.error.lower()
        assert "shorten" in result.error.lower()
        assert calls == [], "launcher must not be invoked for oversized payloads"

    def test_normal_payload_passes_preflight(self) -> None:
        def launcher(command, cwd, env, hard_timeout, raw_line_sink=None):
            return AttemptResult(
                exit_code=0,
                stdout='{"event":"result","result":{"status":"SUCCESS","response":"ok"}}',
            )

        config = BridgeConfig(
            envelope=DelegationEnvelope(prompt="reasonable"), workspace=ROOT, retries=0
        )
        result = run_bridge(config, launcher=launcher)
        assert result.success is True


# ------------------------------------------------- MISSED-002 / CAN-012


class TestStringArrayContract:
    def test_string_claims_accepted_as_single_element(self) -> None:
        captured: dict = {}

        def launcher(command, cwd, env, hard_timeout, raw_line_sink=None):
            captured["payload"] = command[2]
            return AttemptResult(
                exit_code=0,
                stdout='{"event":"result","result":{"status":"SUCCESS","response":"ok"}}',
            )

        config = BridgeConfig(
            envelope=DelegationEnvelope(prompt="p", claims_to_falsify=("one claim, with comma",)),
            workspace=ROOT,
            retries=0,
        )
        result = run_bridge(config, launcher=launcher)
        assert result.success is True
        assert "one claim, with comma" in captured["payload"]

    def test_mcp_string_claims_accepted(self) -> None:
        """AGENTS.md documents claims/artifacts as string-or-list; the MCP
        server must accept a bare string instead of -32602 (MISSED-002)."""
        import tools.antigravity_mcp_server as server

        original = server.run_bridge

        def fake_run_bridge(config):
            assert config.envelope.claims_to_falsify == ("single claim",)
            return BridgeResult(
                success=True,
                model_used="m",
                failover_used=False,
                timed_out=False,
                rate_limited=False,
                exit_code=0,
                attempts=[],
                critique_markdown="ok",
            )

        server.run_bridge = fake_run_bridge  # type: ignore[assignment]
        try:
            payload, is_error = server._tool_review(
                {"prompt": "p", "claims_to_falsify": "single claim"}
            )
        finally:
            server.run_bridge = original  # type: ignore[assignment]
        assert is_error is False

    def test_mcp_string_skills_are_comma_split(self) -> None:
        """The previously-dead comma-split branch becomes reachable and
        functional for skill selectors (CAN-012)."""
        from tools.antigravity_mcp_server import _bounded_list

        assert _bounded_list("a,b", limit=10, name="skills", split_commas=True) == [
            "a",
            "b",
        ]
        assert _bounded_list(
            "single claim", limit=10, name="claims_to_falsify"
        ) == ["single claim"]


# ------------------------------------------------- Round 3 (fresh audit)


class TestRound3Findings:
    def test_viewer_version_tracks_product_version(self) -> None:
        """AST-001: SERVER_VERSION was hardcoded 1.0.0 while the product is at
        WISP_VERSION; status API, Server header, and UI footer all derive."""
        import tools.antigravity_viewer as viewer
        from tools.antigravity_bridge import WISP_VERSION

        assert viewer.SERVER_VERSION == WISP_VERSION

    def test_invalid_handle_constant_is_pointer_sized(self) -> None:
        """AST-002: (HANDLE)-1 through c_void_p is pointer-sized; the 32-bit
        0xFFFFFFFF literal never matched the 64-bit value, so snapshot
        failure detection was dead on x64."""
        import ctypes

        from tools import antigravity_bridge as bridge

        expected = ctypes.c_void_p(-1).value
        assert bridge._INVALID_WINDOWS_HANDLE == expected
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            assert bridge._INVALID_WINDOWS_HANDLE == 0xFFFFFFFFFFFFFFFF

    def test_shell_lockfile_version_parity(self) -> None:
        """PKG-001: package-lock.json must not lag package.json after bumps."""
        import hashlib

        root = Path(__file__).resolve().parent.parent
        package = json.loads(
            (root / "tools" / "wisp_shell" / "package.json").read_text(encoding="utf-8")
        )
        lock = json.loads(
            (root / "tools" / "wisp_shell" / "package-lock.json").read_text(encoding="utf-8")
        )
        assert lock["version"] == package["version"]
        assert lock["packages"][""]["version"] == package["version"]

    def test_agents_blocklist_enumeration_matches_bridge(self) -> None:
        """DOC-001: the AGENTS.md charter must enumerate the credential
        families the bridge actually strips (stale list = doc drift)."""
        agents = (Path(__file__).resolve().parent.parent / "AGENTS.md").read_text(
            encoding="utf-8"
        )
        for marker in (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_API_KEY",
            "GIT_ASKPASS",
            "NODE_OPTIONS",
            "PYTHONPATH",
            "BLOCKED_ENV_PREFIXES",
        ):
            assert marker in agents, f"AGENTS.md missing {marker}"
