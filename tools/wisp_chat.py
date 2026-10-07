"""Persistent conversation threads for operator asks (Wisp chat).

Each thread is one JSON document under ``<live_dir>/chat/thread-<id>.json``:
``{id, title, pinned, created, updated, run, messages[]}`` where every message
is ``{role: "user"|"assistant", content, ts, meta}``. Reads are defensive: a
corrupt file is treated as missing rather than crashing the viewer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

CHAT_DIR_NAME = "chat"
MAX_TITLE_CHARS = 72
MAX_THREADS_LISTED = 60
FAKE_MESSAGE_MARKERS = (
    "[TEST MODE]",
    "Risk: the retry path has no backoff bound",
)
_THREAD_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


def new_thread_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def valid_thread_id(thread_id: str) -> bool:
    return bool(_THREAD_ID_PATTERN.match(str(thread_id or "")))


class ChatStore:
    def __init__(self, live_dir: Path | str) -> None:
        self.directory = Path(live_dir) / CHAT_DIR_NAME

    def _path(self, thread_id: str) -> Path:
        if not valid_thread_id(thread_id):
            raise ValueError(f"invalid thread id: {thread_id!r}")
        return self.directory / f"thread-{thread_id}.json"

    def create(self, first_prompt: str) -> dict[str, Any]:
        thread = {
            "id": new_thread_id(),
            "title": self._title_from(first_prompt),
            "pinned": False,
            "created": time.time(),
            "updated": time.time(),
            "run": None,
            "messages": [],
        }
        self.save(thread)
        return thread

    def load(self, thread_id: str) -> dict[str, Any] | None:
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
        data.setdefault("id", thread_id)
        data.setdefault("title", "Untitled")
        data.setdefault("pinned", False)
        data.setdefault("updated", data.get("created") or time.time())
        data.setdefault("run", None)
        return data

    def save(self, thread: dict[str, Any]) -> None:
        temporary: Path | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._path(str(thread.get("id") or ""))
            payload = json.dumps(thread, indent=2, ensure_ascii=False)
            temporary = path.with_name(path.name + ".tmp." + uuid.uuid4().hex[:8])
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
            temporary = None
        except (OSError, ValueError):
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def delete_thread(self, thread_id: str) -> bool:
        try:
            path = self._path(thread_id)
        except ValueError:
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False

    @staticmethod
    def is_fake_thread(thread: dict[str, Any]) -> bool:
        for message in thread.get("messages") or []:
            if message.get("role") != "assistant":
                continue
            meta = message.get("meta") or {}
            if meta.get("fake") is True:
                return True
            content = str(message.get("content") or "")
            if any(marker in content for marker in FAKE_MESSAGE_MARKERS):
                return True
        return False

    def clean_fake_threads(self) -> list[str]:
        """Deletes threads containing canned test-mode answers; returns their ids."""
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
            if self.is_fake_thread(thread) and self.delete_thread(thread_id):
                removed.append(thread_id)
        return removed

    def append_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        thread = self.load(thread_id)
        if thread is None:
            return None
        thread["messages"].append(
            {
                "role": role,
                "content": str(content or ""),
                "ts": time.time(),
                "meta": dict(meta or {}),
            }
        )
        thread["updated"] = time.time()
        self.save(thread)
        return thread

    def update_run(
        self, thread_id: str, run: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        thread = self.load(thread_id)
        if thread is None:
            return None
        thread["run"] = run
        thread["updated"] = time.time()
        self.save(thread)
        return thread

    def set_pinned(self, thread_id: str, pinned: bool) -> dict[str, Any] | None:
        thread = self.load(thread_id)
        if thread is None:
            return None
        thread["pinned"] = bool(pinned)
        thread["updated"] = time.time()
        self.save(thread)
        return thread

    def list_threads(self, limit: int = MAX_THREADS_LISTED) -> list[dict[str, Any]]:
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
        help="Delete threads containing canned test-mode answers.",
    )
    args = parser.parse_args(argv)
    store = ChatStore(args.live_dir)
    if args.clean_fake:
        removed = store.clean_fake_threads()
        for thread_id in removed:
            print(f"removed {thread_id}")
        print(f"{len(removed)} fake thread(s) removed.")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
