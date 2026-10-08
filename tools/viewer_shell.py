"""Desktop shells that host the Wisp viewer page.

Three ways to put the widget on screen, in order of preference:

* **Electron** (:func:`open_electron_window`) — frameless, per-pixel
  transparent, always-on-top capable; requires ``npm install`` in
  ``tools/wisp_shell``.
* **pywebview** (:func:`open_native_window`) — a frameless native window
  clipped to the creature's shape with a Win32 window region.
* **Browser app window** (:func:`open_app_window`) — a Chromium ``--app``
  window, falling back to the default browser.

Nothing here talks to the viewer server; every function takes the URL to load.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import Any

SHELL_DIR = Path(__file__).resolve().parent / "wisp_shell"
BROWSER_WINDOW_SIZE = (480, 624)  # matches CANVAS_W x CANVAS_H in wisp_shell/main.js
DEFAULT_WORK_AREA = (0, 0, 1920, 1080)
WIDGET_MARGIN = 18
MIN_VIEW_WIDTH = 240
MIN_VIEW_HEIGHT = 200
CARD_CORNER_RADIUS = 26
REGION_POLL_SECONDS = 0.25
SPI_GETWORKAREA = 0x0030

SHAPE_CIRCLE = "circle"
SHAPE_CARD = "card"
SHAPE_NONE = "none"


def _browser_candidates() -> list[str]:
    candidates: list[str] = []
    for name in ("msedge", "chrome", "brave", "chromium"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    program_files = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
    for vendor, product, executable in (
        ("Microsoft", "Edge", "msedge.exe"),
        ("Google", "Chrome", "chrome.exe"),
    ):
        for root in (program_files, program_files_x86):
            path = root / vendor / product / "Application" / executable
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
    if os.name != "nt":
        return DEFAULT_WORK_AREA
    try:
        rect = _RECT()
        if ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
        ):
            if rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right, rect.bottom
    except (AttributeError, OSError):
        pass
    return DEFAULT_WORK_AREA


def bottom_right_position(
    width: int, height: int, margin: int = WIDGET_MARGIN
) -> tuple[int, int]:
    left, top, right, bottom = work_area()
    x = max(left, right - width - margin)
    y = max(top, bottom - height - margin)
    return x, y


def open_app_window(url: str, width: int, height: int) -> bool:
    """Opens ``url`` in a Chromium app window, else in the default browser."""
    x, y = bottom_right_position(width, height)
    profile = Path(tempfile.gettempdir()) / "wisp-app-profile"
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
    except (webbrowser.Error, OSError):
        return False


def _apply_window_region(hwnd: int, shape: str, width: int, height: int) -> None:
    """Clips the window to an ellipse or rounded card; outside stays click-through."""
    if os.name != "nt" or not hwnd:
        return
    try:
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        gdi32.CreateEllipticRgn.restype = ctypes.c_void_p
        gdi32.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
        gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
        gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        right, bottom = int(width) + 1, int(height) + 1
        if shape == SHAPE_CIRCLE:
            region = gdi32.CreateEllipticRgn(0, 0, right, bottom)
        else:
            radius = CARD_CORNER_RADIUS if shape == SHAPE_CARD else 0
            region = gdi32.CreateRoundRectRgn(0, 0, right, bottom, radius, radius)
        if region:
            user32.SetWindowRgn(hwnd, region, True)
    except Exception:
        # Window shaping is cosmetic; never let a GDI failure kill the widget.
        pass


class WindowApi:
    """Methods exposed to the page as ``window.pywebview.api``.

    pywebview publishes every public attribute of ``js_api``, so the window
    handle and helpers stay underscore-prefixed. pywebview backends raise a
    variety of exception types, so the window calls below catch broadly and
    report failure to the page instead of crashing the bridge thread.
    """

    def __init__(self, width: int, height: int) -> None:
        self._window: Any = None
        self.user_positioned = False
        self.shape = SHAPE_NONE
        self.applied_shape: str | None = None
        self.window_size = (int(width), int(height))

    def _attach(self, window: Any) -> None:
        self._window = window

    def _handle(self) -> int | None:
        """The native HWND, when the backend exposes one (WinForms/.NET)."""
        try:
            native = getattr(self._window, "native", None)
            if native is None:
                return None
            return int(native.Handle.ToInt64())
        except Exception:
            # Non-WinForms backends expose other native objects (or none).
            return None

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def toggle_pin(self) -> bool:
        if self._window is None:
            return False
        self._window.on_top = not bool(self._window.on_top)
        return bool(self._window.on_top)

    def move(self, dx: int, dy: int) -> list[int]:
        if self._window is None:
            return [0, 0]
        try:
            self._window.x = int(self._window.x or 0) + int(dx)
            self._window.y = int(self._window.y or 0) + int(dy)
            self.user_positioned = True
            return [self._window.x, self._window.y]
        except Exception:
            return [0, 0]

    def set_view(self, width: int, height: int) -> bool:
        """Resizes the window, keeping it on screen (bottom-right unless moved)."""
        if self._window is None:
            return False
        try:
            w = max(int(width), MIN_VIEW_WIDTH)
            h = max(int(height), MIN_VIEW_HEIGHT)
            left, top, right, bottom = work_area()
            if self.user_positioned:
                x = max(left, min(int(self._window.x or 0), right - w))
                y = max(top, min(int(self._window.y or 0), bottom - h))
            else:
                x = max(left, right - w - WIDGET_MARGIN)
                y = max(top, bottom - h - WIDGET_MARGIN)
            self._window.resize(w, h)
            self._window.move(x, y)
            self.window_size = (w, h)
            self.applied_shape = None
            return True
        except Exception:
            return False

    def set_shape(self, shape: str) -> bool:
        self.shape = str(shape or SHAPE_NONE)
        return True


def _keep_region(api: WindowApi, closed: threading.Event) -> None:
    """Re-applies the window region whenever the page changes shape or size."""
    while not closed.wait(REGION_POLL_SECONDS):
        handle = api._handle()
        if handle is None or api.applied_shape == api.shape:
            continue
        width, height = api.window_size
        _apply_window_region(handle, api.shape, width, height)
        api.applied_shape = api.shape


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
    api = WindowApi(width, height)
    try:
        window = webview.create_window(
            "Wisp",
            url,
            js_api=api,
            width=width,
            height=height,
            x=x,
            y=y,
            frameless=True,
            easy_drag=False,
            on_top=False,
            transparent=transparent,
            background_color="#000000",
            resizable=False,
        )
    except Exception:
        # Missing GUI backends surface as assorted errors; let the caller fall back.
        return False
    api._attach(window)
    closed = threading.Event()
    threading.Thread(target=_keep_region, args=(api, closed), daemon=True).start()
    try:
        webview.start(debug=False)
    finally:
        closed.set()
    return True


def electron_executable() -> Path | None:
    binary = "electron.exe" if os.name == "nt" else "electron"
    candidate = SHELL_DIR / "node_modules" / "electron" / "dist" / binary
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
