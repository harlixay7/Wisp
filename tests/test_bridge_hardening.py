"""Hardening regression tests from the external audit (2026-10).

Each test pins one audited defect class so it cannot silently regress:

* containment is recorded and degrades loudly (audit #1),
* environment blocklist covers interpreter/credential injection vectors (#6),
* workspace instructions are framed as untrusted evidence in the payload (#7),
* stderr-only output no longer counts as success (#8),
* ``recommended_skills`` survives the CLI envelope path (#9),
* diagnostic commands use the same registry resolution as the runtime (#10/#11),
* reports are written atomically and retained (#12),
* tool results, inferred finals, and duplicate fragments surface in the
  organized critique (#20/#21/#23),
* reports carry a schema version and provenance (#63/#64).
No test invokes the real ``agy`` binary or consumes quota.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.antigravity_bridge import (
    BLOCKED_ENV_EXACT,
    BLOCKED_ENV_PREFIXES,
    DEFAULT_KEEP_REPORTS,
    REPORT_SCHEMA_VERSION,
    AttemptResult,
    BridgeConfig,
    DelegationEnvelope,
    aggregate_stream_json,
    attempt_succeeded,
    build_prompt_payload,
    collect_provenance,
    launch_contained,
    main,
    sanitize_environment,
    write_report,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- containment


class TestContainmentRecording:
    def test_real_child_process_records_containment_mode(self, tmp_path: Path) -> None:
        result = launch_contained(
            [sys.executable, "-c", "print('contained-ok')"],
            cwd=tmp_path,
            env=sanitize_environment(),
            hard_timeout_seconds=30,
        )
        assert result.exit_code == 0
        assert "contained-ok" in result.stdout
        assert result.containment in {"job-object", "taskkill-fallback", "process-group"}
        if os.name == "nt":
            assert result.containment in {"job-object", "taskkill-fallback"}
        else:
            assert result.containment == "process-group"

    def test_timeout_kill_still_captures_partial_output(self, tmp_path: Path) -> None:
        script = "import time; print('partial-line', flush=True); time.sleep(60)"
        result = launch_contained(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env=sanitize_environment(),
            hard_timeout_seconds=2,
        )
        assert result.timed_out is True
        assert "partial-line" in result.stdout
        assert result.exit_code not in (0,)

    def test_attempt_dict_exposes_containment(self) -> None:
        attempt = AttemptResult(containment="job-object", exit_code=0)
        config = BridgeConfig(envelope=DelegationEnvelope(prompt="p"))
        from tools.antigravity_bridge import BridgeResult, render_critique

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
        assert "Containment" in render_critique(
            config, "agy", [attempt], False, True, None
        )


# ------------------------------------------------------------ env sanitization


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

    def test_blocklist_constants_cover_audit_findings(self) -> None:
        joined_prefixes = "|".join(BLOCKED_ENV_PREFIXES)
        assert "GOOGLE_APPLICATION_CREDENTIALS" in joined_prefixes
        assert "GIT_ASKPASS" in joined_prefixes
        assert "HF_" in joined_prefixes
        for name in ("NODE_OPTIONS", "PYTHONPATH", "NPM_CONFIG_USERCONFIG"):
            assert name in BLOCKED_ENV_EXACT


# -------------------------------------------------------------- payload trust


class TestWorkspaceTrustFraming:
    def test_payload_classifies_workspace_as_untrusted(self) -> None:
        config = BridgeConfig(envelope=DelegationEnvelope(prompt="review this"))
        payload = build_prompt_payload(config)
        assert "untrusted evidence" in payload
        assert "never instructions" in payload
        # The old instruction to adopt workspace AGENTS.md as protocol is gone.
        assert "Read `AGENTS.md` in the mounted workspace" not in payload

    def test_payload_does_not_instruct_reading_workspace_agents_md(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("workspace charter", encoding="utf-8")
        config = BridgeConfig(envelope=DelegationEnvelope(prompt="p"), workspace=tmp_path)
        payload = build_prompt_payload(config)
        assert "AGENTS.md" not in payload or "untrusted" in payload.split("AGENTS.md")[0][ -200:]


# ------------------------------------------------------------ success semantics


class TestSuccessSemantics:
    @staticmethod
    def _attempt(stdout: str = "", stderr: str = "", exit_code: int = 0) -> AttemptResult:
        return AttemptResult(stdout=stdout, stderr=stderr, exit_code=exit_code)

    def test_stderr_only_is_not_success(self) -> None:
        assert attempt_succeeded(self._attempt(stderr="warning: something")) is False

    def test_stdout_content_is_success(self) -> None:
        assert attempt_succeeded(self._attempt(stdout='{"event": "result"}')) is True

    def test_stderr_only_is_transient_so_it_retries(self) -> None:
        from tools.antigravity_bridge import is_transient_failure

        attempt = self._attempt(stderr="warning: something")
        assert is_transient_failure(attempt) is True

    def test_nonzero_exit_is_never_success(self) -> None:
        assert attempt_succeeded(self._attempt(stdout="out", exit_code=3)) is False


# ----------------------------------------------------------- envelope contract


class TestRecommendedSkillsContract:
    def test_from_mapping_preserves_recommended_skills(self) -> None:
        envelope = DelegationEnvelope.from_mapping(
            {
                "prompt": "p",
                "recommended_skills": ["zero-trust-ast-wiring-verifier"],
            }
        )
        assert envelope.recommended_skills == ("zero-trust-ast-wiring-verifier",)

    def test_from_mapping_accepts_comma_string_form(self) -> None:
        envelope = DelegationEnvelope.from_mapping(
            {"prompt": "p", "recommended_skills": "a, b"}
        )
        assert envelope.recommended_skills == ("a", "b")

    def test_cli_envelope_reaches_config(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
        assert "EMPIRICAL CLAIM FALSIFICATION" in payload["payload"].upper()
        assert "RECOMMENDED ADVERSARIAL SKILLS" in payload["payload"]

    def test_cli_flag_reaches_config(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
        assert "ZERO-TRUST AST" in payload["payload"].upper()


# ------------------------------------------------- registry resolution parity


class TestRegistryResolutionParity:
    def test_status_falls_back_like_the_runtime(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--status", "--workspace", str(tmp_path)])
        assert exit_code == 0
        status = json.loads(capsys.readouterr().out)
        assert status["registry_fell_back"] is True
        assert len(status["skills"]) == 12

    def test_status_nonzero_when_registry_broken(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        broken = tmp_path / "broken-registry"
        broken.mkdir()
        (broken / "bad.md").write_text("no frontmatter here", encoding="utf-8")
        exit_code = main(["--status", "--workspace", str(tmp_path), "--skill-dir", str(broken)])
        status = json.loads(capsys.readouterr().out)
        assert "skill_error" in status
        assert exit_code != 0

    def test_list_skills_falls_back_like_the_runtime(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--list-skills", "--workspace", str(tmp_path)])
        assert exit_code == 0
        assert "adversarial-plan-hardening-engine" in capsys.readouterr().out

    def test_dry_run_falls_back_like_the_runtime(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
        assert "ADVERSARIAL ARCHITECTURAL STRESS-TESTING" in payload["payload"].upper()


# ------------------------------------------------------------- report writing


class TestReportPersistence:
    def test_report_is_atomic_and_schema_versioned(self, tmp_path: Path) -> None:
        result = _fake_result()
        path = write_report(tmp_path, result)
        text = path.read_text(encoding="utf-8")
        assert json.loads(text)["schema_version"] == REPORT_SCHEMA_VERSION
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


# --------------------------------------------------------------- provenance


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


# --------------------------------------------------------------- aggregation


class TestAggregationHardening:
    def test_tool_results_surface_in_organized_critique(self) -> None:
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 1,
                            "state": "DONE",
                            "tool_result": {"path": "src/x.py", "lines": "1-9"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "Final words."},
                    }
                ),
            ]
        )
        critique = aggregate_stream_json(raw)
        assert "### Tool Results" in critique
        assert "src/x.py" in critique

    def test_inferred_final_is_flagged(self) -> None:
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"step_index": 0, "text_delta": "Looked at code."},
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"step_index": 1, "text_delta": "Deep finding: bug."},
                    }
                ),
            ]
        )
        critique = aggregate_stream_json(raw)
        assert "Deep finding: bug." in critique
        assert "Inferred final response" in critique

    def test_authoritative_result_is_not_flagged_inferred(self) -> None:
        raw = json.dumps(
            {
                "event": "result",
                "result": {"status": "SUCCESS", "response": "Authoritative."},
            }
        )
        critique = aggregate_stream_json(raw)
        assert "Authoritative." in critique
        assert "Inferred final response" not in critique

    def test_duplicate_actions_are_counted_not_silently_dropped(self) -> None:
        call = json.dumps({"tool": "read_file", "args": {"path": "same.txt"}})
        raw = "\n".join(
            [
                json.dumps({"event": "step_update", "step_update": {"step_index": 0, "tool_calls": [json.loads(call)]}}),
                json.dumps({"event": "step_update", "step_update": {"step_index": 1, "tool_calls": [json.loads(call)]}}),
                json.dumps({"event": "step_update", "step_update": {"step_index": 2, "tool_calls": [json.loads(call)]}}),
            ]
        )
        critique = aggregate_stream_json(raw)
        assert "duplicate action fragment" in critique
        assert "2 duplicate action fragment(s)" in critique

    def test_duplicate_tool_results_are_counted_across_steps(self) -> None:
        """AST-001 (fresh re-audit): identical tool_results inside separate
        step_update events must dedupe to one entry with the duplicate
        counted - the seen-set add was missing from the step loop."""
        result_payload = json.dumps({"output": "same result text"})
        raw = "\n".join(
            [
                json.dumps({"event": "step_update", "step_update": {"step_index": 1, "tool_result": json.loads(result_payload)}}),
                json.dumps({"event": "step_update", "step_update": {"step_index": 2, "tool_result": json.loads(result_payload)}}),
            ]
        )
        critique = aggregate_stream_json(raw)
        assert critique.count("same result text") == 1
        assert "1 duplicate tool-result fragment(s)" in critique

    def test_coalesce_wrapper_matches_buffer_semantics(self) -> None:
        from tools.antigravity_bridge import _DeltaBuffer

        buffer = _DeltaBuffer()
        for delta in ("Hello", "Hello world", " world", "!", "new"):
            buffer.append(delta)
        # Duplicate-and-cumulative folding leaves one clean accumulated text.
        assert buffer.chunks == ["Hello world", "!", "new"]
        assert buffer.text() == "Hello world!new"


def _fake_result():
    from tools.antigravity_bridge import BridgeResult

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
