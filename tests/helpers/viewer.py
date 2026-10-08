"""Viewer test harness: a child-process viewer, an in-process server, run files, and HTTP calls."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from tests.helpers import ROOT
from tools.antigravity_viewer import ViewerContext, bind_server

STARTUP_TIMEOUT_SECONDS = 20


def write_run(
    live_dir: Path,
    name: str,
    events: list[dict],
    mtime: float | None = None,
) -> Path:
    """Writes a run file of JSONL events, optionally back-dating its mtime."""
    live_dir.mkdir(parents=True, exist_ok=True)
    path = live_dir / name
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class ViewerProcess:
    """Runs the viewer CLI as a child process; yields ``(base_url, live_dir)``.

    The child's stdout goes to DEVNULL and its stderr to a temporary file, so
    neither pipe can fill up and stall the viewer while nobody reads it.
    """

    def __init__(
        self,
        tmp_path: Path,
        extra_env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.extra_env = extra_env or {}
        self.extra_args = extra_args or []
        self.proc: subprocess.Popen | None = None
        self.base = ""
        self._stderr: IO[str] | None = None

    def __enter__(self) -> tuple[str, Path]:
        try:
            return self._start()
        except BaseException:
            self._stop()
            raise

    def __exit__(self, *exc_info: object) -> None:
        self._stop()

    def stderr_text(self) -> str:
        if self._stderr is None:
            return ""
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()

    def _start(self) -> tuple[str, Path]:
        live = self.tmp_path / ".antigravity-reports" / "live"
        write_run(
            live,
            "run-20260101-000000-test01.jsonl",
            [{"kind": "run_start", "run_id": "r", "seq": 1, "ts": time.time(), "text": ""}],
        )
        env = dict(os.environ)
        env.update(self.extra_env)
        self._stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tools" / "antigravity_viewer.py"),
                "--port",
                "0",
                "--workspace",
                str(self.tmp_path),
                "--no-open",
                *self.extra_args,
            ],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=self._stderr,
            env=env,
        )
        manifest = live / "viewer.json"
        deadline = time.time() + STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline and not manifest.exists():
            if self.proc.poll() is not None:
                raise RuntimeError(f"viewer exited early: {self.stderr_text()}")
            time.sleep(0.1)
        if not manifest.exists():
            raise RuntimeError(f"viewer.json was never written: {self.stderr_text()}")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.base = f"http://127.0.0.1:{data['port']}"
        return self.base, live

    def _stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None


@contextmanager
def serving(host: str, workspace: Path) -> Iterator[tuple[str, int, Path]]:
    """Runs a viewer in-process; yields ``(host, port, live_dir)``."""
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


def http_request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    """One HTTP exchange; returns ``(status, decoded JSON body)``."""
    conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        conn.close()


def post_json(base: str, path: str, payload: dict) -> tuple[int, dict]:
    """POSTs JSON with the session header; returns ``(status, body)`` even on errors."""
    request = urllib.request.Request(
        base + path,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=30)
        return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def ipv6_loopback_available() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True
