"""Operator capture helpers for Wisp hotkey asks.

Two capture modes:

* **selection** — simulate Ctrl+C, read the newly copied text, then restore
  the previous *text* clipboard content. If the clipboard held non-text
  content (an image, files), the target app's copy replaces it and only text
  can be restored: non-text clipboard state is NOT preserved.
* **snip** — launch the native Windows snip overlay (``ms-screenclip:``) and
  wait for the resulting image to appear on the clipboard, saving it as PNG.

The capture entry points are timeout-bound and return structured results
instead of raising, so the server request handler always gets an answer.
The storage helpers (:func:`new_capture_path`, :func:`save_pasted_image`)
raise ``OSError`` when the capture directory cannot be written; callers turn
that into an error response. ``WISP_CAPTURE_FAKE`` provides deterministic
responses for tests (``text:<value>``, ``image:<path>``, ``none``).
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent / "capture"
COPY_SELECTION_SCRIPT = SCRIPTS_DIR / "copy_selection.ps1"
SAVE_CLIPBOARD_SCRIPT = SCRIPTS_DIR / "save_clipboard_png.ps1"
CAPTURE_DIR_NAME = "captures"
DEFAULT_KEEP_CAPTURES = 50
SELECTION_TIMEOUT_MS = 750
SNIP_TIMEOUT_SECONDS = 60
CREATE_NO_WINDOW = 0x08000000

# Win32 constants.
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002
KEY_DOWN_MASK = 0x8000
VK_CONTROL = 0x11
VK_C = 0x43
_MODIFIER_VKS = (0x11, 0x12, 0x10, 0x5B, 0x5C)  # Ctrl, Alt, Shift, LWin, RWin


def capture_dir(live_dir: Path) -> Path:
    """Directory holding snips and pasted images (a sibling of the live dir)."""
    return Path(live_dir).parent / CAPTURE_DIR_NAME


def _powershell() -> str:
    return shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"


def _no_window_flags() -> int:
    return CREATE_NO_WINDOW if os.name == "nt" else 0


def _run_powershell(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-STA", *args],
        capture_output=True,
        timeout=timeout,
        creationflags=_no_window_flags(),
    )


def fake_capture() -> dict[str, Any] | None:
    """Returns a scripted capture when ``WISP_CAPTURE_FAKE`` is set, else None."""
    raw = (os.environ.get("WISP_CAPTURE_FAKE") or "").strip()
    if not raw:
        return None
    if raw.startswith("text:"):
        return {"kind": "text", "text": raw[len("text:") :]}
    if raw.startswith("image:"):
        return {"kind": "image", "path": raw[len("image:") :]}
    return {"kind": "none", "reason": "Nothing was captured."}


def _clipboard_text() -> str | None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_clipboard_text(text: str) -> None:
    """Places ``text`` on the real clipboard (binds the Win32 modules)."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _set_clipboard_text_with(user32, kernel32, text)


def _set_clipboard_text_with(user32: Any, kernel32: Any, text: str) -> None:
    """Places ``text`` on the clipboard without leaking the allocated HGLOBAL.

    Memory from ``GlobalAlloc`` belongs to the system only after a
    *successful* ``SetClipboardData`` and must never be freed afterwards.
    On every other path (lock failure, clipboard busy, set failure, or an
    exception in between) the ``finally`` frees it. The modules are
    parameters so tests can drive each path with fakes.
    """
    buffer = ctypes.create_unicode_buffer(str(text) + "\x00")
    size = ctypes.sizeof(buffer)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return
    transferred = False
    try:
        pointer = kernel32.GlobalLock(handle)
        if pointer:
            try:
                ctypes.memmove(pointer, buffer, size)
            finally:
                kernel32.GlobalUnlock(handle)
            if user32.OpenClipboard(None):
                try:
                    user32.EmptyClipboard()
                    transferred = bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
                finally:
                    user32.CloseClipboard()
    finally:
        if not transferred:
            try:
                kernel32.GlobalFree(handle)
            except Exception:
                # A failing free must not mask the error that got us here.
                pass


def _wait_for_modifier_release(timeout_ms: int = 900) -> None:
    """Waits until the hotkey modifiers are physically released.

    The global hotkey fires on key-down; synthesizing Ctrl+C while the operator
    still holds Ctrl+Alt makes the target app see Ctrl+Alt+C and skip the copy.
    """
    user32 = ctypes.windll.user32
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & KEY_DOWN_MASK for vk in _MODIFIER_VKS):
            return
        time.sleep(0.02)


def _native_selection_capture(timeout_ms: int = 650) -> str | None:
    """Ctrl+C round-trip implemented with Win32 APIs (no PowerShell startup)."""
    user32 = ctypes.windll.user32
    _wait_for_modifier_release()
    previous = _clipboard_text()
    sequence_before = user32.GetClipboardSequenceNumber()
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_C, 0, 0, 0)
    user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    found: str | None = None
    while time.monotonic() < deadline:
        time.sleep(0.04)
        changed = user32.GetClipboardSequenceNumber() != sequence_before
        current = _clipboard_text()
        if current and current.strip() and (changed or current != previous):
            found = current
            break
    if previous and previous != found:
        try:
            _set_clipboard_text(previous)
        except Exception:
            # Restoring the operator's clipboard is best effort; the capture
            # itself already succeeded or failed on its own terms.
            pass
    return found


def capture_selection(timeout_ms: int = SELECTION_TIMEOUT_MS) -> dict[str, Any]:
    """Attempts to capture the currently selected text in the foreground app."""
    if os.name != "nt":
        return {"kind": "none", "reason": "Selection capture is available on Windows only."}
    try:
        text = _native_selection_capture(timeout_ms)
        if text:
            return {"kind": "text", "text": text}
        return {"kind": "none", "reason": "No text selection detected."}
    except Exception:
        # Any ctypes/Win32 failure in the fast path falls through to the
        # slower but more tolerant PowerShell helper below.
        pass

    script = COPY_SELECTION_SCRIPT
    if not script.is_file():
        return {"kind": "none", "reason": "Selection capture helper is missing."}
    try:
        result = _run_powershell(
            ["-ExecutionPolicy", "Bypass", "-File", str(script), "-TimeoutMs", str(timeout_ms)],
            timeout=(timeout_ms / 1000.0) + 6.0,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"kind": "none", "reason": f"Selection capture failed: {exc}"}
    if result.returncode != 0:
        return {"kind": "none", "reason": "Selection capture helper failed."}
    text = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    if not text:
        return {"kind": "none", "reason": "No text selection detected."}
    return {"kind": "text", "text": text}


def start_snip() -> dict[str, Any]:
    """Opens the native Windows snip overlay so the operator can select a region."""
    if os.name != "nt":
        return {"kind": "none", "reason": "Screen snip is available on Windows only."}
    try:
        subprocess.Popen(
            ["explorer", "ms-screenclip:"],
            shell=False,
            creationflags=_no_window_flags(),
        )
    except OSError as exc:
        return {"kind": "none", "reason": f"Could not open the snip overlay: {exc}"}
    return {"kind": "snip_started"}


def wait_for_clipboard_image(
    destination: Path, timeout_seconds: int = SNIP_TIMEOUT_SECONDS
) -> bool:
    """Waits for the snipped image on the clipboard and saves it as PNG."""
    script = SAVE_CLIPBOARD_SCRIPT
    if os.name != "nt" or not script.is_file():
        return False
    try:
        result = _run_powershell(
            [
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Path",
                str(destination),
                "-TimeoutSec",
                str(timeout_seconds),
            ],
            timeout=timeout_seconds + 10.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0 and destination.is_file()


def _unique_capture_name(prefix: str, suffix: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{os.urandom(3).hex()}{suffix}"


def new_capture_path(live_dir: Path) -> Path:
    """Returns a unique capture path; the random suffix stops two snips in the
    same second from overwriting each other. Raises ``OSError`` when the
    capture directory cannot be created."""
    directory = capture_dir(live_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / _unique_capture_name("capture", ".png")


def relative_to_workspace(path: Path, workspace: Path) -> str:
    """Returns ``path`` relative to ``workspace`` for display and artifacts.

    Outside-workspace paths fall back to their absolute form; ingestion paths
    (``/api/ask``) reject such paths upstream, and the capture/upload pipelines
    only ever produce workspace-internal paths, so the fallback is a display
    concern rather than a containment boundary.
    """
    try:
        relative = Path(path).resolve().relative_to(Path(workspace).resolve())
    except ValueError:
        return str(path).replace("\\", "/")
    return str(relative).replace("\\", "/")


def sniff_image_suffix(blob: bytes) -> str | None:
    """Returns the canonical extension for a supported image payload, else None."""
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if blob.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    return None


def save_pasted_image(live_dir: Path, blob: bytes) -> Path | None:
    """Persists a pasted image under the captures dir; returns None for junk.

    Raises ``OSError`` when the image cannot be written.
    """
    suffix = sniff_image_suffix(blob)
    if suffix is None:
        return None
    directory = capture_dir(live_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / _unique_capture_name("paste", suffix)
    destination.write_bytes(blob)
    prune_captures(live_dir)
    return destination


def prune_captures(live_dir: Path, keep: int = DEFAULT_KEEP_CAPTURES) -> None:
    directory = capture_dir(live_dir)
    try:
        files = sorted(
            (
                path
                for pattern in ("capture-*.png", "paste-*")
                for path in directory.glob(pattern)
                if path.is_file()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[max(keep, 1) :]:
        try:
            stale.unlink()
        except OSError:
            pass


def capture_auto(
    live_dir: Path,
    *,
    prefer_selection: bool = True,
    snip_timeout_seconds: int = SNIP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Captures a selection when present, otherwise falls through to snip.

    Always returns a dict with ``kind`` in ``{"text", "image", "none"}`` and
    never raises. Images are saved under ``.antigravity-reports/captures/``.
    """
    scripted = fake_capture()
    if scripted is not None:
        try:
            fake_delay = float(os.environ.get("WISP_CAPTURE_FAKE_DELAY", "0") or 0)
        except ValueError:
            fake_delay = 0.0
        if fake_delay > 0:
            time.sleep(fake_delay)
        return scripted

    if prefer_selection:
        selection = capture_selection()
        if selection.get("kind") == "text":
            return selection

    started = start_snip()
    if started.get("kind") != "snip_started":
        return started
    try:
        destination = new_capture_path(live_dir)
    except OSError as exc:
        return {"kind": "none", "reason": f"Could not prepare the capture folder: {exc}"}
    if wait_for_clipboard_image(destination, timeout_seconds=snip_timeout_seconds):
        prune_captures(live_dir)
        return {"kind": "image", "path": str(destination)}
    return {"kind": "none", "reason": "Snip was cancelled or timed out."}
