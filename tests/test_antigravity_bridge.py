"""Deterministic tests for the Antigravity delegation bridge and skill registry.

Covers binary resolution, environment sanitization, rate-limit detection,
command construction, stream aggregation, skill loading, quota failover
orchestration, complete output capture, and process containment. No test
invokes the real ``agy`` binary or consumes quota.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.antigravity_bridge import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_PRIMARY_MODEL,
    AttemptResult,
    BridgeConfig,
    DelegationEnvelope,
    aggregate_stream_json,
    build_agy_command,
    build_prompt_payload,
    extract_reset_text,
    is_rate_limited,
    is_transient_failure,
    launch_contained,
    parse_reset_seconds,
    render_critique,
    resolve_agy_executable,
    run_bridge,
    sanitize_environment,
    write_report,
)
from tools.skill_loader import (
    ALL_SELECTOR,
    SHIPPED_SKILL_DIR,
    SkillLoader,
    SkillNotFoundError,
    SkillValidationError,
    resolve_skill_dir,
)

ROOT = Path(__file__).resolve().parent.parent


def make_skill_yaml(
    name: str = "unit_skill",
    *,
    version: str = "1.2.3",
    payload: str = "UNIT_INSTRUCTIONS",
    kind: str = "skill",
    omit: str | None = None,
) -> str:
    fields = {
        "name": name,
        "version": version,
        "description": "A test skill.",
        "activation_triggers": ["unit", "test"],
        "input_contract": {"required": ["source_paths"]},
        "output_contract": {"format": "markdown", "sections": ["Findings"]},
        "instructions_payload": payload,
    }
    if omit:
        fields.pop(omit, None)
    lines = [f"kind: {kind}"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {sub_key}: {json.dumps(sub_value)}")
        else:
            lines.append(f"{key}: {json.dumps(value)}")
    return "\n".join(lines) + "\n"


@pytest.fixture()
def registry(tmp_path: Path) -> Path:
    skill_dir = tmp_path / ".skills" / "antigravity"
    skill_dir.mkdir(parents=True)
    (skill_dir / "01_crash_ops.yaml").write_text(
        make_skill_yaml("crash_ops", payload="CRASH_OPS_INSTRUCTIONS", omit=None), encoding="utf-8"
    )
    (skill_dir / "02_ast_audit.yaml").write_text(
        make_skill_yaml("ast_audit", payload="AST_AUDIT_INSTRUCTIONS"), encoding="utf-8"
    )
    (skill_dir / "template_custom_skill.yaml").write_text(
        make_skill_yaml("template_custom_skill", kind="template", payload="TEMPLATE_ONLY_INSTRUCTIONS"),
        encoding="utf-8",
    )
    return skill_dir


class ScriptedLauncher:
    """Deterministic launcher fake; records every command and never spawns a process."""

    def __init__(self, results: list[AttemptResult]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.timeouts: list[int] = []
        self.raw_line_sinks: list = []

    def __call__(self, command, cwd, env, hard_timeout_seconds, raw_line_sink=None) -> AttemptResult:
        self.calls.append([str(part) for part in command])
        self.envs.append(dict(env))
        self.timeouts.append(hard_timeout_seconds)
        self.raw_line_sinks.append(raw_line_sink)
        index = min(len(self.calls) - 1, len(self._results) - 1)
        result = self._results[index]
        result.command = tuple(str(part) for part in command)
        return result


def successful_attempt(stdout: str = "FULL_CRITIQUE_TEXT", exit_code: int = 0) -> AttemptResult:
    return AttemptResult(exit_code=exit_code, stdout=stdout, stderr="", duration_seconds=0.01)


def rate_limited_attempt() -> AttemptResult:
    return AttemptResult(
        exit_code=1,
        stdout=json.dumps({"error": {"message": "RESOURCE_EXHAUSTED: Individual quota reached"}}),
        stderr="code 429",
        duration_seconds=0.01,
    )


def transient_attempt() -> AttemptResult:
    return AttemptResult(
        exit_code=1, stdout="", stderr="read ECONNRESET", duration_seconds=0.01
    )


def empty_attempt() -> AttemptResult:
    return AttemptResult(exit_code=0, stdout="", stderr="", duration_seconds=0.01)


def rate_limited_attempt_with_reset(reset: str = "2h") -> AttemptResult:
    return AttemptResult(
        exit_code=1,
        stdout=json.dumps(
            {"error": {"message": f"RESOURCE_EXHAUSTED: quota reached. Resets in {reset}."}}
        ),
        stderr="code 429",
        duration_seconds=0.01,
    )


def envelope(prompt: str = "Stress-test the plan") -> DelegationEnvelope:
    return DelegationEnvelope(prompt=prompt)


class TestBinaryResolution:
    def test_prefers_path_lookup(self) -> None:
        found = resolve_agy_executable(
            which=lambda name: "C:/tools/agy.exe" if name == "agy" else None,
            home=Path("C:/definitely-not-a-home"),
        )

        assert found == "C:/tools/agy.exe"

    def test_falls_back_to_home_gemini_bin(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / ".gemini" / "bin"
        bin_dir.mkdir(parents=True)
        expected_name = "agy.exe" if os.name == "nt" else "agy"
        expected = bin_dir / expected_name
        expected.write_text("", encoding="utf-8")

        found = resolve_agy_executable(which=lambda name: None, home=tmp_path)

        assert Path(found) == expected

    def test_returns_bare_name_when_nothing_found(self, tmp_path: Path) -> None:
        found = resolve_agy_executable(which=lambda name: None, home=tmp_path)

        assert found == "agy"


class TestEnvironmentSanitization:
    def test_strips_all_secret_prefixes_and_preserves_essentials(self, tmp_path: Path) -> None:
        base_env = {
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "AZURE_CLIENT_SECRET": "azure-secret",
            "GITHUB_TOKEN": "gh-pat",
            "GH_TOKEN": "gh-token",
            "SSH_AUTH_SOCK": "ssh-sock",
            "OPENAI_API_KEY": "sk-openai",
            "ANTHROPIC_API_KEY": "sk-anthropic",
            "GEMINI_API_KEY": "gm-key",
            "KEEP_ME": "1",
            "APPDATA": "C:/Users/example/AppData/Roaming",
            "PATH": "C:/windows/system32",
        }

        clean = sanitize_environment(base_env=base_env, home=tmp_path)

        for blocked in base_env:
            if blocked in {"KEEP_ME", "APPDATA", "PATH"}:
                continue
            assert blocked not in clean
        assert clean["KEEP_ME"] == "1"
        assert clean["APPDATA"] == "C:/Users/example/AppData/Roaming"

    def test_prepends_gemini_bin_to_path_idempotently(self, tmp_path: Path) -> None:
        gemini_bin = str(tmp_path / ".gemini" / "bin")
        base_env = {"PATH": os.pathsep.join(["C:/bin", gemini_bin])}

        clean = sanitize_environment(base_env=base_env, home=tmp_path)
        entries = clean["PATH"].split(os.pathsep)

        assert entries.count(gemini_bin) == 1
        assert entries[0] == gemini_bin

    def test_ensures_home_and_userprofile_fallbacks(self, tmp_path: Path) -> None:
        clean = sanitize_environment(base_env={"PATH": ""}, home=tmp_path)

        assert clean["HOME"] == str(tmp_path)
        if os.name == "nt":
            assert clean["USERPROFILE"] == str(tmp_path)

    def test_extra_env_is_sanitized(self, tmp_path: Path) -> None:
        clean = sanitize_environment(
            base_env={"PATH": ""},
            home=tmp_path,
            extra_env={"MY_FLAG": "on", "OPENAI_API_KEY": "leak"},
        )

        assert clean["MY_FLAG"] == "on"
        assert "OPENAI_API_KEY" not in clean


class TestRateLimitDetection:
    @pytest.mark.parametrize(
        "sample",
        [
            '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}',
            "Error: Individual quota reached. Resets in 3h.",
            "HTTP 429 Too Many Requests: rate limit exceeded",
            "code 429",
        ],
    )
    def test_detects_quota_signatures(self, sample: str) -> None:
        assert is_rate_limited(sample)

    def test_benign_text_is_not_rate_limited(self) -> None:
        assert not is_rate_limited("All 429 assertions passed after the fix.")
        assert not is_rate_limited("")
        assert not is_rate_limited(None)


class TestCommandConstruction:
    def test_gemini_command_contains_full_contract(self) -> None:
        command = build_agy_command(
            "agy", "PROMPT", Path("C:/ws"), "gemini-3.8-flash-high", 600
        )

        assert command[0] == "agy"
        assert command[command.index("-p") + 1] == "PROMPT"
        assert command[command.index("--add-dir") + 1] == str(Path("C:/ws"))
        assert command[command.index("--model") + 1] == "gemini-3.8-flash-high"
        assert command[command.index("--effort") + 1] == "high"
        assert command[command.index("--print-timeout") + 1] == "600s"
        assert "--dangerously-skip-permissions" in command
        assert command[command.index("--output-format") + 1] == "stream-json"

    def test_claude_command_omits_gemini_only_effort_flag(self) -> None:
        command = build_agy_command(
            "agy", "PROMPT", Path("C:/ws"), "claude-sonnet-4-6", 600
        )

        assert "--effort" not in command


class TestStreamAggregation:
    def test_preserves_very_long_text_without_truncation(self) -> None:
        long_text = "X" * 20000
        raw = json.dumps({"step_update": {"thinking": "planning", "text": long_text}})

        critique = aggregate_stream_json(raw)

        assert long_text in critique
        assert "### Reasoning & Analysis" in critique

    def test_plain_text_lines_are_kept_verbatim(self) -> None:
        critique = aggregate_stream_json("plain line one\nplain line two")

        assert "plain line one" in critique
        assert "plain line two" in critique

    def test_single_json_container_response_is_extracted(self) -> None:
        raw = json.dumps({"status": "SUCCESS", "response": "VERDICT: plan is unsound"})

        critique = aggregate_stream_json(raw)

        assert "VERDICT: plan is unsound" in critique

    def test_tool_calls_are_rendered_with_full_arguments(self) -> None:
        payload = {
            "event": "step_update",
            "step_update": {
                "tool_calls": [
                    {"name": "run_command", "args": {"command": "pytest -q", "note": "Y" * 5000}}
                ]
            },
        }

        critique = aggregate_stream_json(json.dumps(payload))

        assert "run_command" in critique
        assert "Y" * 5000 in critique

    def test_empty_stdout_is_explicit(self) -> None:
        assert "no stdout" in aggregate_stream_json("").lower()

    def test_lifecycle_beacons_render_compactly_not_as_unparsed(self) -> None:
        lines = [
            json.dumps({"event": "init", "init": {"model": "gemini-3.8-flash-high"}}),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {"step_index": 4, "state": "ACTIVE"},
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {"step_index": 4, "state": "DONE"},
                }
            ),
            json.dumps({"step_update": {"text": "the real answer"}}),
        ]

        critique = aggregate_stream_json("\n".join(lines))

        assert "the real answer" in critique
        assert "### Lifecycle" in critique
        assert "**Digest**: 1 steps" in critique
        assert "1 model" in critique
        assert "Unparsed Stream Lines" not in critique

    def test_real_agy_stream_shapes_are_aggregated(self) -> None:
        lines = [
            json.dumps(
                {
                    "event": "init",
                    "init": {"model": "gemini-3.8-flash-high", "cwd": "C:\\repo"},
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 0,
                        "state": "DONE",
                        "step_type": "user_input",
                    },
                }
            ),
        ]
        for index in (2, 4, 6):
            for state in ("ACTIVE", "DONE"):
                lines.append(
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {
                                "step_index": index,
                                "state": state,
                                "step_type": "tool",
                                "tool_name": "read_file",
                            },
                        }
                    )
                )
        lines += [
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 180,
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": "# VERDICT\n\n",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 180,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": "Claim 1 is FALSIFIED with evidence.",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "response": "# VERDICT\n\nClaim 1 is FALSIFIED with evidence.",
                    },
                }
            ),
        ]

        critique = aggregate_stream_json("\n".join(lines))

        assert "### Findings & Response" in critique
        assert "Claim 1 is FALSIFIED with evidence." in critique
        assert "3 tool" in critique
        assert "result \u00b7 SUCCESS" in critique
        assert "`read_file` \u00d73" in critique
        assert "step 180" in critique
        assert "Complete stdout" not in critique
        assert "Unparsed Stream Lines" not in critique
        assert len(critique) < 4000

    def test_duplicate_and_cumulative_deltas_are_folded(self) -> None:
        lines = [
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 9,
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": "hello ",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 9,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": "hello ",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 9,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": "hello world",
                    },
                }
            ),
        ]

        critique = aggregate_stream_json("\n".join(lines))

        assert critique.count("hello world") == 1
        assert "hello hello" not in critique


class TestSkillLoader:
    def test_valid_registry_loads_and_renders(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        rendered = loader.render_selected(ALL_SELECTOR)

        assert "CRASH_OPS_INSTRUCTIONS" in rendered
        assert "AST_AUDIT_INSTRUCTIONS" in rendered
        assert "TEMPLATE_ONLY_INSTRUCTIONS" not in rendered
        assert loader.available() == [
            "crash_ops",
            "ast_audit",
            "template_custom_skill",
        ]

    def test_load_by_filename_stem(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        skill = loader.load("01_crash_ops")

        assert skill.name == "crash_ops"

    def test_select_deduplicates_and_preserves_order(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        selected = loader.select("ast_audit,ast_audit,crash_ops")

        assert [skill.name for skill in selected] == ["ast_audit", "crash_ops"]

    def test_unknown_identifier_reports_available(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        with pytest.raises(SkillNotFoundError) as excinfo:
            loader.load("does_not_exist")

        assert "ast_audit" in str(excinfo.value)

    def test_missing_required_field_fails_validation(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "broken.yaml").write_text(
            make_skill_yaml("broken", omit="instructions_payload"), encoding="utf-8"
        )

        with pytest.raises(SkillValidationError) as excinfo:
            SkillLoader(skill_dir).skills

        assert "instructions_payload" in str(excinfo.value)

    def test_invalid_yaml_fails_validation(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "bad.yaml").write_text("name: [unclosed", encoding="utf-8")

        with pytest.raises(SkillValidationError):
            SkillLoader(skill_dir).skills

    def test_missing_registry_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SkillNotFoundError):
            SkillLoader(tmp_path / "nope").skills


class TestEnvelope:
    def test_requires_a_prompt(self) -> None:
        with pytest.raises(ValueError):
            DelegationEnvelope.from_mapping({"context": "no prompt"})

    def test_rejects_claims_of_wrong_type(self) -> None:
        with pytest.raises(ValueError):
            DelegationEnvelope.from_mapping({"prompt": "x", "claims_to_falsify": 42})

    def test_payload_includes_claims_and_artifacts(self) -> None:
        config = BridgeConfig(
            envelope=DelegationEnvelope(
                prompt="Review the concurrency design",
                harness="opencode",
                context="Queue is in src/queue.py",
                claims_to_falsify=("O(1) enqueue",),
                artifacts=("src/queue.py:1-120",),
            )
        )

        payload = build_prompt_payload(config)

        assert "Review the concurrency design" in payload
        assert "opencode" in payload
        assert "O(1) enqueue" in payload
        assert "src/queue.py:1-120" in payload


class TestBridgeOrchestration:
    def test_successful_primary_run_reports_success(self) -> None:
        launcher = ScriptedLauncher([successful_attempt("PRIMARY_FULL_TEXT")])
        config = BridgeConfig(envelope=envelope(), workspace=Path.cwd())

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert not result.failover_used
        assert result.model_used == DEFAULT_PRIMARY_MODEL
        assert len(launcher.calls) == 1
        assert "PRIMARY_FULL_TEXT" in result.critique_markdown

    def test_rate_limit_triggers_claude_failover(self) -> None:
        launcher = ScriptedLauncher(
            [rate_limited_attempt(), successful_attempt("FALLBACK_FULL_TEXT")]
        )
        config = BridgeConfig(envelope=envelope(), workspace=Path.cwd())

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert result.failover_used
        assert result.model_used == DEFAULT_FALLBACK_MODEL
        assert len(launcher.calls) == 2
        failover_command = launcher.calls[1]
        assert failover_command[failover_command.index("--model") + 1] == DEFAULT_FALLBACK_MODEL
        assert "--effort" not in failover_command

    def test_failover_retains_primary_and_fallback_outputs(self) -> None:
        launcher = ScriptedLauncher(
            [rate_limited_attempt(), successful_attempt("FALLBACK_FULL_TEXT")]
        )
        config = BridgeConfig(envelope=envelope(), workspace=Path.cwd())

        result = run_bridge(config, launcher=launcher)

        assert "RESOURCE_EXHAUSTED" in result.critique_markdown
        assert "FALLBACK_FULL_TEXT" in result.critique_markdown
        assert len(result.attempts) == 2

    def test_double_rate_limit_fails_loudly(self) -> None:
        launcher = ScriptedLauncher([rate_limited_attempt(), rate_limited_attempt()])
        config = BridgeConfig(envelope=envelope(), workspace=Path.cwd())

        result = run_bridge(config, launcher=launcher)

        assert not result.success
        assert result.rate_limited
        assert result.failover_used
        assert result.error is not None and "Rate limit exhausted" in result.error
        assert len(result.attempts) == 2

    def test_launch_failure_is_reported_with_full_message(self) -> None:
        def exploding_launcher(command, cwd, env, hard_timeout_seconds, raw_line_sink=None):
            raise FileNotFoundError("agy.exe missing from PATH")

        config = BridgeConfig(envelope=envelope(), workspace=Path.cwd())

        result = run_bridge(config, launcher=exploding_launcher)

        assert not result.success
        assert result.exit_code == 127
        assert result.error is not None and "agy.exe missing" in result.error
        assert "DELEGATION FAILED" in result.critique_markdown

    def test_skills_are_embedded_in_the_dispatched_payload(self, registry: Path) -> None:
        launcher = ScriptedLauncher([successful_attempt()])
        config = BridgeConfig(
            envelope=envelope(),
            workspace=registry.parent,
            skills=(ALL_SELECTOR,),
            skill_dir=registry,
        )

        result = run_bridge(config, launcher=launcher)

        assert result.success
        payload = launcher.calls[0][launcher.calls[0].index("-p") + 1]
        assert "CRASH_OPS_INSTRUCTIONS" in payload
        assert "AST_AUDIT_INSTRUCTIONS" in payload
        assert "TEMPLATE_ONLY_INSTRUCTIONS" not in payload

    def test_missing_skill_registry_fails_before_spawning(self) -> None:
        launcher = ScriptedLauncher([successful_attempt()])
        config = BridgeConfig(
            envelope=envelope(),
            workspace=Path.cwd(),
            skills=("all",),
            skill_dir=Path("C:/does/not/exist"),
        )

        result = run_bridge(config, launcher=launcher)

        assert not result.success
        assert result.exit_code == 2
        assert launcher.calls == []

    def test_result_to_dict_carries_full_streams(self) -> None:
        launcher = ScriptedLauncher([successful_attempt("Z" * 50000)])
        config = BridgeConfig(envelope=envelope(), workspace=Path.cwd())

        result = run_bridge(config, launcher=launcher)
        serialized = result.to_dict()

        assert serialized["success"] is True
        assert serialized["attempts"][0]["stdout"] == "Z" * 50000

    def test_default_hard_timeout_includes_grace(self) -> None:
        config = BridgeConfig(envelope=envelope())

        assert config.hard_timeout_seconds() == config.print_timeout_seconds + config.grace_seconds


ESCAPED_MARKDOWN_SKILL = (
    "\\---\n"
    "\n"
    "name: escaped-skill\n"
    "\n"
    "version: 2.0.0\n"
    "\n"
    "description: A rich-text exported skill.\n"
    "\n"
    "activation\\_triggers:\n"
    "\n"
    "&#x20; task\\_modes:\n"
    "\n"
    "&#x20;   - MODE\\_ONE\n"
    "\n"
    "&#x20; keywords:\n"
    "\n"
    "&#x20;   - keyword\n"
    "\n"
    "input\\_contract:\n"
    "\n"
    "&#x20; requires\\_worktree: true\n"
    "\n"
    "output\\_contract:\n"
    "\n"
    "&#x20; requires\\_verdict: true\n"
    "\n"
    "\\---\n"
    "\n"
    "\\# MANDATE\n"
    "\n"
    "Operate \\*\\*strictly\\*\\* in read-only mode.\n"
)


class TestResilienceAndQuotaControls:
    def test_parse_reset_seconds_variants(self) -> None:
        assert parse_reset_seconds("Resets in 3h") == 10800
        assert parse_reset_seconds("quota reached. Resets in 1h 30m.") == 5400
        assert parse_reset_seconds("Resets in 45s") == 45
        assert parse_reset_seconds("no reset info here") is None
        assert extract_reset_text("Resets in 1h 30m.") == "1h 30m"

    def test_transient_classification(self) -> None:
        assert is_transient_failure(transient_attempt())
        assert is_transient_failure(empty_attempt())
        assert not is_transient_failure(successful_attempt())
        assert not is_transient_failure(
            AttemptResult(exit_code=1, stderr="Error: unknown model name")
        )

    def test_transient_failure_is_retried_then_succeeds(self) -> None:
        launcher = ScriptedLauncher([transient_attempt(), successful_attempt("RECOVERED")])
        config = BridgeConfig(
            envelope=envelope(), workspace=Path.cwd(), retries=2, retry_backoff_seconds=0.0
        )

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert len(launcher.calls) == 2
        assert "RECOVERED" in result.critique_markdown

    def test_empty_success_is_treated_as_failure_and_retried(self) -> None:
        launcher = ScriptedLauncher([empty_attempt(), successful_attempt("REAL_OUTPUT")])
        config = BridgeConfig(
            envelope=envelope(), workspace=Path.cwd(), retries=2, retry_backoff_seconds=0.0
        )

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert len(launcher.calls) == 2
        assert "REAL_OUTPUT" in result.critique_markdown

    def test_transient_exhaustion_falls_over_to_fallback(self) -> None:
        launcher = ScriptedLauncher(
            [transient_attempt(), transient_attempt(), successful_attempt("FALLBACK_OK")]
        )
        config = BridgeConfig(
            envelope=envelope(), workspace=Path.cwd(), retries=1, retry_backoff_seconds=0.0
        )

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert result.failover_used
        assert result.model_used == DEFAULT_FALLBACK_MODEL
        assert len(launcher.calls) == 3

    def test_non_transient_failure_does_not_retry_or_failover(self) -> None:
        launcher = ScriptedLauncher(
            [AttemptResult(exit_code=1, stderr="Error: invalid configuration")]
        )
        config = BridgeConfig(
            envelope=envelope(), workspace=Path.cwd(), retries=2, retry_backoff_seconds=0.0
        )

        result = run_bridge(config, launcher=launcher)

        assert not result.success
        assert len(launcher.calls) == 1
        assert not result.failover_used
        assert result.error is not None and "exited with code 1" in result.error

    def test_quota_wait_retries_primary_without_failover(self) -> None:
        launcher = ScriptedLauncher(
            [rate_limited_attempt_with_reset("0s"), successful_attempt("AFTER_WAIT")]
        )
        config = BridgeConfig(
            envelope=envelope(),
            workspace=Path.cwd(),
            retry_backoff_seconds=0.0,
            quota_wait_seconds=10,
        )

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert len(launcher.calls) == 2
        assert not result.failover_used
        assert launcher.calls[1][launcher.calls[1].index("--model") + 1] == DEFAULT_PRIMARY_MODEL

    def test_quota_hook_runs_and_recovers(self) -> None:
        hook = f'"{sys.executable}" -c "print(1)"'
        launcher = ScriptedLauncher(
            [rate_limited_attempt_with_reset("3h"), successful_attempt("AFTER_ROTATION")]
        )
        config = BridgeConfig(
            envelope=envelope(),
            workspace=Path.cwd(),
            retry_backoff_seconds=0.0,
            quota_hook=hook,
        )

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert result.quota_hook_used
        assert result.quota_hook_output is not None
        assert len(launcher.calls) == 2
        assert not result.failover_used

    def test_quota_hook_failure_falls_back_and_is_preserved(self) -> None:
        hook = f'"{sys.executable}" -c "raise SystemExit(1)"'
        launcher = ScriptedLauncher(
            [rate_limited_attempt_with_reset("3h"), successful_attempt("FALLBACK_OK")]
        )
        config = BridgeConfig(
            envelope=envelope(),
            workspace=Path.cwd(),
            retry_backoff_seconds=0.0,
            quota_hook=hook,
        )

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert result.quota_hook_used
        assert result.failover_used
        assert any("failed" in warning for warning in result.warnings)

    def test_rate_limit_reset_is_surfaced_in_failure(self) -> None:
        launcher = ScriptedLauncher(
            [rate_limited_attempt_with_reset("2h"), rate_limited_attempt_with_reset("2h")]
        )
        config = BridgeConfig(envelope=envelope(), workspace=Path.cwd(), retry_backoff_seconds=0.0)

        result = run_bridge(config, launcher=launcher)

        assert not result.success
        assert result.resets_in == "2h"
        assert result.reset_seconds == 7200
        assert result.error is not None and "Quota resets in 2h" in result.error

    def test_write_report_persists_complete_result(self, tmp_path: Path) -> None:
        launcher = ScriptedLauncher([successful_attempt("REPORT_BODY")])
        config = BridgeConfig(envelope=envelope(), workspace=tmp_path)

        result = run_bridge(config, launcher=launcher)
        report_path = write_report(tmp_path, result)

        assert report_path.exists()
        assert report_path.parent.name == ".antigravity-reports"
        saved = json.loads(report_path.read_text(encoding="utf-8"))
        assert saved["critique_markdown"] == result.critique_markdown
        assert saved["attempts"][0]["stdout"] == "REPORT_BODY"


class TestMarkdownAndMixedFormats:
    def test_escaped_rich_text_markdown_skill_loads(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "02_escaped.md").write_text(ESCAPED_MARKDOWN_SKILL, encoding="utf-8")

        skill = SkillLoader(skill_dir).load("escaped-skill")

        assert skill.name == "escaped-skill"
        assert "MODE_ONE" in skill.activation_triggers
        assert "keyword" in skill.activation_triggers
        assert "**strictly**" in skill.instructions_payload
        assert skill.input_contract["requires_worktree"] is True

    def test_markdown_without_frontmatter_is_rejected(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "plain.md").write_text("# No front matter\n\nBody only.", encoding="utf-8")

        with pytest.raises(SkillValidationError):
            SkillLoader(skill_dir).skills

    def test_yaml_mapping_triggers_are_flattened(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "mapping.yaml").write_text(
            "\n".join(
                [
                    "name: mapping_triggers",
                    "version: 1.0.0",
                    "description: Trigger mapping test.",
                    "activation_triggers:",
                    "  task_modes:",
                    "    - A_MODE",
                    "  keywords:",
                    "    - kw",
                    "input_contract:",
                    "  required: [paths]",
                    "output_contract:",
                    "  format: markdown",
                    'instructions_payload: "DO"',
                ]
            ),
            encoding="utf-8",
        )

        skill = SkillLoader(skill_dir).load("mapping_triggers")

        assert skill.activation_triggers == ("A_MODE", "kw")

    def test_empty_placeholder_is_skipped_with_warning(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "01_valid.yaml").write_text(
            make_skill_yaml("valid_one", payload="VALID"), encoding="utf-8"
        )
        (skill_dir / "04_pending.md").write_bytes(b"")

        loader = SkillLoader(skill_dir)
        selected = loader.select(ALL_SELECTOR)

        assert [skill.name for skill in selected] == ["valid_one"]
        assert any("04_pending.md" in warning for warning in loader.warnings)

    def test_payload_points_antigravity_at_registry_on_disk(self) -> None:
        registry = Path("C:/ws/Skills")
        config = BridgeConfig(envelope=envelope())

        payload = build_prompt_payload(config, "SKILL BLOCK", registry)

        assert "SKILL REGISTRY ON DISK" in payload
        assert str(registry) in payload
        assert "SKILL BLOCK" in payload

    def test_default_registry_is_workspace_skills_directory(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "plan.yaml").write_text(
            make_skill_yaml("plan_skill", payload="DEFAULT_REGISTRY_INSTRUCTIONS"),
            encoding="utf-8",
        )
        launcher = ScriptedLauncher([successful_attempt()])
        config = BridgeConfig(envelope=envelope(), workspace=tmp_path, skills=(ALL_SELECTOR,))

        result = run_bridge(config, launcher=launcher)

        assert result.success
        payload = launcher.calls[0][launcher.calls[0].index("-p") + 1]
        assert "DEFAULT_REGISTRY_INSTRUCTIONS" in payload
        assert "SKILL REGISTRY ON DISK" in payload

    def test_workspace_skill_registry_loads(self) -> None:
        registry = ROOT / "Skills"
        if not registry.is_dir():
            pytest.skip("workspace Skills registry is not present")
        loader = SkillLoader(registry)

        skills = loader.skills

        assert skills
        for skill in skills:
            assert skill.instructions_payload.strip()
            assert skill.activation_triggers


class TestProcessContainment:
    def test_captures_complete_stdout_and_stderr(self, tmp_path: Path) -> None:
        script = (
            "import sys;"
            "sys.stdout.write('alpha-line\\n' * 500);"
            "sys.stderr.write('omega-error\\n')"
        )
        env = sanitize_environment(home=tmp_path)

        result = launch_contained(
            [sys.executable, "-c", script], cwd=tmp_path, env=env, hard_timeout_seconds=60
        )

        assert result.exit_code == 0
        assert result.stdout.count("alpha-line") == 500
        assert "omega-error" in result.stderr
        assert not result.timed_out

    def test_timeout_kills_process_and_returns_partial_output(self, tmp_path: Path) -> None:
        script = "import time; print('booted', flush=True); time.sleep(60)"
        env = sanitize_environment(home=tmp_path)
        started = time.monotonic()

        result = launch_contained(
            [sys.executable, "-c", script], cwd=tmp_path, env=env, hard_timeout_seconds=2
        )

        elapsed = time.monotonic() - started
        assert result.timed_out
        assert elapsed < 30
        assert "booted" in result.stdout

    def test_envelope_with_utf8_bom_is_accepted(self, tmp_path: Path) -> None:
        envelope_path = tmp_path / "envelope.json"
        envelope_path.write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"prompt": "BOM review"}).encode("utf-8")
        )

        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "antigravity_bridge.py"),
                "--envelope",
                str(envelope_path),
                "--workspace",
                str(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=60,
        )

        assert proc.returncode == 0, proc.stderr
        assert "BOM review" in json.loads(proc.stdout)["payload"]

    def test_skill_yaml_with_utf8_bom_is_accepted(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "bom_skill.yaml").write_bytes(
            b"\xef\xbb\xbf" + make_skill_yaml("bom_skill", payload="BOM_INSTRUCTIONS").encode("utf-8")
        )

        skill = SkillLoader(skill_dir).load("bom_skill")

        assert skill.instructions_payload == "BOM_INSTRUCTIONS"

    def test_cli_status_emits_health_json(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "antigravity_bridge.py"),
                "--status",
                "--workspace",
                str(ROOT),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=60,
        )

        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data["executable"]
        assert data["fallback_model"] == DEFAULT_FALLBACK_MODEL
        assert data["skill_registry"]
        assert isinstance(data.get("skills"), list)

    def test_cli_dry_run_emits_full_command_without_execution(self, tmp_path: Path) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "antigravity_bridge.py"),
                "--prompt",
                "Review X",
                "--workspace",
                str(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=60,
        )

        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data["dry_run"] is True
        assert data["command"][1] == "-p"
        assert "--add-dir" in data["command"]
        assert "Review X" in data["payload"]


class TestSkillDirResolution:
    def test_workspace_registry_wins(self, registry: Path, tmp_path: Path) -> None:
        resolved, fell_back = resolve_skill_dir(tmp_path, registry)

        assert resolved == registry.resolve()
        assert fell_back is False

    def test_workspace_registry_detected(self, tmp_path: Path) -> None:
        local = tmp_path / "Skills"
        local.mkdir()
        (local / "a.yaml").write_text(make_skill_yaml(), encoding="utf-8")

        resolved, fell_back = resolve_skill_dir(tmp_path)

        assert resolved == local.resolve()
        assert fell_back is False

    def test_falls_back_to_shipped_registry(self, tmp_path: Path) -> None:
        resolved, fell_back = resolve_skill_dir(tmp_path)

        assert fell_back is True
        assert resolved == SHIPPED_SKILL_DIR

    def test_missing_override_is_strict(self, tmp_path: Path) -> None:
        with pytest.raises(SkillNotFoundError):
            resolve_skill_dir(tmp_path, tmp_path / "missing")


class TestSkillManifest:
    def test_manifest_is_metadata_only(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        manifest = loader.render_manifest()

        assert "## ADVERSARIAL SKILL REGISTRY MANIFEST" in manifest
        for skill in loader.skills:
            assert f"**{skill.name}**" in manifest
            assert str(skill.source_path) in manifest
            marker = skill.instructions_payload.strip()[:60]
            assert marker not in manifest

    def test_prompt_blocks_are_labeled(self, registry: Path) -> None:
        loader = SkillLoader(registry)
        skills = loader.select(["crash_ops"])

        active = loader.render_prompt(
            skills, heading="## ACTIVE ADVERSARIAL SKILLS (MANDATORY)"
        )
        recommended = loader.render_prompt(
            skills, heading="## RECOMMENDED ADVERSARIAL SKILLS (TASK-DEPENDENT)"
        )

        assert "## ACTIVE ADVERSARIAL SKILLS (MANDATORY)" in active
        assert "## RECOMMENDED ADVERSARIAL SKILLS (TASK-DEPENDENT)" in recommended


class TestRegistryMounting:
    def test_extra_add_dirs_are_mounted(self, tmp_path: Path) -> None:
        registry = tmp_path / "Registry"
        registry.mkdir()

        command = build_agy_command(
            "agy",
            "payload",
            tmp_path,
            "gemini-3.8-flash-high",
            900,
            extra_add_dirs=(registry,),
        )

        dirs = [
            command[index + 1]
            for index, part in enumerate(command)
            if part == "--add-dir"
        ]
        assert str(tmp_path) in dirs
        assert str(registry) in dirs

    def test_foreign_workspace_falls_back_and_mounts_registry(
        self, tmp_path: Path
    ) -> None:
        launcher = ScriptedLauncher([successful_attempt("CRITIQUE")])
        config = BridgeConfig(
            envelope=envelope(),
            workspace=tmp_path,
            skills=("adversarial-plan-hardening-engine",),
            recommended_skills=("zero-trust-ast-wiring-verifier",),
        )

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert any(
            "fell back to shipped registry" in warning for warning in result.warnings
        )
        command = launcher.calls[0]
        add_dirs = [
            command[index + 1]
            for index, part in enumerate(command)
            if part == "--add-dir"
        ]
        assert len(add_dirs) == 2
        assert str(SHIPPED_SKILL_DIR) in add_dirs
        payload = command[command.index("-p") + 1]
        assert "## ACTIVE ADVERSARIAL SKILLS (MANDATORY)" in payload
        assert "## RECOMMENDED ADVERSARIAL SKILLS (TASK-DEPENDENT)" in payload
        assert "## ADVERSARIAL SKILL REGISTRY MANIFEST" in payload

    def test_critique_renders_warnings(self, tmp_path: Path) -> None:
        config = BridgeConfig(envelope=envelope(), workspace=tmp_path)

        critique = render_critique(
            config,
            "agy",
            [successful_attempt()],
            False,
            True,
            None,
            ["Workspace has no Skills directory; fell back to shipped registry"],
        )

        assert "> **Warnings**:" in critique
        assert "fell back to shipped registry" in critique
