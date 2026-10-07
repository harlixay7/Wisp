"""Regression tests for viewer request handling, run discovery, and caching."""

from __future__ import annotations

import http.client
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools import antigravity_viewer as viewer
from tools.antigravity_viewer import ViewerContext, bind_server, url_host


def _ipv6_loopback_available() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


@contextmanager
def serving(host: str, workspace: Path) -> Iterator[tuple[str, int, Path]]:
    """Runs a viewer in-process; yields (host, port, live_dir)."""
    live = workspace / ".antigravity-reports" / "live"
    live.mkdir(parents=True, exist_ok=True)

    def factory(port: int) -> ViewerContext:
        return ViewerContext(workspace=workspace, live_dir=live, port=port)

    server, port = bind_server(host, 0, factory, max_attempts=1)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield host, port, live
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        conn.close()


class TestIpv6Loopback:
    def test_url_host_brackets_ipv6_literals(self) -> None:
        assert url_host("::1") == "[::1]"
        assert url_host("[::1]") == "[::1]"
        assert url_host("127.0.0.1") == "127.0.0.1"
        assert url_host("localhost") == "localhost"

    @pytest.mark.skipif(not _ipv6_loopback_available(), reason="IPv6 loopback unavailable")
    def test_bracketed_origin_is_accepted_on_ipv6_binding(self, tmp_path: Path) -> None:
        with serving("::1", tmp_path) as (host, port, _live):
            status, _ = _request(host, port, "GET", "/api/assets")
            assert status == 200
            status, data = _request(
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

    def test_allowed_origins_include_bracketed_ipv6(self) -> None:
        assert viewer.allowed_origins(48477) == {
            "http://127.0.0.1:48477",
            "http://localhost:48477",
            "http://[::1]:48477",
        }

    def test_bracketed_ipv6_origin_passes_the_post_guard(self, tmp_path: Path) -> None:
        with serving("127.0.0.1", tmp_path) as (host, port, _live):
            status, data = _request(
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
            status, data = _request(
                host,
                port,
                "POST",
                "/api/account/recheck",
                body=b"{}",
                headers={"X-Wisp-Request": "1", "Origin": "http://evil.example"},
            )
            assert status == 403
            assert data["error"] == "origin not allowed"


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class TestModelInventoryCache:
    @pytest.fixture()
    def agy_calls(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        import subprocess

        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no agy")

        monkeypatch.setattr(viewer, "_MODEL_CACHE", {"ts": 0.0, "models": [], "source": ""})
        monkeypatch.setattr(viewer, "resolve_agy_executable", lambda: "agy")
        monkeypatch.setattr(viewer.subprocess, "run", fake_run)
        return calls

    def test_fallback_inventory_is_cached(self, agy_calls: list[list[str]]) -> None:
        first = viewer.list_models()
        second = viewer.list_models()
        assert first["source"] == second["source"] == "fallback"
        assert first["models"] == list(viewer.FALLBACK_MODELS)
        assert len(agy_calls) == 1

    def test_fallback_cache_expires(self, agy_calls: list[list[str]]) -> None:
        viewer.list_models()
        viewer._MODEL_CACHE["ts"] -= viewer.MODEL_FALLBACK_TTL_SECONDS + 1
        viewer.list_models()
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
            status, data = _request(
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


class TestImageSaveFailures:
    def test_upload_reports_500_when_the_capture_dir_cannot_be_created(
        self, tmp_path: Path
    ) -> None:
        import base64

        with serving("127.0.0.1", tmp_path) as (host, port, live):
            # A regular file where the captures directory belongs makes mkdir fail.
            (live.parent / "captures").write_text("blocker", encoding="utf-8")
            body = json.dumps({"data": base64.b64encode(PNG_BYTES).decode("ascii")})
            status, data = _request(
                host,
                port,
                "POST",
                "/api/chat/upload",
                body=body.encode("utf-8"),
                headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
            )
            assert status == 500
            assert "could not save the image" in data["error"]

    def test_capture_auto_returns_none_when_the_capture_dir_is_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tools import wisp_capture

        monkeypatch.delenv("WISP_CAPTURE_FAKE", raising=False)
        monkeypatch.setattr(wisp_capture, "start_snip", lambda: {"kind": "snip_started"})
        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        (live.parent / "captures").write_text("blocker", encoding="utf-8")

        result = wisp_capture.capture_auto(live, prefer_selection=False)

        assert result["kind"] == "none"
        assert result["reason"]
