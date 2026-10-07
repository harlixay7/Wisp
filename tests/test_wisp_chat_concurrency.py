"""ChatStore mutations are serialized and report persistence failures."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tools.wisp_chat import ChatStore


@pytest.fixture()
def store(tmp_path: Path) -> ChatStore:
    live = tmp_path / ".antigravity-reports" / "live"
    live.mkdir(parents=True)
    return ChatStore(live)


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
