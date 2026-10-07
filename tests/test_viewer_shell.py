"""Widget shell tests: the pywebview window wiring (no real GUI is opened)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from tools.antigravity_viewer import open_native_window


class _FakeWindow:
    def __init__(self) -> None:
        self.x = 10
        self.y = 20
        self.on_top = False
        self.native = None


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

    def test_window_handle_is_not_published_to_the_page(
        self, fake_webview: dict[str, Any]
    ) -> None:
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
