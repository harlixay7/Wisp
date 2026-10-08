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

``tools/call`` runs synchronously: a long review blocks every other request on
this stdio connection until it finishes.

Run directly (stdio server)::

    python tools/antigravity_mcp_server.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, TextIO, TypeVar

if __package__ in (None, ""):
    # Launched as a script (python tools/antigravity_mcp_server.py): put the
    # repository root on sys.path so the absolute tools.* imports resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.antigravity_bridge import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_PRIMARY_MODEL,
    DEFAULT_PRINT_TIMEOUT_SECONDS,
    DELEGATION_MODES,
    REVIEW_MODE,
    WISP_VERSION,
    BridgeConfig,
    DelegationEnvelope,
    executable_available,
    missing_executable_warning,
    resolve_agy_executable,
    run_bridge,
    write_report,
)
from tools.antigravity_live import DEFAULT_KEEP_RUNS, LIVE_DIR_NAME, VIEWER_SETTINGS_FILE
from tools.skill_loader import (
    DEFAULT_SKILL_DIR,
    SkillError,
    SkillLoader,
    resolve_skill_dir,
)

SERVER_NAME = "antigravity-bridge"
SERVER_VERSION = WISP_VERSION
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]

# Upper bounds on tool arguments. They are advertised in inputSchema
# (maxLength/maxItems) and re-checked at runtime because clients do not
# reliably enforce JSON Schema; oversized payloads fail fast with -32602
# instead of reaching agy.
MAX_PROMPT_CHARS = 400_000
MAX_CONTEXT_CHARS = 100_000
MAX_CLAIMS = 100
MAX_ARTIFACTS = 300
MAX_SKILLS = 24
MAX_RECOMMENDED_SKILLS = 3
MAX_MODEL_CHARS = 128
MAX_WORKSPACE_CHARS = 1024

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "antigravity_review",
        "description": (
            "Delegate an adversarial review to Google Antigravity (agy). Use BEFORE implementing "
            "multi-file plans or high-risk changes (concurrency, persistence, process spawning, IPC) "
            "and BEFORE declaring work complete. Pass the necessary skill in 'skills' and up to 3 "
            "task-dependent skills in 'recommended_skills'; the payload also carries a compact "
            "index of every registry skill. Set 'mode' to 'implement' to let the reviewer edit "
            "workspace files (default 'review' is read-only). Returns the organized critique, "
            "the parsed review verdict and stream stats; the complete "
            "forensic report (including raw streams) is saved under .antigravity-reports/."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "maxLength": MAX_PROMPT_CHARS,
                    "description": "The plan, diff summary, question, or claim set under review. Required.",
                },
                "context": {
                    "type": "string",
                    "maxLength": MAX_CONTEXT_CHARS,
                    "description": "Prior art, constraints, failed attempts, or briefing notes.",
                },
                "claims_to_falsify": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_CLAIMS,
                    "description": "Empirical claims that must be independently recomputed or falsified.",
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_ARTIFACTS,
                    "description": "Workspace-relative files or path:line ranges to inspect on disk.",
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_SKILLS,
                    "description": (
                        "Necessary/primary skill names to activate (mandatory instructions), "
                        "or ['all']. Omit for none."
                    ),
                },
                "recommended_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_RECOMMENDED_SKILLS,
                    "description": (
                        f"0-{MAX_RECOMMENDED_SKILLS} task-dependent recommended skills from "
                        "the registry; they are rendered as apply-when-relevant guidance. "
                        f"Exceeding {MAX_RECOMMENDED_SKILLS} is an error."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": list(DELEGATION_MODES),
                    "default": REVIEW_MODE,
                    "description": (
                        "review (default): read-only, changes proposed as unified diffs. "
                        "implement: the reviewer may modify files inside the workspace, "
                        "must leave it test-passing and lists every file it changed."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "maxLength": MAX_WORKSPACE_CHARS,
                    "description": "Workspace root to mount via --add-dir (defaults to the server workspace).",
                },
                "model": {
                    "type": "string",
                    "maxLength": MAX_MODEL_CHARS,
                    "description": f"Primary model (default: {DEFAULT_PRIMARY_MODEL}).",
                },
                "fallback_model": {
                    "type": "string",
                    "maxLength": MAX_MODEL_CHARS,
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


class InvalidParams(Exception):
    """Raised for structurally invalid tool-call parameters (JSON-RPC -32602)."""


def _bounded_str(value: Any, *, limit: int, name: str) -> str | None:
    """Runtime mirror of a schema ``maxLength``; ``None`` (absent) passes through."""
    if value is None:
        return None
    text = str(value)
    if len(text) > limit:
        raise InvalidParams(
            f"'{name}' exceeds its {limit}-character limit (received {len(text)})"
        )
    return text


def _bounded_list(
    value: Any, *, limit: int, name: str, split_commas: bool = False
) -> list[Any]:
    """Runtime mirror of a schema ``maxItems``; raises InvalidParams on breach.

    A bare string is accepted because AGENTS.md documents claims, artifacts,
    and skill selectors as string-or-list. With ``split_commas=True`` the
    string is comma-split (skill selectors); without it the whole string is a
    single item (free-text claims may legitimately contain commas).
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",") if split_commas else [value]
        items: list[Any] = [part.strip() for part in parts if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise InvalidParams(f"'{name}' must be an array")
    if len(items) > limit:
        raise InvalidParams(
            f"'{name}' exceeds its {limit}-item limit (received {len(items)})"
        )
    return items


# Malformed numeric env vars, keyed by variable name so repeated parses of the
# same bad value report it once instead of accumulating duplicates.
_CONFIG_WARNINGS: dict[str, str] = {}

_Number = TypeVar("_Number", int, float)


def _env_number(name: str, default: _Number) -> _Number:
    """Parses a numeric env var as ``type(default)``.

    Malformed values fall back to ``default`` and are recorded in
    ``_CONFIG_WARNINGS`` (surfaced by ``antigravity_status``) rather than
    failing the request.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        _CONFIG_WARNINGS.pop(name, None)
        return default
    try:
        value = type(default)(raw)
    except ValueError:
        kind = "an integer" if isinstance(default, int) else "a number"
        _CONFIG_WARNINGS[name] = f"{name}={raw!r} is not {kind}; using {default}"
        return default
    _CONFIG_WARNINGS.pop(name, None)
    return value


def _env_path(name: str) -> Path | None:
    """Resolved path from an env var, or None when unset or blank."""
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _mode(value: Any) -> str:
    """Runtime mirror of the ``mode`` enum; absent means review."""
    if value is None:
        return REVIEW_MODE
    if not isinstance(value, str) or value not in DELEGATION_MODES:
        raise InvalidParams(
            f"'mode' must be one of {', '.join(DELEGATION_MODES)} (received {value!r})"
        )
    return value


def _workspace(override: str | None = None) -> Path:
    if override and override.strip():
        return Path(override.strip()).expanduser().resolve()
    return _env_path("ANTIGRAVITY_WORKSPACE") or Path.cwd().resolve()


def _skill_dir(workspace: Path) -> Path:
    """Resolved registry path for status/list tools (with shipped fallback)."""
    override = _env_path("ANTIGRAVITY_SKILL_DIR")
    if override is not None:
        return override
    try:
        resolved, _ = resolve_skill_dir(workspace)
        return resolved
    except SkillError:
        return workspace / DEFAULT_SKILL_DIR


def _skill_dir_override() -> Path | None:
    """Explicit registry override only; None lets the bridge resolve + warn."""
    return _env_path("ANTIGRAVITY_SKILL_DIR")


def _live_dir(workspace: Path) -> Path:
    return _env_path("ANTIGRAVITY_LIVE_DIR") or workspace / LIVE_DIR_NAME


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
        path = _live_dir(workspace) / VIEWER_SETTINGS_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return selected
    if isinstance(data, dict):
        for key in ("model", "fallback_model"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                selected[key] = value.strip()
    return selected


def _tool_status() -> dict[str, Any]:
    workspace = _workspace()
    registry = _skill_dir(workspace)
    models = _selected_models(workspace)
    executable = resolve_agy_executable()
    executable_found = executable_available(executable)
    payload: dict[str, Any] = {
        "executable": executable,
        "executable_found": executable_found,
        "workspace": str(workspace),
        "skill_registry": str(registry),
        "primary_model": models["model"],
        "fallback_model": models["fallback_model"],
        "retries": _env_number("ANTIGRAVITY_RETRIES", 2),
        "quota_wait_seconds": _env_number("ANTIGRAVITY_QUOTA_WAIT", 0),
        "quota_hook_configured": bool(os.environ.get("ANTIGRAVITY_QUOTA_HOOK")),
        "live_enabled": _live_enabled(),
        "live_dir": str(_live_dir(workspace)),
        "wisp_version": WISP_VERSION,
        "skills": [],
        "warnings": [],
    }
    if _CONFIG_WARNINGS:
        payload["config_warnings"] = list(_CONFIG_WARNINGS.values())
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
        payload["warnings"] = list(loader.warnings)
    except SkillError as exc:
        payload["skill_error"] = str(exc)
    if not executable_found:
        payload["warnings"].append(missing_executable_warning(executable))
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
    _bounded_str(prompt, limit=MAX_PROMPT_CHARS, name="prompt")
    context = _bounded_str(arguments.get("context"), limit=MAX_CONTEXT_CHARS, name="context")
    workspace_arg = _bounded_str(
        arguments.get("workspace"), limit=MAX_WORKSPACE_CHARS, name="workspace"
    )
    model_arg = _bounded_str(arguments.get("model"), limit=MAX_MODEL_CHARS, name="model")
    fallback_arg = _bounded_str(
        arguments.get("fallback_model"), limit=MAX_MODEL_CHARS, name="fallback_model"
    )
    mode = _mode(arguments.get("mode"))
    raw_skills = _bounded_list(
        arguments.get("skills"), limit=MAX_SKILLS, name="skills", split_commas=True
    )
    raw_recommended = _bounded_list(
        arguments.get("recommended_skills"),
        limit=MAX_RECOMMENDED_SKILLS,
        name="recommended_skills",
        split_commas=True,
    )
    raw_claims = _bounded_list(
        arguments.get("claims_to_falsify"), limit=MAX_CLAIMS, name="claims_to_falsify"
    )
    raw_artifacts = _bounded_list(
        arguments.get("artifacts"), limit=MAX_ARTIFACTS, name="artifacts"
    )

    workspace = _workspace(workspace_arg)
    skills = tuple(str(item).strip() for item in raw_skills if str(item).strip())
    recommended = tuple(
        str(item).strip() for item in raw_recommended if str(item).strip()
    )

    envelope = DelegationEnvelope(
        prompt=prompt,
        harness=os.environ.get("ANTIGRAVITY_HARNESS", "mcp-client"),
        context=(context or "").strip(),
        claims_to_falsify=tuple(str(item) for item in raw_claims if str(item).strip()),
        artifacts=tuple(str(item) for item in raw_artifacts if str(item).strip()),
        mode=mode,
    )
    selected = _selected_models(workspace)
    config = BridgeConfig(
        envelope=envelope,
        workspace=workspace,
        model=model_arg or selected["model"],
        fallback_model=fallback_arg or selected["fallback_model"],
        skills=skills,
        recommended_skills=recommended,
        skill_dir=_skill_dir_override() if (skills or recommended) else None,
        print_timeout_seconds=_env_number(
            "ANTIGRAVITY_PRINT_TIMEOUT", DEFAULT_PRINT_TIMEOUT_SECONDS
        ),
        retries=_env_number("ANTIGRAVITY_RETRIES", 2),
        retry_backoff_seconds=_env_number("ANTIGRAVITY_RETRY_BACKOFF", 5.0),
        quota_wait_seconds=_env_number("ANTIGRAVITY_QUOTA_WAIT", 0),
        quota_hook=os.environ.get("ANTIGRAVITY_QUOTA_HOOK") or None,
        live=_live_enabled(),
        live_dir=_live_dir(workspace),
        live_keep_runs=_env_number("ANTIGRAVITY_LIVE_KEEP", DEFAULT_KEEP_RUNS),
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
        # MCP lifecycle: echo the client's protocolVersion only if we support
        # it; otherwise offer our latest and let the client decide whether to
        # continue.
        requested = str(params.get("protocolVersion") or "")
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOL_DEFINITIONS}
    if method == "tools/call":
        if not isinstance(params, dict):
            raise InvalidParams("'params' must be an object")
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
    """Runs the stdio protocol loop until the client closes stdin.

    JSON-RPC 2.0 rules: non-object frames get -32600, non-object params get
    -32602, notifications (no id) never get a response, and a jsonrpc field
    other than "2.0" is rejected. A frame without a method that carries
    ``result`` or ``error`` is a client response and is ignored; any other
    frame without a method gets -32600.
    """
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout

    def _send(payload: dict[str, Any]) -> None:
        output_stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        output_stream.flush()

    def _error(request_id: Any, code: int, message: str) -> None:
        _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

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
            _error(None, -32700, f"Parse error: {exc}")
            continue
        if not isinstance(message, dict):
            _error(None, -32600, "Invalid Request: frame must be a JSON object")
            continue

        request_id = message.get("id")
        version = message.get("jsonrpc")
        if version is not None and str(version) != "2.0":
            _error(request_id, -32600, f"Invalid Request: unsupported jsonrpc version {version!r}")
            continue
        method = message.get("method")
        if method is None:
            # Responses carry an id too, so the id cannot tell them apart from
            # malformed requests; result/error can. This server never issues
            # requests, so any response is unsolicited, and responses must
            # never be answered.
            if "result" not in message and "error" not in message:
                _error(request_id, -32600, "Invalid Request: missing 'method'")
            continue
        if request_id is None:
            # JSON-RPC notification: this server deliberately stays silent.
            # Executing tools (each a billable delegation) on uncorrelated
            # notification frames would make stray lines burn quota.
            continue

        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            _error(request_id, -32602, f"Invalid params: expected an object, got {type(params).__name__}")
            continue

        try:
            result = handle_request(method, params)
            _send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except MethodNotFound:
            _error(request_id, -32601, f"Method not found: {method}")
        except InvalidParams as exc:
            _error(request_id, -32602, str(exc))
        except Exception as exc:
            sys.stderr.write(f"[{SERVER_NAME}] {method} failed: {exc}\n")
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            _error(request_id, -32603, f"Internal error: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
