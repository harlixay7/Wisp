"""OS-specific process and account helpers for the Wisp viewer.

Process inspection backs ``--replace`` when an old viewer cannot be shut down
through its authenticated handshake: Windows uses tasklist/PowerShell/taskkill,
POSIX reads ``/proc`` (falling back to ``ps``) and signals with SIGTERM.
Account switching relies on the Windows Credential Manager and is Windows-only.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from tools.antigravity_bridge import resolve_agy_executable

PROCESS_QUERY_TIMEOUT_SECONDS = 15
POWERSHELL_QUERY_TIMEOUT_SECONDS = 20
VIEWER_IMAGE_MARKERS = ("python", "electron", "wisp")
VIEWER_COMMAND_MARKER = "antigravity_viewer"
AGY_CREDENTIAL_TARGET = "LegacyGeneric:target=gemini:antigravity"


def _run_query(args: list[str], timeout: float) -> str | None:
    """Runs a read-only query command; returns stdout, or None if it failed to run."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout or ""


def _read_proc_file(pid: int, name: str) -> bytes | None:
    try:
        return (Path("/proc") / str(int(pid)) / name).read_bytes()
    except OSError:
        return None


def _ps_field(pid: int, field_name: str) -> str | None:
    """One ``ps`` column for ``pid``; the fallback where /proc is absent (macOS)."""
    output = _run_query(
        ["ps", "-p", str(int(pid)), "-o", f"{field_name}="], PROCESS_QUERY_TIMEOUT_SECONDS
    )
    return (output or "").strip() or None


def pid_image_name(pid: int) -> str | None:
    """Best-effort image name of a live PID (None when the PID does not exist)."""
    if os.name != "nt":
        comm = _read_proc_file(pid, "comm")
        if comm is not None:
            return comm.decode("utf-8", errors="replace").strip().lower() or None
        name = _ps_field(pid, "comm")
        return Path(name).name.lower() if name else None
    output = _run_query(
        ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
        PROCESS_QUERY_TIMEOUT_SECONDS,
    )
    lines = (output or "").strip().splitlines()
    if not lines or lines[0].upper().startswith(("INFO", '"ERROR"')):
        return None
    return lines[0].split(",")[0].strip('"').lower() or None


def pid_command_line(pid: int) -> str | None:
    """Best-effort command line of a live PID (None when absent or unknown)."""
    if os.name != "nt":
        raw = _read_proc_file(pid, "cmdline")
        if raw is not None:
            # /proc/<pid>/cmdline separates arguments with NUL bytes.
            return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip() or None
        return _ps_field(pid, "command")
    query = f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine"
    output = _run_query(
        ["powershell", "-NoProfile", "-Command", query], POWERSHELL_QUERY_TIMEOUT_SECONDS
    )
    return (output or "").strip() or None


def terminate_process(pid: int) -> bool:
    """Forcefully stops ``pid`` (and, on Windows, its child tree)."""
    if os.name != "nt":
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            return False
        return True
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            capture_output=True,
            timeout=PROCESS_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def looks_like_viewer_process(image_name: str | None, pid: int | None = None) -> bool:
    """Guards a forced kill against PID reuse.

    The image name must look like python/electron/wisp AND the command line
    must mention antigravity_viewer, so a recycled PID belonging to an
    unrelated process is never killed.
    """
    if not image_name:
        return False
    if not any(marker in image_name for marker in VIEWER_IMAGE_MARKERS):
        return False
    if pid is None:
        return False
    return VIEWER_COMMAND_MARKER in (pid_command_line(pid) or "")


def switch_account() -> dict[str, Any]:
    """Clears the stored Google credential and opens an interactive agy sign-in.

    The credential lives in the Windows Credential Manager and the sign-in
    opens in a new console window, so this is Windows-only; elsewhere it
    reports that nothing was done instead of claiming success.
    """
    if os.name != "nt":
        return {"status": "unsupported", "output": "Account switching is Windows-only."}
    output: list[str] = []
    try:
        result = subprocess.run(
            ["cmdkey", f"/delete:{AGY_CREDENTIAL_TARGET}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROCESS_QUERY_TIMEOUT_SECONDS,
        )
        message = (result.stdout or "").strip() or (result.stderr or "").strip()
        if message:
            output.append(message)
    except (OSError, subprocess.SubprocessError) as exc:
        output.append(f"credential clear failed: {exc}")
    executable = resolve_agy_executable()
    try:
        # An argument list (no shell) keeps a path with spaces or shell
        # metacharacters from being reinterpreted.
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "Antigravity Sign-In", "cmd.exe", "/k", executable]
        )
        output.append("Sign-in terminal launched — complete the browser OAuth flow.")
    except OSError as exc:
        output.append(f"sign-in terminal failed to launch: {exc}")
    return {"status": "switching", "output": "\n".join(output)}
