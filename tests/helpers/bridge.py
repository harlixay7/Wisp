"""Bridge test doubles: a scripted launcher, attempt factories, and skill YAML builders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.antigravity_bridge import AttemptResult, BridgeConfig, DelegationEnvelope


def make_skill_yaml(
    name: str = "unit_skill",
    *,
    version: str = "1.2.3",
    payload: str = "UNIT_INSTRUCTIONS",
    kind: str = "skill",
    omit: str | None = None,
) -> str:
    """Renders a complete skill definition; ``omit`` drops one mandatory field."""
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


class ScriptedLauncher:
    """Deterministic launcher fake; records every command and never spawns a process.

    Replays ``results`` in order and repeats the last one once they run out.
    """

    def __init__(self, results: list[AttemptResult]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.timeouts: list[int] = []
        self.raw_line_sinks: list = []

    def __call__(
        self, command, cwd, env, hard_timeout_seconds, raw_line_sink=None
    ) -> AttemptResult:
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
    return AttemptResult(exit_code=1, stdout="", stderr="read ECONNRESET", duration_seconds=0.01)


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


def bridge_config(**overrides: Any) -> BridgeConfig:
    """A config that never sleeps between retries and names a fake executable."""
    defaults: dict[str, Any] = {
        "envelope": DelegationEnvelope(prompt="review"),
        "workspace": Path.cwd(),
        "retry_backoff_seconds": 0.0,
        "executable": "agy-test",
    }
    defaults.update(overrides)
    return BridgeConfig(**defaults)
