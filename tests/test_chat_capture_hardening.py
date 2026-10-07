"""Chat + capture hardening regression tests from the external audit (2026-10).

* persistence failures are visible, not silent (#25),
* schema-level garbage in thread files cannot crash listing (#26),
* roles are constrained to the documented enum (#27),
* message content and per-thread history are bounded (#28),
* fake-thread cleanup is authoritative on meta.fake, content matching is an
  explicit legacy mode (#29),
* clipboard restore honesty (#30, docs),
* GlobalAlloc ownership is freed on failure paths (#31, structure),
* capture filenames cannot collide within one second (#32).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools.wisp_chat import (
    MAX_MESSAGES_PER_THREAD,
    VALID_ROLES,
    ChatStore,
)


@pytest.fixture()
def store(tmp_path: Path) -> ChatStore:
    live = tmp_path / ".antigravity-reports" / "live"
    live.mkdir(parents=True)
    return ChatStore(live)


class TestPersistenceVisibility:
    def test_save_reports_failure_when_directory_is_blocked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        live = tmp_path / "live"
        live.mkdir()
        blocker = live / "chat"
        blocker.write_text("a regular file, not a directory", encoding="utf-8")
        store = ChatStore(live)
        thread = {
            "id": "20260101-000000-abcdef",
            "title": "t",
            "pinned": False,
            "created": 1.0,
            "updated": 1.0,
            "run": None,
            "messages": [],
        }
        assert store.save(thread) is False
        assert "failed to persist" in capsys.readouterr().err

    def test_save_succeeds_normally(self, store: ChatStore) -> None:
        thread = store.create("hello")
        assert store.save(thread) is True

    def test_append_message_reports_persistence_via_save(
        self, store: ChatStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        thread = store.create("hi")
        store.append_message(thread["id"], "user", "message one")
        reloaded = store.load(thread["id"])
        assert reloaded is not None
        assert reloaded["messages"][0]["content"] == "message one"


class TestSchemaDefense:
    def test_malformed_message_entries_are_dropped_not_fatal(self, store: ChatStore) -> None:
        thread = store.create("t")
        path = store.directory / f"thread-{thread['id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["messages"] = [
            "a bare string",
            None,
            42,
            {"role": "user", "content": "valid message", "ts": 1.0, "meta": {}},
            {"role": "not-a-role", "content": "coerced role"},
        ]
        path.write_text(json.dumps(data), encoding="utf-8")
        reloaded = store.load(thread["id"])
        assert reloaded is not None
        assert len(reloaded["messages"]) == 2
        assert reloaded["messages"][0]["content"] == "valid message"
        assert reloaded["messages"][1]["role"] in VALID_ROLES
        # Listing must not raise either (the pre-fix bug crashed here).
        assert any(
            summary["id"] == thread["id"] for summary in store.list_threads()
        )


class TestRoleContract:
    def test_invalid_role_is_rejected(self, store: ChatStore) -> None:
        thread = store.create("t")
        with pytest.raises(ValueError):
            store.append_message(thread["id"], "system", "nope")
        with pytest.raises(ValueError):
            store.append_message(thread["id"], "", "nope")

    def test_valid_roles_accepted(self, store: ChatStore) -> None:
        thread = store.create("t")
        for role in VALID_ROLES:
            assert store.append_message(thread["id"], role, f"{role} msg") is not None


class TestStorageBounds:
    def test_oversized_message_rejected(self, store: ChatStore) -> None:
        thread = store.create("t")
        with pytest.raises(ValueError):
            store.append_message(thread["id"], "user", "x" * 100_001)

    def test_thread_history_is_pruned_to_cap(self, store: ChatStore) -> None:
        thread = store.create("t")
        for index in range(MAX_MESSAGES_PER_THREAD + 25):
            store.append_message(thread["id"], "user", f"msg {index}")
        reloaded = store.load(thread["id"])
        assert reloaded is not None
        assert len(reloaded["messages"]) == MAX_MESSAGES_PER_THREAD
        assert reloaded["messages"][-1]["content"] == f"msg {MAX_MESSAGES_PER_THREAD + 24}"
        assert reloaded["messages"][0]["content"] == "msg 25"


class TestFakeThreadCleanup:
    def test_meta_fake_is_authoritative(self, store: ChatStore) -> None:
        thread = store.create("t")
        store.append_message(
            thread["id"],
            "assistant",
            "A genuine answer that happens to quote: Risk: the retry path has no backoff bound",
        )
        assert ChatStore.is_fake_thread(store.load(thread["id"])) is False

    def test_meta_fake_flag_removes_thread(self, store: ChatStore) -> None:
        thread = store.create("t")
        store.append_message(
            thread["id"], "assistant", "canned", {"fake": True}
        )
        removed = store.clean_fake_threads()
        assert thread["id"] in removed
        assert store.load(thread["id"]) is None

    def test_legacy_markers_only_in_explicit_mode(self, store: ChatStore) -> None:
        thread = store.create("t")
        store.append_message(
            thread["id"],
            "assistant",
            "Risk: the retry path has no backoff bound",
        )
        assert store.clean_fake_threads(include_legacy_markers=False) == []
        removed = store.clean_fake_threads(include_legacy_markers=True)
        assert thread["id"] in removed

    def test_quoted_canned_phrase_survives_default_cleanup(self, store: ChatStore) -> None:
        thread = store.create("audit discussion")
        store.append_message(
            thread["id"],
            "user",
            "Please assess this claim: 'Risk: the retry path has no backoff bound'",
        )
        store.append_message(thread["id"], "assistant", "The claim is stale; backoff exists.")
        assert store.clean_fake_threads() == []
        assert store.load(thread["id"]) is not None


class TestCaptureFilenames:
    def test_capture_paths_have_collision_suffix(self, tmp_path: Path) -> None:
        from tools.wisp_capture import new_capture_path

        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        first = new_capture_path(live)
        second = new_capture_path(live)
        assert first != second
        pattern = re.compile(r"capture-\d{8}-\d{6}-[0-9a-f]{6}\.png")
        assert pattern.match(first.name)
        assert pattern.match(second.name)

    def test_clipboard_ownership_success_never_frees(self) -> None:
        if __import__("os").name != "nt":
            pytest.skip("Win32 clipboard semantics")
        from tools.clipboard_fakes import FakeKernel32, FakeUser32

        kernel32 = FakeKernel32()
        user32 = FakeUser32(open_clipboard=True, set_succeeds=True)
        from tools.wisp_capture import _set_clipboard_text_with

        _set_clipboard_text_with(user32, kernel32, "hello")
        assert kernel32.freed == []
        assert user32.set_arg == kernel32.allocated
        assert user32.close_calls == 1

    def test_clipboard_ownership_open_failure_frees(self) -> None:
        if __import__("os").name != "nt":
            pytest.skip("Win32 clipboard semantics")
        from tools.clipboard_fakes import FakeKernel32, FakeUser32

        kernel32 = FakeKernel32()
        user32 = FakeUser32(open_clipboard=False, set_succeeds=True)
        from tools.wisp_capture import _set_clipboard_text_with

        _set_clipboard_text_with(user32, kernel32, "hello")
        assert kernel32.freed == [kernel32.allocated]
        assert user32.empty_calls == 0

    def test_clipboard_ownership_set_failure_frees(self) -> None:
        if __import__("os").name != "nt":
            pytest.skip("Win32 clipboard semantics")
        from tools.clipboard_fakes import FakeKernel32, FakeUser32

        kernel32 = FakeKernel32()
        user32 = FakeUser32(open_clipboard=True, set_succeeds=False)
        from tools.wisp_capture import _set_clipboard_text_with

        _set_clipboard_text_with(user32, kernel32, "hello")
        assert kernel32.freed == [kernel32.allocated]
        assert user32.close_calls == 1

    def test_clipboard_ownership_exception_frees_and_propagates(self) -> None:
        if __import__("os").name != "nt":
            pytest.skip("Win32 clipboard semantics")
        from tools.clipboard_fakes import FakeKernel32, FakeUser32

        kernel32 = FakeKernel32()
        user32 = FakeUser32(open_clipboard=True, set_succeeds=True)
        user32.EmptyClipboard = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        from tools.wisp_capture import _set_clipboard_text_with

        with pytest.raises(RuntimeError):
            _set_clipboard_text_with(user32, kernel32, "hello")
        assert kernel32.freed == [kernel32.allocated]
        assert user32.close_calls == 1

    def test_clipboard_ownership_lock_failure_frees(self) -> None:
        if __import__("os").name != "nt":
            pytest.skip("Win32 clipboard semantics")
        from tools.clipboard_fakes import FakeKernel32, FakeUser32

        kernel32 = FakeKernel32(lock_ok=False)
        user32 = FakeUser32(open_clipboard=True, set_succeeds=True)
        from tools.wisp_capture import _set_clipboard_text_with

        _set_clipboard_text_with(user32, kernel32, "hello")
        assert kernel32.freed == [kernel32.allocated]
