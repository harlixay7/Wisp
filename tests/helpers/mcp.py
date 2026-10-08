"""MCP stdio client helpers: frame builders and a one-shot serve loop."""

from __future__ import annotations

import json
import subprocess
import sys

from tests.helpers import ROOT

SERVER = ROOT / "tools" / "antigravity_mcp_server.py"


def serve(*frames: str) -> list[dict]:
    """Feeds ``frames`` to a fresh stdio server and returns every response."""
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input="\n".join(frames) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        cwd=str(ROOT),
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def request(
    frame_id: int, method: str, params: dict | None = None, version: str | None = "2.0"
) -> str:
    """Builds one JSON-RPC request frame; ``version=None`` omits ``jsonrpc``."""
    frame: dict = {"id": frame_id, "method": method}
    if version is not None:
        frame["jsonrpc"] = version
    if params is not None:
        frame["params"] = params
    return json.dumps(frame)
