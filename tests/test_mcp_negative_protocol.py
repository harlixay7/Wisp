"""JSON-RPC 2.0 negative-protocol tests for the MCP server (audit #37).

Exercises the stdio serve loop as an independent client would: malformed
frames, foreign protocol versions, invalid params, unknown methods, parse
errors, and notification silence — asserting the correct JSON-RPC error codes
(-32700, -32600, -32601, -32602) end-to-end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "tools" / "antigravity_mcp_server.py"


def _serve(*frames: str) -> list[dict]:
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


def _request(frame_id: int, method: str, params: dict | None = None, version: str | None = "2.0") -> str:
    frame: dict = {"id": frame_id, "method": method}
    if version is not None:
        frame["jsonrpc"] = version
    if params is not None:
        frame["params"] = params
    return json.dumps(frame)


class TestNegativeProtocol:
    def test_parse_error_returns_minus_32700(self) -> None:
        responses = _serve("{not json")
        assert responses[0]["error"]["code"] == -32700

    def test_non_object_frame_returns_minus_32600(self) -> None:
        responses = _serve("[1, 2, 3]")
        assert responses[0]["error"]["code"] == -32600

    def test_foreign_jsonrpc_version_returns_minus_32600(self) -> None:
        responses = _serve(_request(1, "ping", {}, version="1.0"))
        assert responses[0]["error"]["code"] == -32600
        assert "jsonrpc" in responses[0]["error"]["message"]

    def test_request_without_method_returns_minus_32600(self) -> None:
        frame = json.dumps({"jsonrpc": "2.0", "id": 5})
        responses = _serve(frame)
        assert responses[0]["error"]["code"] == -32600

    def test_array_params_return_minus_32602(self) -> None:
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": 6, "method": "ping", "params": [1, 2]}
        )
        responses = _serve(frame)
        assert responses[0]["error"]["code"] == -32602

    def test_unknown_method_returns_minus_32601(self) -> None:
        responses = _serve(_request(8, "no/such/method", {}))
        assert responses[0]["error"]["code"] == -32601

    def test_client_response_frame_is_ignored(self) -> None:
        result_frame = json.dumps({"jsonrpc": "2.0", "id": 9, "result": {}})
        error_frame = json.dumps(
            {"jsonrpc": "2.0", "id": 10, "error": {"code": -1, "message": "no"}}
        )
        responses = _serve(result_frame, error_frame, _request(11, "ping", {}))
        assert responses == [{"jsonrpc": "2.0", "id": 11, "result": {}}]

    def test_notification_is_silently_ignored(self) -> None:
        frame = json.dumps(
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "antigravity_status"}}
        )
        responses = _serve(frame)
        assert responses == []

    def test_supported_protocol_version_is_echoed(self) -> None:
        responses = _serve(
            _request(1, "initialize", {"protocolVersion": "2025-06-18"})
        )
        assert responses[0]["result"]["protocolVersion"] == "2025-06-18"

    def test_unsupported_protocol_version_falls_back(self) -> None:
        from tools.antigravity_mcp_server import SUPPORTED_PROTOCOL_VERSIONS

        responses = _serve(
            _request(2, "initialize", {"protocolVersion": "1999-01-01"})
        )
        assert responses[0]["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[-1]

    def test_supported_versions_constant(self) -> None:
        from tools.antigravity_mcp_server import (
            PROTOCOL_VERSION,
            SUPPORTED_PROTOCOL_VERSIONS,
        )

        assert PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS
        assert len(SUPPORTED_PROTOCOL_VERSIONS) >= 1

    def test_malformed_env_int_degrades_with_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tools import antigravity_mcp_server as server

        monkeypatch.setenv("ANTIGRAVITY_RETRIES", "not-a-number")
        server._CONFIG_WARNINGS.clear()
        try:
            payload = server._tool_status()
        finally:
            warnings = list(server._CONFIG_WARNINGS.values())
            server._CONFIG_WARNINGS.clear()
        assert payload["retries"] == 2
        assert any("ANTIGRAVITY_RETRIES" in warning for warning in warnings)

    def test_schema_bounds_declared(self) -> None:
        from tools.antigravity_mcp_server import TOOL_DEFINITIONS

        review = next(
            tool for tool in TOOL_DEFINITIONS if tool["name"] == "antigravity_review"
        )
        props = review["inputSchema"]["properties"]
        assert props["prompt"]["maxLength"] > 0
        assert props["context"]["maxLength"] > 0
        assert props["claims_to_falsify"]["maxItems"] > 0
        assert props["artifacts"]["maxItems"] > 0
        assert props["skills"]["maxItems"] > 0
        assert props["workspace"]["maxLength"] > 0
        assert props["model"]["maxLength"] > 0
        assert props["fallback_model"]["maxLength"] > 0
