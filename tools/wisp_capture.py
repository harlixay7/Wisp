"""Operator capture helpers for Wisp hotkey asks.

Two capture modes:

* **selection** — simulate Ctrl+C, read the newly copied text, then restore
  the previous *text* clipboard content. Honesty note (audit #30): if the
  clipboard held non-text content (an image, files), the target app's copy
  replaces it and only text can be restored — non-text clipboard state is NOT
  preserved. The docstring previously claimed image clipboards were never
  touched, which was wrong.
* **snip** — launch the native Windows snip overlay (``ms-screenclip:``) and
  wait for the resulting image to appear on the clipboard, saving it as PNG.

Everything is timeout-bound, crash-contained, and returns structured results;
no function raises into the server request handler. ``WISP_CAPTURE_FAKE``
provides deterministic responses for tests (``text:<value>``,
``image:<path>``, ``none``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

CAPTURE_DIR_NAME = Path(".antigravity-reports") / "captures"
SCRIPTS_DIR = Path(__file__).resolve().parent / "capture"
COPY_SELECTION_SCRIPT = SCRIPTS_DIR / "copy_selection.ps1"
SAVE_CLIPBOARD_SCRIPT = SCRIPTS_DIR / "save_clipboard_png.ps1"
DEFAULT_KEEP_CAPTURES = 50
SELECTION_TIMEOUT_MS = 750
SNIP_TIMEOUT_SECONDS = 60
CREATE_NO_WINDOW = 0x08000000


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
    if raw == "none":
        return {"kind": "none", "reason": "Nothing was captured."}
    if raw.startswith("text:"):
        return {"kind": "text", "text": raw[len("text:") :]}
    if raw.startswith("image:"):
        return {"kind": "image", "path": raw[len("image:") :]}
    return {"kind": "none", "reason": "Nothing was captured."}


def _clipboard_text() -> str | None:
    import ctypes
    from ctypes import wintypes

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
        handle = user32.GetClipboardData(13)
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
    """Public wrapper binding the real Win32 modules (see the internal form)."""
    import ctypes
    from ctypes import wintypes

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

    Ownership rules (audit #31, GATE-4 FL-008): memory allocated with
    ``GlobalAlloc`` is either (a) transferred to the system by a *successful*
    ``SetClipboardData`` — never freed afterwards — or (b) released with
    ``GlobalFree`` by the single ownership check in the ``finally``, which
    fires on every failure or exception path (allocation, lock, clipboard
    open, unexpected error between open and set).
    """
    import ctypes

    buffer = ctypes.create_unicode_buffer(str(text) + "\x00")
    size = ctypes.sizeof(buffer)
    handle = kernel32.GlobalAlloc(0x0002, size)
    if not handle:
        return
    transferred = False
    clipboard_open = False
    try:
        pointer = kernel32.GlobalLock(handle)
        if pointer:
            try:
                ctypes.memmove(pointer, buffer, size)
            finally:
                kernel32.GlobalUnlock(handle)
            if user32.OpenClipboard(None):
                clipboard_open = True
                try:
                    user32.EmptyClipboard()
                    transferred = bool(user32.SetClipboardData(13, handle))
                finally:
                    user32.CloseClipboard()
                    clipboard_open = False
    finally:
        if clipboard_open:
            try:
                user32.CloseClipboard()
            except Exception:
                pass
        if not transferred and handle:
            try:
                kernel32.GlobalFree(handle)
            except Exception:
                pass


_MODIFIER_VKS = (0x11, 0x12, 0x10, 0x5B, 0x5C)  # Ctrl, Alt, Shift, LWin, RWin


def _wait_for_modifier_release(timeout_ms: int = 900) -> None:
    """Waits until the hotkey modifiers are physically released.

    The global hotkey fires on key-down; synthesizing Ctrl+C while the operator
    still holds Ctrl+Alt makes the target app see Ctrl+Alt+C and skip the copy.
    """
    import ctypes

    user32 = ctypes.windll.user32
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in _MODIFIER_VKS):
            return
        time.sleep(0.02)


def _native_selection_capture(timeout_ms: int = 650) -> str | None:
    """Ctrl+C round-trip implemented with Win32 APIs (no PowerShell startup)."""
    import ctypes

    user32 = ctypes.windll.user32
    VK_CONTROL = 0x11
    VK_C = 0x43
    KEYEVENTF_KEYUP = 0x0002
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


def new_capture_path(live_dir: Path) -> Path:
    """Returns a collision-proof capture path (audit #32: random suffix, like
    the paste path, so two snips finishing in the same second never clobber
    each other)."""
    capture_dir = Path(live_dir).parent / "captures"
    capture_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return capture_dir / f"capture-{stamp}-{os.urandom(3).hex()}.png"


def relative_to_workspace(path: Path, workspace: Path) -> str:
    """Returns ``path`` relative to ``workspace`` for display and artifacts.

    Outside-workspace paths fall back to their absolute form; ingestion paths
    (``/api/ask``) reject such paths upstream, and the capture/upload pipelines
    only ever produce workspace-internal paths, so the fallback is a display
    concern rather than a containment boundary.
    """
    try:
        return str(Path(path).resolve().relative_to(Path(workspace).resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


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
    """Persists a pasted image under the captures dir; returns None for junk."""
    suffix = sniff_image_suffix(blob)
    if suffix is None:
        return None
    capture_dir = Path(live_dir).parent / "captures"
    capture_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = capture_dir / f"paste-{stamp}-{os.urandom(3).hex()}{suffix}"
    destination.write_bytes(blob)
    prune_captures(live_dir)
    return destination


def prune_captures(live_dir: Path, keep: int = DEFAULT_KEEP_CAPTURES) -> None:
    capture_dir = Path(live_dir).parent / "captures"
    try:
        files = sorted(
            (
                path
                for pattern in ("capture-*.png", "paste-*")
                for path in capture_dir.glob(pattern)
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
        if scripted.get("kind") == "image":
            scripted["path"] = str(scripted.get("path") or "")
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
