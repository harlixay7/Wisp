"""Capture helpers: filenames, clipboard ownership, image paste, and selection capture."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from tests.helpers.win32 import FakeKernel32, FakeUser32
from tools import wisp_capture
from tools.wisp_capture import (
    _set_clipboard_text_with,
    _wait_for_modifier_release,
    new_capture_path,
    save_pasted_image,
    sniff_image_suffix,
)

PNG_BLOB = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class TestCaptureFilenames:
    def test_capture_paths_have_collision_suffix(self, tmp_path: Path) -> None:
        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        first = new_capture_path(live)
        second = new_capture_path(live)
        assert first != second
        pattern = re.compile(r"capture-\d{8}-\d{6}-[0-9a-f]{6}\.png")
        assert pattern.match(first.name)
        assert pattern.match(second.name)


@pytest.mark.skipif(os.name != "nt", reason="Win32 clipboard semantics")
class TestClipboardOwnership:
    def test_clipboard_ownership_success_never_frees(self) -> None:
        kernel32 = FakeKernel32()
        user32 = FakeUser32(open_clipboard=True, set_succeeds=True)

        _set_clipboard_text_with(user32, kernel32, "hello")
        assert kernel32.freed == []
        assert user32.set_arg == kernel32.allocated
        assert user32.close_calls == 1

    def test_clipboard_ownership_open_failure_frees(self) -> None:
        kernel32 = FakeKernel32()
        user32 = FakeUser32(open_clipboard=False, set_succeeds=True)

        _set_clipboard_text_with(user32, kernel32, "hello")
        assert kernel32.freed == [kernel32.allocated]
        assert user32.empty_calls == 0

    def test_clipboard_ownership_set_failure_frees(self) -> None:
        kernel32 = FakeKernel32()
        user32 = FakeUser32(open_clipboard=True, set_succeeds=False)

        _set_clipboard_text_with(user32, kernel32, "hello")
        assert kernel32.freed == [kernel32.allocated]
        assert user32.close_calls == 1

    def test_clipboard_ownership_exception_frees_and_propagates(self) -> None:
        kernel32 = FakeKernel32()
        user32 = FakeUser32(open_clipboard=True, set_succeeds=True)
        user32.EmptyClipboard = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            _set_clipboard_text_with(user32, kernel32, "hello")
        assert kernel32.freed == [kernel32.allocated]
        assert user32.close_calls == 1

    def test_clipboard_ownership_lock_failure_frees(self) -> None:
        kernel32 = FakeKernel32(lock_ok=False)
        user32 = FakeUser32(open_clipboard=True, set_succeeds=True)

        _set_clipboard_text_with(user32, kernel32, "hello")
        assert kernel32.freed == [kernel32.allocated]


class TestImagePaste:
    def test_sniff_formats(self) -> None:
        assert sniff_image_suffix(b"\x89PNG\r\n\x1a\nrest") == ".png"
        assert sniff_image_suffix(b"\xff\xd8\xff\xe0rest") == ".jpg"
        assert sniff_image_suffix(b"GIF89a....") == ".gif"
        assert sniff_image_suffix(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp"
        assert sniff_image_suffix(b"nope") is None

    def test_save_pasted_image(self, tmp_path: Path) -> None:
        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)

        saved = save_pasted_image(live, PNG_BLOB)

        assert saved is not None
        assert saved.parent.name == "captures"
        assert saved.suffix == ".png"
        assert saved.read_bytes() == PNG_BLOB
        assert save_pasted_image(live, b"junk") is None

    def test_capture_auto_returns_none_when_the_capture_dir_is_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WISP_CAPTURE_FAKE", raising=False)
        monkeypatch.setattr(wisp_capture, "start_snip", lambda: {"kind": "snip_started"})
        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        (live.parent / "captures").write_text("blocker", encoding="utf-8")

        result = wisp_capture.capture_auto(live, prefer_selection=False)

        assert result["kind"] == "none"
        assert result["reason"]


class TestCaptureHygiene:
    @pytest.mark.skipif(os.name != "nt", reason="Windows-only console flags")
    def test_powershell_helpers_never_show_a_console(self, monkeypatch) -> None:
        captured = {}

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(args, 0, b"", b"")

        monkeypatch.setattr(wisp_capture.subprocess, "run", fake_run)
        wisp_capture._run_powershell(["-File", "x.ps1"], timeout=1)

        assert captured.get("creationflags") == wisp_capture.CREATE_NO_WINDOW

    def test_snip_wait_stays_bounded(self) -> None:
        assert wisp_capture.SNIP_TIMEOUT_SECONDS <= 60


class TestSelectionCaptureModifiers:
    @pytest.mark.skipif(os.name != "nt", reason="Windows-only capture path")
    def test_modifier_wait_returns_when_keys_are_up(self) -> None:
        started = time.monotonic()
        _wait_for_modifier_release(timeout_ms=500)

        assert time.monotonic() - started < 0.8
