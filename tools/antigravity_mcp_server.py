"""MCP server exposing the Antigravity delegation bridge as callable tools.

Speaks Model Context Protocol v1 over stdio: newline-delimited JSON-RPC 2.0.
stdout is reserved exclusively for protocol frames; all diagnostics go to
stderr. Tools:

* ``antigravity_review`` — delegate an adversarial review to Google Antigravity
  (``agy``) and return the complete untruncated critique plus raw streams.
* ``antigravity_status`` — resolved executable, workspace, registry, config.
* ``antigravity_skills`` — adversarial skill registry listing.

Every review is also persisted to ``<workspace>/.antigravity-reports/`` as a
complete JSON report, so nothing depends on client-side output limits.

Run directly (stdio server)::

    python tools/antigravity_mcp_server.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

try:
    from tools.antigravity_bridge import (
        DEFAULT_FALLBACK_MODEL,
        DEFAULT_PRIMARY_MODEL,
        DEFAULT_PRINT_TIMEOUT_SECONDS,
        BridgeConfig,
        DelegationEnvelope,
        resolve_agy_executable,
        run_bridge,
        write_report,
    )
    from tools.antigravity_live import DEFAULT_KEEP_RUNS, LIVE_DIR_NAME
    from tools.skill_loader import (
        DEFAULT_SKILL_DIR,
        SkillError,
        SkillLoader,
        resolve_skill_dir,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.antigravity_bridge import (  # type: ignore[no-redef]
        DEFAULT_FALLBACK_MODEL,
        DEFAULT_PRIMARY_MODEL,
        DEFAULT_PRINT_TIMEOUT_SECONDS,
        BridgeConfig,
        DelegationEnvelope,
        resolve_agy_executable,
        run_bridge,
        write_report,
    )
    from tools.antigravity_live import (  # type: ignore[no-redef]
        DEFAULT_KEEP_RUNS,
        LIVE_DIR_NAME,
    )
    from tools.skill_loader import (  # type: ignore[no-redef]
        DEFAULT_SKILL_DIR,
        SkillError,
        SkillLoader,
        resolve_skill_dir,
    )

SERVER_NAME = "antigravity-bridge"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "antigravity_review",
        "description": (
            "Delegate an adversarial review to Google Antigravity (agy). Use BEFORE implementing "
            "multi-file plans or high-risk changes (concurrency, persistence, process spawning, IPC) "
            "and BEFORE declaring work complete. Pass the necessary skill in 'skills' and up to 3 "
            "task-dependent skills in 'recommended_skills'; the payload also carries the full "
            "registry manifest. Returns the organized critique plus stream stats; the complete "
            "forensic report (including raw streams) is saved under .antigravity-reports/."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The plan, diff summary, question, or claim set under review. Required.",
                },
                "context": {
                    "type": "string",
                    "description": "Prior art, constraints, failed attempts, or briefing notes.",
                },
                "claims_to_falsify": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Empirical claims that must be independently recomputed or falsified.",
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Workspace-relative files or path:line ranges to inspect on disk.",
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Necessary/primary skill names to activate (mandatory instructions), "
                        "or ['all']. Omit for none."
                    ),
                },
                "recommended_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                    "description": (
                        "0-3 task-dependent recommended skills from the registry; they are "
                        "rendered as apply-when-relevant guidance. Exceeding 3 is an error."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": "Workspace root to mount via --add-dir (defaults to the server workspace).",
                },
                "model": {
                    "type": "string",
                    "description": f"Primary model (default: {DEFAULT_PRIMARY_MODEL}).",
                },
                "fallback_model": {
                    "type": "string",
                    "description": f"Quota failover model (default: {DEFAULT_FALLBACK_MODEL}).",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "antigravity_status",
        "description": (
            "Report Antigravity bridge status: resolved agy executable, workspace, skill registry "
            "location, default models, and registry warnings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "antigravity_skills",
        "description": (
            "List the adversarial skill registry with names, versions, descriptions, and activation "
            "triggers so the right skills can be selected for a delegation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


class MethodNotFound(Exception):
    """Raised for unknown JSON-RPC methods."""


def _workspace(override: str | None = None) -> Path:
    if override and str(override).strip():
        return Path(str(override)).expanduser().resolve()
    env_workspace = os.environ.get("ANTIGRAVITY_WORKSPACE")
    if env_workspace:
        return Path(env_workspace).expanduser().resolve()
    return Path.cwd().resolve()


def _skill_dir(workspace: Path) -> Path:
    """Resolved registry path for status/list tools (with shipped fallback)."""
    env_dir = os.environ.get("ANTIGRAVITY_SKILL_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    try:
        resolved, _ = resolve_skill_dir(workspace)
        return resolved
    except SkillError:
        return workspace / DEFAULT_SKILL_DIR


def _skill_dir_override() -> Path | None:
    """Explicit registry override only; None lets the bridge resolve + warn."""
    env_dir = os.environ.get("ANTIGRAVITY_SKILL_DIR")
    if env_dir and str(env_dir).strip():
        return Path(env_dir).expanduser().resolve()
    return None


def _live_dir(workspace: Path) -> Path:
    env_dir = os.environ.get("ANTIGRAVITY_LIVE_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return workspace / LIVE_DIR_NAME


def _live_enabled() -> bool:
    raw = os.environ.get("ANTIGRAVITY_LIVE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _selected_models(workspace: Path) -> dict[str, str]:
    """Model selection priority: viewer widget selection > env > built-in default."""
    selected = {
        "model": os.environ.get("ANTIGRAVITY_MODEL", DEFAULT_PRIMARY_MODEL),
        "fallback_model": os.environ.get(
            "ANTIGRAVITY_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL
        ),
    }
    try:
        path = workspace / LIVE_DIR_NAME / "viewer_settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in ("model", "fallback_model"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    selected[key] = value.strip()
    except (OSError, json.JSONDecodeError):
        pass
    return selected


def _tool_status() -> dict[str, Any]:
    workspace = _workspace()
    registry = _skill_dir(workspace)
    payload: dict[str, Any] = {
        "executable": resolve_agy_executable(),
        "workspace": str(workspace),
        "skill_registry": str(registry),
        "primary_model": os.environ.get("ANTIGRAVITY_MODEL", DEFAULT_PRIMARY_MODEL),
        "fallback_model": os.environ.get(
            "ANTIGRAVITY_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL
        ),
        "retries": int(os.environ.get("ANTIGRAVITY_RETRIES", "2")),
        "quota_wait_seconds": int(os.environ.get("ANTIGRAVITY_QUOTA_WAIT", "0")),
        "quota_hook_configured": bool(os.environ.get("ANTIGRAVITY_QUOTA_HOOK")),
        "live_enabled": _live_enabled(),
        "live_dir": str(_live_dir(workspace)),
        "skills": [],
        "warnings": [],
    }
    try:
        loader = SkillLoader(registry)
        payload["skills"] = [
            {
                "name": skill.name,
                "version": skill.version,
                "description": skill.description,
            }
            for skill in loader.skills
        ]
        payload["warnings"] = loader.warnings
    except SkillError as exc:
        payload["skill_error"] = str(exc)
    return payload


def _tool_skills() -> dict[str, Any]:
    workspace = _workspace()
    registry = _skill_dir(workspace)
    try:
        loader = SkillLoader(registry)
        skills = [
            {
                "name": skill.name,
                "version": skill.version,
                "description": skill.description,
                "activation_triggers": list(skill.activation_triggers),
                "source": str(skill.source_path),
            }
            for skill in loader.skills
        ]
        return {
            "registry": str(registry),
            "skills": skills,
            "warnings": loader.warnings,
        }
    except SkillError as exc:
        return {"registry": str(registry), "error": str(exc)}


def _tool_review(arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        return {"error": "Missing required argument 'prompt'."}, True

    workspace = _workspace(arguments.get("workspace"))
    raw_skills = arguments.get("skills") or ()
    if isinstance(raw_skills, str):
        skills = tuple(part.strip() for part in raw_skills.split(",") if part.strip())
    else:
        skills = tuple(str(item).strip() for item in raw_skills if str(item).strip())

    raw_recommended = arguments.get("recommended_skills") or ()
    if isinstance(raw_recommended, str):
        recommended = tuple(
            part.strip() for part in raw_recommended.split(",") if part.strip()
        )
    else:
        recommended = tuple(
            str(item).strip() for item in raw_recommended if str(item).strip()
        )
    if len(recommended) > 3:
        return (
            {
                "error": (
                    "Validation error: 'recommended_skills' cannot exceed 3 items; "
                    f"received {len(recommended)}: {list(recommended)}"
                )
            },
            True,
        )

    raw_claims = arguments.get("claims_to_falsify") or ()
    raw_artifacts = arguments.get("artifacts") or ()
    envelope = DelegationEnvelope(
        prompt=prompt,
        harness=os.environ.get("ANTIGRAVITY_HARNESS", "mcp-client"),
        context=str(arguments.get("context") or "").strip(),
        claims_to_falsify=tuple(str(item) for item in raw_claims if str(item).strip()),
        artifacts=tuple(str(item) for item in raw_artifacts if str(item).strip()),
    )
    selected = _selected_models(workspace)
    config = BridgeConfig(
        envelope=envelope,
        workspace=workspace,
        model=str(arguments.get("model") or selected["model"]),
        fallback_model=str(arguments.get("fallback_model") or selected["fallback_model"]),
        skills=skills,
        recommended_skills=recommended,
        skill_dir=_skill_dir_override() if (skills or recommended) else None,
        print_timeout_seconds=int(
            os.environ.get("ANTIGRAVITY_PRINT_TIMEOUT", str(DEFAULT_PRINT_TIMEOUT_SECONDS))
        ),
        retries=int(os.environ.get("ANTIGRAVITY_RETRIES", "2")),
        retry_backoff_seconds=float(os.environ.get("ANTIGRAVITY_RETRY_BACKOFF", "5")),
        quota_wait_seconds=int(os.environ.get("ANTIGRAVITY_QUOTA_WAIT", "0")),
        quota_hook=os.environ.get("ANTIGRAVITY_QUOTA_HOOK") or None,
        live=_live_enabled(),
        live_dir=_live_dir(workspace),
        live_keep_runs=int(os.environ.get("ANTIGRAVITY_LIVE_KEEP", str(DEFAULT_KEEP_RUNS))),
    )
    try:
        result = run_bridge(config)
    except Exception as exc:
        return {"error": f"Delegation failed to run: {exc}"}, True

    payload = result.to_mcp_dict()
    try:
        payload["report_path"] = str(write_report(workspace, result))
    except OSError as exc:
        payload["report_path"] = f"unavailable ({exc})"
    return payload, not result.success


def _handle_tool_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    if name == "antigravity_review":
        payload, is_error = _tool_review(arguments)
    elif name == "antigravity_status":
        payload, is_error = _tool_status(), False
    elif name == "antigravity_skills":
        payload, is_error = _tool_skills(), False
    else:
        payload, is_error = {"error": f"Unknown tool: {name}"}, True
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        ],
        "isError": is_error,
    }


def handle_request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Handles one JSON-RPC request and returns the result payload."""
    params = params or {}
    if method == "initialize":
        return {
            "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOL_DEFINITIONS}
    if method == "tools/call":
        return _handle_tool_call(params)
    if method == "resources/list":
        return {"resources": []}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    if method == "logging/setLevel":
        return {}
    raise MethodNotFound(method)


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Runs the stdio protocol loop until the client closes stdin."""
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout

    def _send(payload: dict[str, Any]) -> None:
        output_stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        output_stream.flush()

    sys.stderr.write(
        f"[{SERVER_NAME}] ready (v{SERVER_VERSION}, workspace="
        f"{_workspace()})\n"
    )
    sys.stderr.flush()

    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
            )
            continue
        if not isinstance(message, dict):
            continue

        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            continue
        if request_id is None:
            continue

        try:
            result = handle_request(method, message.get("params") or {})
            _send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except MethodNotFound:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
        except Exception as exc:
            sys.stderr.write(f"[{SERVER_NAME}] {method} failed: {exc}\n")
            sys.stderr.flush()
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": f"Internal error: {exc}"},
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
