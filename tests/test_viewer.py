"""Viewer server: HTTP endpoints, auth, asks, run discovery, persistence, and containment."""

from __future__ import annotations

import base64
import http.client
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.helpers import ROOT
from tests.helpers.viewer import (
    ViewerProcess,
    http_request,
    ipv6_loopback_available,
    post_json,
    serving,
    write_run,
)
from tools import antigravity_viewer
from tools.antigravity_viewer import (
    ViewerContext,
    _fake_ask_launcher,
    _host_from_header,
    _safe_mtime,
    _write_viewer_manifest,
    bind_server,
    build_ask_prompt,
    describe_run,
    existing_viewer,
    extract_chat_answer,
    list_runs,
    newest_run,
    newest_run_across,
    parse_model_list,
    request_shutdown,
    url_host,
    write_viewer_settings,
)
from tools.antigravity_viewer import main as viewer_main
from tools.wisp_chat import MAX_MESSAGE_CHARS

PNG_BLOB = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture()
def viewer(tmp_path: Path):
    """A viewer child process watching a workspace with one run file."""
    with ViewerProcess(tmp_path) as context:
        yield context


@pytest.fixture()
def fake_viewer(tmp_path: Path):
    """A viewer whose asks and captures are canned: no real agy or clipboard."""
    with ViewerProcess(
        tmp_path,
        extra_env={
            "WISP_ASK_FAKE": "1",
            "WISP_ASK_FAKE_DELAY": "1.5",
            "WISP_CAPTURE_FAKE": "text:selected text from clipboard",
            "WISP_CAPTURE_FAKE_DELAY": "1.2",
        },
    ) as context:
        yield context


class TestPortBinding:
    def test_busy_port_falls_back_to_next(self, tmp_path: Path) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            preferred = probe.getsockname()[1]

        def factory(port: int) -> ViewerContext:
            return ViewerContext(
                workspace=tmp_path,
                live_dir=tmp_path / "live",
                port=port,
            )

        first, first_port = bind_server("127.0.0.1", preferred, factory, max_attempts=10)
        try:
            second, second_port = bind_server("127.0.0.1", preferred, factory, max_attempts=10)
        finally:
            first.server_close()
        second.server_close()

        assert first_port == preferred
        assert second_port == preferred + 1


class TestRunDiscovery:
    def test_newest_run_picks_latest_mtime(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        write_run(live, "run-a.jsonl", [{"kind": "run_start"}], mtime=1000)
        write_run(live, "run-b.jsonl", [{"kind": "run_start"}], mtime=2000)

        assert newest_run(live).name == "run-b.jsonl"

    def test_newest_run_none_when_missing(self, tmp_path: Path) -> None:
        assert newest_run(tmp_path / "missing") is None

    def test_list_runs_sorted_desc(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        write_run(live, "run-a.jsonl", [], mtime=1000)
        write_run(live, "run-b.jsonl", [], mtime=2000)

        runs = list_runs(live)

        assert [run["file"] for run in runs] == ["run-b.jsonl", "run-a.jsonl"]


class TestMultiDirWatch:
    def test_newest_run_across_dirs(self, tmp_path: Path) -> None:
        older = tmp_path / "a"
        newer = tmp_path / "b"
        write_run(older, "run-20260101-000000-aaaaaa.jsonl", [])
        write_run(newer, "run-20260102-000000-bbbbbb.jsonl", [])

        assert newest_run_across([older, newer]).name == "run-20260102-000000-bbbbbb.jsonl"
        assert newest_run_across([]) is None

    def test_newest_run_across_prefers_run_id_order(self, tmp_path: Path) -> None:
        first = tmp_path / "a"
        second = tmp_path / "b"
        write_run(first, "run-20260201-000000-aaaaaa.jsonl", [], mtime=5000)
        write_run(second, "run-20260101-000000-bbbbbb.jsonl", [], mtime=9000)

        assert newest_run_across([first, second]).name == "run-20260201-000000-aaaaaa.jsonl"


class TestRunMetadata:
    def test_describe_run_parses_lifecycle(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        live.mkdir()
        path = live / "run-x.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "kind": "run_start",
                            "run_id": "x",
                            "seq": 1,
                            "ts": 1000.0,
                            "model": "m",
                            "text": "",
                            "meta": {"primary_model": "gemini-3.8-flash-high"},
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "thinking",
                            "run_id": "x",
                            "seq": 2,
                            "ts": 1001.0,
                            "model": "m",
                            "text": "t",
                            "meta": {},
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "run_end",
                            "run_id": "x",
                            "seq": 3,
                            "ts": 1010.0,
                            "model": "m",
                            "text": "",
                            "meta": {
                                "success": True,
                                "elapsed_seconds": 9.5,
                                "failover_used": False,
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        record = describe_run(path)

        assert record["model"] == "gemini-3.8-flash-high"
        assert record["finished"] is True
        assert record["success"] is True
        assert record["elapsed_seconds"] == 9.5
        assert record["failover_used"] is False

    def test_describe_run_unfinished(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        live.mkdir()
        path = live / "run-y.jsonl"
        path.write_text(
            json.dumps(
                {
                    "kind": "run_start",
                    "run_id": "y",
                    "seq": 1,
                    "ts": 2000.0,
                    "model": "m",
                    "text": "",
                    "meta": {"primary_model": "claude-sonnet-4-6"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        record = describe_run(path)

        assert record["finished"] is False
        assert record["success"] is None
        assert record["model"] == "claude-sonnet-4-6"


class TestRunEndDetection:
    def test_only_a_run_end_event_ends_the_run(self) -> None:
        assert antigravity_viewer.is_run_end_line(json.dumps({"kind": "run_end", "seq": 9}))
        assert not antigravity_viewer.is_run_end_line(
            json.dumps({"kind": "stdout", "text": 'the model printed "run_end" here'})
        )
        assert not antigravity_viewer.is_run_end_line('{"kind": "run_end"')
        assert not antigravity_viewer.is_run_end_line('["run_end"]')


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
        log_dir = tmp_path / "logdir"
        log_dir.mkdir()
        (log_dir / "agy.log").write_text("hello\n", encoding="utf-8")
        monkeypatch.setattr(
            antigravity_viewer,
            "_AUTH_LOG_DIRS",
            (log_dir,),
        )

        original_stat = Path.stat

        def flaky_stat(self: Path, *args, **kwargs):
            if self.suffix == ".log":
                raise FileNotFoundError(self)
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        state = antigravity_viewer.collect_auth_state()
        monkeypatch.undo()
        assert state["account"] is None or isinstance(state["account"], str)


class TestForeignWorkspaceVisibility:
    def test_runs_merge_and_foreign_replay(self, tmp_path: Path) -> None:
        registry = tmp_path / "registry.json"
        foreign_workspace = tmp_path / "foreign"
        foreign_live = foreign_workspace / ".antigravity-reports" / "live"
        foreign_live.mkdir(parents=True)
        write_run(
            foreign_live,
            "run-20260102-000000-foreign.jsonl",
            [
                {
                    "kind": "run_start",
                    "run_id": "foreign-run",
                    "seq": 1,
                    "ts": time.time(),
                    "text": "",
                }
            ],
        )
        registry.write_text(
            json.dumps(
                [
                    {
                        "path": str(foreign_live.resolve()),
                        "workspace": str(foreign_workspace),
                        "last_seen": time.time(),
                    }
                ]
            ),
            encoding="utf-8",
        )

        with ViewerProcess(
            tmp_path / "own",
            extra_env={"ANTIGRAVITY_LIVE_REGISTRY": str(registry)},
        ) as (base, _live):
            data = json.loads(urllib.request.urlopen(base + "/api/runs", timeout=10).read())
            records = [
                record
                for record in data["runs"]
                if record["file"] == "run-20260102-000000-foreign.jsonl"
            ]
            assert records
            assert records[0]["is_foreign"] is True
            assert records[0]["workspace"] == str(foreign_workspace)

            status = json.loads(urllib.request.urlopen(base + "/api/status", timeout=10).read())
            assert str(foreign_live.resolve()) in status["watched_dirs"]

            response = urllib.request.urlopen(
                base + "/events?file=run-20260102-000000-foreign.jsonl", timeout=15
            )
            buffer = b""
            deadline = time.time() + 10
            while b"replay_end" not in buffer and time.time() < deadline:
                buffer += response.readline()
            response.close()

            assert b"run file not found" not in buffer
            assert b"replay_end" in buffer


class TestHttpEndpoints:
    def test_index_serves_wisp_ui(self, viewer) -> None:
        base, _ = viewer

        html = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")

        assert "Wisp" in html
        assert "/events" in html
        assert "modelBtn" in html
        assert "streamScroll" in html

    def test_health_reports_watching(self, viewer) -> None:
        base, _ = viewer

        health = json.loads(urllib.request.urlopen(base + "/health", timeout=10).read())
        details = json.loads(urllib.request.urlopen(base + "/health/details", timeout=10).read())

        # /health is deliberately minimal (unauthenticated liveness probe).
        assert health["status"] == "ok"
        assert "watching" not in health
        assert "live_dir" not in health
        assert details["watching"].startswith("run-")

    def test_assets_and_runs_endpoints(self, viewer) -> None:
        base, _ = viewer

        assets = json.loads(urllib.request.urlopen(base + "/api/assets", timeout=10).read())
        runs = json.loads(urllib.request.urlopen(base + "/api/runs", timeout=10).read())

        assert "assets" in assets
        assert any(run["file"].startswith("run-20260101") for run in runs["runs"])

    def test_post_requires_session_header(self, viewer) -> None:
        base, _ = viewer
        request = urllib.request.Request(base + "/api/account/recheck", method="POST")

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)

        assert excinfo.value.code == 403

    def test_unknown_route_404(self, viewer) -> None:
        base, _ = viewer

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(base + "/nope", timeout=10)

        assert excinfo.value.code == 404

    def test_model_selection_persists(self, viewer) -> None:
        base, live = viewer

        defaults = json.loads(urllib.request.urlopen(base + "/api/model", timeout=10).read())
        assert defaults["model"] == "gemini-3.8-flash-high"

        request = urllib.request.Request(
            base + "/api/model",
            method="POST",
            data=json.dumps({"model": "claude-sonnet-4-6"}).encode("utf-8"),
            headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
        )
        saved = json.loads(urllib.request.urlopen(request, timeout=10).read())
        assert saved["model"] == "claude-sonnet-4-6"
        assert saved["model_known"] is True

        again = json.loads(urllib.request.urlopen(base + "/api/model", timeout=10).read())
        assert again["model"] == "claude-sonnet-4-6"
        assert (live / "viewer_settings.json").exists()

        # Malformed names are rejected outright.
        malformed = urllib.request.Request(
            base + "/api/model",
            method="POST",
            data=json.dumps({"model": "not a valid name!"}).encode("utf-8"),
            headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(malformed, timeout=10)
        assert excinfo.value.code == 400

        # Well-formed but unknown ids are rejected unless allow_custom is set.
        unknown = urllib.request.Request(
            base + "/api/model",
            method="POST",
            data=json.dumps({"model": "zzz-not-a-real-model"}).encode("utf-8"),
            headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(unknown, timeout=10)
        assert excinfo.value.code == 400
        body = json.loads(excinfo.value.read().decode("utf-8"))
        assert "known_models" in body

        forced = urllib.request.Request(
            base + "/api/model",
            method="POST",
            data=json.dumps({"model": "zzz-not-a-real-model", "allow_custom": True}).encode(
                "utf-8"
            ),
            headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
        )
        saved_custom = json.loads(urllib.request.urlopen(forced, timeout=10).read())
        assert saved_custom["model"] == "zzz-not-a-real-model"
        assert saved_custom["model_known"] is False

    def test_ask_rejects_unknown_model(self, fake_viewer) -> None:
        base, _ = fake_viewer
        status, data = post_json(
            base,
            "/api/ask",
            {"prompt": "hi", "model": "zzz-not-a-real-model"},
        )
        assert status == 400
        assert "unknown model" in data["error"]

    def test_model_post_requires_session_header(self, viewer) -> None:
        base, _ = viewer
        request = urllib.request.Request(
            base + "/api/model",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)

        assert excinfo.value.code == 403

    def test_status_counts_runs_cheaply(self, fake_viewer) -> None:
        base, live = fake_viewer

        status = json.loads(urllib.request.urlopen(base + "/api/status", timeout=10).read())
        on_disk = len(list(live.glob("run-*.jsonl")))
        assert status["runs"] == on_disk


class TestServerSentEvents:
    def test_streams_hello_and_new_events(self, viewer) -> None:
        base, live = viewer
        response = urllib.request.urlopen(base + "/events", timeout=15)

        assert "text/event-stream" in response.headers.get("Content-Type", "")
        buffer = b""
        deadline = time.time() + 10
        while b"event: hello" not in buffer and time.time() < deadline:
            buffer += response.readline()
        assert b"event: hello" in buffer

        run_file = newest_run(live)
        with open(run_file, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": "thinking",
                        "run_id": "r",
                        "seq": 2,
                        "ts": time.time(),
                        "text": "live thought",
                    }
                )
                + "\n"
            )

        deadline = time.time() + 10
        while b"live thought" not in buffer and time.time() < deadline:
            buffer += response.readline()
        assert b"live thought" in buffer
        response.close()


class TestRejectedPostConnection:
    def test_unread_body_is_not_parsed_as_a_second_request(self, tmp_path: Path) -> None:
        body = b'{"model": "x"}'
        raw = (
            b"POST /api/model HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"X-Wisp-Request: 1\r\n"
            b"Origin: http://evil.example\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
        )
        with serving("127.0.0.1", tmp_path) as (host, port, _live):
            with socket.create_connection((host, port), timeout=10) as conn:
                conn.sendall(raw)
                received = b""
                while chunk := conn.recv(65536):
                    received += chunk
        assert received.startswith(b"HTTP/1.1 403")
        assert b"Connection: close" in received
        assert received.count(b"HTTP/1.") == 1, received


class TestShutdownAuthorization:
    AUTH_TOKEN = "test-bearer-token"

    @pytest.fixture()
    def token_viewer(self, tmp_path: Path):
        with ViewerProcess(tmp_path, extra_args=["--auth-token", self.AUTH_TOKEN]) as context:
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
        base, manifest = token_viewer
        request = urllib.request.Request(
            f"{base}/api/status",
            headers={"Authorization": "Bearer not-the-token"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=15)
        assert excinfo.value.code == 401

    def test_wrong_bearer_of_equal_length_is_rejected(self, token_viewer) -> None:
        base, _ = token_viewer
        forged = "x" * len(self.AUTH_TOKEN)
        request = urllib.request.Request(
            f"{base}/api/status",
            headers={"Authorization": f"Bearer {forged}"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=15)
        assert excinfo.value.code == 401

    def test_regular_get_with_correct_bearer_passes(self, token_viewer) -> None:
        base, manifest = token_viewer
        request = urllib.request.Request(
            f"{base}/api/status",
            headers={"Authorization": f"Bearer {self.AUTH_TOKEN}"},
        )
        response = urllib.request.urlopen(request, timeout=15)
        assert response.status == 200

    def test_query_token_on_regular_get_is_rejected(self, token_viewer) -> None:
        base, manifest = token_viewer
        # Even a VALID token must not travel in the query string on regular
        # routes - only /events may use it (EventSource limitation).
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"{base}/api/status?token={self.AUTH_TOKEN}", timeout=15)
        assert excinfo.value.code == 401

    def test_non_loopback_bind_refuses_without_token(self, tmp_path: Path) -> None:
        exit_code = viewer_main(
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


class TestShutdownHandshake:
    def test_authenticated_shutdown_success_shuts_the_viewer_down(self, tmp_path: Path) -> None:
        auth = "test-auth-token"
        process = ViewerProcess(tmp_path, extra_args=["--auth-token", auth])
        with process as context:
            base, live = context
            manifest = json.loads((live / "viewer.json").read_text(encoding="utf-8"))
            instance = manifest["token"]
            port = manifest["port"]

            # The handshake: bearer auth plus the instance token in its
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
                if process.proc.poll() is not None:
                    break
                time.sleep(0.2)
            assert process.proc.poll() is not None, "authenticated shutdown did not stop the viewer"

    def test_request_shutdown_helper_stops_an_authenticated_viewer(self, tmp_path: Path) -> None:
        auth = "test-auth-token"
        process = ViewerProcess(tmp_path, extra_args=["--auth-token", auth])
        with process as (_base, live):
            manifest = json.loads((live / "viewer.json").read_text(encoding="utf-8"))
            port, instance = manifest["port"], manifest["token"]
            # Without the operator bearer the instance secret alone is refused.
            assert request_shutdown(port, instance) is False
            assert process.proc.poll() is None
            # With both secrets (bearer + X-Instance-Token) the viewer stops.
            assert request_shutdown(port, instance, auth) is True
            deadline = time.time() + 10
            while time.time() < deadline and process.proc.poll() is None:
                time.sleep(0.2)
            assert process.proc.poll() is not None, "request_shutdown did not stop the viewer"


class TestExistingViewerDetection:
    def test_dead_manifest_returns_none(self, tmp_path: Path) -> None:
        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        (live / "viewer.json").write_text(
            json.dumps({"port": 59999, "pid": 12345}), encoding="utf-8"
        )

        assert existing_viewer(live) is None

    def test_missing_manifest_returns_none(self, tmp_path: Path) -> None:
        assert existing_viewer(tmp_path) is None


class TestIpv6Loopback:
    def test_url_host_brackets_ipv6_literals(self) -> None:
        assert url_host("::1") == "[::1]"
        assert url_host("[::1]") == "[::1]"
        assert url_host("127.0.0.1") == "127.0.0.1"
        assert url_host("localhost") == "localhost"

    def test_host_header_handles_ipv6_brackets(self) -> None:
        assert _host_from_header("[::1]:48477") == "::1"
        assert _host_from_header("[::1]") == "::1"
        assert _host_from_header("127.0.0.1:48477") == "127.0.0.1"
        assert _host_from_header("localhost") == "localhost"
        assert _host_from_header("evil.example.com") == "evil.example.com"
        assert _host_from_header("") == ""

    def test_allowed_origins_include_bracketed_ipv6(self) -> None:
        assert antigravity_viewer.allowed_origins(48477) == {
            "http://127.0.0.1:48477",
            "http://localhost:48477",
            "http://[::1]:48477",
        }

    @pytest.mark.skipif(not ipv6_loopback_available(), reason="IPv6 loopback unavailable")
    def test_bracketed_origin_is_accepted_on_ipv6_binding(self, tmp_path: Path) -> None:
        with serving("::1", tmp_path) as (host, port, _live):
            status, _ = http_request(host, port, "GET", "/api/assets")
            assert status == 200
            status, data = http_request(
                host,
                port,
                "POST",
                "/api/account/recheck",
                body=b"{}",
                headers={
                    "X-Wisp-Request": "1",
                    "Origin": f"http://[::1]:{port}",
                    "Content-Type": "application/json",
                },
            )
            assert status == 200, data
            assert "auth" in data

    def test_bracketed_ipv6_origin_passes_the_post_guard(self, tmp_path: Path) -> None:
        with serving("127.0.0.1", tmp_path) as (host, port, _live):
            status, data = http_request(
                host,
                port,
                "POST",
                "/api/account/recheck",
                body=b"{}",
                headers={"X-Wisp-Request": "1", "Origin": f"http://[::1]:{port}"},
            )
            assert status == 200, data

    def test_foreign_origin_is_still_rejected(self, tmp_path: Path) -> None:
        with serving("127.0.0.1", tmp_path) as (host, port, _live):
            status, data = http_request(
                host,
                port,
                "POST",
                "/api/account/recheck",
                body=b"{}",
                headers={"X-Wisp-Request": "1", "Origin": "http://evil.example"},
            )
            assert status == 403
            assert data["error"] == "origin not allowed"

    def test_ipv6_loopback_binding_serves_loopback_requests(self, tmp_path: Path) -> None:
        """End-to-end: --host ::1 must not 403 every request."""
        try:
            with ViewerProcess(tmp_path, extra_args=["--host", "::1"]) as ctx:
                base, _ = ctx
                health = urllib.request.urlopen(base + "/health", timeout=10)
                assert health.status == 200
        except (OSError, RuntimeError):
            pytest.skip("IPv6 loopback unavailable in this environment")


class TestModelListParsing:
    def test_parse_model_list(self) -> None:
        output = (
            "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
            "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
            "\n"
        )

        assert parse_model_list(output) == [
            "gemini-3.8-flash-high",
            "claude-sonnet-4-6",
        ]


class TestModelInventoryCache:
    @pytest.fixture()
    def agy_calls(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no agy")

        monkeypatch.setattr(
            antigravity_viewer, "_MODEL_CACHE", {"ts": 0.0, "models": [], "source": ""}
        )
        monkeypatch.setattr(antigravity_viewer, "resolve_agy_executable", lambda: "agy")
        monkeypatch.setattr(antigravity_viewer.subprocess, "run", fake_run)
        return calls

    def test_fallback_inventory_is_cached(self, agy_calls: list[list[str]]) -> None:
        first = antigravity_viewer.list_models()
        second = antigravity_viewer.list_models()
        assert first["source"] == second["source"] == "fallback"
        assert first["models"] == list(antigravity_viewer.FALLBACK_MODELS)
        assert len(agy_calls) == 1

    def test_fallback_cache_expires(self, agy_calls: list[list[str]]) -> None:
        antigravity_viewer.list_models()
        antigravity_viewer._MODEL_CACHE["ts"] -= antigravity_viewer.MODEL_FALLBACK_TTL_SECONDS + 1
        antigravity_viewer.list_models()
        assert len(agy_calls) == 2

    def test_model_post_queries_the_inventory_once(
        self, agy_calls: list[list[str]], tmp_path: Path
    ) -> None:
        with serving("127.0.0.1", tmp_path) as (host, port, _live):
            body = json.dumps(
                {
                    "model": "claude-sonnet-4-6",
                    "fallback_model": "claude-opus-4-6-thinking",
                    "chat_model": "gemini-3.8-flash-high",
                }
            ).encode("utf-8")
            status, data = http_request(
                host,
                port,
                "POST",
                "/api/model",
                body=body,
                headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
            )
        assert status == 200, data
        assert data["model_known"] is True
        assert len(agy_calls) == 1


class TestCaptureEndpoints:
    def test_concurrent_captures_rejected(self, fake_viewer) -> None:
        base, _ = fake_viewer
        results: list[int] = []

        def first_capture() -> None:
            request = urllib.request.Request(
                base + "/api/capture",
                method="POST",
                data=json.dumps({"mode": "auto"}).encode("utf-8"),
                headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    results.append(response.status)
            except urllib.error.HTTPError as exc:
                results.append(exc.code)

        worker = threading.Thread(target=first_capture)
        worker.start()
        time.sleep(0.4)
        status, data = post_json(base, "/api/capture", {"mode": "auto"})
        worker.join(timeout=30)

        assert status == 409
        assert data["error"] == "busy"
        assert results and results[0] == 200

    def test_capture_text_fake(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, data = post_json(base, "/api/capture", {"mode": "auto"})

        assert status == 200
        assert data["kind"] == "text"
        assert "clipboard" in data["text"]

    def test_capture_diagnostic_contains_no_secret_material(self, fake_viewer) -> None:
        base, _ = fake_viewer

        _, data = post_json(base, "/api/capture", {})

        assert "password" not in json.dumps(data).lower()


class TestImageAttachments:
    def _upload(self, base: str, blob: bytes) -> tuple[int, dict]:
        return post_json(
            base,
            "/api/chat/upload",
            {"data": base64.b64encode(blob).decode("ascii")},
        )

    def test_upload_and_serve_roundtrip(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, data = self._upload(base, PNG_BLOB)

        assert status == 200
        assert data["kind"] == "image"
        assert data["name"].startswith("paste-")
        assert data["rel_path"].startswith(".antigravity-reports/captures/")
        saved = Path(data["path"])
        assert saved.is_file()
        assert saved.read_bytes() == PNG_BLOB
        with urllib.request.urlopen(base + "/captures/" + data["name"], timeout=10) as response:
            assert response.status == 200
            assert response.headers.get("Content-Type", "").startswith("image/png")
            assert response.read() == PNG_BLOB

    def test_upload_rejects_non_image_bytes(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, data = self._upload(base, b"just some text")

        assert status == 400
        assert "unsupported" in data["error"]

    def test_upload_rejects_invalid_base64(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, _ = post_json(base, "/api/chat/upload", {"data": "!!!not-base64!!!"})

        assert status == 400

    def test_upload_rejects_oversize(self, fake_viewer) -> None:
        base, _ = fake_viewer
        blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * (12 * 1024 * 1024)

        status, data = self._upload(base, blob)

        assert status == 413
        assert "12 MB" in data["error"]

    def test_capture_route_blocks_traversal(self, fake_viewer) -> None:
        base, _ = fake_viewer

        for path in (
            "/captures/..%2F..%2Fviewer.json",
            "/captures/",
            "/captures/nope.exe",
        ):
            try:
                with urllib.request.urlopen(base + path, timeout=10) as response:
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            assert status == 404, path

    def test_ask_records_image_paths(self, fake_viewer) -> None:
        base, live = fake_viewer
        capture_dir = live.parent / "captures"
        capture_dir.mkdir(parents=True, exist_ok=True)
        first = capture_dir / "paste-test-a.png"
        second = capture_dir / "paste-test-b.png"
        first.write_bytes(PNG_BLOB)
        second.write_bytes(PNG_BLOB)

        status, data = post_json(
            base,
            "/api/ask",
            {"prompt": "two images", "images": [str(first), str(second)]},
        )
        assert status == 200
        thread_id = data["thread_id"]
        deadline = time.time() + 30
        thread = None
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(base + "/api/chat/thread/" + thread_id, timeout=10).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        # Contained artifacts are recorded workspace-relative.
        expected_rel = [
            str(first.relative_to(fake_viewer[1].parent.parent)).replace("\\", "/"),
            str(second.relative_to(fake_viewer[1].parent.parent)).replace("\\", "/"),
        ]
        user_meta = thread["messages"][0]["meta"]
        assert user_meta["image_paths"] == expected_rel
        assert user_meta["image_path"] == expected_rel[0]

    def test_ask_rejects_outside_artifact_paths(self, fake_viewer) -> None:
        base, _ = fake_viewer
        outside = Path(fake_viewer[1].parent.parent) / "outside-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        status, data = post_json(
            base,
            "/api/ask",
            {"prompt": "peek", "images": [str(outside)]},
        )
        assert status == 400
        assert data["rejected"] == [str(outside)]

    def test_upload_reports_500_when_the_capture_dir_cannot_be_created(
        self, tmp_path: Path
    ) -> None:
        with serving("127.0.0.1", tmp_path) as (host, port, live):
            # A regular file where the captures directory belongs makes mkdir fail.
            (live.parent / "captures").write_text("blocker", encoding="utf-8")
            body = json.dumps({"data": base64.b64encode(PNG_BYTES).decode("ascii")})
            status, data = http_request(
                host,
                port,
                "POST",
                "/api/chat/upload",
                body=body.encode("utf-8"),
                headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
            )
            assert status == 500
            assert "could not save the image" in data["error"]

    def test_user_message_records_the_validated_image_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WISP_ASK_FAKE", "1")
        with serving("127.0.0.1", tmp_path) as (host, port, live):
            image = live.parent / "captures" / "paste-test.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(PNG_BYTES)
            body = json.dumps({"prompt": "look", "image_path": f"  {image}  "})
            status, data = http_request(
                host,
                port,
                "POST",
                "/api/ask",
                body=body.encode("utf-8"),
                headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
            )
            assert status == 200, data
            thread_id = data["thread_id"]
            deadline = time.time() + 30
            thread: dict = {}
            while time.time() < deadline:
                _, payload = http_request(host, port, "GET", f"/api/chat/thread/{thread_id}")
                thread = payload["thread"]
                if (thread.get("run") or {}).get("status") in ("done", "failed"):
                    break
                time.sleep(0.1)
        meta = thread["messages"][0]["meta"]
        assert meta["image_path"] == ".antigravity-reports/captures/paste-test.png"
        assert meta["image_paths"] == [meta["image_path"]]


class TestAskPrompt:
    def test_prompt_lists_all_images(self) -> None:
        prompt = build_ask_prompt("check", "", ("a/b.png", "c/d.jpg"))

        assert "## ATTACHED IMAGES" in prompt
        assert "`a/b.png`" in prompt
        assert "`c/d.jpg`" in prompt
        assert "ATTACHED IMAGES" not in build_ask_prompt("check", "", ())

    def test_ask_prompt_demands_brevity(self) -> None:
        prompt = build_ask_prompt("what is a list comprehension?", "", ())

        assert "Be brief" in prompt
        assert "fewest words" in prompt


class TestAskFlow:
    def test_ask_roundtrip_with_fake_runner(self, fake_viewer) -> None:
        base, live = fake_viewer

        status, data = post_json(
            base,
            "/api/ask",
            {"prompt": "Check this snippet", "context_text": "x = 1"},
        )
        assert status == 200
        thread_id = data["thread_id"]
        assert data["status"] == "running"

        deadline = time.time() + 30
        thread = None
        while time.time() < deadline:
            raw = urllib.request.urlopen(base + "/api/chat/thread/" + thread_id, timeout=10).read()
            thread = json.loads(raw)["thread"]
            if len(thread["messages"]) >= 2 and thread["run"]["status"] in ("done", "failed"):
                break
            time.sleep(0.3)

        assert thread is not None
        assert thread["messages"][0]["role"] == "user"
        assistant = thread["messages"][1]
        assert assistant["role"] == "assistant"
        assert "Quick review" in assistant["content"]
        assert assistant["meta"]["success"] is True
        assert thread["run"]["status"] == "done"
        run_files = list(live.glob("run-*.jsonl"))
        assert len(run_files) >= 2

    def test_ask_busy_returns_409(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status_a, data_a = post_json(base, "/api/ask", {"prompt": "first"})
        assert status_a == 200
        status_b, data_b = post_json(base, "/api/ask", {"prompt": "second"})
        assert status_b == 409
        assert data_b["error"] == "busy"
        assert data_b["thread_id"] == data_a["thread_id"]

    def test_follow_up_reuses_thread(self, fake_viewer) -> None:
        base, _ = fake_viewer

        _, data = post_json(base, "/api/ask", {"prompt": "first question"})
        thread_id = data["thread_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(base + "/api/chat/thread/" + thread_id, timeout=10).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        status, data2 = post_json(
            base,
            "/api/ask",
            {"prompt": "follow-up question", "thread_id": thread_id},
        )
        assert status == 200
        assert data2["thread_id"] == thread_id

        deadline = time.time() + 30
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(base + "/api/chat/thread/" + thread_id, timeout=10).read()
            )["thread"]
            if len(thread["messages"]) >= 4:
                break
            time.sleep(0.3)
        assert len(thread["messages"]) >= 4
        roles = [message["role"] for message in thread["messages"]]
        assert roles[:4] == ["user", "assistant", "user", "assistant"]

    def test_thread_pin_and_ordering(self, fake_viewer) -> None:
        base, _ = fake_viewer

        _, first = post_json(base, "/api/ask", {"prompt": "first"})
        deadline = time.time() + 30
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + first["thread_id"], timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        status, pinned = post_json(
            base,
            "/api/chat/thread/" + first["thread_id"] + "/pin",
            {"pinned": True},
        )
        assert status == 200
        assert pinned["thread"]["pinned"] is True

        threads = json.loads(urllib.request.urlopen(base + "/api/chat/threads", timeout=10).read())[
            "threads"
        ]
        assert threads[0]["id"] == first["thread_id"]
        assert threads[0]["pinned"] is True

    def test_thread_persisted_to_disk(self, fake_viewer) -> None:
        base, live = fake_viewer

        _, data = post_json(base, "/api/ask", {"prompt": "persist me"})

        thread_path = live / "chat" / f"thread-{data['thread_id']}.json"
        assert thread_path.exists()
        document = json.loads(thread_path.read_text(encoding="utf-8"))
        assert document["messages"][0]["content"] == "persist me"

    def test_worker_clamps_oversized_answers(self, tmp_path: Path) -> None:
        """A successful delegation with a huge critique is stored clamped and
        reported as done, not 'Delegation failed before answering'."""

        with ViewerProcess(
            tmp_path,
            extra_env={
                "WISP_ASK_FAKE": "1",
                "WISP_ASK_FAKE_BIG": "1",
            },
        ) as context:
            base, live = context
            status, data = post_json(base, "/api/ask", {"prompt": "big answer please"})
            assert status == 200
            thread_id = data["thread_id"]
            deadline = time.time() + 40
            thread = None
            while time.time() < deadline:
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


class TestAskLockRelease:
    def test_oversized_prompt_is_rejected_without_acquiring_lock(self, fake_viewer) -> None:
        base, _ = fake_viewer
        status, data = post_json(base, "/api/ask", {"prompt": "x" * (MAX_MESSAGE_CHARS + 1)})
        assert status == 400
        assert "MAX_MESSAGE_CHARS" in data["error"]

    def test_oversized_prompt_does_not_brick_the_ask_feature(self, fake_viewer) -> None:
        base, _ = fake_viewer
        status, _ = post_json(base, "/api/ask", {"prompt": "x" * (MAX_MESSAGE_CHARS + 1)})
        assert status == 400
        # The very next (valid) ask must NOT be 409-bricked by a leaked lock.
        status2, data2 = post_json(base, "/api/ask", {"prompt": "still works"})
        assert status2 == 200, data2

    def test_malformed_thread_id_after_lock_is_rejected(self, fake_viewer) -> None:
        base, _ = fake_viewer
        # A valid prompt but invalid thread id: must 404 and not brick.
        status, _ = post_json(
            base, "/api/ask", {"prompt": "hi", "thread_id": "not-a-valid-thread-id"}
        )
        assert status == 404
        status2, _ = post_json(base, "/api/ask", {"prompt": "recovers"})
        assert status2 == 200


class TestChatModelScoping:
    def test_ask_uses_chat_model_when_set(self, fake_viewer) -> None:
        base, _ = fake_viewer
        status, _ = post_json(base, "/api/model", {"chat_model": "claude-opus-4-6-thinking"})
        assert status == 200

        status, data = post_json(base, "/api/ask", {"prompt": "quick one"})
        assert status == 200
        deadline = time.time() + 30
        thread = None
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + data["thread_id"], timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        assert thread["messages"][1]["meta"]["model"] == "claude-opus-4-6-thinking"

    def test_ask_falls_back_to_global_model(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, data = post_json(base, "/api/ask", {"prompt": "quick two"})
        assert status == 200
        deadline = time.time() + 30
        thread = None
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + data["thread_id"], timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        assert thread["messages"][1]["meta"]["model"] == "gemini-3.8-flash-high"


class TestChatAnswerExtraction:
    def test_prefers_critique_section(self) -> None:
        report = "\n".join(
            [
                "# ANTIGRAVITY ADVERSARIAL DELEGATION REPORT",
                "- **Verdict**: SUCCESS",
                "",
                "## Antigravity Critique â€” `gemini-3.8-flash-high` (PRIMARY)",
                "",
                "### Findings & Response",
                "",
                "Bottom line: looks fine.",
                "",
                "### Lifecycle",
                "- step 4 Â· DONE",
                "",
                "## Complete stdout (verbatim) â€” `gemini-3.8-flash-high`",
                "```text",
                json.dumps({"event": "step_update", "step_update": {"text_delta": "raw"}}),
                "```",
            ]
        )

        answer = extract_chat_answer(report)

        assert "Bottom line: looks fine." in answer
        assert "step 4" in answer
        assert "Complete stdout" not in answer
        assert "step_update" not in answer
        assert not answer.startswith("## Antigravity Critique")

    def test_fallback_trim_without_marker(self) -> None:
        answer = extract_chat_answer("plain answer\n## Complete stderr\njunk")

        assert answer == "plain answer"

    def test_empty_report_is_explicit(self) -> None:
        assert "No answer" in extract_chat_answer("")


class TestTestModeHygiene:
    def test_fake_launcher_marks_output(self) -> None:
        result = _fake_ask_launcher(["agy"], Path("."), {}, 30)

        assert result.stdout
        assert "[TEST MODE]" in result.stdout

    def test_status_reports_test_mode_on(self, fake_viewer) -> None:
        base, _ = fake_viewer

        data = json.loads(urllib.request.urlopen(base + "/api/status", timeout=10).read())

        assert data["test_mode"] is True

    def test_status_reports_test_mode_off(self, viewer) -> None:
        base, _ = viewer

        data = json.loads(urllib.request.urlopen(base + "/api/status", timeout=10).read())

        assert data["test_mode"] is False

    def test_fake_ask_marks_message_meta(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, data = post_json(base, "/api/ask", {"prompt": "meta check"})
        assert status == 200
        deadline = time.time() + 30
        thread = None
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + data["thread_id"], timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        assistant = thread["messages"][1]
        assert assistant["meta"]["fake"] is True
        assert "[TEST MODE]" in assistant["content"]


class TestUiAssets:
    def test_ui_ships_pending_reconciliation(self) -> None:
        html = (ROOT / "tools" / "antigravity_viewer.html").read_text(encoding="utf-8")

        assert "threadHasPendingAsk" in html
        assert "reconcilePendingAsk" in html

    def test_ui_ships_collapsible_run_details(self) -> None:
        html = (ROOT / "tools" / "antigravity_viewer.html").read_text(encoding="utf-8")

        assert "splitAssistantDetails" in html
        assert "SHOW RUN DETAILS" in html
        assert "'### Lifecycle'" in html


class TestAtomicViewerFiles:
    def test_manifest_write_is_atomic(self, tmp_path: Path) -> None:
        _write_viewer_manifest(tmp_path, {"port": 1})
        leftovers = [p for p in tmp_path.glob("viewer.json.tmp.*")]
        assert leftovers == []
        assert json.loads((tmp_path / "viewer.json").read_text(encoding="utf-8")) == {"port": 1}

    def test_settings_write_is_atomic(self, tmp_path: Path) -> None:
        write_viewer_settings(tmp_path, {"model": "gemini-3.8-flash-high"})
        leftovers = list(tmp_path.glob("viewer_settings.json.tmp.*"))
        assert leftovers == []


class TestArtifactContainment:
    def test_capture_inside_workspace_is_contained_and_rejoinable(self, tmp_path: Path) -> None:
        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        capture = tmp_path / ".antigravity-reports" / "captures" / "paste-x.png"
        capture.parent.mkdir(parents=True)
        capture.write_bytes(b"\x89PNG\r\n\x1a\n")

        context = ViewerContext(workspace=tmp_path, live_dir=live, port=0)
        contained = context.contain_artifact(str(capture))
        assert contained is not None
        # The worker must be able to rejoin the contained path against the
        # workspace and find the same file.
        rejoined = (tmp_path / contained).resolve()
        assert rejoined.is_file()
        assert rejoined == capture.resolve()

    def test_artifact_outside_workspace_is_rejected_entirely(self, tmp_path: Path) -> None:
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
