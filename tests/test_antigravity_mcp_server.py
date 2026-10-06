"""Deterministic tests for the Antigravity MCP server (protocol and tools).

No test invokes the real ``agy`` binary; the review tool is exercised against a
patched bridge, and the stdio server is validated with a scripted handshake.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.antigravity_bridge import BridgeResult
from tools.antigravity_mcp_server import (
    TOOL_DEFINITIONS,
    MethodNotFound,
    handle_request,
)
from tools.skill_loader import SHIPPED_SKILL_DIR

ROOT = Path(__file__).resolve().parent.parent

VALID_SKILL_YAML = "\n".join(
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
) + "\n"


def _make_workspace(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "Skills"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "01_mcp_test.yaml").write_text(VALID_SKILL_YAML, encoding="utf-8")
    return tmp_path


def _text_of(response: dict) -> dict:
    return json.loads(response["content"][0]["text"])


class TestProtocol:
    def test_initialize_echoes_protocol_and_declares_tools(self) -> None:
        result = handle_request(
            "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}
        )

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


class TestToolCalls:
    def test_unknown_tool_is_an_error(self) -> None:
        response = handle_request(
            "tools/call", {"name": "nope", "arguments": {}}
        )

        assert response["isError"] is True

    def test_review_requires_prompt(self) -> None:
        response = handle_request(
            "tools/call", {"name": "antigravity_review", "arguments": {}}
        )

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
            assert config.recommended_skills == ("runtime-security-vault-engine",)
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
                    "recommended_skills": ["runtime-security-vault-engine"],
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

    def test_review_failure_is_error_with_full_payload(
        self, tmp_path: Path, monkeypatch
    ) -> None:
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
        from tools import antigravity_mcp_server as server
        from tools.antigravity_bridge import (
            AttemptResult,
            run_bridge as real_run_bridge,
        )

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
                    "skills": ["adversarial-plan-hardening-engine"],
                },
            },
        )

        assert response["isError"] is False
        payload = _text_of(response)
        assert payload["success"] is True
        assert any(
            "fell back to shipped registry" in warning
            for warning in payload["warnings"]
        )
        add_dirs = [
            command[index + 1]
            for command in calls
            for index, part in enumerate(command)
            if part == "--add-dir"
        ]
        assert str(SHIPPED_SKILL_DIR) in add_dirs


class TestChatModelIsolation:
    def test_selected_models_ignore_chat_model(self, tmp_path: Path) -> None:
        from tools.antigravity_mcp_server import _selected_models

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


class TestRecommendedSkillsValidation:
    def test_recommended_skills_capped_at_three(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        workspace = _make_workspace(tmp_path)

        def exploding_run_bridge(config):  # pragma: no cover - must not run
            raise AssertionError("run_bridge must not be called for invalid input")

        monkeypatch.setattr(
            "tools.antigravity_mcp_server.run_bridge", exploding_run_bridge
        )

        response = handle_request(
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

        assert response["isError"] is True
        payload = _text_of(response)
        assert "cannot exceed 3" in payload["error"]

    def test_schema_declares_max_items(self) -> None:
        review = next(
            tool for tool in TOOL_DEFINITIONS if tool["name"] == "antigravity_review"
        )

        schema = review["inputSchema"]["properties"]["recommended_skills"]

        assert schema["maxItems"] == 3


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
