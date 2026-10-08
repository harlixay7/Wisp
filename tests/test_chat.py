"""Chat store: threads, schema defense, bounds, fake-thread cleanup, persistence, concurrency."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tools.wisp_chat import MAX_MESSAGES_PER_THREAD, VALID_ROLES, ChatStore


@pytest.fixture()
def store(tmp_path: Path) -> ChatStore:
    live = tmp_path / ".antigravity-reports" / "live"
    live.mkdir(parents=True)
    return ChatStore(live)


class TestThreadLifecycle:
    def test_invalid_thread_id_rejected(self, tmp_path: Path) -> None:
        store = ChatStore(tmp_path)
        assert store.load("../../etc/passwd") is None
        store.append_message("../../etc/passwd", "user", "nope")

    def test_thread_lifecycle(self, tmp_path: Path) -> None:
        store = ChatStore(tmp_path)
        thread = store.create("Hello world, this is a long enough prompt for titling")
        assert thread["title"].startswith("Hello world")

        store.append_message(thread["id"], "user", "hi")
        store.append_message(thread["id"], "assistant", "hello", {"success": True})
        loaded = store.load(thread["id"])
        assert [message["role"] for message in loaded["messages"]] == ["user", "assistant"]

        summaries = store.list_threads()
        assert summaries[0]["id"] == thread["id"]
        assert summaries[0]["message_count"] == 2
        assert "hello" in summaries[0]["preview"]

    def test_delete_thread(self, tmp_path: Path) -> None:
        store = ChatStore(tmp_path)
        thread = store.create("hello")

        assert store.delete_thread(thread["id"]) is True
        assert store.load(thread["id"]) is None
        assert store.delete_thread(thread["id"]) is False

    def test_save_leaves_no_temp_files(self, tmp_path: Path) -> None:
        store = ChatStore(tmp_path)
        thread = store.create("atomic")
        store.append_message(thread["id"], "user", "again")

        chat_dir = tmp_path / "chat"
        assert list(chat_dir.glob("*.tmp.*")) == []
        document = json.loads(
            (chat_dir / f"thread-{thread['id']}.json").read_text(encoding="utf-8")
        )
        assert len(document["messages"]) == 1


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


class TestPersistenceFailures:
    def test_mutators_return_none_when_save_fails(
        self, store: ChatStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        thread = store.create("persist")
        assert thread is not None
        monkeypatch.setattr(store, "save", lambda thread: False)

        assert store.create("another") is None
        assert store.append_message(thread["id"], "user", "lost") is None
        assert store.update_run(thread["id"], {"status": "running"}) is None
        assert store.set_pinned(thread["id"], True) is None

    def test_mutators_return_the_thread_on_success(self, store: ChatStore) -> None:
        thread = store.create("persist")
        assert thread is not None
        updated = store.append_message(thread["id"], "user", "kept")
        assert updated is not None
        assert updated["messages"][-1]["content"] == "kept"
        assert store.set_pinned(thread["id"], True)["pinned"] is True


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
        # Listing must not raise either.
        assert any(summary["id"] == thread["id"] for summary in store.list_threads())


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
        store.append_message(thread["id"], "assistant", "canned", {"fake": True})
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

    def test_clean_fake_threads_matches_all_markers(self, tmp_path: Path) -> None:
        store = ChatStore(tmp_path)
        legacy = store.create("legacy")
        store.append_message(legacy["id"], "assistant", "Risk: the retry path has no backoff bound")
        modern = store.create("modern")
        store.append_message(modern["id"], "assistant", "[TEST MODE] Canned critique")
        flagged = store.create("flagged")
        store.append_message(flagged["id"], "assistant", "looks real", {"fake": True})
        genuine = store.create("genuine")
        store.append_message(genuine["id"], "assistant", "A genuine critique with findings")
        quoted = store.create("quoted")
        store.append_message(
            quoted["id"],
            "assistant",
            "Assessing the claim: 'Risk: the retry path has no backoff bound'",
        )

        # Default cleanup is authoritative on meta.fake only:
        # genuine text that merely quotes a canned test phrase must survive.
        removed = store.clean_fake_threads()
        assert set(removed) == {flagged["id"]}
        for survivor in (legacy, modern, genuine, quoted):
            assert store.load(survivor["id"]) is not None

        # Legacy content matching is an explicit migration mode: it matches
        # every canned-marker thread (including quoted text) by design, which
        # is why it is opt-in.
        removed_legacy = store.clean_fake_threads(include_legacy_markers=True)
        assert set(removed_legacy) == {legacy["id"], modern["id"], quoted["id"]}
        assert store.load(genuine["id"]) is not None


class TestConcurrentMutation:
    def test_concurrent_appends_and_pins_lose_nothing(self, store: ChatStore) -> None:
        thread = store.create("race")
        assert thread is not None
        writers, per_writer = 6, 15
        barrier = threading.Barrier(writers + 1)

        def append(worker: int) -> None:
            # Each writer uses its own store object, as the viewer's
            # request threads and ask worker do.
            local = ChatStore(store.directory.parent)
            barrier.wait()
            for index in range(per_writer):
                local.append_message(thread["id"], "user", f"{worker}:{index}")

        def pin() -> None:
            local = ChatStore(store.directory.parent)
            barrier.wait()
            for index in range(per_writer):
                local.set_pinned(thread["id"], index % 2 == 0)

        threads = [threading.Thread(target=append, args=(n,)) for n in range(writers)]
        threads.append(threading.Thread(target=pin))
        for worker in threads:
            worker.start()
        for worker in threads:
            worker.join(timeout=60)

        final = store.load(thread["id"])
        assert final is not None
        contents = {message["content"] for message in final["messages"]}
        expected = {f"{w}:{i}" for w in range(writers) for i in range(per_writer)}
        assert contents == expected
