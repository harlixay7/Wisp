"""Widget shell: the pywebview window wiring and screen helpers (no real GUI is opened)."""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from tools import antigravity_viewer, viewer_shell
from tools.viewer_shell import open_native_window, work_area


class _FakeHandle:
    def ToInt64(self) -> int:  # mirrors the .NET IntPtr API
        return 4242


class _FakeNative:
    Handle = _FakeHandle()


class _FakeWindow:
    def __init__(self) -> None:
        self.x = 10
        self.y = 20
        self.on_top = False
        self.native: object | None = None


@pytest.fixture()
def fake_webview(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {}
    module = types.ModuleType("webview")

    def create_window(title: str, url: str, **kwargs: Any) -> _FakeWindow:
        calls["title"] = title
        calls["url"] = url
        calls["kwargs"] = kwargs
        calls["window"] = _FakeWindow()
        return calls["window"]

    def start(**kwargs: Any) -> None:
        calls["started"] = True

    module.create_window = create_window  # type: ignore[attr-defined]
    module.start = start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", module)
    return calls


class TestNativeWindow:
    def test_js_api_is_passed_to_the_window(self, fake_webview: dict[str, Any]) -> None:
        assert open_native_window("http://127.0.0.1:1/", 240, 240) is True
        api = fake_webview["kwargs"].get("js_api")
        assert api is not None
        for name in ("set_view", "set_shape", "toggle_pin", "move", "close"):
            assert callable(getattr(api, name, None)), name
        # The exposed API must drive the window it was attached to.
        assert api.toggle_pin() is True
        assert fake_webview["window"].on_top is True
        assert api.move(5, 5) == [15, 25]

    def test_window_handle_is_not_published_to_the_page(self, fake_webview: dict[str, Any]) -> None:
        open_native_window("http://127.0.0.1:1/", 240, 240)
        api = fake_webview["kwargs"]["js_api"]
        public = [name for name in vars(api) if not name.startswith("_")]
        assert "window" not in public

    @pytest.mark.parametrize("transparent", [True, False])
    def test_transparent_flag_reaches_the_window(
        self, fake_webview: dict[str, Any], transparent: bool
    ) -> None:
        open_native_window("http://127.0.0.1:1/", 240, 240, transparent=transparent)
        assert fake_webview["kwargs"]["transparent"] is transparent


class TestWindowRegion:
    def test_region_is_applied_while_the_window_is_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        applied: list[tuple[int, str, int, int]] = []
        region_set = threading.Event()

        def record(hwnd: int, shape: str, width: int, height: int) -> None:
            applied.append((hwnd, shape, width, height))
            region_set.set()

        module = types.ModuleType("webview")
        created: dict[str, Any] = {}

        def create_window(title: str, url: str, **kwargs: Any) -> _FakeWindow:
            window = _FakeWindow()
            window.native = _FakeNative()
            created["api"] = kwargs["js_api"]
            return window

        def start(**kwargs: Any) -> None:
            created["api"].set_shape(viewer_shell.SHAPE_CIRCLE)
            region_set.wait(timeout=5)

        module.create_window = create_window  # type: ignore[attr-defined]
        module.start = start  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "webview", module)
        monkeypatch.setattr(viewer_shell, "_apply_window_region", record)

        assert open_native_window("http://127.0.0.1:1/", 300, 280) is True
        assert applied and applied[0] == (4242, viewer_shell.SHAPE_CIRCLE, 300, 280)


class TestShellHelpers:
    def test_bottom_right_position_stays_inside_the_work_area(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(viewer_shell, "work_area", lambda: (0, 0, 1000, 800))
        assert viewer_shell.bottom_right_position(200, 100) == (782, 682)
        assert viewer_shell.bottom_right_position(5000, 5000) == (0, 0)

    def test_work_area_has_a_sane_default(self) -> None:
        left, top, right, bottom = work_area()
        assert right > left and bottom > top

    def test_viewer_uses_the_shell_module(self) -> None:
        assert antigravity_viewer.open_native_window is viewer_shell.open_native_window


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX Electron layouts")
class TestElectronExecutable:
    def _install(self, root: Path, *parts: str) -> Path:
        binary = root.joinpath("node_modules", "electron", "dist", *parts)
        binary.parent.mkdir(parents=True)
        binary.write_text("", encoding="utf-8")
        return binary

    def test_finds_the_macos_app_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = self._install(tmp_path, "Electron.app", "Contents", "MacOS", "Electron")
        monkeypatch.setattr(viewer_shell, "SHELL_DIR", tmp_path)
        monkeypatch.setattr(viewer_shell.sys, "platform", "darwin")
        assert viewer_shell.electron_executable() == binary

    def test_finds_the_linux_binary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        binary = self._install(tmp_path, "electron")
        monkeypatch.setattr(viewer_shell, "SHELL_DIR", tmp_path)
        monkeypatch.setattr(viewer_shell.sys, "platform", "linux")
        assert viewer_shell.electron_executable() == binary

    def test_missing_shell_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(viewer_shell, "SHELL_DIR", tmp_path)
        monkeypatch.setattr(viewer_shell.sys, "platform", "darwin")
        assert viewer_shell.electron_executable() is None
