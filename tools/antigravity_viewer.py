"""Local live viewer for Antigravity delegation telemetry.

Serves a single-page animated UI on ``127.0.0.1`` and streams live events from
the newest run file under ``<workspace>/.antigravity-reports/live/`` via
Server-Sent Events. Read-only by default; the POST actions (account switch,
status refresh, asks, settings) are guarded by a custom header plus an origin
allow-list.

Security posture:

* **Loopback by default.** Binding a non-loopback host is an explicit opt-in
  that requires ``--auth-token`` (or ``--generate-token``); every route then
  demands a bearer token, and ``/health`` deliberately stays minimal.
* **Artifact containment.** Image paths supplied to ``/api/ask`` must resolve
  inside the workspace or the capture directory with an image extension;
  anything else is rejected rather than normalized.
* **Bounded bodies.** ``Content-Length`` is validated against a global ceiling
  before any body byte is read.
* **Instance identity.** The viewer writes a per-instance token into its
  manifest; ``--replace`` uses an authenticated shutdown handshake instead of
  trusting a stale PID.

This is a local operator tool, not a hardened multi-user web service; see
``SECURITY.md`` for the full trust model.

The viewer is strictly optional: the delegation engine and the MCP server work
identically whether or not this process is running. The desktop shells that
host the page live in :mod:`tools.viewer_shell`; OS process and account
helpers live in :mod:`tools.viewer_platform`.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

if __package__ in (None, ""):
    # Run as a script (``python tools/antigravity_viewer.py``): make the
    # repository root importable so the ``tools.`` imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.antigravity_bridge import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_PRIMARY_MODEL,
    WISP_VERSION,
    AttemptResult,
    BridgeConfig,
    DelegationEnvelope,
    resolve_agy_executable,
    run_bridge,
    write_report,
)
from tools.antigravity_live import LIVE_DIR_NAME, known_live_dirs
from tools.skill_loader import (
    DEFAULT_SKILL_DIR,
    SkillError,
    SkillLoader,
    resolve_skill_dir,
)
from tools.viewer_platform import (
    looks_like_viewer_process,
    pid_image_name,
    switch_account,
    terminate_process,
)
from tools.viewer_shell import (
    BROWSER_WINDOW_SIZE,
    open_app_window,
    open_electron_window,
    open_native_window,
)
from tools.wisp_capture import (
    capture_auto,
    capture_dir,
    relative_to_workspace,
    save_pasted_image,
)
from tools.wisp_chat import (
    MAX_MESSAGE_CHARS,
    ChatStore,
    valid_thread_id,
    write_json_atomic,
)

# --------------------------------------------------------------------------
# Server identity and timing

SERVER_NAME = "wisp-viewer"
SERVER_VERSION = WISP_VERSION
DEFAULT_PORT = 48477
PORT_SCAN_RANGE = 50
SESSION_HEADER = "X-Wisp-Request"
INSTANCE_TOKEN_HEADER = "X-Instance-Token"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
POLL_INTERVAL_SECONDS = 0.15
KEEPALIVE_SECONDS = 10.0
REPLAY_LINE_DELAY_SECONDS = 0.04
LIVE_LATCH_IDLE_SECONDS = 15.0
WATCH_REFRESH_SECONDS = 2.0
HANDSHAKE_TIMEOUT_SECONDS = 6.0
HEALTH_PROBE_TIMEOUT_SECONDS = 2.0
REPLACE_SETTLE_SECONDS = 1.5
SERVE_POLL_SECONDS = 0.3

# --------------------------------------------------------------------------
# Limits

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_LOG_TAIL_BYTES = 400_000
LAST_EVENT_TAIL_BYTES = 16_384
MAX_AUTH_LOGS_SCANNED = 20
RUN_LIST_LIMIT = 60
MAX_ASK_SKILLS = 32
MAX_ASK_CONTEXT_CHARS = 4000
RATE_LIMIT_WINDOW_SECONDS = 45 * 60

# --------------------------------------------------------------------------
# Files and assets

TOOLS_DIR = Path(__file__).resolve().parent
ASSET_DIR = TOOLS_DIR / "viewer_assets"
HTML_PATH = TOOLS_DIR / "antigravity_viewer.html"
MANIFEST_NAME = "viewer.json"
SETTINGS_NAME = "viewer_settings.json"
STATE_NAMES = (
    "dormant",
    "awakening",
    "deliberating",
    "reaching",
    "absorbing",
    "strained",
    "shifting",
    "composing",
    "impatient",
    "triumph",
    "withered",
)
ASSET_EXTENSIONS = (".png", ".webp", ".gif", ".jpg", ".jpeg")
CAPTURE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_RUN_FILE_PATTERN = re.compile(r"run-[A-Za-z0-9\-_]+\.jsonl")

# --------------------------------------------------------------------------
# Account and quota detection (agy logs)

_RATE_LIMIT_MARKERS = ("RESOURCE_EXHAUSTED", "code 429", "Individual quota reached")
_RESET_PATTERN = re.compile(r"Resets in ([0-9]+\s*[smhdw][0-9]*\s*[smhdw]*)", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(
    r"authenticated successfully as ([A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+)"
)
_AUTH_LOG_DIRS = (
    Path.home() / ".gemini" / "antigravity-cli" / "log",
    Path.home() / ".gemini" / "antigravity" / "log",
)

# --------------------------------------------------------------------------
# Models

FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.7-flash-low",
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)
DEFAULT_SELECTED_MODEL = DEFAULT_PRIMARY_MODEL
DEFAULT_SELECTED_FALLBACK = DEFAULT_FALLBACK_MODEL
MODEL_SETTING_KEYS = ("model", "fallback_model", "chat_model")
MODEL_CACHE_TTL_SECONDS = 600.0
# Short TTL for the fallback list: long enough that a missing or broken agy
# is not re-spawned (up to AGY_MODELS_TIMEOUT_SECONDS) on every request, short
# enough that a freshly installed agy is picked up quickly.
MODEL_FALLBACK_TTL_SECONDS = 60.0
AGY_MODELS_TIMEOUT_SECONDS = 25
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MODEL_CACHE: dict[str, Any] = {"ts": 0.0, "models": [], "source": ""}
# Held across the agy call so concurrent requests share one subprocess.
_MODEL_CACHE_LOCK = threading.Lock()

ASK_DEFAULT_PROMPT = (
    "Review this quickly and flag anything important, risky, or broken. "
    "Lead with the bottom line, then the specifics."
)

Query = Mapping[str, list[str]]
SendEvent = Callable[[str, str], bool]


# --------------------------------------------------------------------------
# Model inventory and viewer settings


def parse_model_list(output: str) -> list[str]:
    names: list[str] = []
    for line in (output or "").splitlines():
        name = line.strip().split("\t")[0].split("  ")[0].strip()
        if name and _MODEL_NAME_PATTERN.match(name) and name not in names:
            names.append(name)
    return names


def list_models() -> dict[str, Any]:
    """Model inventory from ``agy models``, falling back to a built-in list.

    Both outcomes are cached (the fallback briefly) so callers can treat this
    as cheap after the first call.
    """
    with _MODEL_CACHE_LOCK:
        now = time.time()
        cached = list(_MODEL_CACHE.get("models") or [])
        source = _MODEL_CACHE.get("source")
        age = now - float(_MODEL_CACHE.get("ts") or 0.0)
        if cached and source == "agy" and age < MODEL_CACHE_TTL_SECONDS:
            return {"models": cached, "source": "cache"}
        if cached and source == "fallback" and age < MODEL_FALLBACK_TTL_SECONDS:
            return {"models": cached, "source": "fallback"}
        try:
            result = subprocess.run(
                [resolve_agy_executable(), "models"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=AGY_MODELS_TIMEOUT_SECONDS,
            )
            models = parse_model_list(result.stdout or "")
        except (OSError, subprocess.SubprocessError):
            models = []
        source = "agy" if models else "fallback"
        if not models:
            models = list(FALLBACK_MODELS)
        _MODEL_CACHE.update({"ts": now, "models": models, "source": source})
        return {"models": list(models), "source": source}


def _model_known(model: str) -> tuple[bool, list[str]]:
    """Checks a model id against the (cached) inventory; returns it as well."""
    known = list_models()["models"]
    return (model in known), known


def _clean_skills(raw: Sequence[Any]) -> list[str]:
    """Stripped, non-empty skill names from an untrusted list."""
    return [str(skill).strip() for skill in raw if str(skill).strip()]


def settings_path(live_dir: Path) -> Path:
    return Path(live_dir) / SETTINGS_NAME


def manifest_path(live_dir: Path) -> Path:
    return Path(live_dir) / MANIFEST_NAME


def read_viewer_settings(live_dir: Path) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "model": DEFAULT_SELECTED_MODEL,
        "fallback_model": DEFAULT_SELECTED_FALLBACK,
        "chat_model": "",
        "ask_skills": [],
    }
    try:
        data = json.loads(settings_path(live_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if not isinstance(data, dict):
        return settings
    for key in MODEL_SETTING_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            settings[key] = value.strip()
    skills = data.get("ask_skills")
    if isinstance(skills, list):
        settings["ask_skills"] = _clean_skills(skills)[:MAX_ASK_SKILLS]
    return settings


def write_viewer_settings(live_dir: Path, patch: dict[str, Any]) -> dict[str, Any]:
    """Merges ``patch`` into the stored settings and returns the result.

    Invalid model names are ignored rather than stored. Persistence is best
    effort: the merged settings are returned even when the write fails.
    """
    settings = read_viewer_settings(live_dir)
    for key in MODEL_SETTING_KEYS:
        value = patch.get(key)
        if isinstance(value, str) and value.strip() and _MODEL_NAME_PATTERN.match(value.strip()):
            settings[key] = value.strip()
    if "ask_skills" in patch:
        skills = patch.get("ask_skills")
        if isinstance(skills, list):
            settings["ask_skills"] = _clean_skills(skills)[:MAX_ASK_SKILLS]
        elif skills == "all":
            settings["ask_skills"] = ["all"]
    try:
        write_json_atomic(settings_path(live_dir), settings)
    except OSError:
        pass
    return settings


def _effective_ask_model(model: str, settings: Mapping[str, Any]) -> str:
    """The model an ask runs on: explicit choice, then chat model, then global."""
    return model or settings.get("chat_model") or settings["model"]


# --------------------------------------------------------------------------
# Workspace, assets and hosts


def resolve_workspace(override: str | None = None) -> Path:
    if override and override.strip():
        return Path(override).expanduser().resolve()
    env_workspace = os.environ.get("ANTIGRAVITY_WORKSPACE")
    if env_workspace:
        return Path(env_workspace).expanduser().resolve()
    return Path.cwd().resolve()


def resolve_live_dir(workspace: Path, override: str | None = None) -> Path:
    if override and override.strip():
        return Path(override).expanduser().resolve()
    env_dir = os.environ.get("ANTIGRAVITY_LIVE_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return workspace / LIVE_DIR_NAME


def load_assets() -> dict[str, str]:
    """Returns the state -> filename mapping for available creature assets."""
    found: dict[str, str] = {}
    for state in STATE_NAMES:
        for extension in ASSET_EXTENSIONS:
            candidate = ASSET_DIR / f"{state}{extension}"
            if candidate.is_file():
                found[state] = candidate.name
                break
    return found


def _host_from_header(host_header: str) -> str:
    """Extracts the hostname from a Host header, RFC 3986 brackets included.

    ``"[::1]:48477".split(":")[0]`` yields ``"["``, which would reject every
    request on an IPv6 loopback binding.
    """
    raw = (host_header or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("[") and "]" in raw:
        return raw[1 : raw.index("]")]
    return raw.split(":")[0]


def url_host(host: str) -> str:
    """Formats ``host`` for a URL authority: IPv6 literals need brackets."""
    bare = (host or "").strip()
    if ":" in bare and not bare.startswith("["):
        return f"[{bare}]"
    return bare


def allowed_origins(port: int) -> frozenset[str]:
    """Browser origins permitted to POST: the loopback names on our port."""
    return frozenset(f"http://{url_host(host)}:{port}" for host in LOOPBACK_HOSTS)


def client_host(bind_host: str) -> str:
    """Address a local client should dial to reach a server bound to ``bind_host``."""
    bare = (bind_host or "").strip().strip("[]")
    if bare in ("", "0.0.0.0"):
        return "127.0.0.1"
    if bare == "::":
        return "::1"
    return bare


# --------------------------------------------------------------------------
# Run discovery


def _safe_mtime(path: Path) -> float:
    """stat() mtime, or 0.0 if the file vanished (retention pruning can delete
    runs between glob and sort)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _run_files(live_dir: Path) -> list[Path]:
    try:
        return [path for path in live_dir.glob("run-*.jsonl") if path.is_file()]
    except OSError:
        return []


def _run_order(path: Path) -> tuple[str, float]:
    """Newest-run ordering: run ids embed their start time, mtime breaks ties.

    Ordering by id rather than mtime keeps an older run that is still being
    written from displacing a newer one.
    """
    return path.name, _safe_mtime(path)


def newest_run(live_dir: Path) -> Path | None:
    runs = _run_files(live_dir)
    return max(runs, key=_run_order) if runs else None


def newest_run_across(dirs: Sequence[Path]) -> Path | None:
    """Newest run across several live directories (same ordering as newest_run)."""
    candidates = [run for run in (newest_run(directory) for directory in dirs) if run]
    return max(candidates, key=_run_order) if candidates else None


def _read_first_event(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                data = json.loads(stripped)
                return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _read_last_event(path: Path) -> dict[str, Any] | None:
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            handle.seek(max(0, size - LAST_EVENT_TAIL_BYTES))
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        return data if isinstance(data, dict) else None
    return None


def is_run_end_line(line: str) -> bool:
    """True when a live-feed line is the run's terminal ``run_end`` event."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    return isinstance(event, dict) and event.get("kind") == "run_end"


def describe_run(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    first = _read_first_event(path)
    last = _read_last_event(path)
    start = first if first and first.get("kind") == "run_start" else None
    end = last if last and last.get("kind") == "run_end" else None
    start_meta = (start or {}).get("meta") or {}
    end_meta = (end or {}).get("meta") or {}
    return {
        "file": path.name,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "age_seconds": round(time.time() - stat.st_mtime),
        "started_ts": (start or {}).get("ts"),
        "model": start_meta.get("primary_model") or (start or {}).get("model") or "",
        "finished": bool(end),
        "success": end_meta.get("success") if end else None,
        "elapsed_seconds": end_meta.get("elapsed_seconds") if end else None,
        "failover_used": bool(end_meta.get("failover_used")) if end else False,
    }


def list_runs(live_dir: Path, limit: int = RUN_LIST_LIMIT) -> list[dict[str, Any]]:
    """Run summaries for the history list, most recently modified first."""
    runs = _run_files(live_dir)
    runs.sort(key=lambda path: (_safe_mtime(path), path.name), reverse=True)
    entries: list[dict[str, Any]] = []
    for path in runs[:limit]:
        record = describe_run(path)
        if record:
            entries.append(record)
    return entries


# --------------------------------------------------------------------------
# Account, quota and status


def mask_email(address: str | None) -> str | None:
    """Masks a detected account address for API exposure (keeps the domain)."""
    if not address or "@" not in address:
        return address
    local, _, domain = address.partition("@")
    masked = local[0] + "*" if len(local) <= 2 else local[:2] + "***"
    return f"{masked}@{domain}"


def _read_log_tail(path: Path) -> str:
    try:
        size = path.stat().st_size
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            if size > MAX_LOG_TAIL_BYTES:
                handle.seek(size - MAX_LOG_TAIL_BYTES)
            return handle.read()
    except OSError:
        return ""


def collect_auth_state() -> dict[str, Any]:
    """Best-effort account and quota state from recent agy logs (honest, no guessing)."""
    account: str | None = None
    rate_limited = False
    resets_in: str | None = None
    newest_log_age: float | None = None
    logs: list[Path] = []
    for directory in _AUTH_LOG_DIRS:
        if directory.is_dir():
            try:
                logs.extend(path for path in directory.glob("*.log") if path.is_file())
            except OSError:
                continue
    logs.sort(key=_safe_mtime, reverse=True)
    now = time.time()
    for path in logs[:MAX_AUTH_LOGS_SCANNED]:
        content = _read_log_tail(path)
        if not content:
            continue
        if account is None:
            emails = _EMAIL_PATTERN.findall(content)
            if emails:
                account = emails[-1]
        if newest_log_age is None:
            try:
                newest_log_age = round(now - path.stat().st_mtime)
            except OSError:
                newest_log_age = None
        if any(marker in content for marker in _RATE_LIMIT_MARKERS):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                age = RATE_LIMIT_WINDOW_SECONDS + 1
            if age < RATE_LIMIT_WINDOW_SECONDS:
                rate_limited = True
                matches = _RESET_PATTERN.findall(content)
                if matches:
                    resets_in = " ".join(matches[-1].split())
    return {
        "account": mask_email(account),
        "account_source": "log-heuristic" if account else None,
        "account_masked": True,
        "rate_limited": rate_limited,
        "resets_in": resets_in,
        "newest_log_age_seconds": newest_log_age,
    }


def collect_status(context: ViewerContext) -> dict[str, Any]:
    skills: list[str] = []
    skill_warnings: list[str] = []
    skill_error: str | None = None
    try:
        registry, _ = resolve_skill_dir(context.workspace)
    except SkillError:
        registry = context.workspace / DEFAULT_SKILL_DIR
    try:
        loader = SkillLoader(registry)
        skills = [skill.name for skill in loader.skills]
        skill_warnings = loader.warnings
    except SkillError as exc:
        skill_error = str(exc)
    return {
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "port": context.port,
        "workspace": str(context.workspace),
        "live_dir": str(context.live_dir),
        "test_mode": _fake_asks_enabled(),
        "watched_dirs": [str(directory) for directory in context.watched_dirs()],
        "executable": resolve_agy_executable(),
        "models": {
            "primary": os.environ.get("ANTIGRAVITY_MODEL", DEFAULT_PRIMARY_MODEL),
            "fallback": os.environ.get("ANTIGRAVITY_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL),
        },
        "selected": read_viewer_settings(context.live_dir),
        "auth": collect_auth_state(),
        "skill_registry": str(registry),
        "skills": skills,
        "skill_warnings": skill_warnings,
        "skill_error": skill_error,
        "runs": len(_run_files(context.live_dir)),
        "uptime_seconds": round(time.time() - context.started, 1),
    }


# --------------------------------------------------------------------------
# Operator asks


def build_ask_prompt(
    user_prompt: str, context_text: str, image_rels: tuple[str, ...]
) -> str:
    parts = [
        "You are consulting live with the human operator through Wisp capture.",
        "",
        "## OPERATOR REQUEST",
        user_prompt or ASK_DEFAULT_PROMPT,
    ]
    if context_text:
        parts += ["", "## CAPTURED CONTEXT (selected text)", context_text]
    if image_rels:
        listing = "\n".join(f"- `{rel}`" for rel in image_rels)
        parts += [
            "",
            "## ATTACHED IMAGES",
            "Inspect the attached image(s) (workspace-relative paths):",
            listing,
        ]
    parts += [
        "",
        "## RESPONSE REQUIREMENTS",
        "- Be brief: use the fewest words that fully answer the question. A simple question gets a few sentences, not a report.",
        "- Lead with the direct answer; include only the details that matter. Do not restate the question, pad with caveats, or add structure the answer does not need.",
        "- If the capture shows code or UI, cite exactly what you observe, name risks, and give precise fixes.",
        "- Use Markdown only when it genuinely helps; keep it tight.",
        "- If something is unclear, state the assumption you are making; never invent facts.",
    ]
    return "\n".join(parts)


def extract_chat_answer(critique: str) -> str:
    """Extracts the aggregated critique from a full report for chat display.

    Prefers the ``## Antigravity Critique`` section (excluding report framing,
    raw appendix, and lifecycle noise); falls back to everything before the
    verbatim stream appendices.
    """
    text = str(critique or "")
    marker = "## Antigravity Critique"
    start = text.find(marker)
    if start != -1:
        rest = text[start:]
        next_section = rest.find("\n## ", len(marker))
        section = rest[:next_section] if next_section != -1 else rest
        lines = section.splitlines()
        if lines and lines[0].startswith(marker):
            lines = lines[1:]
        answer = "\n".join(lines).strip()
        if answer:
            return answer
    for trailer in ("\n## Complete stdout", "\n## Complete stderr"):
        if trailer in text:
            text = text.split(trailer)[0].rstrip()
    return text.strip() or "No answer was produced."


def _fake_asks_enabled() -> bool:
    return os.environ.get("WISP_ASK_FAKE") == "1"


def _fake_ask_launcher(
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    hard_timeout_seconds: int,
    raw_line_sink: Callable[[str, str], None] | None = None,
) -> AttemptResult:
    """Canned agy run used when ``WISP_ASK_FAKE=1`` (tests and UI demos)."""
    try:
        delay = float(os.environ.get("WISP_ASK_FAKE_DELAY", "0") or 0)
    except ValueError:
        delay = 0.0
    if delay > 0:
        time.sleep(delay)
    answer = (
        "[TEST MODE] Canned critique — fake launcher active.\n\n"
        "# Quick review\n\n"
        "**Bottom line:** the capture looks structurally sound; one risk stands out.\n\n"
        "- Risk: the retry path has no backoff bound.\n"
        "- Fix: cap retries at 3 with exponential delay.\n\n"
        "```python\nfor attempt in range(3):\n    time.sleep(2 ** attempt)\n```\n"
    )
    if os.environ.get("WISP_ASK_FAKE_BIG") == "1":
        # Oversized answer (> MAX_MESSAGE_CHARS) to check that a successful
        # run is clamped, not reported as a failure.
        answer += "\n" + ("detail " * 20_000)
    lines = [
        json.dumps({"step_update": {"thinking": "Considering the operator's capture."}}),
        json.dumps(
            {"step_update": {"tool_calls": [{"name": "view_file", "args": {"path": "notes.md"}}]}}
        ),
        json.dumps({"tool_result": {"output": "notes.md inspected (21 lines)"}}),
        json.dumps({"step_update": {"text": answer}}),
    ]
    if raw_line_sink is not None:
        for line in lines:
            raw_line_sink("stdout", line)
    return AttemptResult(
        exit_code=0,
        stdout=json.dumps({"step_update": {"text": answer}}),
        stderr="",
        duration_seconds=0.08,
    )


def _ask_worker(
    context: ViewerContext,
    thread_id: str,
    prompt: str,
    context_text: str,
    image_paths: tuple[str, ...],
    model: str,
    skills: tuple[str, ...],
) -> None:
    """Runs one delegation for an ask and records the answer in its thread.

    Owns ``context.ask_lock`` (acquired by the request handler) and always
    releases it.
    """
    store = ChatStore(context.live_dir)
    fake = _fake_asks_enabled()
    try:
        settings = read_viewer_settings(context.live_dir)
        # The handler already contained these paths; re-check in case a file
        # was deleted or replaced while the ask was queued.
        artifacts = tuple(
            contained
            for contained in (
                context.contain_artifact(str(context.workspace / rel)) for rel in image_paths
            )
            if contained
        )
        config = BridgeConfig(
            envelope=DelegationEnvelope(
                prompt=build_ask_prompt(prompt, context_text, artifacts),
                harness="wisp-hotkey",
                artifacts=artifacts,
            ),
            workspace=context.workspace,
            model=_effective_ask_model(model, settings),
            fallback_model=settings["fallback_model"],
            skills=skills,
            live=True,
            live_dir=context.live_dir,
        )
        captured_run: dict[str, str] = {}

        def _on_event(event: Any) -> None:
            if getattr(event, "kind", "") == "run_start":
                captured_run["id"] = getattr(event, "run_id", "")

        config.live_callback = _on_event
        result = (
            run_bridge(config, launcher=_fake_ask_launcher) if fake else run_bridge(config)
        )
        try:
            report_path = str(write_report(context.workspace, result))
        except OSError:
            report_path = ""
        answer = extract_chat_answer(
            result.critique_markdown or result.error or "No answer was produced."
        )
        # Clamp before persisting: append_message rejects oversized content,
        # and a long but successful answer must not turn into a failure.
        answer = answer[:MAX_MESSAGE_CHARS]
        run_id = captured_run.get("id", "")
        store.append_message(
            thread_id,
            "assistant",
            answer,
            {
                "success": result.success,
                "model": result.model_used,
                "run_id": run_id,
                "report_path": report_path,
                "error": result.error,
                "fake": fake,
            },
        )
        store.update_run(
            thread_id,
            {
                "status": "done" if result.success else "failed",
                "run_id": run_id,
                "success": result.success,
                "ended": time.time(),
            },
        )
    except Exception as exc:
        # Deliberately broad: this thread has no caller to report to, so any
        # failure must land in the chat thread for the operator to see.
        store.append_message(
            thread_id,
            "assistant",
            f"Delegation failed before answering: {exc}",
            {"success": False, "error": str(exc)},
        )
        store.update_run(thread_id, {"status": "failed", "error": str(exc)})
    finally:
        context.active_ask = {}
        _release(context.ask_lock)


def _release(lock: threading.Lock) -> None:
    try:
        lock.release()
    except RuntimeError:
        pass


# --------------------------------------------------------------------------
# Server state


@dataclass
class ViewerContext:
    workspace: Path
    live_dir: Path
    port: int
    started: float = field(default_factory=time.time)
    assets: dict[str, str] = field(default_factory=load_assets)
    ask_lock: threading.Lock = field(default_factory=threading.Lock)
    active_ask: dict[str, Any] = field(default_factory=dict)
    capture_lock: threading.Lock = field(default_factory=threading.Lock)
    auth_token: str = ""
    instance_token: str = ""
    _watch_cache: tuple[float, tuple[Path, ...]] = field(
        default_factory=lambda: (0.0, ()), repr=False
    )

    def artifact_roots(self) -> list[Path]:
        """Directories from which /api/ask may attach files.

        Only roots inside the workspace qualify: delegation artifacts are
        passed workspace-relative so the mounted reviewer can resolve them,
        and the ask worker re-joins them against the workspace.
        """
        roots = [self.workspace]
        captures = capture_dir(self.live_dir)
        try:
            captures.relative_to(self.workspace.resolve())
        except ValueError:
            pass
        else:
            roots.append(captures)
        unique: list[Path] = []
        for root in roots:
            resolved = root.resolve()
            if resolved not in unique:
                unique.append(resolved)
        return unique

    def contain_artifact(self, raw: str) -> str | None:
        """Returns a workspace-relative path when ``raw`` is an approved image.

        Approved means: resolves to an existing file, carries an image
        extension, and lives inside one of :meth:`artifact_roots`, all of
        which are inside the workspace, so the returned relative path always
        rejoins against the workspace. Everything else is rejected, so
        arbitrary local files can never be forwarded to the reviewer as
        artifacts.
        """
        candidate = str(raw or "").strip()
        if not candidate:
            return None
        try:
            path = Path(candidate).expanduser().resolve()
        except (OSError, ValueError):
            return None
        if not path.is_file() or path.suffix.lower() not in CAPTURE_EXTENSIONS:
            return None
        workspace = self.workspace.resolve()
        for root in self.artifact_roots():
            try:
                path.relative_to(root)
            except ValueError:
                continue
            try:
                return str(path.relative_to(workspace)).replace("\\", "/")
            except ValueError:
                return None
        return None

    def watched_dirs(self) -> list[Path]:
        """Own live dir plus every registered live dir (cached briefly)."""
        now = time.time()
        cached_at, cached = self._watch_cache
        if cached and now - cached_at < WATCH_REFRESH_SECONDS:
            return list(cached)
        dirs: list[Path] = [self.live_dir]
        try:
            for entry in known_live_dirs():
                candidate = Path(entry["path"])
                if candidate not in dirs:
                    dirs.append(candidate)
        except (OSError, KeyError, TypeError, ValueError):
            # A corrupt or unreadable registry must not break the own live view.
            pass
        self._watch_cache = (now, tuple(dirs))
        return dirs


# --------------------------------------------------------------------------
# Instance discovery and the shutdown handshake


def existing_viewer(live_dir: Path) -> dict[str, Any] | None:
    """Returns {host, port, pid, token?, auth_token?} when a live viewer already
    owns this workspace.

    Liveness is established by an HTTP probe; ``token`` is the per-instance
    secret from the manifest used for the authenticated shutdown handshake.
    """
    try:
        data = json.loads(manifest_path(live_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        port = int(data.get("port") or 0)
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if not port:
        return None
    host = client_host(str(data.get("host") or "127.0.0.1"))
    try:
        with urlopen(
            f"http://{url_host(host)}:{port}/health", timeout=HEALTH_PROBE_TIMEOUT_SECONDS
        ) as response:
            if response.status != 200:
                return None
    except (OSError, http.client.HTTPException, ValueError):
        return None
    info: dict[str, Any] = {"port": port, "pid": pid, "host": host}
    token = str(data.get("token") or "")
    if token:
        info["token"] = token
    auth = str(data.get("auth_token") or "")
    if auth:
        info["auth_token"] = auth
    return info


def request_shutdown(
    port: int, instance_token: str, auth_token: str = "", host: str = "127.0.0.1"
) -> bool:
    """Asks the viewer on ``port`` to shut down gracefully.

    Sends the instance secret in the dedicated ``X-Instance-Token`` header,
    plus the operator bearer when the target bound with ``--auth-token``: one
    Authorization header cannot carry both secrets.
    """
    if not port or not instance_token:
        return False
    headers = {
        SESSION_HEADER: "1",
        INSTANCE_TOKEN_HEADER: instance_token,
        "Authorization": f"Bearer {auth_token or instance_token}",
    }
    request = Request(
        f"http://{url_host(host)}:{port}/api/shutdown",
        data=b"{}",
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=HANDSHAKE_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (OSError, http.client.HTTPException, ValueError):
        return False


# --------------------------------------------------------------------------
# HTTP server


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(
        self,
        address: tuple[str, int],
        context_factory: Callable[[int], ViewerContext],
    ) -> None:
        # Set before binding: socketserver creates the socket from this.
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, ViewerHandler)
        try:
            self.context = context_factory(int(self.server_address[1]))
        except BaseException:
            self.server_close()
            raise


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = 60
    _body: bytes = b""

    @property
    def context(self) -> ViewerContext:
        return self.server.context  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    # ------------------------------------------------------------- responses

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_bytes(
        self, status: int, body: bytes, content_type: str, cache: str = "no-store"
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self._security_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionError:
            pass

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_html(self, status: int, html: str) -> None:
        self._send_bytes(status, html.encode("utf-8"), "text/html; charset=utf-8")

    # ----------------------------------------------------------------- guards

    def _authorized(self, query: Query | None = None) -> bool:
        """Bearer-token gate; enforced only when the server bound non-loopback.

        Query-string tokens are accepted only on ``/events`` (EventSource
        cannot set headers); elsewhere a URL token would leak into proxy logs
        and browser history. Comparisons use ``secrets.compare_digest`` to
        avoid timing leaks.
        """
        token = self.context.auth_token
        if not token:
            return True
        header = self.headers.get("Authorization") or ""
        if secrets.compare_digest(header, f"Bearer {token}"):
            return True
        if query:
            supplied = query.get("token") or []
            parsed = urlparse(self.path)
            if parsed.path == "/events" and any(
                secrets.compare_digest(str(candidate), token) for candidate in supplied
            ):
                return True
        return False

    def _check_host(self) -> bool:
        """DNS-rebinding guard for loopback bindings: Host must be loopback."""
        if self.context.auth_token:
            return True
        host = _host_from_header(self.headers.get("Host") or "")
        if not host:
            return True
        return host in LOOPBACK_HOSTS

    def _guard_post(self) -> bool:
        """Session header, origin allow-list and bounded body; reads the body."""
        if self.headers.get(SESSION_HEADER) != "1":
            self._send_json(403, {"error": "missing local session header"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in allowed_origins(self.context.port):
            self._send_json(403, {"error": "origin not allowed"})
            return False
        raw_length = (self.headers.get("Content-Length") or "").strip()
        if not raw_length:
            self._send_json(411, {"error": "Content-Length required"})
            return False
        try:
            length = int(raw_length)
        except ValueError:
            length = -1
        if length < 0:
            self._send_json(400, {"error": "invalid Content-Length"})
            return False
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": f"body exceeds {MAX_BODY_BYTES} byte limit"})
            return False
        self._body = self.rfile.read(length) if length else b""
        return True

    def _json_body(self) -> dict[str, Any] | None:
        """The request body as a JSON object; sends a 400 and returns None otherwise."""
        try:
            payload = json.loads(self._body or b"{}")
        except ValueError:
            self._send_json(400, {"error": "invalid JSON body"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be an object"})
            return None
        return payload

    # ---------------------------------------------------------------- routing

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if not self._check_host():
            self._send_json(403, {"error": "host header not allowed"})
            return
        if route == "/health":
            # Deliberately minimal: this endpoint is unauthenticated by design
            # (liveness probe), so it exposes no paths, workspace, or state.
            self._send_json(200, {"status": "ok", "port": self.context.port})
            return
        if not self._authorized(query):
            self._send_json(401, {"error": "unauthorized"})
            return
        handler = self._GET_ROUTES.get(route)
        if handler is not None:
            handler(self, query)
            return
        for prefix, prefix_handler in self._GET_PREFIX_ROUTES:
            if route.startswith(prefix):
                prefix_handler(self, route[len(prefix) :])
                return
        self._send_json(404, {"error": f"not found: {route}"})

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if not self._check_host():
            self._send_json(403, {"error": "host header not allowed"})
            return
        if route == "/api/shutdown":
            # Authenticates with its own token scheme (see _handle_shutdown).
            self._handle_shutdown()
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if not self._guard_post():
            return
        handler = self._POST_ROUTES.get(route)
        if handler is not None:
            handler(self)
            return
        thread_prefix, pin_suffix = "/api/chat/thread/", "/pin"
        if route.startswith(thread_prefix) and route.endswith(pin_suffix):
            self._post_pin(route[len(thread_prefix) : -len(pin_suffix)].strip("/"))
            return
        self._send_json(404, {"error": f"not found: {route}"})

    # ------------------------------------------------------------- GET routes

    def _get_index(self, query: Query) -> None:
        try:
            html = HTML_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            self._send_html(500, f"<h1>viewer html missing</h1><pre>{exc}</pre>")
            return
        self._send_html(200, html)

    def _get_health_details(self, query: Query) -> None:
        watched = self.context.watched_dirs()
        run = newest_run_across(watched)
        self._send_json(
            200,
            {
                "status": "ok",
                "watching": run.name if run else None,
                "live_dir": str(self.context.live_dir),
                "watched_dirs": [str(directory) for directory in watched],
                "port": self.context.port,
            },
        )

    def _get_status(self, query: Query) -> None:
        self._send_json(200, collect_status(self.context))

    def _get_runs(self, query: Query) -> None:
        watched = self.context.watched_dirs()
        merged: list[dict[str, Any]] = []
        for directory in watched:
            workspace_root = str(Path(directory).parent.parent)
            for record in list_runs(directory):
                record["workspace"] = workspace_root
                record["live_dir"] = str(directory)
                record["is_foreign"] = Path(directory) != self.context.live_dir
                merged.append(record)
        merged.sort(
            key=lambda record: (record.get("mtime") or 0, record.get("file") or ""),
            reverse=True,
        )
        self._send_json(
            200,
            {
                "runs": merged[:RUN_LIST_LIMIT],
                "live_dir": str(self.context.live_dir),
                "watched_dirs": [str(directory) for directory in watched],
            },
        )

    def _get_assets(self, query: Query) -> None:
        self._send_json(200, {"assets": self.context.assets})

    def _get_models(self, query: Query) -> None:
        self._send_json(200, list_models())

    def _get_model(self, query: Query) -> None:
        self._send_json(200, read_viewer_settings(self.context.live_dir))

    def _get_threads(self, query: Query) -> None:
        store = ChatStore(self.context.live_dir)
        self._send_json(200, {"threads": store.list_threads()})

    def _get_events(self, query: Query) -> None:
        self._serve_events(query.get("file", [None])[0])

    def _get_thread(self, rest: str) -> None:
        thread_id = rest.strip("/").split("/")[0]
        store = ChatStore(self.context.live_dir)
        thread = store.load(thread_id) if valid_thread_id(thread_id) else None
        if thread is None:
            self._send_json(404, {"error": "thread not found"})
            return
        self._send_json(200, {"thread": thread})

    def _get_capture(self, rest: str) -> None:
        name = unquote(rest)
        safe = name.replace("\\", "/").rsplit("/", 1)[-1]
        if not safe or safe != name:
            self._send_json(404, {"error": "capture not found"})
            return
        path = capture_dir(self.context.live_dir) / safe
        if path.suffix.lower() not in CAPTURE_EXTENSIONS or not path.is_file():
            self._send_json(404, {"error": "capture not found"})
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "capture unreadable"})
            return
        content_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        self._send_bytes(200, body, content_type, cache="no-store")

    def _get_asset(self, rest: str) -> None:
        safe = unquote(rest).replace("\\", "/").rsplit("/", 1)[-1]
        if safe not in self.context.assets.values():
            self._send_json(404, {"error": "asset not found"})
            return
        try:
            body = (ASSET_DIR / safe).read_bytes()
        except OSError:
            self._send_json(404, {"error": "asset unreadable"})
            return
        content_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        self._send_bytes(200, body, content_type, cache="public, max-age=3600")

    # ------------------------------------------------------------ POST routes

    def _post_account_switch(self) -> None:
        self._send_json(200, switch_account())

    def _post_account_recheck(self) -> None:
        self._send_json(200, {"auth": collect_auth_state()})

    def _post_model(self) -> None:
        payload = self._json_body()
        if payload is None:
            return
        requested = {
            key: payload[key].strip()
            for key in MODEL_SETTING_KEYS
            if isinstance(payload.get(key), str) and payload[key].strip()
        }
        inventory = list_models()["models"]
        unknown = [key for key, value in requested.items() if not _MODEL_NAME_PATTERN.match(value)]
        if not unknown and not payload.get("allow_custom"):
            unknown = [key for key, value in requested.items() if value not in inventory]
        if unknown:
            self._send_json(
                400,
                {
                    "error": f"unknown model for: {', '.join(unknown)}",
                    "known_models": inventory,
                    "hint": "resubmit with allow_custom=true to force a custom id",
                },
            )
            return
        settings = write_viewer_settings(self.context.live_dir, payload)
        for key in MODEL_SETTING_KEYS:
            value = settings.get(key)
            settings[f"{key}_known"] = bool(value) and value in inventory
        self._send_json(200, settings)

    def _post_capture(self) -> None:
        payload = self._json_body()
        if payload is None:
            return
        if not self.context.capture_lock.acquire(blocking=False):
            self._send_json(
                409, {"error": "busy", "message": "A capture is already in progress."}
            )
            return
        try:
            mode = str(payload.get("mode") or "auto")
            result = capture_auto(self.context.live_dir, prefer_selection=(mode != "snip"))
            if result.get("kind") == "image" and result.get("path"):
                result["rel_path"] = relative_to_workspace(
                    Path(result["path"]), self.context.workspace
                )
        except OSError as exc:
            self._send_json(500, {"error": f"capture failed: {exc}"})
            return
        finally:
            _release(self.context.capture_lock)
        self._send_json(200, result)

    def _post_upload(self) -> None:
        payload = self._json_body()
        if payload is None:
            return
        data = str(payload.get("data") or "")
        if not data:
            self._send_json(400, {"error": "missing image data"})
            return
        try:
            blob = base64.b64decode(data, validate=True)
        except ValueError:
            self._send_json(400, {"error": "invalid base64 image data"})
            return
        if not blob:
            self._send_json(400, {"error": "empty image"})
            return
        if len(blob) > MAX_UPLOAD_BYTES:
            limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
            self._send_json(413, {"error": f"image exceeds {limit_mb} MB limit"})
            return
        try:
            saved = save_pasted_image(self.context.live_dir, blob)
        except OSError as exc:
            self._send_json(500, {"error": f"could not save the image: {exc}"})
            return
        if saved is None:
            self._send_json(400, {"error": "unsupported image format"})
            return
        self._send_json(
            200,
            {
                "kind": "image",
                "path": str(saved),
                "rel_path": relative_to_workspace(saved, self.context.workspace),
                "name": saved.name,
            },
        )

    def _post_pin(self, thread_id: str) -> None:
        payload = self._json_body()
        if payload is None:
            return
        store = ChatStore(self.context.live_dir)
        thread = (
            store.set_pinned(thread_id, bool(payload.get("pinned")))
            if valid_thread_id(thread_id)
            else None
        )
        if thread is None:
            self._send_json(404, {"error": "thread not found"})
            return
        self._send_json(200, {"thread": thread})

    def _post_ask(self) -> None:
        payload = self._json_body()
        if payload is None:
            return
        context = self.context
        prompt = str(payload.get("prompt") or "").strip() or ASK_DEFAULT_PROMPT
        if len(prompt) > MAX_MESSAGE_CHARS:
            # Validate before taking ask_lock: an oversized prompt would make
            # append_message raise while the lock is held.
            self._send_json(
                400,
                {"error": f"prompt exceeds MAX_MESSAGE_CHARS ({MAX_MESSAGE_CHARS})"},
            )
            return
        context_text = str(payload.get("context_text") or "").strip()
        image_paths = self._contained_ask_images(payload)
        if image_paths is None:
            return
        model = str(payload.get("model") or "").strip()
        if model:
            known, known_models = _model_known(model)
            if not known and not payload.get("allow_custom"):
                self._send_json(
                    400, {"error": f"unknown model: {model}", "known_models": known_models}
                )
                return
        settings = read_viewer_settings(context.live_dir)
        skills_raw = payload.get("skills")
        skills = tuple(
            _clean_skills(skills_raw)
            if isinstance(skills_raw, list)
            else settings.get("ask_skills") or ()
        )

        store = ChatStore(context.live_dir)
        thread_id = str(payload.get("thread_id") or "").strip()
        existing: dict[str, Any] | None = None
        if thread_id:
            existing = store.load(thread_id) if valid_thread_id(thread_id) else None
            if existing is None:
                self._send_json(404, {"error": "thread not found"})
                return

        if not context.ask_lock.acquire(blocking=False):
            self._send_json(
                409,
                {
                    "error": "busy",
                    "message": "Wisp is already answering; wait for the current ask.",
                    "thread_id": (context.active_ask or {}).get("thread_id"),
                },
            )
            return
        thread = self._record_ask(store, thread_id, existing, prompt, context_text, image_paths)
        if thread is None:
            return
        threading.Thread(
            target=_ask_worker,
            args=(context, thread["id"], prompt, context_text, tuple(image_paths), model, skills),
            daemon=True,
        ).start()
        self._send_json(
            200,
            {
                "thread_id": thread["id"],
                "status": "running",
                "model": _effective_ask_model(model, settings),
            },
        )

    def _contained_ask_images(self, payload: Mapping[str, Any]) -> list[str] | None:
        """Workspace-relative paths for the ask's images (``image_path`` first,
        then ``images``, deduplicated). Sends a 400 and returns None if any
        path is not an approved image."""
        requested: list[str] = []
        image_path = str(payload.get("image_path") or "").strip()
        if image_path:
            requested.append(image_path)
        images = payload.get("images")
        if isinstance(images, list):
            requested.extend(str(item or "").strip() for item in images)
        contained: list[str] = []
        rejected: list[str] = []
        for raw in dict.fromkeys(path for path in requested if path):
            relative = self.context.contain_artifact(raw)
            if relative is None:
                rejected.append(raw)
            else:
                contained.append(relative)
        if rejected:
            self._send_json(
                400,
                {
                    "error": (
                        "image paths must be existing files inside the workspace or "
                        f"capture directory with one of: {', '.join(sorted(CAPTURE_EXTENSIONS))}"
                    ),
                    "rejected": rejected,
                },
            )
            return None
        return contained

    def _record_ask(
        self,
        store: ChatStore,
        thread_id: str,
        existing: dict[str, Any] | None,
        prompt: str,
        context_text: str,
        image_paths: list[str],
    ) -> dict[str, Any] | None:
        """Records the user's message while the caller holds ``ask_lock``.

        Create the thread only once the lock is held, so a 409 never leaves an
        empty thread behind; any failure below releases the lock. Returns the
        thread, or None after answering 500.
        """
        context = self.context
        try:
            # Reload a follow-up's thread: it may have changed while unlocked.
            thread = (store.load(thread_id) or existing) if thread_id else store.create(prompt)
            if thread is None:
                raise OSError("the chat thread could not be saved")
            context.active_ask = {"thread_id": thread["id"], "started": time.time()}
            recorded = store.append_message(
                thread["id"],
                "user",
                prompt,
                {
                    "context_text": context_text[:MAX_ASK_CONTEXT_CHARS],
                    "image_path": image_paths[0] if image_paths else "",
                    "image_paths": image_paths,
                },
            )
            if recorded is None:
                raise OSError("the chat thread could not be saved")
            store.update_run(thread["id"], {"status": "running", "started": time.time()})
        except Exception as exc:
            # Deliberately broad: anything escaping here would leak ask_lock
            # and wedge /api/ask with 409s until the viewer restarts.
            context.active_ask = {}
            _release(context.ask_lock)
            self._send_json(500, {"error": f"failed to record the ask: {exc}"})
            return None
        return thread

    def _handle_shutdown(self) -> None:
        """Authenticated shutdown handshake used by ``--replace``.

        With an operator bearer token, the request must carry ``Authorization:
        Bearer <auth_token>`` AND the instance secret in ``X-Instance-Token``;
        one Authorization header cannot match two distinct secrets. Without an
        operator token, the bearer is the instance token. A stale port reused
        by an unrelated process can never produce the instance secret.
        """
        expected = self.context.instance_token
        if not expected:
            self._send_json(404, {"error": "not found"})
            return
        auth_header = self.headers.get("Authorization") or ""
        if self.context.auth_token:
            if not secrets.compare_digest(auth_header, f"Bearer {self.context.auth_token}"):
                self._send_json(401, {"error": "unauthorized"})
                return
            supplied = self.headers.get(INSTANCE_TOKEN_HEADER) or ""
        else:
            supplied = auth_header[len("Bearer ") :] if auth_header.startswith("Bearer ") else ""
        if not supplied or not secrets.compare_digest(supplied, expected):
            self._send_json(403, {"error": "invalid instance token"})
            return
        self._send_json(200, {"status": "shutting-down"})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    # ------------------------------------------------------------ live events

    def _send_event(self, event: str, data: str) -> bool:
        try:
            self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
        except OSError:
            return False
        return True

    def _send_keepalive(self) -> bool:
        try:
            self.wfile.write(b": keepalive\n\n")
            self.wfile.flush()
        except OSError:
            return False
        return True

    def _serve_events(self, replay_file: str | None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._security_headers()
        self.end_headers()
        send: SendEvent = self._send_event
        if not send("hello", json.dumps({"status": "ready", "port": self.context.port})):
            return
        try:
            if replay_file:
                self._stream_replay(replay_file, send)
            else:
                self._stream_live(send)
        except OSError:
            return

    def _stream_replay(self, replay_file: str, send: SendEvent) -> None:
        safe = replay_file.replace("\\", "/").rsplit("/", 1)[-1]
        if not _RUN_FILE_PATTERN.fullmatch(safe):
            send("replay_end", json.dumps({"error": "invalid run file"}))
            return
        path = next(
            (
                directory / safe
                for directory in self.context.watched_dirs()
                if (directory / safe).is_file()
            ),
            None,
        )
        if path is None:
            send("replay_end", json.dumps({"error": "run file not found"}))
            return
        if not send("reset", json.dumps({"file": safe, "replay": True})):
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if not send("live", stripped):
                        return
                    time.sleep(REPLAY_LINE_DELAY_SECONDS)
        except OSError:
            send("replay_end", json.dumps({"error": "run file unreadable"}))
            return
        send("replay_end", json.dumps({"file": safe}))

    def _stream_live(self, send: SendEvent) -> None:
        """Tails the newest run, latching onto it until it ends or goes idle.

        Latching keeps concurrent runs from thrashing the view: the stream
        only switches files once the current run has finished or has been
        silent for LIVE_LATCH_IDLE_SECONDS.
        """
        current: Path | None = None
        offset = 0
        buffer = ""
        current_done = False
        last_data = time.time()
        last_keepalive = time.time()
        while True:
            now = time.time()
            candidate = newest_run_across(self.context.watched_dirs())
            if current is None:
                should_switch = candidate is not None
            elif current_done or now - last_data >= LIVE_LATCH_IDLE_SECONDS:
                should_switch = candidate is not None and candidate.name != current.name
            else:
                should_switch = False
            if should_switch and candidate is not None:
                current = candidate
                offset = 0
                buffer = ""
                current_done = False
                last_data = now
                if not send("reset", json.dumps({"file": current.name})):
                    return
            if current is None:
                if now - last_keepalive > KEEPALIVE_SECONDS:
                    if not self._send_keepalive():
                        return
                    last_keepalive = now
                time.sleep(POLL_INTERVAL_SECONDS * 2)
                continue
            try:
                with open(current, "rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
            except OSError:
                chunk = b""
            if chunk:
                offset += len(chunk)
                last_data = last_keepalive = time.time()
                buffer += chunk.decode("utf-8", errors="replace")
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if is_run_end_line(stripped):
                        current_done = True
                    if not send("live", stripped):
                        return
            elif time.time() - last_keepalive > KEEPALIVE_SECONDS:
                if not self._send_keepalive():
                    return
                last_keepalive = time.time()
            time.sleep(POLL_INTERVAL_SECONDS)

    # ----------------------------------------------------------- route tables

    _GET_ROUTES: ClassVar[dict[str, Callable[[ViewerHandler, Query], None]]] = {
        "/": _get_index,
        "/health/details": _get_health_details,
        "/api/status": _get_status,
        "/api/runs": _get_runs,
        "/api/assets": _get_assets,
        "/api/models": _get_models,
        "/api/model": _get_model,
        "/api/chat/threads": _get_threads,
        "/events": _get_events,
    }
    # Checked in order after the exact routes; the handler receives the path
    # remainder after the prefix.
    _GET_PREFIX_ROUTES: ClassVar[tuple[tuple[str, Callable[[ViewerHandler, str], None]], ...]] = (
        ("/api/chat/thread/", _get_thread),
        ("/captures/", _get_capture),
        ("/assets/", _get_asset),
    )
    _POST_ROUTES: ClassVar[dict[str, Callable[[ViewerHandler], None]]] = {
        "/api/account/switch": _post_account_switch,
        "/api/account/recheck": _post_account_recheck,
        "/api/model": _post_model,
        "/api/capture": _post_capture,
        "/api/chat/upload": _post_upload,
        "/api/ask": _post_ask,
    }


def bind_server(
    host: str,
    preferred_port: int,
    context_factory: Callable[[int], ViewerContext],
    max_attempts: int = PORT_SCAN_RANGE,
) -> tuple[ViewerServer, int]:
    """Binds the first free port from ``preferred_port`` upward."""
    last_error: OSError | None = None
    for candidate in range(preferred_port, preferred_port + max_attempts):
        try:
            server = ViewerServer((host, candidate), context_factory)
        except OSError as exc:
            last_error = exc
            continue
        return server, int(server.server_address[1])
    raise OSError(
        f"no free port in {preferred_port}-{preferred_port + max_attempts - 1}: {last_error}"
    )


# --------------------------------------------------------------------------
# Command line


def _write_viewer_manifest(live_dir: Path, payload: dict[str, Any]) -> None:
    """Best effort: without a manifest the viewer still serves, but
    ``--replace`` and duplicate detection cannot find it."""
    try:
        write_json_atomic(manifest_path(live_dir), payload)
    except OSError:
        pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="antigravity_viewer.py",
        description="Local live viewer for Antigravity delegation telemetry.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Non-loopback binding requires --auth-token or --generate-token.",
    )
    parser.add_argument(
        "--auth-token",
        default="",
        help="Bearer token required on every route when bound non-loopback.",
    )
    parser.add_argument(
        "--generate-token",
        action="store_true",
        help="Generate and print a random bearer token, then require it on every route.",
    )
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--live-dir", default=None)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Terminate an existing viewer for this workspace before starting.",
    )
    parser.add_argument(
        "--shell",
        choices=("auto", "electron", "native", "browser"),
        default="auto",
        help="Widget shell: electron (frameless transparent, preferred), native (pywebview), browser, or auto (default).",
    )
    parser.add_argument("--width", type=int, default=240)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument(
        "--transparent",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Native shell only: attempt a transparent widget background (not supported on all WebView2 builds).",
    )
    parser.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Open the widget window (default: on).",
    )
    return parser


def _replace_existing_viewer(
    existing: dict[str, Any], live_dir: Path, auth_token: str
) -> int | None:
    """Stops the viewer described by ``existing``.

    Returns None when startup may proceed, or an exit code when the viewer
    must not start (the target could not be safely identified).
    """
    pid = existing["pid"]
    instance = str(existing.get("token") or "")
    # The target viewer's own auth token (from its manifest) outranks the new
    # invocation's: it must match the running process, not these arguments.
    target_auth = str(existing.get("auth_token") or "") or auth_token
    if instance and request_shutdown(
        existing["port"], instance, auth_token=target_auth, host=existing["host"]
    ):
        print(
            f"[wisp-viewer] sent authenticated shutdown to the viewer on "
            f"port {existing['port']}"
        )
        time.sleep(REPLACE_SETTLE_SECONDS)
        return None
    image = pid_image_name(pid)
    if not looks_like_viewer_process(image, pid=pid):
        print(
            f"[wisp-viewer] refusing to replace: the manifest PID {pid} does not "
            f"look like a viewer process ({image or 'gone'}) and no instance token "
            f"is available; the port is likely owned by another program. Remove "
            f"{manifest_path(live_dir)} if this is stale.",
            file=sys.stderr,
        )
        return 2
    print(
        f"[wisp-viewer] legacy manifest without instance token; pid {pid} "
        f"verified as a viewer process ({image}); forcing."
    )
    if terminate_process(pid):
        time.sleep(REPLACE_SETTLE_SECONDS)
    return None


def _run_widget_shell(
    shell: str, url: str, width: int, height: int, transparent: bool
) -> bool:
    """Opens the widget window for ``shell``.

    Returns True when a blocking shell (Electron or pywebview) ran until its
    window closed, meaning the viewer should exit; False when the page was
    handed to a browser and the server should keep serving.
    """
    if shell in ("auto", "electron"):
        electron = open_electron_window(url)
        if electron is not None:
            print(f"[wisp-viewer] electron widget on {url}")
            try:
                electron.wait()
            except KeyboardInterrupt:
                pass
            return True
        if shell == "electron":
            print(
                "[wisp-viewer] electron shell unavailable (run npm install in tools/wisp_shell)",
                file=sys.stderr,
            )
    if shell in ("auto", "native"):
        try:
            if open_native_window(url, width, height, transparent=transparent):
                return True
        except Exception as exc:
            # GUI backends fail in backend-specific ways; fall back to a browser.
            print(f"[wisp-viewer] native shell failed: {exc}", file=sys.stderr)
    if not open_app_window(url, *BROWSER_WINDOW_SIZE):
        print(f"[wisp-viewer] open {url} in your browser", file=sys.stderr)
    return False


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    workspace = resolve_workspace(args.workspace)
    live_dir = resolve_live_dir(workspace, args.live_dir)

    auth_token = str(args.auth_token or "").strip()
    if args.generate_token:
        auth_token = secrets.token_urlsafe(32)
    # Accept "[::1]" as well as "::1"; sockets want the bare literal.
    host = str(args.host or "").strip().strip("[]")
    non_loopback = host not in LOOPBACK_HOSTS
    if non_loopback and not auth_token:
        print(
            "[wisp-viewer] refusing to bind a non-loopback host without an auth "
            "token: pass --auth-token <secret> or --generate-token.",
            file=sys.stderr,
        )
        return 2

    existing = existing_viewer(live_dir)
    if existing:
        if not args.replace:
            print(
                f"[wisp-viewer] already running on port {existing['port']} "
                f"(pid {existing['pid']}); use --replace to restart it."
            )
            return 0
        refused = _replace_existing_viewer(existing, live_dir, auth_token)
        if refused is not None:
            return refused

    def context_factory(port: int) -> ViewerContext:
        return ViewerContext(workspace=workspace, live_dir=live_dir, port=port)

    try:
        server, port = bind_server(host, args.port, context_factory)
    except OSError as exc:
        print(f"[wisp-viewer] failed to bind: {exc}", file=sys.stderr)
        return 1

    instance_token = secrets.token_urlsafe(24)
    server.context.auth_token = auth_token
    server.context.instance_token = instance_token

    url = f"http://{url_host(host)}:{port}/"
    manifest: dict[str, Any] = {
        "host": host,
        "port": port,
        "url": url,
        "pid": os.getpid(),
        "token": instance_token,
        "workspace": str(workspace),
        "started": time.time(),
    }
    if auth_token:
        # Same local trust boundary as the instance token; without it a later
        # --replace could not authenticate its shutdown request.
        manifest["auth_token"] = auth_token
    _write_viewer_manifest(live_dir, manifest)
    print(f"[wisp-viewer] serving on {url} (live dir: {live_dir})")
    if non_loopback:
        print("[wisp-viewer] NON-LOOPBACK BINDING: bearer token required on every route.")
        if args.generate_token:
            print(f"[wisp-viewer] generated auth token: {auth_token}")
    print("[wisp-viewer] watching for delegations; Ctrl+C to stop.")

    def _shutdown(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGINT, _shutdown)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _shutdown)
    except (ValueError, OSError):
        # Not the main thread (e.g. embedded in a test); Ctrl+C still works.
        pass

    serving_thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": SERVE_POLL_SECONDS}, daemon=True
    )
    serving_thread.start()
    try:
        if args.open and _run_widget_shell(
            args.shell, url, args.width, args.height, args.transparent
        ):
            return 0
        while serving_thread.is_alive():
            serving_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
