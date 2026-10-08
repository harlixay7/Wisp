"""MCP server: handshake, JSON-RPC errors, tool calls, payload bounds, and env config."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import ROOT
from tests.helpers.mcp import request, serve
from tools import antigravity_mcp_server as server
from tools.antigravity_bridge import AttemptResult, BridgeResult, run_bridge as real_run_bridge
from tools.antigravity_mcp_server import (
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOL_DEFINITIONS,
    InvalidParams,
    MethodNotFound,
    _bounded_list,
    _selected_models,
    handle_request,
)
from tools.skill_loader import SHIPPED_SKILL_DIR

VALID_SKILL_YAML = (
    "\n".join(
        [
            "name: mcp_test_skill",
            "version: 1.0.0",
            "description: Skill used by MCP tests.",
            "activation_triggers:",
            "  - mcp",
            "input_contract:",
            "  required: [paths]",
            "output_contract:",
            "  format: markdown",
            'instructions_payload: "MCP_TEST_INSTRUCTIONS"',
        ]
    )
    + "\n"
)


def _make_workspace(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "Skills"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "01_mcp_test.yaml").write_text(VALID_SKILL_YAML, encoding="utf-8")
    return tmp_path


def _text_of(response: dict) -> dict:
    return json.loads(response["content"][0]["text"])


class TestProtocol:
    def test_initialize_echoes_protocol_and_declares_tools(self) -> None:
        result = handle_request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

        assert result["protocolVersion"] == "2025-06-18"
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "antigravity-bridge"

    def test_ping_returns_empty_result(self) -> None:
        assert handle_request("ping", {}) == {}

    def test_tools_list_exposes_three_tools(self) -> None:
        result = handle_request("tools/list", {})

        names = {tool["name"] for tool in result["tools"]}
        assert names == {"antigravity_review", "antigravity_status", "antigravity_skills"}
        review = next(t for t in result["tools"] if t["name"] == "antigravity_review")
        assert review["inputSchema"]["required"] == ["prompt"]

    def test_defensive_empty_listings(self) -> None:
        assert handle_request("resources/list", {}) == {"resources": []}
        assert handle_request("prompts/list", {}) == {"prompts": []}

    def test_unknown_method_raises(self) -> None:
        with pytest.raises(MethodNotFound):
            handle_request("does/not/exist", {})


class TestProtocolVersionNegotiation:
    def test_supported_protocol_version_is_echoed(self) -> None:
        responses = serve(request(1, "initialize", {"protocolVersion": "2025-06-18"}))
        assert responses[0]["result"]["protocolVersion"] == "2025-06-18"

    def test_unsupported_protocol_version_falls_back(self) -> None:
        responses = serve(request(2, "initialize", {"protocolVersion": "1999-01-01"}))
        assert responses[0]["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[-1]

    def test_supported_versions_constant(self) -> None:
        assert PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS
        assert len(SUPPORTED_PROTOCOL_VERSIONS) >= 1


class TestJsonRpcErrors:
    def test_parse_error_returns_minus_32700(self) -> None:
        responses = serve("{not json")
        assert responses[0]["error"]["code"] == -32700

    def test_non_object_frame_returns_minus_32600(self) -> None:
        responses = serve("[1, 2, 3]")
        assert responses[0]["error"]["code"] == -32600

    def test_foreign_jsonrpc_version_returns_minus_32600(self) -> None:
        responses = serve(request(1, "ping", {}, version="1.0"))
        assert responses[0]["error"]["code"] == -32600
        assert "jsonrpc" in responses[0]["error"]["message"]

    def test_request_without_method_returns_minus_32600(self) -> None:
        frame = json.dumps({"jsonrpc": "2.0", "id": 5})
        responses = serve(frame)
        assert responses[0]["error"]["code"] == -32600

    def test_array_params_return_minus_32602(self) -> None:
        frame = json.dumps({"jsonrpc": "2.0", "id": 6, "method": "ping", "params": [1, 2]})
        responses = serve(frame)
        assert responses[0]["error"]["code"] == -32602

    def test_unknown_method_returns_minus_32601(self) -> None:
        responses = serve(request(8, "no/such/method", {}))
        assert responses[0]["error"]["code"] == -32601

    def test_client_response_frame_is_ignored(self) -> None:
        result_frame = json.dumps({"jsonrpc": "2.0", "id": 9, "result": {}})
        error_frame = json.dumps(
            {"jsonrpc": "2.0", "id": 10, "error": {"code": -1, "message": "no"}}
        )
        responses = serve(result_frame, error_frame, request(11, "ping", {}))
        assert responses == [{"jsonrpc": "2.0", "id": 11, "result": {}}]

    def test_notification_is_silently_ignored(self) -> None:
        frame = json.dumps(
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "antigravity_status"}}
        )
        responses = serve(frame)
        assert responses == []


class TestStdioServer:
    def test_scripted_handshake(self, tmp_path: Path) -> None:
        initialize = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            }
        )
        tools_list = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "antigravity_mcp_server.py")],
            input=f"{initialize}\n{tools_list}\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=90,
        )

        assert proc.returncode == 0, proc.stderr
        responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        assert len(responses) == 2
        assert responses[0]["id"] == 1
        assert responses[0]["result"]["serverInfo"]["name"] == "antigravity-bridge"
        assert responses[1]["id"] == 2
        assert len(responses[1]["result"]["tools"]) == 3

    def test_registry_tool_definitions_are_wellformed(self) -> None:
        for tool in TOOL_DEFINITIONS:
            assert tool["name"].startswith("antigravity_")
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"


class TestToolCalls:
    def test_unknown_tool_is_an_error(self) -> None:
        response = handle_request("tools/call", {"name": "nope", "arguments": {}})

        assert response["isError"] is True

    def test_review_requires_prompt(self) -> None:
        response = handle_request("tools/call", {"name": "antigravity_review", "arguments": {}})

        assert response["isError"] is True
        assert "prompt" in _text_of(response)["error"]

    def test_status_reports_registry(self, tmp_path: Path, monkeypatch) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setenv("ANTIGRAVITY_WORKSPACE", str(workspace))

        response = handle_request("tools/call", {"name": "antigravity_status", "arguments": {}})

        assert response["isError"] is False
        payload = _text_of(response)
        assert payload["workspace"] == str(workspace)
        assert any(skill["name"] == "mcp_test_skill" for skill in payload["skills"])

    def test_status_flags_missing_executable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTIGRAVITY_WORKSPACE", str(_make_workspace(tmp_path)))
        monkeypatch.setattr(server, "resolve_agy_executable", lambda: "wisp-test-missing-agy")

        response = handle_request("tools/call", {"name": "antigravity_status", "arguments": {}})

        assert response["isError"] is False
        payload = _text_of(response)
        assert payload["executable_found"] is False
        assert any("wisp-test-missing-agy" in warning for warning in payload["warnings"])

    def test_status_confirms_found_executable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTIGRAVITY_WORKSPACE", str(_make_workspace(tmp_path)))
        monkeypatch.setattr(server, "resolve_agy_executable", lambda: sys.executable)

        payload = _text_of(
            handle_request("tools/call", {"name": "antigravity_status", "arguments": {}})
        )

        assert payload["executable_found"] is True
        assert not any("was not found" in warning for warning in payload["warnings"])

    def test_skills_lists_triggers(self, tmp_path: Path, monkeypatch) -> None:
        workspace = _make_workspace(tmp_path)
        monkeypatch.setenv("ANTIGRAVITY_WORKSPACE", str(workspace))

        response = handle_request("tools/call", {"name": "antigravity_skills", "arguments": {}})

        payload = _text_of(response)
        skill = next(s for s in payload["skills"] if s["name"] == "mcp_test_skill")
        assert "mcp" in skill["activation_triggers"]
        assert skill["description"]

    def test_review_persists_full_report_and_returns_critique(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        workspace = _make_workspace(tmp_path)

        def fake_run_bridge(config) -> BridgeResult:
            assert config.envelope.prompt == "Audit the seam"
            assert config.skills == ("all",)
            assert config.recommended_skills == ("security-review",)
            return BridgeResult(
                success=True,
                model_used="gemini-3.8-flash-high",
                failover_used=False,
                timed_out=False,
                rate_limited=False,
                exit_code=0,
                attempts=[],
                critique_markdown="FULL CRITIQUE BODY",
            )

        monkeypatch.setattr("tools.antigravity_mcp_server.run_bridge", fake_run_bridge)

        response = handle_request(
            "tools/call",
            {
                "name": "antigravity_review",
                "arguments": {
                    "prompt": "Audit the seam",
                    "skills": ["all"],
                    "recommended_skills": ["security-review"],
                    "workspace": str(workspace),
                    "claims_to_falsify": ["No data loss"],
                },
            },
        )

        assert response["isError"] is False
        payload = _text_of(response)
        assert payload["success"] is True
        assert "FULL CRITIQUE BODY" in payload["critique_markdown"]
        report_path = Path(payload["report_path"])
        assert report_path.exists()
        saved = json.loads(report_path.read_text(encoding="utf-8"))
        assert saved["critique_markdown"] == "FULL CRITIQUE BODY"

    def test_review_failure_is_error_with_full_payload(self, tmp_path: Path, monkeypatch) -> None:
        workspace = _make_workspace(tmp_path)

        def fake_run_bridge(config) -> BridgeResult:
            return BridgeResult(
                success=False,
                model_used="gemini-3.8-flash-high",
                failover_used=True,
                timed_out=False,
                rate_limited=True,
                exit_code=1,
                attempts=[],
                critique_markdown="PRESERVED OUTPUT",
                error="Rate limit exhausted",
                resets_in="2h",
            )

        monkeypatch.setattr("tools.antigravity_mcp_server.run_bridge", fake_run_bridge)

        response = handle_request(
            "tools/call",
            {
                "name": "antigravity_review",
                "arguments": {"prompt": "Audit", "workspace": str(workspace)},
            },
        )

        assert response["isError"] is True
        payload = _text_of(response)
        assert payload["resets_in"] == "2h"
        assert "PRESERVED OUTPUT" in payload["critique_markdown"]


class TestForeignWorkspaceSkills:
    def test_review_with_skills_from_workspace_without_registry(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        calls: list[list[str]] = []

        def scripted_launcher(
            command, cwd, env, hard_timeout_seconds, raw_line_sink=None
        ) -> AttemptResult:
            calls.append([str(part) for part in command])
            return AttemptResult(
                exit_code=0,
                stdout='{"event": "result", "result": {"status": "SUCCESS", "response": "OK"}}',
                stderr="",
                duration_seconds=0.01,
            )

        def scripted_run(config):
            return real_run_bridge(config, launcher=scripted_launcher)

        monkeypatch.setattr(server, "run_bridge", scripted_run)
        workspace = tmp_path / "foreign"
        workspace.mkdir()

        response = handle_request(
            "tools/call",
            {
                "name": "antigravity_review",
                "arguments": {
                    "prompt": "Audit from a workspace without a registry",
                    "workspace": str(workspace),
                    "skills": ["plan-review"],
                },
            },
        )

        assert response["isError"] is False
        payload = _text_of(response)
        assert payload["success"] is True
        assert any("fell back to shipped registry" in warning for warning in payload["warnings"])
        add_dirs = [
            command[index + 1]
            for command in calls
            for index, part in enumerate(command)
            if part == "--add-dir"
        ]
        assert str(SHIPPED_SKILL_DIR) in add_dirs


class TestStringArrayArguments:
    def test_string_claims_are_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The delegation skill documents claims/artifacts as string-or-list; the MCP
        server must accept a bare string instead of -32602."""

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

        monkeypatch.setattr(server, "run_bridge", fake_run_bridge)
        _, is_error = server._tool_review({"prompt": "p", "claims_to_falsify": "single claim"})
        assert is_error is False

    def test_string_skills_are_comma_split(self) -> None:
        """Skill selectors given as one string are comma-split; claims are not."""
        assert _bounded_list("a,b", limit=10, name="skills", split_commas=True) == [
            "a",
            "b",
        ]
        assert _bounded_list("single claim", limit=10, name="claims_to_falsify") == ["single claim"]


class TestPayloadBounds:
    def test_schema_bounds_declared(self) -> None:
        review = next(tool for tool in TOOL_DEFINITIONS if tool["name"] == "antigravity_review")
        props = review["inputSchema"]["properties"]
        assert props["prompt"]["maxLength"] > 0
        assert props["context"]["maxLength"] > 0
        assert props["claims_to_falsify"]["maxItems"] > 0
        assert props["artifacts"]["maxItems"] > 0
        assert props["skills"]["maxItems"] > 0
        assert props["workspace"]["maxLength"] > 0
        assert props["model"]["maxLength"] > 0
        assert props["fallback_model"]["maxLength"] > 0

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


class TestRecommendedSkillsValidation:
    def test_recommended_skills_capped_at_three(self, tmp_path: Path, monkeypatch) -> None:
        workspace = _make_workspace(tmp_path)

        def exploding_run_bridge(config):  # pragma: no cover - must not run
            raise AssertionError("run_bridge must not be called for invalid input")

        monkeypatch.setattr("tools.antigravity_mcp_server.run_bridge", exploding_run_bridge)

        with pytest.raises(InvalidParams) as excinfo:
            handle_request(
                "tools/call",
                {
                    "name": "antigravity_review",
                    "arguments": {
                        "prompt": "Audit",
                        "workspace": str(workspace),
                        "recommended_skills": ["a", "b", "c", "d"],
                    },
                },
            )
        assert "recommended_skills" in str(excinfo.value)

    def test_recommended_skills_cap_is_protocol_error_over_stdio(self, tmp_path: Path) -> None:
        """End-to-end: the serve loop translates the typed error into -32602."""
        workspace = _make_workspace(tmp_path)
        frame = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "antigravity_review",
                    "arguments": {
                        "prompt": "Audit",
                        "workspace": str(workspace),
                        "recommended_skills": ["a", "b", "c", "d"],
                    },
                },
            }
        )
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "antigravity_mcp_server.py")],
            input=frame + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            cwd=str(ROOT),
        )
        lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        assert lines, proc.stdout + proc.stderr
        error = lines[0].get("error")
        assert error is not None
        assert error["code"] == -32602
        assert "recommended_skills" in error["message"]

    def test_schema_declares_max_items(self) -> None:
        review = next(tool for tool in TOOL_DEFINITIONS if tool["name"] == "antigravity_review")

        schema = review["inputSchema"]["properties"]["recommended_skills"]

        assert schema["maxItems"] == 3


class TestReviewMode:
    @staticmethod
    def _review_schema() -> dict:
        review = next(tool for tool in TOOL_DEFINITIONS if tool["name"] == "antigravity_review")
        return review["inputSchema"]

    def test_schema_declares_mode_enum_with_review_default(self) -> None:
        schema = self._review_schema()

        assert schema["properties"]["mode"]["enum"] == ["review", "implement"]
        assert schema["properties"]["mode"]["default"] == "review"
        assert schema["additionalProperties"] is False
        assert "mode" not in schema["required"]

    @pytest.mark.parametrize("bad", ["write", "Implement", 5, ["review"]])
    def test_bad_mode_is_invalid_params_before_dispatch(self, bad, monkeypatch) -> None:
        def exploding_run_bridge(config):  # pragma: no cover - must not run
            raise AssertionError("run_bridge must not be called for invalid input")

        monkeypatch.setattr(server, "run_bridge", exploding_run_bridge)

        with pytest.raises(InvalidParams) as excinfo:
            handle_request(
                "tools/call",
                {"name": "antigravity_review", "arguments": {"prompt": "p", "mode": bad}},
            )
        assert "'mode'" in str(excinfo.value)

    @pytest.mark.parametrize(
        ("arguments", "expected"), [({}, "review"), ({"mode": "implement"}, "implement")]
    )
    def test_mode_reaches_the_envelope(self, arguments, expected, tmp_path, monkeypatch) -> None:
        captured: dict = {}

        def capturing_run_bridge(config):
            captured["mode"] = config.envelope.mode
            return real_run_bridge(
                config,
                launcher=lambda *args: AttemptResult(exit_code=0, stdout="CRITIQUE"),
            )

        monkeypatch.setattr(server, "run_bridge", capturing_run_bridge)

        server._tool_review({"prompt": "p", "workspace": str(tmp_path), **arguments})

        assert captured["mode"] == expected

    def test_stdio_lists_mode_and_rejects_bad_mode_with_32602(self) -> None:
        responses = serve(
            request(1, "initialize", {"protocolVersion": PROTOCOL_VERSION}),
            request(2, "tools/list"),
            request(
                3,
                "tools/call",
                {"name": "antigravity_review", "arguments": {"prompt": "p", "mode": "write"}},
            ),
        )

        by_id = {response["id"]: response for response in responses}
        review = next(
            tool for tool in by_id[2]["result"]["tools"] if tool["name"] == "antigravity_review"
        )
        assert review["inputSchema"]["properties"]["mode"]["enum"] == ["review", "implement"]
        assert by_id[3]["error"]["code"] == -32602
        assert "mode" in by_id[3]["error"]["message"]


class TestConfigFromEnvironment:
    def test_malformed_env_int_degrades_with_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTIGRAVITY_RETRIES", "not-a-number")
        server._CONFIG_WARNINGS.clear()
        try:
            payload = server._tool_status()
        finally:
            warnings = list(server._CONFIG_WARNINGS.values())
            server._CONFIG_WARNINGS.clear()
        assert payload["retries"] == 2
        assert any("ANTIGRAVITY_RETRIES" in warning for warning in warnings)

    def test_repeated_config_warnings_do_not_accumulate(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTIGRAVITY_RETRIES", "many")
        monkeypatch.setattr(server, "_CONFIG_WARNINGS", {})
        for _ in range(3):
            payload = server._tool_status()

        assert len(payload["config_warnings"]) == 1


class TestModelSelection:
    def test_selected_models_prefer_viewer_settings(self, tmp_path: Path) -> None:
        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        (live / "viewer_settings.json").write_text(
            json.dumps({"model": "claude-sonnet-4-6"}),
            encoding="utf-8",
        )

        selected = _selected_models(tmp_path)

        assert selected["model"] == "claude-sonnet-4-6"
        assert selected["fallback_model"] == "claude-opus-4-6-thinking"

    def test_selected_models_ignore_chat_model(self, tmp_path: Path) -> None:
        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        (live / "viewer_settings.json").write_text(
            json.dumps(
                {
                    "model": "gemini-3.8-flash-high",
                    "fallback_model": "claude-opus-4-6-thinking",
                    "chat_model": "claude-sonnet-4-6",
                }
            ),
            encoding="utf-8",
        )

        selected = _selected_models(tmp_path)

        assert selected["model"] == "gemini-3.8-flash-high"
        assert selected["fallback_model"] == "claude-opus-4-6-thinking"

    def test_selected_models_follow_live_dir_override(self, tmp_path: Path, monkeypatch) -> None:
        live = tmp_path / "custom-live"
        live.mkdir()
        (live / "viewer_settings.json").write_text(
            json.dumps({"model": "claude-sonnet-4-6"}), encoding="utf-8"
        )
        monkeypatch.setenv("ANTIGRAVITY_LIVE_DIR", str(live))

        assert _selected_models(tmp_path / "elsewhere")["model"] == "claude-sonnet-4-6"

    def test_status_reports_viewer_selected_models(self, tmp_path: Path, monkeypatch) -> None:
        workspace = _make_workspace(tmp_path)
        live = workspace / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        (live / "viewer_settings.json").write_text(
            json.dumps({"model": "claude-sonnet-4-6"}), encoding="utf-8"
        )
        monkeypatch.setenv("ANTIGRAVITY_WORKSPACE", str(workspace))
        monkeypatch.delenv("ANTIGRAVITY_LIVE_DIR", raising=False)

        response = handle_request("tools/call", {"name": "antigravity_status", "arguments": {}})

        assert _text_of(response)["primary_model"] == "claude-sonnet-4-6"
