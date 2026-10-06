"""Harness-agnostic bridge for delegating adversarial review to Google Antigravity (agy).

Agent 1 (any coding harness) calls this module to spawn the Antigravity CLI as a
ruthless adversarial sub-agent. The bridge guarantees:

* safe binary resolution (system PATH first, then ``~/.gemini/bin``),
* credential-hygienic environments (cloud/API secrets stripped, the Google OAuth
  token cache and OS runtime variables preserved),
* OS-level process containment (Win32 Job Object with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` on Windows, session process groups on POSIX),
* complete, untruncated capture of stdout and stderr,
* automatic quota failover from Gemini to Claude when a rate limit is detected,
* a single unabridged Markdown critique plus every raw line surfaced to Agent 1.

CLI::

    python tools/antigravity_bridge.py --prompt "Review the plan" --skills all
    python tools/antigravity_bridge.py --envelope envelope.json --json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from tools.antigravity_live import (
        DEFAULT_KEEP_RUNS,
        LIVE_DIR_NAME,
        CallbackSink,
        JsonlSink,
        LiveEmitter,
        NullSink,
        _beacon_label,
        new_run_id,
        parse_stream_line,
        register_live_dir,
    )
    from tools.skill_loader import (
        DEFAULT_SKILL_DIR,
        SkillError,
        SkillLoader,
        resolve_skill_dir,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.antigravity_live import (  # type: ignore[no-redef]
        DEFAULT_KEEP_RUNS,
        LIVE_DIR_NAME,
        CallbackSink,
        JsonlSink,
        LiveEmitter,
        NullSink,
        _beacon_label,
        new_run_id,
        parse_stream_line,
        register_live_dir,
    )
    from tools.skill_loader import (  # type: ignore[no-redef]
        DEFAULT_SKILL_DIR,
        SkillError,
        SkillLoader,
        resolve_skill_dir,
    )

DEFAULT_PRIMARY_MODEL = "gemini-3.8-flash-high"
DEFAULT_FALLBACK_MODEL = "claude-opus-4-6-thinking"
DEFAULT_PRINT_TIMEOUT_SECONDS = 1200
DEFAULT_GRACE_SECONDS = 60

RATE_LIMIT_PATTERN = re.compile(
    r"RESOURCE_EXHAUSTED|code\s*429|individual quota reached|rate limit exceeded",
    re.IGNORECASE,
)

BLOCKED_ENV_PREFIXES: tuple[str, ...] = (
    "AWS_",
    "AZURE_",
    "GITHUB_",
    "GH_",
    "SSH_",
    "OPENAI_",
    "ANTHROPIC_",
    "GEMINI_API_KEY",
)

_ESSENTIAL_ENV_WINDOWS: tuple[str, ...] = (
    "USERPROFILE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES",
    "ALLUSERSPROFILE",
    "OS",
)
_ESSENTIAL_ENV_POSIX: tuple[str, ...] = (
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)

_POLL_INTERVAL_SECONDS = 0.05
_READER_JOIN_TIMEOUT_SECONDS = 5.0

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryLimit", ctypes.c_size_t),
        ("PeakJobMemoryLimit", ctypes.c_size_t),
    ]


_KERNEL32: Any = None


def _get_kernel32() -> Any:
    global _KERNEL32
    if _KERNEL32 is None:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        _KERNEL32 = kernel32
    return _KERNEL32


def _create_job_object() -> int | None:
    if os.name != "nt":
        return None
    try:
        kernel32 = _get_kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not configured:
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except Exception:
        return None


def _close_job_object(job_handle: int | None) -> None:
    if os.name != "nt" or not job_handle:
        return
    try:
        _get_kernel32().CloseHandle(job_handle)
    except Exception:
        pass


def _terminate_job_object(job_handle: int | None) -> None:
    if os.name != "nt" or not job_handle:
        return
    try:
        _get_kernel32().TerminateJobObject(job_handle, 1)
    except Exception:
        pass


def _taskkill_process_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass


def _terminate_process_tree(proc: subprocess.Popen, job_handle: int | None) -> None:
    if os.name == "nt":
        if job_handle:
            _terminate_job_object(job_handle)
        else:
            _taskkill_process_tree(proc.pid)
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _is_blocked_variable(name: str) -> bool:
    upper = name.upper()
    return any(upper.startswith(prefix) for prefix in BLOCKED_ENV_PREFIXES)


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def sanitize_environment(
    base_env: Mapping[str, str] | None = None,
    home: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Returns a credential-hygienic environment for the Antigravity subprocess.

    All variables matching ``BLOCKED_ENV_PREFIXES`` are excised so no cloud or
    API secrets can leak into the child process. OS runtime essentials are
    preserved, ``~/.gemini/bin`` is prepended to ``PATH`` (idempotently), and
    the Google OAuth token cache under ``~/.gemini`` remains untouched because
    it lives on disk, not in the environment.
    """
    raw: dict[str, str] = dict(base_env) if base_env is not None else dict(os.environ)
    home_path = Path(home) if home is not None else Path.home()

    sanitized: dict[str, str] = {
        str(key): str(value)
        for key, value in raw.items()
        if not _is_blocked_variable(str(key))
    }

    essentials = _ESSENTIAL_ENV_WINDOWS if os.name == "nt" else _ESSENTIAL_ENV_POSIX
    for variable in essentials:
        if variable not in sanitized and variable in raw:
            sanitized[variable] = str(raw[variable])

    gemini_bin = str(home_path / ".gemini" / "bin")
    path_key = next((key for key in sanitized if key.upper() == "PATH"), None)
    current_path = sanitized.get(path_key, "") if path_key else ""
    entries = [
        part
        for part in current_path.split(os.pathsep)
        if part and not _same_path(part, gemini_bin)
    ]
    entries.insert(0, gemini_bin)
    sanitized["PATH"] = os.pathsep.join(entries)
    if path_key and path_key != "PATH":
        sanitized.pop(path_key, None)

    if os.name == "nt":
        sanitized.setdefault("USERPROFILE", str(home_path))
    sanitized.setdefault("HOME", str(home_path))

    if extra_env:
        for key, value in extra_env.items():
            if not _is_blocked_variable(str(key)):
                sanitized[str(key)] = str(value)

    return sanitized


def resolve_agy_executable(
    which: Callable[[str], str | None] | None = None,
    home: Path | None = None,
) -> str:
    """Locates the ``agy`` executable without hardcoding machine-specific paths."""
    lookup = which or shutil.which
    found = lookup("agy")
    if found:
        return found
    base = (Path(home) if home is not None else Path.home()) / ".gemini" / "bin"
    candidates = ("agy.exe", "agy") if os.name == "nt" else ("agy", "agy.exe")
    for name in candidates:
        candidate = base / name
        if candidate.is_file():
            return str(candidate)
    return "agy"


def is_rate_limited(text: str | None) -> bool:
    """Detects HTTP 429 / quota-exhaustion signatures anywhere in a stream."""
    if not text:
        return False
    return RATE_LIMIT_PATTERN.search(text) is not None


_TRANSIENT_PATTERN = re.compile(
    r"ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|EPIPE|socket hang up|"
    r"connection reset|temporarily unavailable|service unavailable|bad gateway|"
    r"internal server error|gateway timeout|overloaded|UNAVAILABLE|network error|"
    r"TLS handshake timeout|request timed out",
    re.IGNORECASE,
)
_RESET_PATTERN = re.compile(r"resets?\s+in\s+((?:\d+\s*[smhdw]\s*)+)", re.IGNORECASE)
_RESET_PART_PATTERN = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_RESET_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_reset_seconds(text: str | None) -> int | None:
    """Parses quota reset notices (e.g. ``Resets in 1h 30m``) into seconds."""
    if not text:
        return None
    match = _RESET_PATTERN.search(text)
    if not match:
        return None
    total = 0
    for value, unit in _RESET_PART_PATTERN.findall(match.group(1)):
        total += int(value) * _RESET_UNIT_SECONDS[unit.lower()]
    return total


def extract_reset_text(text: str | None) -> str | None:
    """Returns the human-readable reset fragment (e.g. ``1h 30m``) or ``None``."""
    if not text:
        return None
    match = _RESET_PATTERN.search(text)
    if not match:
        return None
    return " ".join(match.group(1).split())


def attempt_succeeded(attempt: "AttemptResult") -> bool:
    """A run is successful only when it exits cleanly with actual content."""
    if attempt.exit_code != 0 or attempt.timed_out or attempt.interrupted:
        return False
    return bool((attempt.stdout or "").strip() or (attempt.stderr or "").strip())


def is_transient_failure(attempt: "AttemptResult") -> bool:
    """Classifies retryable failures (network resets, 5xx, empty responses)."""
    if attempt.exit_code == 0:
        return not attempt_succeeded(attempt)
    return _TRANSIENT_PATTERN.search(attempt.combined_output) is not None


def run_quota_hook(command: str) -> tuple[bool, str]:
    """Executes an operator credential-rotation command when quota is exhausted.

    The command is an operator-authored shell string (e.g. a script that flips
    the stored Google OAuth credential to a different account). It runs through
    ``cmd.exe /c`` on Windows and ``/bin/sh -c`` on POSIX; stdout and stderr are
    captured in full and returned verbatim.
    """
    if not command or not command.strip():
        return False, "empty quota hook command"
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            shell=True,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output
    except Exception as exc:
        return False, f"quota hook execution failed: {exc}"


def build_agy_command(
    executable: str,
    payload: str,
    workspace: Path,
    model: str,
    print_timeout_seconds: int,
    output_format: str = "stream-json",
    extra_add_dirs: Sequence[Path] = (),
) -> list[str]:
    """Builds the exact ``agy`` invocation contract for a delegation attempt.

    ``extra_add_dirs`` mounts additional read roots (e.g. a fallback skill
    registry outside the workspace) so the child's tool permissions cover
    every path referenced by the payload.
    """
    command = [
        executable,
        "-p",
        payload,
        "--add-dir",
        str(workspace),
        "--model",
        model,
    ]
    for directory in extra_add_dirs:
        command += ["--add-dir", str(directory)]
    if model.lower().startswith("gemini"):
        command += ["--effort", "high"]
    command += [
        "--print-timeout",
        f"{int(print_timeout_seconds)}s",
        "--dangerously-skip-permissions",
        "--output-format",
        output_format,
    ]
    return command


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ValueError(f"Envelope field '{field_name}' must be a string or list of strings")


@dataclass(frozen=True)
class DelegationEnvelope:
    """Structured input contract between Agent 1 and the Antigravity sub-agent."""

    prompt: str
    harness: str = "agent-1"
    context: str = ""
    claims_to_falsify: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    notes: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "DelegationEnvelope":
        if not isinstance(data, Mapping):
            raise ValueError("Delegation envelope must be a JSON object")
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("Delegation envelope requires a non-empty 'prompt' field")
        return cls(
            prompt=prompt,
            harness=str(data.get("harness") or "agent-1").strip() or "agent-1",
            context=str(data.get("context") or "").strip(),
            claims_to_falsify=_string_tuple(data.get("claims_to_falsify"), "claims_to_falsify"),
            artifacts=_string_tuple(data.get("artifacts"), "artifacts"),
            notes=str(data.get("notes") or "").strip(),
        )


@dataclass
class BridgeConfig:
    """Execution configuration for one delegation run."""

    envelope: DelegationEnvelope
    workspace: Path = field(default_factory=lambda: Path.cwd())
    model: str = DEFAULT_PRIMARY_MODEL
    fallback_model: str = DEFAULT_FALLBACK_MODEL
    skills: tuple[str, ...] = ()
    recommended_skills: tuple[str, ...] = ()
    skill_dir: Path | None = None
    print_timeout_seconds: int = DEFAULT_PRINT_TIMEOUT_SECONDS
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    retries: int = 2
    retry_backoff_seconds: float = 5.0
    quota_wait_seconds: int = 0
    quota_hook: str | None = None
    live: bool = False
    live_dir: Path | None = None
    live_keep_runs: int = DEFAULT_KEEP_RUNS
    live_callback: Callable[[Any], None] | None = None
    executable: str | None = None

    def hard_timeout_seconds(self) -> int:
        return max(int(self.print_timeout_seconds) + int(self.grace_seconds), 1)

    def resolved_workspace(self) -> Path:
        return Path(self.workspace).expanduser().resolve()

    def resolved_live_dir(self, workspace: Path) -> Path:
        return Path(self.live_dir) if self.live_dir else (workspace / LIVE_DIR_NAME)


@dataclass
class AttemptResult:
    """Complete, untruncated result of a single contained ``agy`` execution."""

    model: str = ""
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    interrupted: bool = False

    @property
    def combined_output(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts)

    @property
    def rate_limited(self) -> bool:
        return is_rate_limited(self.combined_output)


@dataclass
class BridgeResult:
    """Aggregate outcome of a delegation run, including every raw stream."""

    success: bool
    model_used: str
    failover_used: bool
    timed_out: bool
    rate_limited: bool
    exit_code: int | None
    attempts: list[AttemptResult]
    critique_markdown: str
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    quota_hook_used: bool = False
    quota_hook_output: str | None = None
    resets_in: str | None = None
    reset_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "verdict": "SUCCESS" if self.success else "FAILED",
            "model_used": self.model_used,
            "failover_used": self.failover_used,
            "timed_out": self.timed_out,
            "rate_limited": self.rate_limited,
            "exit_code": self.exit_code,
            "error": self.error,
            "warnings": list(self.warnings),
            "quota_hook_used": self.quota_hook_used,
            "quota_hook_output": self.quota_hook_output,
            "resets_in": self.resets_in,
            "reset_seconds": self.reset_seconds,
            "critique_markdown": self.critique_markdown,
            "attempts": [
                {
                    "model": attempt.model,
                    "command": list(attempt.command),
                    "exit_code": attempt.exit_code,
                    "duration_seconds": attempt.duration_seconds,
                    "timed_out": attempt.timed_out,
                    "interrupted": attempt.interrupted,
                    "rate_limited": attempt.rate_limited,
                    "stdout": attempt.stdout,
                    "stderr": attempt.stderr,
                }
                for attempt in self.attempts
            ],
        }

    def to_mcp_dict(self) -> dict[str, Any]:
        """Compact MCP payload: organized critique plus stream statistics.

        Raw stdout/stderr are omitted here (they remain in the persisted report
        under ``attempts[]``) so tool responses stay organized and bounded while
        nothing is lost on disk.
        """
        data = self.to_dict()
        data.pop("attempts", None)
        data["stream_stats"] = [
            {
                "model": attempt.model,
                "exit_code": attempt.exit_code,
                "duration_seconds": attempt.duration_seconds,
                "timed_out": attempt.timed_out,
                "rate_limited": attempt.rate_limited,
                "stdout_chars": len(attempt.stdout),
                "stderr_chars": len(attempt.stderr),
            }
            for attempt in self.attempts
        ]
        return data


LaunchFn = Callable[[Sequence[str], Path, Mapping[str, str], int], AttemptResult]


def launch_contained(
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    hard_timeout_seconds: int,
    raw_line_sink: Callable[[str, str], None] | None = None,
) -> AttemptResult:
    """Spawns ``command`` inside an OS containment boundary and captures everything.

    On Windows the child is assigned to a Job Object configured with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` so a forced stop annihilates the
    entire process tree. On POSIX the child starts a new session and is killed
    via its process group. Reader threads drain both pipes so no line is lost,
    and the full stdout/stderr is returned even on timeout, cancellation, or
    crash. The call never truncates output.
    """
    argv = [str(part) for part in command]
    start = time.monotonic()
    job_handle = _create_job_object() if os.name == "nt" else None

    popen_kwargs: dict[str, Any] = {
        "cwd": str(Path(cwd)),
        "env": dict(env),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = 0
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except BaseException:
        _close_job_object(job_handle)
        raise

    if job_handle:
        try:
            _get_kernel32().AssignProcessToJobObject(job_handle, int(proc._handle))
        except Exception:
            _close_job_object(job_handle)
            job_handle = None

    output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _reader(pipe: Any, sink: list[str], name: str) -> None:
        try:
            for line in iter(pipe.readline, ""):
                sink.append(line)
                output_queue.put((name, line))
                if raw_line_sink is not None:
                    try:
                        raw_line_sink(name, line)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    threads: list[threading.Thread] = []
    if proc.stdout is not None:
        thread = threading.Thread(
            target=_reader, args=(proc.stdout, stdout_lines, "stdout"), daemon=True
        )
        thread.start()
        threads.append(thread)
    if proc.stderr is not None:
        thread = threading.Thread(
            target=_reader, args=(proc.stderr, stderr_lines, "stderr"), daemon=True
        )
        thread.start()
        threads.append(thread)

    timed_out = False
    interrupted = False
    deadline = start + max(int(hard_timeout_seconds), 1)

    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_tree(proc, job_handle)
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        interrupted = True
        _terminate_process_tree(proc, job_handle)

    try:
        proc.wait(timeout=_READER_JOIN_TIMEOUT_SECONDS)
    except Exception:
        _terminate_process_tree(proc, job_handle)
        try:
            proc.wait(timeout=_READER_JOIN_TIMEOUT_SECONDS)
        except Exception:
            pass

    for thread in threads:
        thread.join(timeout=_READER_JOIN_TIMEOUT_SECONDS)

    _close_job_object(job_handle)

    duration = time.monotonic() - start
    return AttemptResult(
        model="",
        command=tuple(argv),
        exit_code=proc.returncode,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        duration_seconds=round(duration, 3),
        timed_out=timed_out,
        interrupted=interrupted,
    )


_REASONING_KEYS = frozenset(
    {
        "thinking",
        "thought",
        "reasoning",
        "reasoning_content",
        "thinking_delta",
        "thought_delta",
        "reasoning_delta",
    }
)
_TEXT_KEYS = frozenset(
    {
        "text",
        "content",
        "response",
        "output",
        "delta",
        "message",
        "text_delta",
        "content_delta",
        "output_delta",
        "response_delta",
        "error",
        "detail",
        "details",
    }
)
_TOOL_KEYS = frozenset(
    {"tool_calls", "tool_call", "function_call", "tool", "actions", "action"}
)
_NESTED_KEYS = frozenset(
    {"events", "steps", "items", "messages", "updates", "step_update", "error", "details", "data"}
)


def _fence(text: str, language: str = "text") -> str:
    longest = 0
    for match in re.finditer(r"`+", text):
        longest = max(longest, len(match.group(0)))
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _iter_tool_entries(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [entry for entry in value if entry is not None]
    return [value]


def _format_tool(tool: Any) -> str:
    if isinstance(tool, Mapping):
        name = tool.get("name") or tool.get("tool") or tool.get("type") or "tool"
        arguments = tool.get(
            "args",
            tool.get("arguments", tool.get("parameters", tool.get("input", {}))),
        )
        try:
            rendered = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            rendered = str(arguments)
        return f"**{name}**\n\n{_fence(rendered, 'json')}"
    return f"`{tool}`"


def _extract_fragments(data: Any, depth: int = 0) -> list[tuple[str, str]]:
    if data is None or depth > 8:
        return []
    if isinstance(data, str):
        return []
    fragments: list[tuple[str, str]] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            lower = str(key).lower()
            if lower == "step_update" and isinstance(value, Mapping):
                fragments.extend(_extract_fragments(value, depth + 1))
                continue
            if lower == "result":
                if isinstance(value, str) and value.strip():
                    fragments.append(("text", value.strip()))
                elif isinstance(value, (Mapping, list, tuple)):
                    fragments.extend(_extract_fragments(value, depth + 1))
                continue
            if lower in _REASONING_KEYS and isinstance(value, str) and value.strip():
                fragments.append(("reasoning", value.strip()))
            elif lower in _TEXT_KEYS and isinstance(value, str) and value.strip():
                fragments.append(("text", value.strip()))
            elif lower in _TOOL_KEYS:
                for tool in _iter_tool_entries(value):
                    fragments.append(("action", _format_tool(tool)))
            elif lower in _NESTED_KEYS and isinstance(value, (list, tuple, Mapping)):
                if isinstance(value, Mapping):
                    fragments.extend(_extract_fragments(value, depth + 1))
                else:
                    for item in value:
                        fragments.extend(_extract_fragments(item, depth + 1))
        return fragments
    if isinstance(data, (list, tuple)):
        for item in data:
            fragments.extend(_extract_fragments(item, depth + 1))
    return fragments


_STEP_TEXT_KEYS = (
    "text_delta",
    "content_delta",
    "output_delta",
    "response_delta",
    "text",
    "content",
    "response",
    "delta",
)


def _step_delta(step: Mapping[str, Any]) -> str:
    for key in _STEP_TEXT_KEYS:
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _coalesce_chunks(chunks: list[str], delta: str) -> list[str]:
    """Appends a stream delta, folding duplicate and cumulative resends."""
    if not delta:
        return chunks
    if not chunks:
        return [delta]
    if delta == chunks[-1]:
        return chunks
    accumulated = "".join(chunks)
    if delta.startswith(accumulated):
        return [delta]
    if accumulated.endswith(delta):
        return chunks
    return chunks + [delta]


def _render_lifecycle(
    init_labels: list[str],
    step_meta: dict[int, dict[str, Any]],
    final_status: str,
    text_chars: int,
) -> list[str]:
    type_counts: dict[str, int] = {}
    notable: list[str] = []
    for index in sorted(step_meta):
        meta = step_meta[index]
        step_type = str(meta.get("type") or "model")
        type_counts[step_type] = type_counts.get(step_type, 0) + 1
        if step_type not in ("tool", "model"):
            state = str(meta.get("state") or "")
            suffix = f" · {state}" if state else ""
            notable.append(f"- step {index} · {step_type}{suffix}")
    total_steps = sum(type_counts.values())
    digest = (
        "- **Digest**: "
        + f"{total_steps} steps"
        + "".join(f" · {count} {name}" for name, count in sorted(type_counts.items()))
        + f" · {text_chars} chars of agent text"
    )
    lines = [digest]
    lines += [f"- {label}" for label in init_labels]
    lines += notable
    if final_status:
        lines.append(f"- result · {final_status}")
    return lines


def _render_sections(
    final_text: str,
    notes: list[str],
    reasoning: list[str],
    actions: list[str],
    lifecycle: list[str],
    unparsed: list[str],
) -> str:
    sections: list[str] = []
    if final_text:
        sections.append("### Findings & Response\n\n" + final_text)
    else:
        sections.append(
            "### Findings & Response\n\n"
            "_The stream contained no final response text; see the lifecycle digest "
            "and the report's raw streams in `attempts[]`._"
        )
    if notes:
        sections.append("### Working Notes (pre-final)\n\n" + "\n\n".join(notes))
    if reasoning:
        sections.append("### Reasoning & Analysis\n\n" + "\n\n".join(reasoning))
    if actions:
        sections.append("### Tool Activity\n\n" + "\n".join(actions))
    if lifecycle:
        sections.append("### Lifecycle\n\n" + "\n".join(lifecycle))
    if unparsed:
        sections.append("### Unparsed Stream Lines (verbatim)\n\n" + _fence("\n".join(unparsed)))
    return "\n\n".join(sections)


def aggregate_stream_json(raw: str) -> str:
    """Aggregates raw ``stream-json`` output into a cohesive Markdown critique.

    The final ``result`` response is authoritative and leads the output; streamed
    per-step text is coalesced (duplicate and cumulative resends are folded),
    tool names are tallied, and lifecycle beacons collapse into a compact digest.
    Unparseable lines are preserved verbatim. Nothing is truncated: the complete
    raw streams remain available in ``attempts[]`` of the report.
    """
    if not raw or not raw.strip():
        return "_Antigravity produced no stdout._"

    reasoning: list[str] = []
    actions: list[str] = []
    notes: list[str] = []
    unparsed: list[str] = []
    init_labels: list[str] = []
    final_response = ""
    final_status = ""
    step_meta: dict[int, dict[str, Any]] = {}
    step_texts: dict[int, list[str]] = {}
    other_texts: list[str] = []
    tool_counts: dict[str, int] = {}

    def _collect_fragments(data: Any) -> bool:
        collected = False
        for kind, fragment in _extract_fragments(data):
            if kind == "reasoning":
                if fragment not in reasoning:
                    reasoning.append(fragment)
                    collected = True
            elif kind == "action":
                if fragment not in actions:
                    actions.append(fragment)
                    collected = True
            elif fragment not in other_texts:
                other_texts.append(fragment)
                collected = True
        return collected

    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            unparsed.append(candidate)
            continue
        if not isinstance(data, Mapping):
            if not _collect_fragments(data):
                unparsed.append(candidate)
            continue

        event = str(data.get("event") or "").lower()
        if event == "init":
            init_labels.append(_beacon_label(data))
            continue

        inner = data.get("result")
        if event == "result" or (
            isinstance(inner, Mapping) and "status" in inner and "response" in inner
        ):
            if isinstance(inner, Mapping):
                response = inner.get("response")
                if isinstance(response, str) and response.strip():
                    final_response = response.strip()
                status = inner.get("status")
                if isinstance(status, str) and status.strip():
                    final_status = status.strip()
                if not final_response and not final_status:
                    init_labels.append(_beacon_label(data))
            elif isinstance(inner, str) and inner.strip():
                final_response = inner.strip()
            else:
                init_labels.append(_beacon_label(data))
            continue

        step = data.get("step_update")
        if isinstance(step, Mapping):
            index = step.get("step_index")
            index = index if isinstance(index, int) else None
            state = step.get("state")
            step_type = str(step.get("step_type") or "")
            delta = _step_delta(step)
            if delta:
                if index is not None:
                    step_texts[index] = _coalesce_chunks(step_texts.get(index, []), delta)
                else:
                    other_texts = _coalesce_chunks(other_texts, delta)
            tool_name = step.get("tool_name")
            if isinstance(tool_name, str) and tool_name.strip():
                name = tool_name.strip()
                if index is not None:
                    meta = step_meta.setdefault(index, {})
                    names = meta.setdefault("tools", [])
                    if name not in names:
                        names.append(name)
                else:
                    tool_counts[name] = tool_counts.get(name, 0) + 1
            if step.get("tool_calls") is not None:
                for kind, fragment in _extract_fragments(step):
                    if kind == "action" and fragment not in actions:
                        actions.append(fragment)
            for kind, fragment in _extract_fragments(step):
                if kind == "reasoning" and fragment not in reasoning:
                    reasoning.append(fragment)
            if index is not None and (state or step_type):
                meta = step_meta.setdefault(index, {})
                if state:
                    meta["state"] = str(state)
                if step_type:
                    meta["type"] = step_type
            elif index is None and not delta and not tool_name:
                if data.get("event") is not None:
                    init_labels.append(_beacon_label(data))
            continue

        if not _collect_fragments(data):
            if data.get("event") is not None:
                init_labels.append(_beacon_label(data))
            else:
                unparsed.append(candidate)

    if step_texts:
        if not final_response:
            final_index = max(step_texts)
            final_response = "".join(step_texts.pop(final_index)).strip()
        else:
            step_texts.pop(max(step_texts), None)
        notes = [
            f"**step {index}** \u2014 {''.join(chunks).strip()}"
            for index, chunks in sorted(step_texts.items())
            if "".join(chunks).strip()
        ]
    if other_texts and not final_response:
        final_response = max(other_texts, key=len).strip()
        other_texts = [text for text in other_texts if text != final_response]
    notes = [text for text in other_texts if text.strip()] + notes

    for index in sorted(step_meta):
        for name in step_meta[index].get("tools", []):
            tool_counts[name] = tool_counts.get(name, 0) + 1
    if tool_counts:
        actions = [
            f"- `{name}`" + (f" \u00d7{count}" if count > 1 else "")
            for name, count in sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))
        ] + actions
    text_chars = (
        len(final_response)
        + sum(len("".join(chunks)) for chunks in step_texts.values())
        + sum(len(text) for text in other_texts)
    )
    lifecycle = _render_lifecycle(init_labels, step_meta, final_status, text_chars)
    return _render_sections(final_response, notes, reasoning, actions, lifecycle, unparsed)


def build_prompt_payload(
    config: BridgeConfig,
    skill_block: str = "",
    skill_registry_path: Path | str | None = None,
) -> str:
    """Assembles the complete delegation payload sent with ``agy -p``."""
    envelope = config.envelope
    parts: list[str] = [
        "# ADVERSARIAL VERIFICATION DELEGATION ENVELOPE",
        "",
        (
            "You are Antigravity, engaged as a ruthless Staff Systems Architect and "
            "Verification Lead by an autonomous coding agent (Agent 1). Your mandate is "
            "adversarial: stress-test implementation plans, audit ASTs and wiring directly "
            "on disk, expose crash and concurrency failure modes, recalculate empirical "
            "claims, and falsify unproven assertions. Do not flatter, do not summarize "
            "politely, and do not stop at surface-level review."
        ),
        "",
        f"**Originating harness**: {envelope.harness}",
        "",
        "## REQUEST / PLAN UNDER REVIEW",
        envelope.prompt,
    ]
    if envelope.context.strip():
        parts += ["", "## CONTEXT & PRIOR ART", envelope.context.strip()]
    if envelope.claims_to_falsify:
        parts += ["", "## EMPIRICAL CLAIMS TO FALSIFY"]
        parts += [f"- {claim}" for claim in envelope.claims_to_falsify]
    if envelope.artifacts:
        parts += ["", "## ARTIFACTS ON DISK (mounted via --add-dir)"]
        parts += [f"- `{artifact}`" for artifact in envelope.artifacts]
    if envelope.notes.strip():
        parts += ["", "## OPERATOR NOTES", envelope.notes.strip()]
    if skill_block.strip():
        if skill_registry_path is not None:
            parts += [
                "",
                "## SKILL REGISTRY ON DISK",
                (
                    f"The complete adversarial skill registry is mounted at `{skill_registry_path}`. "
                    "The skill digests below are binding; read the raw skill files in full before "
                    "executing any procedure that demands exact steps."
                ),
            ]
        parts += ["", skill_block.strip()]
    parts += [
        "",
        "## OUTPUT MANDATE",
        "- Read `AGENTS.md` in the mounted workspace for the full calling protocol and reconciliation rules.",
        "- Return one complete Markdown critique. Explicitly separate Evidence, Findings, Risk Rating, and Required Revisions.",
        "- Cite file paths and line numbers for every code claim. No hedging, no summarizing, no silent omissions.",
        "- Your full response is surfaced verbatim to Agent 1; length is not a constraint.",
    ]
    return "\n".join(parts)


def _failure_critique(message: str) -> str:
    return "\n".join(
        [
            "# ANTIGRAVITY ADVERSARIAL DELEGATION REPORT",
            "",
            "## DELEGATION FAILED BEFORE CRITIQUE",
            "",
            message,
        ]
    )


def _failure_result(
    config: BridgeConfig,
    message: str,
    exit_code: int | None = None,
    warnings: list[str] | None = None,
) -> BridgeResult:
    return BridgeResult(
        success=False,
        model_used=config.model,
        failover_used=False,
        timed_out=False,
        rate_limited=False,
        exit_code=exit_code,
        attempts=[],
        critique_markdown=_failure_critique(message),
        error=message,
        warnings=list(warnings or []),
    )


def render_critique(
    config: BridgeConfig,
    executable: str,
    attempts: Sequence[AttemptResult],
    failover_used: bool,
    success: bool,
    error: str | None,
    warnings: Sequence[str] = (),
) -> str:
    """Builds the complete report Agent 1 ingests; raw streams live in attempts[]."""
    final = attempts[-1]
    lines: list[str] = [
        "# ANTIGRAVITY ADVERSARIAL DELEGATION REPORT",
        "",
        f"- **Executable**: `{executable}`",
        f"- **Workspace mounted**: `{config.resolved_workspace()}`",
        f"- **Model chain**: {' -> '.join(attempt.model for attempt in attempts)}",
        f"- **Failover engaged**: {'YES' if failover_used else 'NO'}",
        f"- **Verdict**: {'SUCCESS' if success else 'FAILED'} (exit code {final.exit_code})",
        f"- **Duration**: {final.duration_seconds:.2f}s",
    ]
    if error:
        lines += ["", f"> **Bridge error**: {error}"]
    if warnings:
        lines += ["", "> **Warnings**:"]
        lines += [f"> - {warning}" for warning in warnings]
    for index, attempt in enumerate(attempts):
        label = "PRIMARY" if index == 0 else f"ATTEMPT {index + 1}"
        lines += [
            "",
            f"## Antigravity Critique — `{attempt.model}` ({label})",
            "",
            aggregate_stream_json(attempt.stdout),
            "",
            (
                f"_Complete raw streams retained in this report under `attempts[]` "
                f"(stdout {len(attempt.stdout)} chars, stderr {len(attempt.stderr)} chars). "
                f"Use the bridge CLI `--json` for the full forensic payload._"
            ),
        ]
    return "\n".join(lines) + "\n"


def run_bridge(config: BridgeConfig, launcher: LaunchFn | None = None) -> BridgeResult:
    """Executes the delegation lifecycle with retries and quota controls.

    Transient failures (connection resets, 5xx, empty responses) are retried
    with exponential backoff. Rate limits trigger an optional quota wait or an
    operator-supplied rotation hook, then fail over to the fallback model with
    the payload unchanged. Every attempt and raw byte is retained in the result.
    """
    launch = launcher or launch_contained
    workspace = config.resolved_workspace()
    executable = config.executable or resolve_agy_executable()

    skill_blocks: list[str] = []
    registry_path: Path | None = None
    warnings: list[str] = []
    if config.skills or config.recommended_skills:
        try:
            registry_path, fell_back = resolve_skill_dir(workspace, config.skill_dir)
        except SkillError as exc:
            return _failure_result(config, f"Skill registry error: {exc}", exit_code=2)
        if fell_back:
            warnings.append(
                f"Workspace '{workspace}' has no Skills directory; "
                f"fell back to shipped registry at '{registry_path}'"
            )
        try:
            loader = SkillLoader(registry_path)
            warnings.extend(loader.warnings)
            active = loader.select(list(config.skills)) if config.skills else []
            if active:
                skill_blocks.append(
                    loader.render_prompt(
                        active,
                        heading="## ACTIVE ADVERSARIAL SKILLS (MANDATORY)",
                        note=(
                            "These skills are binding for this consultation; execute "
                            "their operating procedures exactly."
                        ),
                    )
                )
            if config.recommended_skills:
                active_names = {skill.name for skill in active}
                recommended = [
                    skill
                    for skill in loader.select(list(config.recommended_skills))
                    if skill.name not in active_names
                ]
                if recommended:
                    skill_blocks.append(
                        loader.render_prompt(
                            recommended,
                            heading="## RECOMMENDED ADVERSARIAL SKILLS (TASK-DEPENDENT)",
                            note=(
                                "Apply these when the task touches their domain; read "
                                "their raw files for exact procedures."
                            ),
                        )
                    )
            skill_blocks.append(loader.render_manifest())
        except SkillError as exc:
            return _failure_result(config, f"Skill registry error: {exc}", exit_code=2)

    extra_add_dirs: tuple[Path, ...] = ()
    if registry_path is not None:
        resolved_registry = registry_path.resolve()
        try:
            resolved_registry.relative_to(workspace)
        except ValueError:
            extra_add_dirs = (resolved_registry,)

    payload = build_prompt_payload(
        config,
        "\n\n".join(block for block in skill_blocks if block.strip()),
        registry_path if skill_blocks else None,
    )
    env = sanitize_environment()
    attempts: list[AttemptResult] = []
    launch_errors: list[str] = []
    max_retries = max(int(config.retries), 0)
    backoff = max(float(config.retry_backoff_seconds), 0.0)
    failover_used = False
    quota_hook_used = False
    quota_hook_output: str | None = None
    started = time.monotonic()

    run_id = new_run_id()
    emitter: LiveEmitter | None = None
    if config.live or config.live_callback is not None:
        sinks: list[Any] = []
        if config.live:
            resolved_live = config.resolved_live_dir(workspace)
            register_live_dir(resolved_live, workspace)
            try:
                sinks.append(
                    JsonlSink(
                        resolved_live,
                        run_id,
                        config.live_keep_runs,
                    )
                )
            except OSError:
                sinks.append(NullSink())
        if config.live_callback is not None:
            sinks.append(CallbackSink(config.live_callback))
        if sinks:
            emitter = LiveEmitter(run_id, sinks)
    current_model: dict[str, str] = {"name": config.model}

    def _emit(kind: str, text: str = "", model: str = "", **meta: Any) -> None:
        if emitter is not None:
            emitter.emit(kind, text, model=model, **meta)

    def _on_raw_line(stream: str, line: str) -> None:
        if emitter is None:
            return
        if stream == "stderr":
            text = line.rstrip("\r\n")
            if text:
                _emit("stderr", text, model=current_model["name"])
            return
        for kind, text, meta in parse_stream_line(line):
            _emit(kind, text, model=current_model["name"], **meta)

    def _finish(success: bool, error: str | None) -> None:
        _emit(
            "run_end",
            "delegation complete" if success else "delegation failed",
            model=current_model["name"],
            success=success,
            error=error,
            failover_used=failover_used,
            resets_in=reset_text,
            quota_hook_used=quota_hook_used,
            elapsed_seconds=round(time.monotonic() - started, 3),
            attempts=len(attempts),
        )
        if emitter is not None:
            emitter.close()

    _emit(
        "run_start",
        "",
        model=config.model,
        workspace=str(workspace),
        executable=executable,
        primary_model=config.model,
        fallback_model=config.fallback_model,
        skills=list(config.skills),
        harness=config.envelope.harness,
        prompt_chars=len(config.envelope.prompt),
    )

    def _execute(model_name: str) -> AttemptResult:
        current_model["name"] = model_name
        command = build_agy_command(
            executable,
            payload,
            workspace,
            model_name,
            config.print_timeout_seconds,
            extra_add_dirs=extra_add_dirs,
        )
        _emit("attempt_start", model_name, model=model_name)
        result = launch(
            command,
            workspace,
            env,
            config.hard_timeout_seconds(),
            _on_raw_line,
        )
        result.model = model_name
        _emit(
            "attempt_end",
            f"exit {result.exit_code}",
            model=model_name,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            rate_limited=result.rate_limited,
            timed_out=result.timed_out,
            interrupted=result.interrupted,
        )
        return result

    def _run_model_chain(model_name: str) -> tuple[list[AttemptResult], str]:
        results: list[AttemptResult] = []
        for index in range(max_retries + 1):
            try:
                attempt = _execute(model_name)
            except FileNotFoundError as exc:
                launch_errors.append(
                    f"Antigravity executable {executable!r} was not found: {exc}. "
                    "Install the CLI or pass an explicit executable path."
                )
                return results, "LAUNCH_ERROR"
            except OSError as exc:
                launch_errors.append(f"Failed to launch Antigravity: {exc}")
                return results, "LAUNCH_ERROR"
            results.append(attempt)
            attempts.append(attempt)
            if attempt.rate_limited and not attempt.timed_out:
                return results, "RATE_LIMITED"
            if attempt_succeeded(attempt):
                return results, "SUCCESS"
            if attempt.timed_out or attempt.interrupted:
                return results, "FATAL"
            if not is_transient_failure(attempt):
                return results, "FATAL"
            if index < max_retries:
                _emit("retry", f"retry {index + 1}/{max_retries}", model=model_name)
                if backoff > 0:
                    time.sleep(backoff * (2 ** index))
        return results, "EXHAUSTED"

    primary_results, status = _run_model_chain(config.model)
    reset_text: str | None = None
    reset_seconds: int | None = None

    if status == "RATE_LIMITED" and primary_results:
        primary_text = primary_results[-1].combined_output
        reset_text = extract_reset_text(primary_text)
        reset_seconds = parse_reset_seconds(primary_text)

        if (
            config.quota_wait_seconds > 0
            and reset_seconds is not None
            and reset_seconds <= config.quota_wait_seconds
        ):
            _emit(
                "quota_wait",
                f"waiting {reset_seconds}s for quota reset",
                model=config.model,
                seconds=reset_seconds,
            )
            if reset_seconds > 0:
                time.sleep(reset_seconds)
            primary_results, status = _run_model_chain(config.model)

        if status != "SUCCESS" and config.quota_hook:
            _emit("quota_hook", "running quota hook", model=config.model)
            hook_ok, quota_hook_output = run_quota_hook(config.quota_hook)
            quota_hook_used = True
            _emit(
                "quota_hook_end",
                "hook succeeded" if hook_ok else "hook failed",
                model=config.model,
                ok=hook_ok,
            )
            warnings.append(
                "Quota hook executed "
                f"({'succeeded' if hook_ok else 'failed'}): {config.quota_hook}"
            )
            if hook_ok:
                primary_results, status = _run_model_chain(config.model)

    if (
        status != "SUCCESS"
        and status in ("RATE_LIMITED", "EXHAUSTED")
        and config.fallback_model
        and config.fallback_model != config.model
    ):
        failover_used = True
        _emit(
            "failover",
            f"{config.model} -> {config.fallback_model}",
            model=config.fallback_model,
            from_model=config.model,
            to_model=config.fallback_model,
        )
        _, status = _run_model_chain(config.fallback_model)

    final = attempts[-1] if attempts else None
    success = status == "SUCCESS" and final is not None

    if final is not None and final.rate_limited and reset_text is None:
        reset_text = extract_reset_text(final.combined_output)
        reset_seconds = parse_reset_seconds(final.combined_output)

    if final is None:
        message = "; ".join(launch_errors) or "Antigravity did not run."
        exit_code: int | None = 127 if "not found" in message.lower() else 1
        _finish(False, message)
        return BridgeResult(
            success=False,
            model_used=config.model,
            failover_used=failover_used,
            timed_out=False,
            rate_limited=False,
            exit_code=exit_code,
            attempts=[],
            critique_markdown=_failure_critique(message),
            error=message,
            warnings=warnings,
            quota_hook_used=quota_hook_used,
            quota_hook_output=quota_hook_output,
            resets_in=reset_text,
            reset_seconds=reset_seconds,
        )

    error: str | None = None
    if not success:
        if final.interrupted:
            error = (
                "Antigravity delegation was interrupted by the operator; "
                "the process tree was terminated."
            )
        elif final.timed_out:
            error = (
                f"Antigravity timed out after {config.hard_timeout_seconds()}s "
                f"(last exit code: {final.exit_code})."
            )
        elif final.rate_limited:
            chain = ", ".join(dict.fromkeys(attempt.model for attempt in attempts))
            error = f"Rate limit exhausted on all attempted models ({chain})."
            if reset_text:
                error += f" Quota resets in {reset_text}."
            error += (
                " Wait for the reset, re-authenticate, or configure --quota-hook; "
                "full output is preserved below."
            )
        elif status == "EXHAUSTED":
            error = (
                f"No usable critique after {max_retries + 1} attempt(s) on the final model; "
                "transient errors or empty responses persisted. "
                "Full stdout and stderr are preserved below."
            )
        elif final.exit_code not in (0, None):
            error = (
                f"Antigravity exited with code {final.exit_code}. "
                "Full stdout and stderr are preserved below."
            )
        else:
            error = (
                "Antigravity finished without a usable critique. "
                "Full stdout and stderr are preserved below."
            )

    _finish(success, error)
    critique = render_critique(
        config, executable, attempts, failover_used, success, error, warnings
    )
    return BridgeResult(
        success=success,
        model_used=final.model,
        failover_used=failover_used,
        timed_out=final.timed_out,
        rate_limited=final.rate_limited,
        exit_code=final.exit_code,
        attempts=attempts,
        critique_markdown=critique,
        error=error,
        warnings=warnings,
        quota_hook_used=quota_hook_used,
        quota_hook_output=quota_hook_output,
        resets_in=reset_text,
        reset_seconds=reset_seconds,
    )


def write_report(workspace: Path, result: BridgeResult) -> Path:
    """Persists the complete result as JSON under ``<workspace>/.antigravity-reports/``.

    Guarantees an on-disk, untruncated record of the delegation independent of
    any harness-side output limits. Returns the report path; raises ``OSError``
    when the workspace is not writable.
    """
    report_dir = Path(workspace) / ".antigravity-reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = report_dir / f"antigravity-report-{stamp}-{uuid.uuid4().hex[:8]}.json"
    path.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _load_envelope(path: Path) -> tuple[DelegationEnvelope, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    envelope = DelegationEnvelope.from_mapping(data)
    return envelope, data if isinstance(data, dict) else {}


def _build_config(args: argparse.Namespace, parser: argparse.ArgumentParser) -> BridgeConfig:
    envelope_data: dict[str, Any] = {}
    envelope: DelegationEnvelope | None = None
    if args.envelope:
        try:
            envelope, envelope_data = _load_envelope(args.envelope)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            parser.error(f"invalid --envelope: {exc}")

    prompt = (args.prompt or "").strip() or (envelope.prompt if envelope else "")
    if not prompt:
        parser.error("a non-empty --prompt (or --envelope with a prompt) is required")

    context_parts: list[str] = []
    if envelope and envelope.context:
        context_parts.append(envelope.context)
    if args.context:
        context_parts.append(args.context.strip())

    skills_selector = args.skills
    envelope_skills = envelope_data.get("skills")
    if not skills_selector and isinstance(envelope_skills, str):
        skills_selector = envelope_skills
    if not skills_selector and isinstance(envelope_skills, (list, tuple)):
        skills_selector = ",".join(str(item) for item in envelope_skills)

    claims = tuple(envelope.claims_to_falsify if envelope else ()) + tuple(args.claim or ())
    artifacts = tuple(envelope.artifacts if envelope else ()) + tuple(args.artifact or ())

    merged = DelegationEnvelope(
        prompt=prompt,
        harness=(args.harness or (envelope.harness if envelope else "agent-1")).strip()
        or "agent-1",
        context="\n\n".join(part for part in context_parts if part).strip(),
        claims_to_falsify=claims,
        artifacts=artifacts,
        notes=(envelope.notes if envelope else ""),
    )

    skills: tuple[str, ...] = ()
    if skills_selector:
        skills = tuple(
            part.strip() for part in str(skills_selector).split(",") if part.strip()
        )

    return BridgeConfig(
        envelope=merged,
        workspace=args.workspace,
        model=args.model,
        fallback_model=args.fallback_model,
        skills=skills,
        skill_dir=args.skill_dir,
        print_timeout_seconds=args.print_timeout,
        grace_seconds=args.grace_seconds,
        retries=args.retries,
        retry_backoff_seconds=args.retry_backoff,
        quota_wait_seconds=args.quota_wait,
        quota_hook=args.quota_hook,
        live=args.live,
        live_dir=args.live_dir,
        live_keep_runs=args.live_keep_runs,
        executable=args.executable,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="antigravity_bridge.py",
        description=(
            "Spawn Google Antigravity (agy) as an adversarial verification sub-agent "
            "and return its complete, untruncated critique."
        ),
    )
    parser.add_argument("--prompt", "-p", help="Plan, question, or artifact description to stress-test.")
    parser.add_argument("--envelope", type=Path, help="Path to a JSON delegation envelope.")
    parser.add_argument("--context", default="", help="Additional context appended to the envelope.")
    parser.add_argument("--claim", action="append", default=[], help="Empirical claim to falsify (repeatable).")
    parser.add_argument("--artifact", action="append", default=[], help="Artifact path to audit (repeatable).")
    parser.add_argument("--skills", default="", help="Comma-separated skill names, or 'all' for the full registry.")
    parser.add_argument(
        "--skill-dir",
        type=Path,
        default=None,
        help=(
            "Skill registry directory (default: <workspace>/Skills, falling back "
            "to the registry shipped with this bridge)."
        ),
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace root mounted via --add-dir (default: cwd).")
    parser.add_argument("--model", default=DEFAULT_PRIMARY_MODEL, help=f"Primary model (default: {DEFAULT_PRIMARY_MODEL}).")
    parser.add_argument("--fallback-model", default=DEFAULT_FALLBACK_MODEL, help=f"Quota failover model (default: {DEFAULT_FALLBACK_MODEL}).")
    parser.add_argument("--print-timeout", type=int, default=DEFAULT_PRINT_TIMEOUT_SECONDS, help="agy --print-timeout in seconds.")
    parser.add_argument("--grace-seconds", type=int, default=DEFAULT_GRACE_SECONDS, help="Extra seconds before the bridge force-kills agy.")
    parser.add_argument("--retries", type=int, default=2, help="Transient-failure retries per model (default: 2).")
    parser.add_argument("--retry-backoff", type=float, default=5.0, help="Base seconds for exponential retry backoff (default: 5.0).")
    parser.add_argument("--quota-wait", type=int, default=0, help="Max seconds to wait for a quota reset before failing over (default: 0 = never wait).")
    parser.add_argument("--quota-hook", default=None, help="Command to run on quota exhaustion (e.g. an account-switch script) before retrying.")
    parser.add_argument("--live", action=argparse.BooleanOptionalAction, default=True, help="Emit live viewer events (default: on; use --no-live to disable).")
    parser.add_argument("--live-dir", type=Path, default=None, help="Live event directory (default: <workspace>/.antigravity-reports/live).")
    parser.add_argument("--live-keep-runs", type=int, default=DEFAULT_KEEP_RUNS, help="Live run files to retain (default: 20).")
    parser.add_argument("--executable", default=None, help="Explicit agy executable path (skips resolution).")
    parser.add_argument("--harness", default="", help="Originating harness name recorded in the envelope.")
    parser.add_argument("--json", action="store_true", help="Emit the complete result as JSON.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve skills and print the exact command without executing agy.")
    parser.add_argument("--list-skills", action="store_true", help="List available skills and exit.")
    parser.add_argument("--status", action="store_true", help="Print bridge health (executable, workspace, registry, models) and exit.")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser()
    if args.status:
        registry = args.skill_dir or (workspace.resolve() / DEFAULT_SKILL_DIR)
        live_dir = args.live_dir or (workspace.resolve() / LIVE_DIR_NAME)
        status: dict[str, Any] = {
            "executable": resolve_agy_executable(),
            "workspace": str(workspace.resolve()),
            "skill_registry": str(registry),
            "primary_model": args.model,
            "fallback_model": args.fallback_model,
            "retries": args.retries,
            "quota_wait_seconds": args.quota_wait,
            "quota_hook_configured": bool(args.quota_hook),
            "live_enabled": bool(args.live),
            "live_dir": str(live_dir),
            "live_keep_runs": args.live_keep_runs,
        }
        try:
            loader = SkillLoader(registry)
            status["skills"] = [skill.name for skill in loader.skills]
            status["warnings"] = loader.warnings
        except SkillError as exc:
            status["skill_error"] = str(exc)
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0
    if args.list_skills:
        loader = SkillLoader(args.skill_dir or (workspace.resolve() / DEFAULT_SKILL_DIR))
        try:
            for skill in loader.skills:
                print(f"{skill.name} (v{skill.version}) [{skill.kind}]")
        except SkillError as exc:
            print(f"[SKILL REGISTRY ERROR] {exc}", file=sys.stderr)
            return 2
        for warning in loader.warnings:
            print(f"[SKILL WARNING] {warning}", file=sys.stderr)
        return 0

    config = _build_config(args, parser)

    if args.dry_run:
        skill_block = ""
        registry_path = (
            Path(config.skill_dir)
            if config.skill_dir
            else (config.resolved_workspace() / DEFAULT_SKILL_DIR)
        )
        if config.skills:
            try:
                loader = SkillLoader(registry_path)
                skill_block = loader.render_prompt(loader.select(list(config.skills)))
            except SkillError as exc:
                print(f"[SKILL REGISTRY ERROR] {exc}", file=sys.stderr)
                return 2
            for warning in loader.warnings:
                print(f"[SKILL WARNING] {warning}", file=sys.stderr)
        payload = build_prompt_payload(
            config,
            skill_block,
            registry_path if config.skills else None,
        )
        executable = config.executable or resolve_agy_executable()
        command = build_agy_command(
            executable,
            payload,
            config.resolved_workspace(),
            config.model,
            config.print_timeout_seconds,
        )
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "executable": executable,
                    "workspace": str(config.resolved_workspace()),
                    "command": command,
                    "payload": payload,
                    "hard_timeout_seconds": config.hard_timeout_seconds(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    result = run_bridge(config)

    for warning in result.warnings:
        print(f"[SKILL WARNING] {warning}", file=sys.stderr)

    try:
        report_path: str | None = str(write_report(config.resolved_workspace(), result))
    except OSError:
        report_path = None

    if args.json:
        data = result.to_dict()
        if report_path:
            data["report_path"] = report_path
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(result.critique_markdown)
        if result.error:
            print(f"\n[BRIDGE ERROR] {result.error}", file=sys.stderr)
        if report_path:
            print(f"[BRIDGE] Complete JSON report: {report_path}", file=sys.stderr)

    if result.success:
        return 0
    if result.exit_code is not None and 0 < result.exit_code <= 255:
        return result.exit_code
    if result.exit_code is None and result.error and "not found" in result.error.lower():
        return 127
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
