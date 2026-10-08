"""Persistent conversation threads for operator asks (Wisp chat).

Each thread is one JSON document under ``<live_dir>/chat/thread-<id>.json``:
``{id, title, pinned, created, updated, run, messages[]}`` where every message
is ``{role: "user"|"assistant", content, ts, meta}``. Reads are defensive: a
corrupt file, or one with schema-level garbage (non-dict message entries),
is treated as missing/malformed rather than crashing the viewer.

Storage bounds: message content is capped at ``MAX_MESSAGE_CHARS``, a thread
holds at most ``MAX_MESSAGES_PER_THREAD`` messages (oldest pruned), and
persistence failures surface as a ``False`` return from
:meth:`ChatStore.save` (``None`` from the mutators) so callers can tell the
operator instead of silently reporting success.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

CHAT_DIR_NAME = "chat"
MAX_TITLE_CHARS = 72
MAX_THREADS_LISTED = 60
MAX_MESSAGE_CHARS = 100_000
MAX_MESSAGES_PER_THREAD = 500
VALID_ROLES = ("user", "assistant")
FAKE_MESSAGE_MARKERS = (
    "[TEST MODE]",
    "Risk: the retry path has no backoff bound",
)
_THREAD_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
# Serializes every load/mutate/save cycle. The viewer builds a ChatStore per
# request, so a per-instance lock would not stop the ask worker and an HTTP
# thread (e.g. a pin) from overwriting each other's changes. Reentrant so a
# mutator can call save() while holding it.
_STORE_LOCK = threading.RLock()


def write_json_atomic(path: Path, payload: Any) -> None:
    """Writes ``payload`` as JSON so readers never observe a partial file.

    The document goes to a uniquely named sibling temp file first and is then
    moved into place with ``os.replace``. Raises ``OSError`` (or ``TypeError``
    for unserializable payloads); the temp file is removed on failure.
    """
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def new_thread_id() -> str:
    """A sortable thread id: ``YYYYmmdd-HHMMSS-<6 hex>``."""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def valid_thread_id(thread_id: str) -> bool:
    """True for ids shaped like :func:`new_thread_id` output (safe as a filename)."""
    return bool(_THREAD_ID_PATTERN.match(str(thread_id or "")))


class ChatStore:
    """Chat threads stored as one JSON file each under ``<live_dir>/chat/``."""

    def __init__(self, live_dir: Path | str) -> None:
        self.directory = Path(live_dir) / CHAT_DIR_NAME

    def _path(self, thread_id: str) -> Path:
        if not valid_thread_id(thread_id):
            raise ValueError(f"invalid thread id: {thread_id!r}")
        return self.directory / f"thread-{thread_id}.json"

    def create(self, first_prompt: str) -> dict[str, Any] | None:
        """Creates and persists a new thread; ``None`` when it cannot be saved."""
        thread = {
            "id": new_thread_id(),
            "title": self._title_from(first_prompt),
            "pinned": False,
            "created": time.time(),
            "updated": time.time(),
            "run": None,
            "messages": [],
        }
        return thread if self.save(thread) else None

    @staticmethod
    def _sanitize_messages(messages: Any) -> list[dict[str, Any]]:
        """Keeps only well-formed message dicts; hand-edited or corrupt files
        must not crash listing."""
        if not isinstance(messages, list):
            return []
        clean: list[dict[str, Any]] = []
        for entry in messages:
            if not isinstance(entry, dict):
                continue
            role = entry.get("role")
            clean.append(
                {
                    "role": role if role in VALID_ROLES else "assistant",
                    "content": str(entry.get("content") or ""),
                    "ts": entry.get("ts") if isinstance(entry.get("ts"), (int, float)) else 0.0,
                    "meta": entry.get("meta") if isinstance(entry.get("meta"), dict) else {},
                }
            )
        return clean

    def load(self, thread_id: str) -> dict[str, Any] | None:
        """The thread with defaults filled in; ``None`` when missing or malformed."""
        try:
            path = self._path(thread_id)
        except ValueError:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        data["messages"] = self._sanitize_messages(data.get("messages"))
        data.setdefault("id", thread_id)
        data.setdefault("title", "Untitled")
        data.setdefault("pinned", False)
        data.setdefault("updated", data.get("created") or time.time())
        data.setdefault("run", None)
        return data

    def save(self, thread: dict[str, Any]) -> bool:
        """Persists a thread atomically; returns ``False`` when persistence fails.

        Failures are also written to stderr so data loss is visible to
        whichever process launched the viewer or chat CLI.
        """
        try:
            path = self._path(str(thread.get("id") or ""))
            with _STORE_LOCK:
                write_json_atomic(path, thread)
            return True
        except (OSError, ValueError, TypeError) as exc:
            try:
                sys.stderr.write(f"[wisp-chat] failed to persist thread: {exc}\n")
                sys.stderr.flush()
            except OSError:
                pass
            return False

    def delete_thread(self, thread_id: str) -> bool:
        """Deletes the thread file; ``False`` when the id is invalid or nothing was deleted."""
        try:
            path = self._path(thread_id)
        except ValueError:
            return False
        try:
            with _STORE_LOCK:
                path.unlink()
            return True
        except OSError:
            return False

    @staticmethod
    def is_fake_thread(thread: dict[str, Any], include_legacy_markers: bool = False) -> bool:
        """True when any assistant message has ``meta.fake`` set to True.

        Content-marker matching is opt-in (``include_legacy_markers``) because
        a real thread may quote a canned test phrase.
        """
        for message in thread.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            meta = message.get("meta") or {}
            if isinstance(meta, dict) and meta.get("fake") is True:
                return True
            if include_legacy_markers:
                content = str(message.get("content") or "")
                if any(marker in content for marker in FAKE_MESSAGE_MARKERS):
                    return True
        return False

    def clean_fake_threads(self, include_legacy_markers: bool = False) -> list[str]:
        """Deletes fake threads; returns their ids.

        ``include_legacy_markers=True`` also matches the canned test phrases
        that threads created before ``meta.fake`` existed carry; it is a
        migration path, not the default.
        """
        removed: list[str] = []
        try:
            files = [
                path
                for path in self.directory.glob("thread-*.json")
                if path.is_file()
            ]
        except OSError:
            return removed
        for path in files:
            thread_id = path.stem[len("thread-") :]
            thread = self.load(thread_id)
            if thread is None:
                continue
            if (
                self.is_fake_thread(thread, include_legacy_markers=include_legacy_markers)
                and self.delete_thread(thread_id)
            ):
                removed.append(thread_id)
        return removed

    def append_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Appends a message and returns the updated thread (``None`` if unsaved).

        Raises ``ValueError`` for an unknown role or content longer than
        ``MAX_MESSAGE_CHARS``; callers clamp or reject long text first.
        """
        if role not in VALID_ROLES:
            raise ValueError(
                f"invalid message role {role!r}; expected one of {VALID_ROLES}"
            )
        text = str(content or "")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValueError(
                f"message is {len(text)} characters; the limit is {MAX_MESSAGE_CHARS}"
            )
        message = {
            "role": role,
            "content": text,
            "ts": time.time(),
            "meta": dict(meta or {}),
        }

        def add(thread: dict[str, Any]) -> None:
            messages: list[dict[str, Any]] = thread["messages"]
            messages.append(message)
            if len(messages) > MAX_MESSAGES_PER_THREAD:
                # Oldest-first pruning keeps the document bounded; the cap is a
                # storage bound, not a conversational statement.
                del messages[: len(messages) - MAX_MESSAGES_PER_THREAD]

        return self._mutate(thread_id, add)

    def update_run(
        self, thread_id: str, run: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Replaces the thread's run status record; returns the thread or ``None``."""
        return self._mutate(thread_id, lambda thread: thread.update(run=run))

    def set_pinned(self, thread_id: str, pinned: bool) -> dict[str, Any] | None:
        """Pins or unpins the thread; returns the thread or ``None``."""
        return self._mutate(thread_id, lambda thread: thread.update(pinned=bool(pinned)))

    def _mutate(
        self, thread_id: str, change: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any] | None:
        """Applies ``change`` under the store lock and persists the result.

        Returns the updated thread, or ``None`` when the thread does not exist
        or could not be saved.
        """
        with _STORE_LOCK:
            thread = self.load(thread_id)
            if thread is None:
                return None
            change(thread)
            thread["updated"] = time.time()
            return thread if self.save(thread) else None

    def list_threads(self, limit: int = MAX_THREADS_LISTED) -> list[dict[str, Any]]:
        """Thread summaries, pinned first, then most recently updated."""
        try:
            files = [path for path in self.directory.glob("thread-*.json") if path.is_file()]
        except OSError:
            return []
        summaries: list[dict[str, Any]] = []
        for path in files:
            thread_id = path.stem[len("thread-") :]
            thread = self.load(thread_id)
            if thread is None:
                continue
            messages = thread.get("messages") or []
            preview = ""
            for message in reversed(messages):
                if message.get("role") == "assistant" and message.get("content"):
                    preview = str(message["content"])[:120]
                    break
            summaries.append(
                {
                    "id": thread.get("id"),
                    "title": thread.get("title"),
                    "pinned": bool(thread.get("pinned")),
                    "updated": thread.get("updated") or 0,
                    "message_count": len(messages),
                    "preview": preview,
                    "run": thread.get("run"),
                }
            )
        summaries.sort(
            key=lambda item: (item.get("pinned", False), item.get("updated") or 0),
            reverse=True,
        )
        return summaries[:limit]

    @staticmethod
    def _title_from(prompt: str) -> str:
        flat = " ".join(str(prompt or "").split())
        if not flat:
            return "New conversation"
        return flat[:MAX_TITLE_CHARS] + ("\u2026" if len(flat) > MAX_TITLE_CHARS else "")


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wisp_chat",
        description="Inspect and clean Wisp chat threads.",
    )
    parser.add_argument(
        "--live-dir",
        type=Path,
        required=True,
        help="<workspace>/.antigravity-reports/live",
    )
    parser.add_argument(
        "--clean-fake",
        action="store_true",
        help="Delete threads flagged meta.fake=true (plus legacy marker migration).",
    )
    parser.add_argument(
        "--clean-fake-strict",
        action="store_true",
        help="Delete only threads flagged meta.fake=true (no legacy content matching).",
    )
    args = parser.parse_args(argv)
    store = ChatStore(args.live_dir)
    if args.clean_fake or args.clean_fake_strict:
        removed = store.clean_fake_threads(
            include_legacy_markers=bool(args.clean_fake and not args.clean_fake_strict)
        )
        for thread_id in removed:
            print(f"removed {thread_id}")
        print(f"{len(removed)} fake thread(s) removed.")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
