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
