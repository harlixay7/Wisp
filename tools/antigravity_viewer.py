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
identically whether or not this process is running.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import urlopen as _urlopen

try:
    from tools.antigravity_bridge import (
        DEFAULT_FALLBACK_MODEL,
        DEFAULT_PRIMARY_MODEL,
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
    from tools.wisp_capture import (
        capture_auto,
        relative_to_workspace,
        save_pasted_image,
    )
    from tools.wisp_chat import ChatStore, valid_thread_id
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.antigravity_bridge import (  # type: ignore[no-redef]
        DEFAULT_FALLBACK_MODEL,
        DEFAULT_PRIMARY_MODEL,
        AttemptResult,
        BridgeConfig,
        DelegationEnvelope,
        resolve_agy_executable,
        run_bridge,
        write_report,
    )
    from tools.antigravity_live import (  # type: ignore[no-redef]
        LIVE_DIR_NAME,
        known_live_dirs,
    )
    from tools.skill_loader import (  # type: ignore[no-redef]
        DEFAULT_SKILL_DIR,
        SkillError,
        SkillLoader,
        resolve_skill_dir,
    )
    from tools.wisp_capture import (  # type: ignore[no-redef]
        capture_auto,
        relative_to_workspace,
        save_pasted_image,
    )
    from tools.wisp_chat import (  # type: ignore[no-redef]
        ChatStore,
        valid_thread_id,
    )

SERVER_NAME = "wisp-viewer"
SERVER_VERSION = "1.0.0"
DEFAULT_PORT = 48477
PORT_SCAN_RANGE = 50
SESSION_HEADER = "X-Wisp-Request"
POLL_INTERVAL_SECONDS = 0.15
KEEPALIVE_SECONDS = 10.0
REPLAY_LINE_DELAY_SECONDS = 0.04
LIVE_LATCH_IDLE_SECONDS = 15.0
WATCH_REFRESH_SECONDS = 2.0
MAX_LOG_TAIL_BYTES = 400_000
RATE_LIMIT_WINDOW_SECONDS = 45 * 60

TOOLS_DIR = Path(__file__).resolve().parent
ASSET_DIR = TOOLS_DIR / "viewer_assets"
HTML_PATH = TOOLS_DIR / "antigravity_viewer.html"
SHELL_DIR = TOOLS_DIR / "wisp_shell"
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

_RATE_LIMIT_MARKERS = ("RESOURCE_EXHAUSTED", "code 429", "Individual quota reached")
_RESET_PATTERN = re.compile(r"Resets in ([0-9]+\s*[smhdw][0-9]*\s*[smhdw]*)", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(
    r"authenticated successfully as ([A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+)"
)


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
DEFAULT_SELECTED_MODEL = "gemini-3.8-flash-high"
DEFAULT_SELECTED_FALLBACK = "claude-opus-4-6-thinking"
_MODEL_CACHE: dict[str, Any] = {"ts": 0.0, "models": []}
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_model_list(output: str) -> list[str]:
    names: list[str] = []
    for line in (output or "").splitlines():
        name = line.strip().split("\t")[0].split("  ")[0].strip()
        if name and _MODEL_NAME_PATTERN.match(name) and name not in names:
            names.append(name)
    return names


def list_models() -> dict[str, Any]:
    now = time.time()
    cached = _MODEL_CACHE.get("models") or []
    if cached and now - float(_MODEL_CACHE.get("ts") or 0) < 600:
        return {"models": cached, "source": "cache"}
    try:
        result = subprocess.run(
            [resolve_agy_executable(), "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
        )
        models = parse_model_list(result.stdout or "")
        if models:
            _MODEL_CACHE["models"] = models
            _MODEL_CACHE["ts"] = now
            return {"models": models, "source": "agy"}
    except Exception:
        pass
    return {"models": list(FALLBACK_MODELS), "source": "fallback"}


def _model_known(model: str) -> tuple[bool, list[str]]:
    """Checks a model id against the current inventory (cached by list_models)."""
    known = list_models().get("models") or []
    return (model in known), list(known)


def settings_path(live_dir: Path) -> Path:
    return Path(live_dir) / "viewer_settings.json"


def read_viewer_settings(live_dir: Path) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "model": DEFAULT_SELECTED_MODEL,
        "fallback_model": DEFAULT_SELECTED_FALLBACK,
        "chat_model": "",
        "ask_skills": [],
    }
    try:
        data = json.loads(settings_path(live_dir).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if isinstance(data.get("model"), str) and data["model"].strip():
                settings["model"] = data["model"].strip()
            if isinstance(data.get("chat_model"), str) and data["chat_model"].strip():
                settings["chat_model"] = data["chat_model"].strip()
            if (
                isinstance(data.get("fallback_model"), str)
                and data["fallback_model"].strip()
            ):
                settings["fallback_model"] = data["fallback_model"].strip()
            skills = data.get("ask_skills")
            if isinstance(skills, list):
                settings["ask_skills"] = [
                    str(skill).strip() for skill in skills if str(skill).strip()
                ][:32]
    except (OSError, json.JSONDecodeError):
        pass
    return settings


def write_viewer_settings(live_dir: Path, patch: dict[str, Any]) -> dict[str, Any]:
    settings = read_viewer_settings(live_dir)
    for key in ("model", "fallback_model", "chat_model"):
        value = patch.get(key)
        if isinstance(value, str) and value.strip() and _MODEL_NAME_PATTERN.match(value.strip()):
            settings[key] = value.strip()
    if "ask_skills" in patch:
        skills = patch.get("ask_skills")
        if isinstance(skills, list):
            settings["ask_skills"] = [
                str(skill).strip() for skill in skills if str(skill).strip()
            ][:32]
        elif skills == "all":
            settings["ask_skills"] = ["all"]
    try:
        path = settings_path(live_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass
    return settings


def resolve_workspace(override: str | None = None) -> Path:
    if override and str(override).strip():
        return Path(str(override)).expanduser().resolve()
    env_workspace = os.environ.get("ANTIGRAVITY_WORKSPACE")
    if env_workspace:
        return Path(env_workspace).expanduser().resolve()
    return Path.cwd().resolve()


def resolve_live_dir(workspace: Path, override: str | None = None) -> Path:
    if override and str(override).strip():
        return Path(str(override)).expanduser().resolve()
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


def newest_run(live_dir: Path) -> Path | None:
    try:
        runs = [
            path
            for path in live_dir.glob("run-*.jsonl")
            if path.is_file()
        ]
    except OSError:
        return None
    if not runs:
        return None
    runs.sort(key=lambda path: (path.stat().st_mtime, path.name))
    return runs[-1]


def newest_run_across(dirs: Sequence[Path]) -> Path | None:
    """Newest run across several live directories (ordered by run id, then mtime)."""
    best: Path | None = None
    best_key: tuple[str, float] | None = None
    for directory in dirs:
        candidate = newest_run(directory)
        if candidate is None:
            continue
        try:
            key = (candidate.name, candidate.stat().st_mtime)
        except OSError:
            key = (candidate.name, 0.0)
        if best_key is None or key > best_key:
            best = candidate
            best_key = key
    return best


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
            handle.seek(max(0, size - 16384))
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


def list_runs(live_dir: Path, limit: int = 60) -> list[dict[str, Any]]:
    try:
        runs = [path for path in live_dir.glob("run-*.jsonl") if path.is_file()]
    except OSError:
        return []
    runs.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    entries: list[dict[str, Any]] = []
    for path in runs[:limit]:
        record = describe_run(path)
        if record:
            entries.append(record)
    return entries


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
    log_dirs = (
        Path.home() / ".gemini" / "antigravity-cli" / "log",
        Path.home() / ".gemini" / "antigravity" / "log",
    )
    logs: list[Path] = []
    for directory in log_dirs:
        if directory.is_dir():
            try:
                logs.extend(path for path in directory.glob("*.log") if path.is_file())
            except OSError:
                continue
    logs.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    now = time.time()
    for path in logs[:20]:
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


def collect_status(context: "ViewerContext") -> dict[str, Any]:
    auth = collect_auth_state()
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
        "test_mode": os.environ.get("WISP_ASK_FAKE") == "1",
        "watched_dirs": [str(directory) for directory in context.watched_dirs()],
        "executable": resolve_agy_executable(),
        "models": {
            "primary": os.environ.get("ANTIGRAVITY_MODEL", DEFAULT_PRIMARY_MODEL),
            "fallback": os.environ.get(
                "ANTIGRAVITY_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL
            ),
        },
        "selected": read_viewer_settings(context.live_dir),
        "auth": auth,
        "skill_registry": str(registry),
        "skills": skills,
        "skill_warnings": skill_warnings,
        "skill_error": skill_error,
        "runs": len(list_runs(context.live_dir, limit=1000)),
        "uptime_seconds": round(time.time() - context.started, 1),
    }


def switch_account() -> dict[str, Any]:
    """Clears the stored Google credential and opens an interactive agy sign-in."""
    output: list[str] = []
    try:
        result = subprocess.run(
            ["cmdkey", "/delete:LegacyGeneric:target=gemini:antigravity"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        message = (result.stdout or "").strip() or (result.stderr or "").strip()
        if message:
            output.append(message)
    except Exception as exc:
        output.append(f"credential clear failed: {exc}")
    executable = resolve_agy_executable()
    try:
        subprocess.Popen(
            f'start "Antigravity Sign-In" cmd.exe /k "{executable}"',
            shell=True,
        )
        output.append("Sign-in terminal launched — complete the browser OAuth flow.")
    except Exception as exc:
        output.append(f"sign-in terminal failed to launch: {exc}")
    return {"status": "switching", "output": "\n".join(output)}


ASK_DEFAULT_PROMPT = (
    "Review this quickly and flag anything important, risky, or broken. "
    "Lead with the bottom line, then the specifics."
)

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024
CAPTURE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
HANDSHAKE_TIMEOUT_SECONDS = 6.0


def mask_email(address: str | None) -> str | None:
    """Masks a detected account address for API exposure (keeps the domain)."""
    if not address or "@" not in address:
        return address
    local, _, domain = address.partition("@")
    if len(local) <= 2:
        masked = local[0] + "*"
    else:
        masked = local[:2] + "***"
    return f"{masked}@{domain}"


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


def _fake_ask_launcher(
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    hard_timeout_seconds: int,
    raw_line_sink: Any = None,
) -> AttemptResult:
    try:
        delay = float(os.environ.get("WISP_ASK_FAKE_DELAY", "0") or 0)
    except ValueError:
        delay = 0.0
    if delay > 0:
        time.sleep(delay)
    answer = (
        "[TEST MODE] Canned critique \u2014 fake launcher active.\n\n"
        "# Quick review\n\n"
        "**Bottom line:** the capture looks structurally sound; one risk stands out.\n\n"
        "- Risk: the retry path has no backoff bound.\n"
        "- Fix: cap retries at 3 with exponential delay.\n\n"
        "```python\nfor attempt in range(3):\n    time.sleep(2 ** attempt)\n```\n"
    )
    lines = [
        json.dumps(
            {"step_update": {"thinking": "Considering the operator's capture."}}
        ),
        json.dumps(
            {
                "step_update": {
                    "tool_calls": [{"name": "view_file", "args": {"path": "notes.md"}}]
                }
            }
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
    context: "ViewerContext",
    thread_id: str,
    prompt: str,
    context_text: str,
    image_paths: tuple[str, ...],
    model: str,
    skills: tuple[str, ...],
) -> None:
    store = ChatStore(context.live_dir)
    try:
        settings = read_viewer_settings(context.live_dir)
        artifacts: list[str] = []
        for raw in image_paths:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = Path(context.workspace) / raw
            if raw and context.contain_artifact(str(candidate)) and candidate.is_file():
                artifacts.append(
                    relative_to_workspace(candidate, context.workspace)
                )
        envelope = DelegationEnvelope(
            prompt=build_ask_prompt(prompt, context_text, tuple(artifacts)),
            harness="wisp-hotkey",
            artifacts=tuple(artifacts),
        )
        config = BridgeConfig(
            envelope=envelope,
            workspace=context.workspace,
            model=model or settings.get("chat_model") or settings["model"],
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
            run_bridge(config, launcher=_fake_ask_launcher)
            if os.environ.get("WISP_ASK_FAKE") == "1"
            else run_bridge(config)
        )
        report_path = ""
        try:
            report_path = str(write_report(context.workspace, result))
        except OSError:
            report_path = ""
        answer = extract_chat_answer(
            result.critique_markdown or result.error or "No answer was produced."
        )
        store.append_message(
            thread_id,
            "assistant",
            answer,
            {
                "success": result.success,
                "model": result.model_used,
                "run_id": captured_run.get("id", ""),
                "report_path": report_path,
                "error": result.error,
                "fake": os.environ.get("WISP_ASK_FAKE") == "1",
            },
        )
        store.update_run(
            thread_id,
            {
                "status": "done" if result.success else "failed",
                "run_id": captured_run.get("id", ""),
                "success": result.success,
                "ended": time.time(),
            },
        )
    except Exception as exc:
        store.append_message(
            thread_id,
            "assistant",
            f"Delegation failed before answering: {exc}",
            {"success": False, "error": str(exc)},
        )
        store.update_run(thread_id, {"status": "failed", "error": str(exc)})
    finally:
        context.active_ask = {}
        try:
            context.ask_lock.release()
        except RuntimeError:
            pass


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

        Only roots **inside the workspace** qualify: delegation artifacts are
        emitted workspace-relative so the mounted reviewer can resolve them,
        and `_ask_worker` re-joins them against the workspace (GATE-4 FL-007).
        """
        roots = [self.workspace]
        captures = Path(self.live_dir).parent / "captures"
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
        extension, and lives inside one of :meth:`artifact_roots` — all of
        which are inside the workspace, so the returned relative path always
        rejoins against the workspace. Everything else is rejected (audit
        finding: arbitrary local paths previously flowed straight into
        delegation artifacts).
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
                if candidate != self.live_dir and candidate not in dirs:
                    dirs.append(candidate)
        except Exception:
            pass
        self._watch_cache = (now, tuple(dirs))
        return dirs


def existing_viewer(live_dir: Path) -> dict[str, Any] | None:
    """Returns {port, pid, token?} when a live viewer already owns this workspace.

    Liveness is established by an HTTP probe; ``token`` is the per-instance
    secret from the manifest used for the authenticated shutdown handshake.
    """
    try:
        data = json.loads(settings_path(live_dir).parent.joinpath("viewer.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        port = int(data.get("port") or 0)
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if not port:
        return None
    try:
        with _urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            if response.status == 200:
                info: dict[str, Any] = {"port": port, "pid": pid}
                token = str(data.get("token") or "")
                if token:
                    info["token"] = token
                return info
    except Exception:
        return None
    return None


def request_shutdown(port: int, token: str) -> bool:
    """Asks the viewer on ``port`` (holding ``token``) to shut down gracefully."""
    if not port or not token:
        return False
    try:
        request = _urlopen(
            _build_request(
                f"http://127.0.0.1:{port}/api/shutdown",
                token=token,
            ),
            timeout=HANDSHAKE_TIMEOUT_SECONDS,
        )
        with request:
            return 200 <= request.status < 300
    except Exception:
        return False


def _build_request(url: str, token: str = "") -> Any:
    from urllib.request import Request

    headers = {SESSION_HEADER: "1", "Origin": f"http://127.0.0.1:{urlparse(url).port}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, data=b"{}", headers=headers, method="POST")


def pid_image_name(pid: int) -> str | None:
    """Best-effort image name of a live PID (None when the PID does not exist)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        line = (result.stdout or "").strip().splitlines()
        if not line or line[0].upper().startswith(("INFO", '"ERROR"')):
            return None
        return line[0].split(",")[0].strip('"').lower() or None
    except Exception:
        return None


def pid_command_line(pid: int) -> str | None:
    """Best-effort command line of a live PID (None when absent or unknown)."""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Process -Filter 'ProcessId = "
                + str(int(pid))
                + "').CommandLine",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        line = (result.stdout or "").strip()
        return line or None
    except Exception:
        return None


def looks_like_viewer_process(image_name: str | None, pid: int | None = None) -> bool:
    """Conservative PID-reuse guard before any forced kill (GATE-4 FL-005).

    A bare "this is some python process" match once allowed a recycled PID to
    route an innocent interpreter into ``taskkill /F /T``; a forced kill now
    additionally requires the process command line to name this viewer.
    """
    if not image_name:
        return False
    if not any(marker in image_name for marker in ("python", "electron", "wisp")):
        return False
    if pid is None:
        return False
    command_line = pid_command_line(pid) or ""
    return "antigravity_viewer" in command_line


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], context: ViewerContext):
        super().__init__(address, ViewerHandler)
        self.context = context


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = 60

    @property
    def context(self) -> ViewerContext:
        return self.server.context  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _authorized(self, query: Mapping[str, list[str]] | None = None) -> bool:
        """Bearer-token gate; enforced only when the server bound non-loopback.

        Query-string tokens are accepted ONLY on ``/events`` (EventSource
        cannot set headers) and never on other routes, where a token in the
        URL would leak into proxy logs and history (GATE-4 FL-006).
        """
        token = self.context.auth_token
        if not token:
            return True
        header = self.headers.get("Authorization") or ""
        if header == f"Bearer {token}":
            return True
        if query and token in (query.get("token") or []):
            parsed = urlparse(self.path)
            return parsed.path == "/events"
        return False

    def _check_host(self) -> bool:
        """DNS-rebinding guard for loopback bindings: Host must be loopback."""
        if self.context.auth_token:
            return True
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        if not host:
            return True
        return host in {"127.0.0.1", "localhost", "::1"}

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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_html(self, status: int, html: str) -> None:
        self._send_bytes(status, html.encode("utf-8"), "text/html; charset=utf-8")

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
        if route == "/":
            self._serve_html()
            return
        if route == "/health/details":
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
            return
        if route == "/api/status":
            self._send_json(200, collect_status(self.context))
            return
        if route == "/api/runs":
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
                    "runs": merged[:60],
                    "live_dir": str(self.context.live_dir),
                    "watched_dirs": [str(directory) for directory in watched],
                },
            )
            return
        if route == "/api/assets":
            self._send_json(200, {"assets": self.context.assets})
            return
        if route == "/api/models":
            self._send_json(200, list_models())
            return
        if route == "/api/model":
            settings = read_viewer_settings(self.context.live_dir)
            self._send_json(200, settings)
            return
        if route == "/api/chat/threads":
            store = ChatStore(self.context.live_dir)
            self._send_json(200, {"threads": store.list_threads()})
            return
        if route.startswith("/api/chat/thread/"):
            thread_id = route[len("/api/chat/thread/") :].strip("/").split("/")[0]
            store = ChatStore(self.context.live_dir)
            thread = store.load(thread_id) if valid_thread_id(thread_id) else None
            if thread is None:
                self._send_json(404, {"error": "thread not found"})
                return
            self._send_json(200, {"thread": thread})
            return
        if route.startswith("/captures/"):
            name = unquote(route[len("/captures/") :])
            safe = name.replace("\\", "/").rsplit("/", 1)[-1]
            if not safe or safe != name:
                self._send_json(404, {"error": "capture not found"})
                return
            path = Path(self.context.live_dir).parent / "captures" / safe
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
            return
        if route.startswith("/assets/"):
            self._serve_asset(route[len("/assets/") :])
            return
        if route == "/events":
            replay = query.get("file", [None])[0]
            self._serve_events(replay)
            return
        self._send_json(404, {"error": f"not found: {route}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self._check_host():
            self._send_json(403, {"error": "host header not allowed"})
            return
        if parsed.path == "/api/shutdown":
            self._handle_shutdown()
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if not self._guard_post():
            return
        if parsed.path == "/api/account/switch":
            self._send_json(200, switch_account())
            return
        if parsed.path == "/api/account/recheck":
            self._send_json(200, {"auth": collect_auth_state()})
            return
        if parsed.path == "/api/model":
            try:
                payload = json.loads(getattr(self, "_body", b"") or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "body must be an object"})
                return
            unknown = [
                key
                for key in ("model", "fallback_model", "chat_model")
                if isinstance(payload.get(key), str)
                and payload[key].strip()
                and not _MODEL_NAME_PATTERN.match(payload[key].strip())
            ]
            if not unknown and not bool(payload.get("allow_custom")):
                unknown = [
                    key
                    for key in ("model", "fallback_model", "chat_model")
                    if isinstance(payload.get(key), str)
                    and payload[key].strip()
                    and not _model_known(payload[key].strip())[0]
                ]
            if unknown:
                self._send_json(
                    400,
                    {
                        "error": f"unknown model for: {', '.join(unknown)}",
                        "known_models": list_models().get("models") or [],
                        "hint": "resubmit with allow_custom=true to force a custom id",
                    },
                )
                return
            settings = write_viewer_settings(self.context.live_dir, payload)
            inventory = list_models().get("models") or []
            for key in ("model", "fallback_model", "chat_model"):
                value = settings.get(key)
                settings[f"{key}_known"] = bool(value) and value in inventory
            self._send_json(200, settings)
            return
        if parsed.path == "/api/capture":
            payload = self._json_body()
            if payload is None:
                return
            if not self.context.capture_lock.acquire(blocking=False):
                self._send_json(
                    409,
                    {
                        "error": "busy",
                        "message": "A capture is already in progress.",
                    },
                )
                return
            try:
                mode = str(payload.get("mode") or "auto")
                result = capture_auto(
                    self.context.live_dir, prefer_selection=(mode != "snip")
                )
                if result.get("kind") == "image" and result.get("path"):
                    result["rel_path"] = relative_to_workspace(
                        Path(result["path"]), self.context.workspace
                    )
            finally:
                try:
                    self.context.capture_lock.release()
                except RuntimeError:
                    pass
            self._send_json(200, result)
            return
        if parsed.path == "/api/chat/upload":
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
                self._send_json(413, {"error": "image exceeds 12 MB limit"})
                return
            saved = save_pasted_image(self.context.live_dir, blob)
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
            return
        if parsed.path == "/api/ask":
            payload = self._json_body()
            if payload is None:
                return
            self._handle_ask(payload)
            return
        if parsed.path.startswith("/api/chat/thread/") and parsed.path.endswith("/pin"):
            thread_id = (
                parsed.path[len("/api/chat/thread/") : -len("/pin")].strip("/")
            )
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
            return
        self._send_json(404, {"error": f"not found: {parsed.path}"})

    def _json_body(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(getattr(self, "_body", b"") or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid JSON body"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be an object"})
            return None
        return payload

    def _handle_ask(self, payload: dict[str, Any]) -> None:
        context = self.context
        prompt = str(payload.get("prompt") or "").strip() or ASK_DEFAULT_PROMPT
        context_text = str(payload.get("context_text") or "").strip()
        image_path = str(payload.get("image_path") or "").strip()
        image_paths: list[str] = []
        images_raw = payload.get("images")
        if isinstance(images_raw, list):
            for item in images_raw:
                candidate = str(item or "").strip()
                if candidate:
                    image_paths.append(candidate)
        if image_path:
            image_paths.insert(0, image_path)
        seen: set[str] = set()
        image_paths = [p for p in image_paths if not (p in seen or seen.add(p))]
        rejected: list[str] = []
        contained: list[str] = []
        for raw_path in image_paths:
            contained_rel = context.contain_artifact(raw_path)
            if contained_rel is None:
                rejected.append(raw_path)
            else:
                contained.append(contained_rel)
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
            return
        image_paths = contained
        model = str(payload.get("model") or "").strip()
        if model:
            known, known_models = _model_known(model)
            if not known and not bool(payload.get("allow_custom")):
                self._send_json(
                    400,
                    {"error": f"unknown model: {model}", "known_models": known_models},
                )
                return
        thread_id = str(payload.get("thread_id") or "").strip()
        skills_raw = payload.get("skills")
        settings = read_viewer_settings(context.live_dir)
        if isinstance(skills_raw, list):
            skills = tuple(str(skill).strip() for skill in skills_raw if str(skill).strip())
        else:
            skills = tuple(settings.get("ask_skills") or ())

        store = ChatStore(context.live_dir)
        if thread_id:
            thread = store.load(thread_id) if valid_thread_id(thread_id) else None
            if thread is None:
                self._send_json(404, {"error": "thread not found"})
                return
        else:
            thread = store.create(prompt)

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

        context.active_ask = {"thread_id": thread["id"], "started": time.time()}
        store.append_message(
            thread["id"],
            "user",
            prompt,
            {
                "context_text": context_text[:4000],
                "image_path": image_path or (image_paths[0] if image_paths else ""),
                "image_paths": image_paths,
            },
        )
        store.update_run(
            thread["id"], {"status": "running", "started": time.time()}
        )
        worker = threading.Thread(
            target=_ask_worker,
            args=(
                context,
                thread["id"],
                prompt,
                context_text,
                tuple(image_paths),
                model,
                skills,
            ),
            daemon=True,
        )
        worker.start()
        self._send_json(
            200,
            {
                "thread_id": thread["id"],
                "status": "running",
                "model": model
                or settings.get("chat_model")
                or settings["model"],
            },
        )

    def _handle_shutdown(self) -> None:
        """Authenticated shutdown handshake (audit finding #5).

        Requires the per-instance token from the manifest, and — when the
        server binds with an operator bearer token — that token too
        (GATE-4 REQ-HARD-003). A stale port shared by an unrelated process can
        never produce the instance token. Replaces the old trust-the-manifest-
        PID ``taskkill`` path.
        """
        expected = self.context.instance_token
        if not expected:
            self._send_json(404, {"error": "not found"})
            return
        if self.context.auth_token:
            header = self.headers.get("Authorization") or ""
            if header != f"Bearer {self.context.auth_token}":
                self._send_json(401, {"error": "unauthorized"})
                return
        header = self.headers.get("Authorization") or ""
        supplied = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        if not supplied or not secrets.compare_digest(supplied, expected):
            self._send_json(403, {"error": "invalid instance token"})
            return
        self._send_json(200, {"status": "shutting-down"})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _guard_post(self) -> bool:
        if self.headers.get(SESSION_HEADER) != "1":
            self._send_json(403, {"error": "missing local session header"})
            return False
        origin = self.headers.get("Origin")
        if origin:
            allowed = {
                f"http://127.0.0.1:{self.context.port}",
                f"http://localhost:{self.context.port}",
            }
            if origin not in allowed:
                self._send_json(403, {"error": "origin not allowed"})
                return False
        raw_length = (self.headers.get("Content-Length") or "").strip()
        if not raw_length:
            self._send_json(411, {"error": "Content-Length required"})
            return False
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return False
        if length < 0:
            self._send_json(400, {"error": "invalid Content-Length"})
            return False
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": f"body exceeds {MAX_BODY_BYTES} byte limit"})
            return False
        self._body = self.rfile.read(length) if length else b""
        return True

    def _serve_html(self) -> None:
        try:
            html = HTML_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            self._send_html(500, f"<h1>viewer html missing</h1><pre>{exc}</pre>")
            return
        self._send_html(200, html)

    def _serve_asset(self, name: str) -> None:
        safe = unquote(name).replace("\\", "/").rsplit("/", 1)[-1]
        if safe not in self.context.assets.values():
            self._send_json(404, {"error": "asset not found"})
            return
        path = ASSET_DIR / safe
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "asset unreadable"})
            return
        content_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        self._send_bytes(200, body, content_type, cache="public, max-age=3600")

    def _serve_events(self, replay_file: str | None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._security_headers()
        self.end_headers()

        def send(event: str, data: str) -> bool:
            try:
                self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return False

        if not send("hello", json.dumps({"status": "ready", "port": self.context.port})):
            return
        try:
            if replay_file:
                self._stream_replay(replay_file, send)
            else:
                self._stream_live(send)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def _stream_replay(self, replay_file: str, send: Any) -> None:
        safe = replay_file.replace("\\", "/").rsplit("/", 1)[-1]
        if not re.fullmatch(r"run-[A-Za-z0-9\-_]+\.jsonl", safe):
            send("replay_end", json.dumps({"error": "invalid run file"}))
            return
        path: Path | None = None
        for directory in self.context.watched_dirs():
            candidate = directory / safe
            if candidate.is_file():
                path = candidate
                break
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

    def _stream_live(self, send: Any) -> None:
        current: Path | None = None
        offset = 0
        buffer = ""
        current_done = False
        last_data = time.time()
        last_keepalive = time.time()
        while True:
            now = time.time()
            idle = now - last_data
            candidate = newest_run_across(self.context.watched_dirs())
            should_switch = False
            if current is None:
                should_switch = candidate is not None
            elif current_done or idle >= LIVE_LATCH_IDLE_SECONDS:
                should_switch = (
                    candidate is not None and candidate.name != current.name
                )
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
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except OSError:
                        return
                    last_keepalive = now
                time.sleep(POLL_INTERVAL_SECONDS * 2)
                continue
            chunk = b""
            try:
                with open(current, "rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
            except OSError:
                chunk = b""
            if chunk:
                offset += len(chunk)
                last_data = time.time()
                buffer += chunk.decode("utf-8", errors="replace")
                lines = buffer.split("\n")
                buffer = lines.pop()
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if '"run_end"' in stripped:
                        current_done = True
                    if not send("live", stripped):
                        return
                last_keepalive = time.time()
            elif time.time() - last_keepalive > KEEPALIVE_SECONDS:
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except OSError:
                    return
                last_keepalive = time.time()
            time.sleep(POLL_INTERVAL_SECONDS)


def bind_server(
    host: str,
    preferred_port: int,
    context_factory: Any,
    max_attempts: int = PORT_SCAN_RANGE,
) -> tuple[ViewerServer, int]:
    last_error: OSError | None = None
    for candidate in range(preferred_port, preferred_port + max_attempts):
        try:
            probe = ViewerServer.__new__(ViewerServer)
            ThreadingHTTPServer.__init__(probe, (host, candidate), ViewerHandler)
            probe.daemon_threads = True
            actual_port = int(probe.server_address[1])
            probe.context = context_factory(actual_port)
            return probe, actual_port
        except OSError as exc:
            last_error = exc
            continue
    raise OSError(
        f"no free port in {preferred_port}-{preferred_port + max_attempts - 1}: {last_error}"
    )


def _browser_candidates() -> list[str]:
    candidates: list[str] = []
    for name in ("msedge", "chrome", "brave", "chromium"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for path in (
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
    ):
        if path.is_file():
            candidates.append(str(path))
    return candidates


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def work_area() -> tuple[int, int, int, int]:
    """Returns the desktop work area (excludes the taskbar) or a 1080p fallback."""
    try:
        rect = _RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            if rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1080


def bottom_right_position(width: int, height: int, margin: int = 18) -> tuple[int, int]:
    left, top, right, bottom = work_area()
    x = max(left, right - width - margin)
    y = max(top, bottom - height - margin)
    return x, y


def open_app_window(url: str, width: int, height: int) -> bool:
    x, y = bottom_right_position(width, height)
    profile = Path(os.environ.get("TEMP", ".")) / "wisp-app-profile"
    for executable in _browser_candidates():
        try:
            subprocess.Popen(
                [
                    executable,
                    f"--app={url}",
                    f"--window-size={width},{height}",
                    f"--window-position={x},{y}",
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-features=Translate,AutofillServerCommunication",
                ]
            )
            return True
        except OSError:
            continue
    try:
        return webbrowser.open(url)
    except Exception:
        return False


_SHAPE_CIRCLE = "circle"
_SHAPE_CARD = "card"
_SHAPE_NONE = "none"


def _apply_window_region(hwnd: int, shape: str, width: int, height: int) -> None:
    """Clips the window to an ellipse or rounded card; outside stays click-through."""
    if os.name != "nt" or not hwnd:
        return
    try:
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        gdi32.CreateEllipticRgn.restype = ctypes.c_void_p
        gdi32.CreateEllipticRgn.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
        gdi32.CreateRoundRectRgn.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        if shape == _SHAPE_CIRCLE:
            region = gdi32.CreateEllipticRgn(0, 0, int(width) + 1, int(height) + 1)
        elif shape == _SHAPE_CARD:
            region = gdi32.CreateRoundRectRgn(
                0, 0, int(width) + 1, int(height) + 1, 26, 26
            )
        else:
            region = gdi32.CreateRoundRectRgn(
                0, 0, int(width) + 1, int(height) + 1, 0, 0
            )
        if region:
            user32.SetWindowRgn(hwnd, region, True)
    except Exception:
        pass


def open_native_window(url: str, width: int, height: int, transparent: bool = False) -> bool:
    """Opens a frameless desktop widget via pywebview.

    Blocks until the window closes. Returns ``False`` when pywebview (or the
    WebView2 runtime) is unavailable so the caller can fall back. The window is
    clipped with a native region (ellipse for the aura, rounded card for panels)
    so only Wisp and his glow are visible on the desktop.
    """
    try:
        import webview
    except ImportError:
        return False

    x, y = bottom_right_position(width, height)

    class WindowApi:
        def __init__(self) -> None:
            self.window: Any = None
            self.user_positioned = False
            self.shape = _SHAPE_NONE
            self.applied_shape = None
            self.window_size = (int(width), int(height))

        def attach(self, window: Any) -> None:
            self.window = window

        def _handle(self) -> int | None:
            try:
                native = getattr(self.window, "native", None)
                if native is None:
                    return None
                return int(native.Handle.ToInt64())
            except Exception:
                return None

        def close(self) -> None:
            if self.window is not None:
                self.window.destroy()

        def minimize(self) -> None:
            if self.window is not None:
                self.window.minimize()

        def toggle_pin(self) -> bool:
            if self.window is None:
                return False
            self.window.on_top = not bool(self.window.on_top)
            return bool(self.window.on_top)

        def move(self, dx: int, dy: int) -> list[int]:
            if self.window is None:
                return [0, 0]
            try:
                self.window.x = int(self.window.x or 0) + int(dx)
                self.window.y = int(self.window.y or 0) + int(dy)
                self.user_positioned = True
                return [self.window.x, self.window.y]
            except Exception:
                return [0, 0]

        def set_view(self, width: int, height: int) -> bool:
            if self.window is None:
                return False
            try:
                w = max(int(width), 240)
                h = max(int(height), 200)
                left, top, right, bottom = work_area()
                if self.user_positioned:
                    x = int(self.window.x or 0)
                    y = int(self.window.y or 0)
                    x = max(left, min(x, right - w))
                    y = max(top, min(y, bottom - h))
                else:
                    x = max(left, right - w - 18)
                    y = max(top, bottom - h - 18)
                self.window.resize(w, h)
                self.window.move(x, y)
                self.window_size = (w, h)
                self.applied_shape = None
                return True
            except Exception:
                return False

        def set_shape(self, shape: str) -> bool:
            self.shape = str(shape or _SHAPE_NONE)
            return True

    api = WindowApi()

    def region_keeper() -> None:
        while True:
            time.sleep(0.25)
            if api.window is None:
                continue
            handle = api._handle()
            if handle is None:
                continue
            if api.applied_shape != api.shape:
                w, h = api.window_size
                _apply_window_region(handle, api.shape, w, h)
                api.applied_shape = api.shape

    watcher = threading.Thread(target=region_keeper, daemon=True)
    watcher.start()

    try:
        window = webview.create_window(
            "Wisp",
            url,
            width=width,
            height=height,
            x=x,
            y=y,
            frameless=True,
            easy_drag=False,
            on_top=False,
            transparent=False,
            background_color="#000000",
            resizable=False,
        )
    except Exception:
        return False
    api.attach(window)
    webview.start(debug=False)
    return True


def electron_executable() -> Path | None:
    if os.name == "nt":
        candidate = SHELL_DIR / "node_modules" / "electron" / "dist" / "electron.exe"
    else:
        candidate = SHELL_DIR / "node_modules" / "electron" / "dist" / "electron"
    return candidate if candidate.exists() else None


def open_electron_window(url: str) -> subprocess.Popen | None:
    """Spawns the frameless transparent Electron shell for the widget.

    True per-pixel transparency and always-on-top control are only reliable in
    Chromium-based shells on Windows; pywebview/WinForms cannot composite the
    page over the desktop. Returns the Electron process, or ``None`` when the
    shell is not installed so the caller can fall back.
    """
    executable = electron_executable()
    if executable is None:
        return None
    env = os.environ.copy()
    env["WISP_URL"] = url + ("&" if "?" in url else "?") + "transparent=1"
    try:
        return subprocess.Popen(
            [str(executable), str(SHELL_DIR)],
            cwd=str(SHELL_DIR),
            env=env,
        )
    except OSError:
        return None


def _write_viewer_manifest(live_dir: Path, payload: dict[str, Any]) -> None:
    try:
        live_dir.mkdir(parents=True, exist_ok=True)
        (live_dir / "viewer.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    workspace = resolve_workspace(args.workspace)
    live_dir = resolve_live_dir(workspace, args.live_dir)

    auth_token = str(args.auth_token or "").strip()
    if args.generate_token:
        auth_token = secrets.token_urlsafe(32)
    non_loopback = args.host not in LOOPBACK_HOSTS
    if non_loopback and not auth_token:
        print(
            "[wisp-viewer] refusing to bind a non-loopback host without an auth "
            "token: pass --auth-token <secret> or --generate-token.",
            file=sys.stderr,
        )
        return 2

    existing = existing_viewer(live_dir)
    if existing and not args.replace:
        print(
            f"[wisp-viewer] already running on port {existing['port']} "
            f"(pid {existing['pid']}); use --replace to restart it."
        )
        return 0
    if existing and args.replace:
        token = str(existing.get("token") or "")
        replaced = False
        if token and request_shutdown(existing["port"], token):
            replaced = True
            print(
                f"[wisp-viewer] sent authenticated shutdown to the viewer on "
                f"port {existing['port']}"
            )
        else:
            image = pid_image_name(existing["pid"])
            if looks_like_viewer_process(image, pid=existing["pid"]):
                print(
                    f"[wisp-viewer] legacy manifest without instance token; "
                    f"pid {existing['pid']} verified as a viewer process "
                    f"({image}); forcing."
                )
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(existing["pid"]), "/T", "/F"],
                        capture_output=True,
                        timeout=15,
                    )
                    replaced = True
                except Exception:
                    replaced = False
            else:
                print(
                    f"[wisp-viewer] refusing to replace: the manifest PID "
                    f"{existing['pid']} does not look like a viewer process "
                    f"({image or 'gone'}) and no instance token is available; "
                    f"the port is likely owned by another program. Remove "
                    f"{live_dir / 'viewer.json'} if this is stale.",
                    file=sys.stderr,
                )
                return 2
        if replaced:
            time.sleep(1.5)

    def context_factory(port: int) -> ViewerContext:
        return ViewerContext(workspace=workspace, live_dir=live_dir, port=port)

    try:
        server, port = bind_server(args.host, args.port, context_factory)
    except OSError as exc:
        print(f"[wisp-viewer] failed to bind: {exc}", file=sys.stderr)
        return 1

    instance_token = secrets.token_urlsafe(24)
    server.context.auth_token = auth_token
    server.context.instance_token = instance_token

    url = f"http://{args.host}:{port}/"
    _write_viewer_manifest(
        live_dir,
        {
            "port": port,
            "url": url,
            "pid": os.getpid(),
            "token": instance_token,
            "workspace": str(workspace),
            "started": time.time(),
        },
    )
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
        pass

    serving_thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True
    )
    serving_thread.start()

    if args.open and args.shell in ("auto", "electron"):
        electron_proc = open_electron_window(url)
        if electron_proc is not None:
            print(f"[wisp-viewer] electron widget on {url}")
            try:
                electron_proc.wait()
            except KeyboardInterrupt:
                pass
            server.shutdown()
            server.server_close()
            return 0
        if args.shell == "electron":
            print(
                "[wisp-viewer] electron shell unavailable (run npm install in tools/wisp_shell)",
                file=sys.stderr,
            )

    if args.open and args.shell in ("auto", "native"):
        try:
            if open_native_window(
                url, args.width, args.height, transparent=args.transparent
            ):
                server.shutdown()
                server.server_close()
                return 0
        except Exception as exc:
            print(f"[wisp-viewer] native shell failed: {exc}", file=sys.stderr)

    if args.open:
        opened = open_app_window(url, 470, 452)
        if not opened:
            print(f"[wisp-viewer] open {url} in your browser", file=sys.stderr)

    try:
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
