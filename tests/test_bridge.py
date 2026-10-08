"""Bridge engine: envelopes, env hygiene, commands, orchestration, reports, and the CLI."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import time
import typing
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import ROOT
from tests.helpers.bridge import (
    ScriptedLauncher,
    bridge_config,
    empty_attempt,
    envelope,
    make_skill_yaml,
    rate_limited_attempt,
    rate_limited_attempt_with_reset,
    successful_attempt,
    transient_attempt,
)
from tools import antigravity_bridge as bridge
from tools.antigravity_bridge import (
    BLOCKED_ENV_EXACT,
    BLOCKED_ENV_PREFIXES,
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_KEEP_REPORTS,
    DEFAULT_PRIMARY_MODEL,
    REPORT_SCHEMA_VERSION,
    AttemptResult,
    BridgeConfig,
    BridgeResult,
    DelegationEnvelope,
    _prune_reports,
    attempt_succeeded,
    build_agy_command,
    build_prompt_payload,
    collect_provenance,
    extract_reset_text,
    is_rate_limited,
    is_transient_failure,
    main,
    parse_reset_seconds,
    render_critique,
    resolve_agy_executable,
    run_bridge,
    sanitize_environment,
    write_report,
)
from tools.skill_loader import ALL_SELECTOR, SHIPPED_SKILL_DIR, SkillLoader, render_skill_block


def _fake_result() -> BridgeResult:
    """A successful single-attempt result carrying this platform's containment label."""
    return BridgeResult(
        success=True,
        model_used="m",
        failover_used=False,
        timed_out=False,
        rate_limited=False,
        exit_code=0,
        attempts=[
            AttemptResult(
                model="m",
                exit_code=0,
                stdout='{"event":"result","result":{"status":"SUCCESS","response":"ok"}}',
                containment="job-object" if os.name == "nt" else "process-group",
            )
        ],
        critique_markdown="ok",
    )


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

    def test_executable_available_follows_lookup(self) -> None:
        assert bridge.executable_available("agy", which=lambda name: "/opt/agy") is True
        assert bridge.executable_available("agy", which=lambda name: None) is False
        assert bridge.executable_available("", which=lambda name: "/opt/agy") is False


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


class TestEnvironmentBlocklist:
    @pytest.mark.parametrize(
        "variable",
        [
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_API_KEY",
            "HF_TOKEN",
            "HUGGINGFACEHUB_API_TOKEN",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
        ],
    )
    def test_credential_variables_are_stripped(self, variable: str) -> None:
        env = sanitize_environment(base_env={variable: "leak", "PATH": os.environ["PATH"]})
        assert variable not in env

    @pytest.mark.parametrize(
        "variable",
        ["NODE_OPTIONS", "NPM_CONFIG_USERCONFIG", "PYTHONPATH", "PYTHONHOME"],
    )
    def test_injection_variables_are_stripped(self, variable: str) -> None:
        env = sanitize_environment(base_env={variable: "evil.js", "PATH": os.environ["PATH"]})
        assert variable not in env

    def test_blocklist_constants_cover_credential_and_injection_vectors(self) -> None:
        joined_prefixes = "|".join(BLOCKED_ENV_PREFIXES)
        assert "GOOGLE_APPLICATION_CREDENTIALS" in joined_prefixes
        assert "GIT_ASKPASS" in joined_prefixes
        assert "HF_" in joined_prefixes
        for name in ("NODE_OPTIONS", "PYTHONPATH", "NPM_CONFIG_USERCONFIG"):
            assert name in BLOCKED_ENV_EXACT


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
        command = build_agy_command("agy", "PROMPT", Path("C:/ws"), "gemini-3.8-flash-high", 600)

        assert command[0] == "agy"
        assert command[command.index("-p") + 1] == "PROMPT"
        assert command[command.index("--add-dir") + 1] == str(Path("C:/ws"))
        assert command[command.index("--model") + 1] == "gemini-3.8-flash-high"
        assert command[command.index("--effort") + 1] == "high"
        assert command[command.index("--print-timeout") + 1] == "600s"
        assert "--dangerously-skip-permissions" in command
        assert command[command.index("--output-format") + 1] == "stream-json"

    def test_claude_command_omits_gemini_only_effort_flag(self) -> None:
        command = build_agy_command("agy", "PROMPT", Path("C:/ws"), "claude-sonnet-4-6", 600)

        assert "--effort" not in command


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


class TestRecommendedSkillsContract:
    def test_from_mapping_preserves_recommended_skills(self) -> None:
        parsed = DelegationEnvelope.from_mapping(
            {
                "prompt": "p",
                "recommended_skills": ["zero-trust-ast-wiring-verifier"],
            }
        )
        assert parsed.recommended_skills == ("zero-trust-ast-wiring-verifier",)

    def test_from_mapping_accepts_comma_string_form(self) -> None:
        parsed = DelegationEnvelope.from_mapping({"prompt": "p", "recommended_skills": "a, b"})
        assert parsed.recommended_skills == ("a", "b")

    def test_cli_envelope_reaches_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        envelope_path = tmp_path / "envelope.json"
        envelope_path.write_text(
            json.dumps(
                {
                    "prompt": "audit plan",
                    "recommended_skills": ["empirical-claim-falsification-engine"],
                }
            ),
            encoding="utf-8",
        )
        shipped = ROOT / "Skills"
        exit_code = main(
            [
                "--envelope",
                str(envelope_path),
                "--workspace",
                str(tmp_path),
                "--skill-dir",
                str(shipped),
                "--dry-run",
            ]
        )
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert "### empirical-claim-falsification-engine (v" in payload["payload"]
        assert "\u2014 recommended\nFull procedure: " in payload["payload"]

    def test_cli_flag_reaches_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        shipped = ROOT / "Skills"
        exit_code = main(
            [
                "--prompt",
                "audit plan",
                "--workspace",
                str(tmp_path),
                "--skill-dir",
                str(shipped),
                "--recommended-skills",
                "zero-trust-ast-wiring-verifier",
                "--dry-run",
            ]
        )
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert "### zero-trust-ast-wiring-verifier (v" in payload["payload"]


class TestWorkspaceTrustFraming:
    def test_payload_classifies_workspace_as_untrusted(self) -> None:
        config = BridgeConfig(envelope=DelegationEnvelope(prompt="review this"))
        payload = build_prompt_payload(config)
        assert "untrusted evidence" in payload
        assert "never instructions" in payload
        # A workspace charter is evidence under review, never the review protocol.
        assert "Read `AGENTS.md` in the mounted workspace" not in payload

    def test_payload_does_not_instruct_reading_workspace_agents_md(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("workspace charter", encoding="utf-8")
        config = BridgeConfig(envelope=DelegationEnvelope(prompt="p"), workspace=tmp_path)
        payload = build_prompt_payload(config)
        assert "AGENTS.md" not in payload or "untrusted" in payload.split("AGENTS.md")[0][-200:]


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
        assert "Delegation failed" in result.critique_markdown

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
        active = payload.split("## Active skills", 1)[1].split("## Skill registry", 1)[0]
        assert "### crash_ops (v1.2.3) \u2014 mandatory" in active
        assert "### ast_audit (v1.2.3) \u2014 mandatory" in active
        assert str(registry / "01_crash_ops.yaml") in active
        assert "template_custom_skill" not in active
        # Full bodies stay on disk; the reviewer is told to read them.
        assert "CRASH_OPS_INSTRUCTIONS" not in payload

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


class TestRetriesAndQuotaControls:
    def test_parse_reset_seconds_variants(self) -> None:
        assert parse_reset_seconds("Resets in 3h") == 10800
        assert parse_reset_seconds("quota reached. Resets in 1h 30m.") == 5400
        assert parse_reset_seconds("Resets in 45s") == 45
        assert parse_reset_seconds("no reset info here") is None
        assert extract_reset_text("Resets in 1h 30m.") == "1h 30m"
        two_notices = "Resets in 2h 5m. Later: resets in 1h 30m."
        assert extract_reset_text(two_notices) == "2h 5m"
        assert extract_reset_text(two_notices, last=True) == "1h 30m"
        assert extract_reset_text("no notice", last=True) is None

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


class TestQuotaHookScope:
    def test_hook_does_not_run_after_a_non_rate_limit_failure(self, tmp_path: Path) -> None:
        # The quota wait re-runs the primary model; that rerun fails for an
        # unrelated reason. Rotating credentials cannot help, so the operator
        # hook must not run.
        marker = tmp_path / "hook-ran"
        hook = f'"{sys.executable}" -c "open({str(marker)!r}, \'w\').close()"'
        rate_limited = bridge.AttemptResult(
            exit_code=1, stderr="RESOURCE_EXHAUSTED: quota reached. Resets in 0s."
        )
        fatal = bridge.AttemptResult(exit_code=1, stdout="boom", stderr="fatal: bad flag")
        launcher = ScriptedLauncher([rate_limited, fatal])

        result = bridge.run_bridge(
            bridge_config(quota_wait_seconds=10, quota_hook=hook), launcher=launcher
        )

        assert not result.success
        assert not result.quota_hook_used
        assert result.quota_hook_output is None
        assert not marker.exists()
        assert len(launcher.calls) == 2
        assert not result.failover_used

    def test_hook_runs_when_still_rate_limited_after_the_wait(self, tmp_path: Path) -> None:
        hook = f'"{sys.executable}" -c "print(1)"'
        rate_limited = bridge.AttemptResult(
            exit_code=1, stderr="RESOURCE_EXHAUSTED: quota reached. Resets in 0s."
        )
        success = bridge.AttemptResult(exit_code=0, stdout="CRITIQUE")
        launcher = ScriptedLauncher([rate_limited, rate_limited, success])

        result = bridge.run_bridge(
            bridge_config(quota_wait_seconds=10, quota_hook=hook), launcher=launcher
        )

        assert result.success
        assert result.quota_hook_used
        assert len(launcher.calls) == 3


class TestSuccessSemantics:
    @staticmethod
    def _attempt(stdout: str = "", stderr: str = "", exit_code: int = 0) -> AttemptResult:
        return AttemptResult(stdout=stdout, stderr=stderr, exit_code=exit_code)

    def test_stderr_only_is_not_success(self) -> None:
        assert attempt_succeeded(self._attempt(stderr="warning: something")) is False

    def test_stdout_content_is_success(self) -> None:
        assert attempt_succeeded(self._attempt(stdout='{"event": "result"}')) is True

    def test_stderr_only_is_transient_so_it_retries(self) -> None:
        attempt = self._attempt(stderr="warning: something")
        assert is_transient_failure(attempt) is True

    def test_nonzero_exit_is_never_success(self) -> None:
        assert attempt_succeeded(self._attempt(stdout="out", exit_code=3)) is False


class TestSuccessIsNotRateLimited:
    def test_quota_text_inside_tool_result_does_not_trigger_failover(self) -> None:
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "step_update": {
                            "step_index": 1,
                            "tool_result": {
                                "path": "docs/quota.md",
                                "content": "On RESOURCE_EXHAUSTED (code 429) we fail over.",
                            },
                        }
                    }
                ),
                json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "Findings: none."},
                    }
                ),
            ]
        )
        attempt = bridge.AttemptResult(exit_code=0, stdout=stdout, duration_seconds=0.01)
        launcher = ScriptedLauncher([attempt])

        result = bridge.run_bridge(bridge_config(), launcher=launcher)

        assert result.success
        assert not result.failover_used
        assert not result.rate_limited
        assert len(launcher.calls) == 1
        assert result.to_dict()["attempts"][0]["rate_limited"] is False

    def test_clean_exit_with_only_a_quota_message_fails_over(self) -> None:
        quota = bridge.AttemptResult(
            exit_code=0,
            stdout="Error: RESOURCE_EXHAUSTED (code 429): Individual quota reached",
            duration_seconds=0.01,
        )
        recovered = bridge.AttemptResult(
            exit_code=0,
            stdout=json.dumps({"event": "result", "result": "Findings: none."}),
            duration_seconds=0.01,
        )
        launcher = ScriptedLauncher([quota, recovered])

        result = bridge.run_bridge(bridge_config(), launcher=launcher)

        assert quota.rate_limited
        assert result.success
        assert result.failover_used
        assert len(launcher.calls) == 2

    def test_failed_attempt_with_quota_text_is_still_rate_limited(self) -> None:
        attempt = bridge.AttemptResult(exit_code=1, stderr="RESOURCE_EXHAUSTED")

        assert attempt.rate_limited


class TestCommandLineLimit:
    @pytest.mark.skipif(os.name != "nt", reason="Windows CreateProcess argv limit")
    def test_oversized_payload_fails_fast_with_actionable_error(self) -> None:
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


class TestLaunchFnContract:
    def test_launch_fn_declares_the_raw_line_sink_parameter(self) -> None:
        # run_bridge calls the launcher with five positional arguments; the
        # type must describe the same contract launch_contained implements.
        parameters, returns = typing.get_args(bridge.LaunchFn)
        assert len(parameters) == 5
        assert parameters[4] == (bridge.RawLineSink | None)
        assert returns is bridge.AttemptResult
        signature = inspect.signature(bridge.launch_contained)
        assert len(signature.parameters) == len(parameters)


class TestReportPersistence:
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

    def test_report_is_atomic_and_schema_versioned(self, tmp_path: Path) -> None:
        result = _fake_result()
        path = write_report(tmp_path, result, keep_reports=DEFAULT_KEEP_REPORTS)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["schema_version"] == REPORT_SCHEMA_VERSION
        assert data["attempts"][0]["containment"] == result.attempts[0].containment
        leftovers = list(tmp_path.glob(".antigravity-reports/*.tmp.*"))
        assert leftovers == []

    def test_report_retention_prunes_oldest(self, tmp_path: Path) -> None:
        for _ in range(4):
            write_report(tmp_path, _fake_result(), keep_reports=2)
        reports = sorted((tmp_path / ".antigravity-reports").glob("antigravity-report-*.json"))
        assert len(reports) == 2

    def test_zero_keep_disables_pruning(self, tmp_path: Path) -> None:
        for _ in range(3):
            write_report(tmp_path, _fake_result(), keep_reports=0)
        reports = list((tmp_path / ".antigravity-reports").glob("antigravity-report-*.json"))
        assert len(reports) == 3

    def test_default_keep_matches_constant(self) -> None:
        assert DEFAULT_KEEP_REPORTS >= 10

    def test_cli_warns_when_the_report_cannot_be_written(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def unwritable(*args: Any, **kwargs: Any) -> Path:
            raise PermissionError("read-only workspace")

        monkeypatch.setattr(bridge, "run_bridge", lambda config: _fake_result())
        monkeypatch.setattr(bridge, "write_report", unwritable)

        exit_code = main(["--prompt", "p", "--workspace", str(tmp_path), "--no-live"])

        assert exit_code == 0
        stderr = capsys.readouterr().err
        assert "could not write the JSON report" in stderr
        assert "read-only workspace" in stderr

    def test_attempt_dict_exposes_containment(self) -> None:
        attempt = AttemptResult(containment="job-object", exit_code=0)
        config = BridgeConfig(envelope=DelegationEnvelope(prompt="p"))
        bridge_result = BridgeResult(
            success=True,
            model_used="m",
            failover_used=False,
            timed_out=False,
            rate_limited=False,
            exit_code=0,
            attempts=[attempt],
            critique_markdown="x",
            containment="job-object",
        )
        data = bridge_result.to_dict()
        assert data["attempts"][0]["containment"] == "job-object"
        assert data["containment"] == "job-object"
        assert "Containment" in render_critique(config, "agy", [attempt], False, True, None)


class TestReportPruningKeepsNewest:
    def test_new_report_survives_its_own_pruning_pass(self, tmp_path: Path) -> None:
        # Four older reports with FUTURE mtimes sort ahead of the fresh one;
        # with keep=2 the fresh report lands past the keep line and only the
        # explicit exclusion saves it.
        future = time.time() + 3600
        for index in range(4):
            candidate = tmp_path / f"antigravity-report-20260101-00000{index}-aaaaaaaa.json"
            candidate.write_text("{}", encoding="utf-8")
            os.utime(candidate, (future, future))
        fresh = tmp_path / "antigravity-report-20260101-000009-00000000.json"
        fresh.write_text("{}", encoding="utf-8")

        _prune_reports(tmp_path, keep=2, exclude=fresh)

        assert fresh.is_file(), "pruning deleted the report it just wrote"
        survivors = sorted(path.name for path in tmp_path.glob("antigravity-report-*.json"))
        assert len(survivors) == 3  # fresh + the two newest older reports

    def test_write_report_integration_keeps_newest(self, tmp_path: Path) -> None:
        result = BridgeResult(
            success=True,
            model_used="m",
            failover_used=False,
            timed_out=False,
            rate_limited=False,
            exit_code=0,
            attempts=[AttemptResult(exit_code=0, stdout="x")],
            critique_markdown="x",
        )
        write_report(tmp_path, result, keep_reports=1)
        second = write_report(tmp_path, result, keep_reports=1)
        assert second.is_file()
        reports = list((tmp_path / ".antigravity-reports").glob("antigravity-report-*.json"))
        assert len(reports) == 1


class TestProvenance:
    def test_provenance_block_shape(self) -> None:
        block = collect_provenance("agy-executable-does-not-exist")
        assert block["wisp_version"]
        assert block["report_schema_version"] == REPORT_SCHEMA_VERSION
        assert block["platform"]
        assert block["python_version"]
        assert block["generated_at"].endswith("Z")
        assert block["agy_version"] is None  # unknown binary resolves to nothing
        assert block["registry_hash"] is None

    def test_registry_hash_covers_skill_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text("name: a", encoding="utf-8")
        (tmp_path / "b.md").write_text("b", encoding="utf-8")
        block = collect_provenance("agy", registry_path=tmp_path)
        assert block["registry_hash"]
        assert len(block["registry_hash"]) == 64

    def test_result_dict_carries_provenance_and_schema(self) -> None:
        result = _fake_result()
        result.provenance = collect_provenance("agy")
        data = result.to_dict()
        assert data["schema_version"] == REPORT_SCHEMA_VERSION
        assert data["provenance"]["wisp_version"]


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

        dirs = [command[index + 1] for index, part in enumerate(command) if part == "--add-dir"]
        assert str(tmp_path) in dirs
        assert str(registry) in dirs

    def test_foreign_workspace_falls_back_and_mounts_registry(self, tmp_path: Path) -> None:
        launcher = ScriptedLauncher([successful_attempt("CRITIQUE")])
        config = BridgeConfig(
            envelope=envelope(),
            workspace=tmp_path,
            skills=("adversarial-plan-hardening-engine",),
            recommended_skills=("zero-trust-ast-wiring-verifier",),
        )

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert any("fell back to shipped registry" in warning for warning in result.warnings)
        command = launcher.calls[0]
        add_dirs = [command[index + 1] for index, part in enumerate(command) if part == "--add-dir"]
        assert len(add_dirs) == 2
        assert str(SHIPPED_SKILL_DIR) in add_dirs
        payload = command[command.index("-p") + 1]
        assert "### adversarial-plan-hardening-engine (v" in payload
        assert "\u2014 mandatory\nFull procedure: " in payload
        assert "### zero-trust-ast-wiring-verifier (v" in payload
        assert "\u2014 recommended\nFull procedure: " in payload
        assert f"## Skill registry\nEvery registered skill under `{SHIPPED_SKILL_DIR}`" in payload

    def test_payload_points_antigravity_at_registry_on_disk(self) -> None:
        registry = Path("C:/ws/Skills")
        config = BridgeConfig(envelope=envelope())

        payload = build_prompt_payload(
            config,
            bridge.SkillSections(
                active="SKILL BLOCK", index="- INDEX LINE", registry_path=registry
            ),
        )

        assert "## Skill registry" in payload
        assert f"under `{registry}`" in payload
        assert "SKILL BLOCK" in payload
        assert "- INDEX LINE" in payload

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
        assert f"Full procedure: {skill_dir.resolve() / 'plan.yaml'}" in payload
        assert f"under `{skill_dir.resolve()}`" in payload

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


class TestCli:
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


class TestRegistryResolutionParity:
    def test_status_falls_back_like_the_runtime(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(["--status", "--workspace", str(tmp_path)])
        assert exit_code == 0
        status = json.loads(capsys.readouterr().out)
        assert status["registry_fell_back"] is True
        assert len(status["skills"]) == len(SkillLoader(SHIPPED_SKILL_DIR).skills)

    def test_status_reports_missing_executable_without_failing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(bridge, "resolve_agy_executable", lambda: "wisp-test-missing-agy")
        exit_code = main(["--status", "--workspace", str(tmp_path)])
        status = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert status["executable"] == "wisp-test-missing-agy"
        assert status["executable_found"] is False
        assert any(
            "wisp-test-missing-agy" in warning and "antigravity.google" in warning
            for warning in status["warnings"]
        )

    def test_status_honours_explicit_executable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(
            ["--status", "--workspace", str(tmp_path), "--executable", sys.executable]
        )
        status = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert status["executable"] == sys.executable
        assert status["executable_found"] is True
        assert not any("was not found" in warning for warning in status["warnings"])

    def test_status_missing_executable_keeps_registry_failure_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(bridge, "resolve_agy_executable", lambda: "wisp-test-missing-agy")
        broken = tmp_path / "broken-registry"
        broken.mkdir()
        (broken / "bad.md").write_text("no frontmatter here", encoding="utf-8")
        exit_code = main(["--status", "--workspace", str(tmp_path), "--skill-dir", str(broken)])
        status = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert status["executable_found"] is False

    def test_status_nonzero_when_registry_broken(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        broken = tmp_path / "broken-registry"
        broken.mkdir()
        (broken / "bad.md").write_text("no frontmatter here", encoding="utf-8")
        exit_code = main(["--status", "--workspace", str(tmp_path), "--skill-dir", str(broken)])
        status = json.loads(capsys.readouterr().out)
        assert "skill_error" in status
        assert exit_code != 0

    def test_list_skills_falls_back_like_the_runtime(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(["--list-skills", "--workspace", str(tmp_path)])
        assert exit_code == 0
        assert "adversarial-plan-hardening-engine" in capsys.readouterr().out

    def test_dry_run_falls_back_like_the_runtime(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(
            [
                "--prompt",
                "p",
                "--workspace",
                str(tmp_path),
                "--skills",
                "adversarial-plan-hardening-engine",
                "--dry-run",
            ]
        )
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert f"Full procedure: {SHIPPED_SKILL_DIR}" in payload["payload"]
        assert "### adversarial-plan-hardening-engine (v" in payload["payload"]


class TestDryRunMatchesDispatch:
    SKILLS = ("adversarial-plan-hardening-engine",)
    RECOMMENDED = ("zero-trust-ast-wiring-verifier",)

    def test_dry_run_prints_the_command_run_bridge_executes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = bridge.main(
            [
                "--prompt",
                "Review the plan",
                "--workspace",
                str(tmp_path),
                "--skills",
                ",".join(self.SKILLS),
                "--recommended-skills",
                ",".join(self.RECOMMENDED),
                "--executable",
                "agy-test",
                "--dry-run",
            ]
        )
        assert exit_code == 0
        dry_run = json.loads(capsys.readouterr().out)

        success = bridge.AttemptResult(exit_code=0, stdout="CRITIQUE")
        launcher = ScriptedLauncher([success])
        config = bridge_config(
            envelope=bridge.DelegationEnvelope(prompt="Review the plan"),
            workspace=tmp_path,
            skills=self.SKILLS,
            recommended_skills=self.RECOMMENDED,
        )
        assert bridge.run_bridge(config, launcher=launcher).success

        dispatched = launcher.calls[0]
        assert dry_run["command"] == dispatched
        assert dry_run["payload"] == dispatched[dispatched.index("-p") + 1]
        assert "## Skill registry" in dry_run["payload"]
        # The workspace has no Skills/, so the shipped registry must be mounted.
        assert dry_run["command"].count("--add-dir") == 2

    @pytest.mark.skipif(os.name != "nt", reason="Windows CreateProcess argv limit")
    def test_dry_run_applies_the_windows_argv_preflight(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(bridge, "_WINDOWS_COMMAND_LINE_LIMIT", 100)
        exit_code = bridge.main(["--prompt", "x" * 200, "--workspace", str(tmp_path), "--dry-run"])
        assert exit_code == 2
        assert "command line" in capsys.readouterr().err


# A realistic delegation prompt: a plan plus a diff summary of about 4,000 chars.
REALISTIC_PROMPT = ("Review this plan and diff summary for wiring and failure modes. " * 63)[:4000]


def _windows_command_chars(config: BridgeConfig, workspace: Path) -> tuple[int, Any]:
    """Size of the dispatched command as CreateProcess would see it, on any OS."""
    plan = bridge._plan_dispatch(config, workspace)
    command = build_agy_command(
        "agy",
        plan.payload,
        workspace,
        config.model,
        config.print_timeout_seconds,
        extra_add_dirs=plan.extra_add_dirs,
    )
    return len(subprocess.list2cmdline(command)), plan


def _briefed_registry(root: Path, count: int, brief_chars: int) -> Path:
    """A registry of ``count`` skills whose briefs are ``brief_chars`` long."""
    skill_dir = root / "Skills"
    skill_dir.mkdir(parents=True)
    for index in range(count):
        name = f"skill_{index:02d}"
        (skill_dir / f"{index:02d}_{name}.yaml").write_text(
            make_skill_yaml(
                name,
                description=f"Use when checking area {index}. Not for other areas.",
                brief=f"BRIEF_{index:02d} " + "x" * brief_chars,
                payload=f"BODY_{index:02d}",
            ),
            encoding="utf-8",
        )
    return skill_dir


class TestPayloadBudget:
    def test_three_largest_shipped_skills_with_a_realistic_prompt_fit_windows(
        self, tmp_path: Path
    ) -> None:
        loader = SkillLoader(SHIPPED_SKILL_DIR)
        largest = sorted(
            loader.skills,
            key=lambda skill: len(render_skill_block(skill, "mandatory")),
            reverse=True,
        )[:3]
        config = bridge_config(
            envelope=DelegationEnvelope(prompt=REALISTIC_PROMPT, context="SQLite WAL, one writer"),
            workspace=tmp_path,
            skills=tuple(skill.name for skill in largest),
            skill_dir=SHIPPED_SKILL_DIR,
        )

        chars, plan = _windows_command_chars(config, tmp_path)

        assert chars < bridge._WINDOWS_COMMAND_LINE_LIMIT
        for skill in largest:
            assert f"### {skill.name} (v{skill.version}) — mandatory" in plan.payload

    def test_all_shipped_skills_fit_windows(self, tmp_path: Path) -> None:
        config = bridge_config(
            envelope=DelegationEnvelope(prompt=REALISTIC_PROMPT),
            workspace=tmp_path,
            skills=(ALL_SELECTOR,),
            skill_dir=SHIPPED_SKILL_DIR,
        )

        chars, _plan = _windows_command_chars(config, tmp_path)

        assert chars < bridge._WINDOWS_COMMAND_LINE_LIMIT

    def test_all_with_overflowing_briefs_uses_pointers_and_warns(self, tmp_path: Path) -> None:
        # 17 skills with ~1,800-char briefs (the authoring maximum) far exceed
        # the inline budget; the remainder must become path-only pointers.
        skill_dir = _briefed_registry(tmp_path, count=17, brief_chars=1_800)
        config = bridge_config(
            envelope=DelegationEnvelope(prompt=REALISTIC_PROMPT),
            workspace=tmp_path,
            skills=(ALL_SELECTOR,),
        )

        chars, plan = _windows_command_chars(config, tmp_path)

        assert chars < bridge._WINDOWS_COMMAND_LINE_LIMIT
        assert any("Inline skill budget" in warning for warning in plan.warnings)
        active = plan.payload.split(bridge.ACTIVE_SKILLS_HEADING, 1)[1].split(
            bridge.SKILL_REGISTRY_HEADING, 1
        )[0]
        assert "BRIEF_00" in active
        assert "BRIEF_16" not in active
        assert "- **skill_16** (v1.2.3) — mandatory" in active
        assert str(skill_dir / "16_skill_16.yaml") in active
        assert "BODY_" not in plan.payload

    def test_budget_overflow_warning_reaches_the_result(self, tmp_path: Path) -> None:
        _briefed_registry(tmp_path, count=17, brief_chars=1_800)
        launcher = ScriptedLauncher([successful_attempt()])
        config = bridge_config(
            envelope=DelegationEnvelope(prompt="p"), workspace=tmp_path, skills=(ALL_SELECTOR,)
        )

        result = run_bridge(config, launcher=launcher)

        assert any("Inline skill budget" in warning for warning in result.warnings)

    def test_a_lower_priority_skill_never_displaces_a_higher_one(self, tmp_path: Path) -> None:
        skill_dir = _briefed_registry(tmp_path, count=3, brief_chars=10)
        loader = SkillLoader(skill_dir)
        first, second, third = loader.skills
        budget = len(render_skill_block(first, "mandatory")) + 5

        text, overflow = bridge._render_active_skills(
            [(first, "mandatory"), (second, "mandatory"), (third, "recommended")], budget=budget
        )

        assert overflow == [second, third]
        assert "BRIEF_00" in text
        assert "BRIEF_02" not in text
        assert text.index("- **skill_01**") < text.index("- **skill_02**")


class TestPayloadStructure:
    @staticmethod
    def _payload(workspace: Path, **overrides: Any) -> str:
        config = bridge_config(
            envelope=DelegationEnvelope(
                prompt="Review the plan", context="ctx", claims_to_falsify=("claim",)
            ),
            workspace=workspace,
            **overrides,
        )
        return bridge._plan_dispatch(config, workspace).payload

    def test_sections_appear_once_and_in_order(self, registry: Path) -> None:
        payload = self._payload(
            registry.parent,
            skills=("crash_ops",),
            recommended_skills=("ast_audit",),
            skill_dir=registry,
        )

        headings = [
            "## Request / plan under review",
            "## Context and prior art",
            "## Claims to falsify",
            bridge.REVIEW_PROTOCOL_HEADING,
            bridge.ACTIVE_SKILLS_HEADING,
            bridge.SKILL_REGISTRY_HEADING,
            bridge.OUTPUT_REMINDER_HEADING,
        ]
        for heading in headings:
            assert payload.count(f"\n{heading}\n") == 1, heading
        positions = [payload.index(f"\n{heading}\n") for heading in headings]
        assert positions == sorted(positions)
        assert payload.count("<<<WISP_VERDICT") == 1
        assert payload.count("Finding format:") == 1
        assert payload.index("crash_ops (v1.2.3) — mandatory") < payload.index(
            "ast_audit (v1.2.3) — recommended"
        )

    def test_registry_index_lists_every_shipped_skill_with_its_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(
            [
                "--prompt",
                "p",
                "--workspace",
                str(tmp_path),
                "--skills",
                "adversarial-plan-hardening-engine",
                "--dry-run",
            ]
        )
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)["payload"]
        index = payload.split(bridge.SKILL_REGISTRY_HEADING, 1)[1]
        for skill in SkillLoader(SHIPPED_SKILL_DIR).skills:
            assert f"- **{skill.name}** — " in index
            assert f" · {skill.absolute_path}" in index
            # Bodies stay on disk.
            assert skill.instructions_payload[200:400] not in payload

    def test_no_skills_means_no_skill_sections(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)

        assert bridge.REVIEW_PROTOCOL_HEADING in payload
        assert bridge.ACTIVE_SKILLS_HEADING not in payload
        assert bridge.SKILL_REGISTRY_HEADING not in payload
        assert "Skills: before starting" not in payload
        assert payload.rstrip().endswith("nothing may follow it.")


class TestDelegationMode:
    REVIEW_LINE = "Workspace access: review mode, read-only. Do not create, modify or delete files"
    IMPLEMENT_LINE = "Workspace access: implement mode. You may modify files inside the workspace"

    def test_envelope_defaults_to_review(self) -> None:
        assert DelegationEnvelope(prompt="p").mode == "review"
        assert DelegationEnvelope.from_mapping({"prompt": "p"}).mode == "review"
        assert DelegationEnvelope.from_mapping({"prompt": "p", "mode": None}).mode == "review"

    def test_envelope_normalizes_and_validates_mode(self) -> None:
        assert DelegationEnvelope.from_mapping({"prompt": "p", "mode": " Implement "}).mode == (
            "implement"
        )
        with pytest.raises(ValueError, match="mode"):
            DelegationEnvelope.from_mapping({"prompt": "p", "mode": "write"})
        with pytest.raises(ValueError, match="mode"):
            DelegationEnvelope(prompt="p", mode="yolo")

    def test_review_and_implement_render_different_access_rules(self) -> None:
        review = build_prompt_payload(BridgeConfig(envelope=DelegationEnvelope(prompt="p")))
        implement = build_prompt_payload(
            BridgeConfig(envelope=DelegationEnvelope(prompt="p", mode="implement"))
        )

        assert self.REVIEW_LINE in review
        assert "propose every change as a unified diff" in review
        assert self.IMPLEMENT_LINE not in review
        assert "**Mode**: review (read-only)" in review
        assert self.IMPLEMENT_LINE in implement
        assert "never create, modify or delete anything outside it" in implement
        assert "list every file you changed" in implement
        assert self.REVIEW_LINE not in implement

    def test_cli_flag_selects_implement(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(
            ["--prompt", "p", "--workspace", str(tmp_path), "--mode", "implement", "--dry-run"]
        )
        assert exit_code == 0
        assert self.IMPLEMENT_LINE in json.loads(capsys.readouterr().out)["payload"]

    def test_cli_rejects_unknown_mode(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--prompt", "p", "--workspace", str(tmp_path), "--mode", "write", "--dry-run"])
        assert excinfo.value.code == 2

    def test_envelope_mode_applies_and_cli_flag_overrides(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        envelope_path = tmp_path / "envelope.json"
        envelope_path.write_text(json.dumps({"prompt": "p", "mode": "implement"}), encoding="utf-8")
        base = ["--envelope", str(envelope_path), "--workspace", str(tmp_path), "--dry-run"]

        assert main(base) == 0
        assert self.IMPLEMENT_LINE in json.loads(capsys.readouterr().out)["payload"]
        assert main([*base, "--mode", "review"]) == 0
        assert self.REVIEW_LINE in json.loads(capsys.readouterr().out)["payload"]

    def test_invalid_envelope_mode_is_a_usage_error(self, tmp_path: Path) -> None:
        envelope_path = tmp_path / "envelope.json"
        envelope_path.write_text(json.dumps({"prompt": "p", "mode": "write"}), encoding="utf-8")
        with pytest.raises(SystemExit) as excinfo:
            main(["--envelope", str(envelope_path), "--workspace", str(tmp_path), "--dry-run"])
        assert excinfo.value.code == 2

    def test_write_access_skill_keeps_review_mode_with_a_warning(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "01_fixer.yaml").write_text(
            make_skill_yaml("fixer", input_contract={"write_access": "required"}),
            encoding="utf-8",
        )

        def plan_for(mode: str) -> Any:
            config = bridge_config(
                envelope=DelegationEnvelope(prompt="p", mode=mode),
                workspace=tmp_path,
                skills=("fixer",),
            )
            return bridge._plan_dispatch(config, tmp_path)

        review = plan_for("review")
        implement = plan_for("implement")

        assert any("'fixer' requires write access" in warning for warning in review.warnings)
        assert self.REVIEW_LINE in review.payload
        assert not any("requires write access" in warning for warning in implement.warnings)
        assert self.IMPLEMENT_LINE in implement.payload


VERDICT_RESPONSE = (
    "### F-001 · P1 · high · concurrency\n...\n\n"
    "<<<WISP_VERDICT\nverdict: PASS_WITH_FIXES\nconfidence: high\n"
    "summary: One race to fix.\ncounts: P0=0 P1=1 P2=0 P3=0\nmust_fix: F-001\n"
    "WISP_VERDICT>>>"
)


def _result_stdout(response: str) -> str:
    return json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": response}})


class TestReviewVerdict:
    def test_report_schema_version_covers_the_verdict(self) -> None:
        assert REPORT_SCHEMA_VERSION == 2

    def test_verdict_is_parsed_into_result_report_and_critique(self, tmp_path: Path) -> None:
        stdout = _result_stdout(VERDICT_RESPONSE)
        launcher = ScriptedLauncher([successful_attempt(stdout)])
        config = bridge_config(workspace=tmp_path)

        result = run_bridge(config, launcher=launcher)

        expected = {
            "verdict": "PASS_WITH_FIXES",
            "confidence": "high",
            "summary": "One race to fix.",
            "counts": {"P0": 0, "P1": 1, "P2": 0, "P3": 0},
            "must_fix": ["F-001"],
        }
        assert result.success
        assert result.review_verdict == expected
        assert result.to_dict()["review_verdict"] == expected
        assert result.to_mcp_dict()["review_verdict"] == expected
        saved = json.loads(write_report(tmp_path, result).read_text(encoding="utf-8"))
        assert saved["review_verdict"] == expected
        critique = result.critique_markdown
        assert critique.index("## Review verdict") < critique.index("## Antigravity Critique")
        assert "PASS_WITH_FIXES (confidence: high)" in critique
        assert not any("verdict" in warning.lower() for warning in result.warnings)

    def test_missing_block_is_none_with_a_warning(self, tmp_path: Path) -> None:
        launcher = ScriptedLauncher([successful_attempt(_result_stdout("No verdict here."))])

        result = run_bridge(bridge_config(workspace=tmp_path), launcher=launcher)

        assert result.success
        assert result.review_verdict is None
        assert result.to_dict()["review_verdict"] is None
        assert any("the reviewer did not emit a verdict block" in w for w in result.warnings)
        assert "## Review verdict" not in result.critique_markdown

    def test_failed_run_has_no_verdict_and_no_verdict_warning(self, tmp_path: Path) -> None:
        launcher = ScriptedLauncher([AttemptResult(exit_code=3, stdout="", stderr="boom")])

        result = run_bridge(
            bridge_config(workspace=tmp_path, retries=0, fallback_model=""), launcher=launcher
        )

        assert not result.success
        assert result.review_verdict is None
        assert not any("verdict" in warning.lower() for warning in result.warnings)

    def test_invalid_verdict_value_is_flagged(self, tmp_path: Path) -> None:
        block = "<<<WISP_VERDICT\nverdict: MAYBE\nWISP_VERDICT>>>"
        launcher = ScriptedLauncher([successful_attempt(_result_stdout(block))])

        result = run_bridge(bridge_config(workspace=tmp_path), launcher=launcher)

        assert result.review_verdict is not None
        assert result.review_verdict["verdict"] is None
        assert any("Review verdict invalid" in warning for warning in result.warnings)


class TestConsultation:
    """Widget chat and hotkey asks get an answer, not a formal review."""

    @staticmethod
    def _envelope(**overrides: Any) -> DelegationEnvelope:
        fields: dict[str, Any] = {
            "prompt": "Why does this fail on CI?",
            "harness": bridge.CONSULTATION_HARNESS,
            "artifacts": (".antigravity-reports/captures/snip.png",),
        }
        fields.update(overrides)
        return DelegationEnvelope(**fields)

    def test_chat_ask_without_skills_gets_consultation_rules(self, tmp_path: Path) -> None:
        config = bridge_config(envelope=self._envelope(), workspace=tmp_path)

        assert bridge.is_consultation(config)
        payload = bridge._plan_dispatch(config, tmp_path).payload
        assert payload.startswith("# Wisp consultation\n")
        assert "Why does this fail on CI?" in payload
        assert "- `.antigravity-reports/captures/snip.png`" in payload
        assert bridge.CONSULTATION_RULES_HEADING in payload
        assert TestDelegationMode.REVIEW_LINE in payload
        assert "No findings format and no verdict block." in payload
        for review_only in (
            bridge.REVIEW_PROTOCOL_HEADING,
            bridge.SKILL_REGISTRY_HEADING,
            bridge.OUTPUT_REMINDER_HEADING,
            "<<<WISP_VERDICT",
            "Finding format:",
        ):
            assert review_only not in payload

    def test_chat_ask_with_skills_runs_the_full_review(self, registry: Path) -> None:
        config = bridge_config(
            envelope=self._envelope(),
            workspace=registry.parent,
            skills=("crash_ops",),
            skill_dir=registry,
        )

        assert not bridge.is_consultation(config)
        payload = bridge._plan_dispatch(config, registry.parent).payload
        assert bridge.REVIEW_PROTOCOL_HEADING in payload
        assert payload.count("<<<WISP_VERDICT") == 1

    def test_other_harnesses_always_get_the_review_protocol(self, tmp_path: Path) -> None:
        config = bridge_config(envelope=self._envelope(harness="opencode"), workspace=tmp_path)

        assert not bridge.is_consultation(config)
        assert bridge.REVIEW_PROTOCOL_HEADING in bridge._plan_dispatch(config, tmp_path).payload

    def test_consultation_answer_needs_no_verdict(self, tmp_path: Path) -> None:
        launcher = ScriptedLauncher([successful_attempt(_result_stdout("It is the cache key."))])
        config = bridge_config(envelope=self._envelope(), workspace=tmp_path)

        result = run_bridge(config, launcher=launcher)

        assert result.success
        assert result.review_verdict is None
        assert not any("verdict" in warning.lower() for warning in result.warnings)
