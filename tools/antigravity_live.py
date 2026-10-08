"""Live event feed for the Antigravity delegation bridge.

Produces structured, viewer-grade telemetry for every delegation as newline
delimited JSON (one file per run under
``<workspace>/.antigravity-reports/live/run-<id>.jsonl``). The live viewer tails
the newest run file over SSE.

Guarantees:

* nothing in this module writes to stdout (MCP JSON-RPC purity),
* sink failures are swallowed and can never break a delegation; events are
  emitted synchronously under a lock, so sequence numbers match file write
  order. A pathologically slow sink therefore adds backpressure to the
  delegating process rather than dropping or reordering data — durability is
  chosen over throughput for this forensic feed,
* event text is never truncated; retention prunes whole runs one at a time and
  never deletes a run whose file was modified within
  ``ACTIVE_RUN_GRACE_SECONDS`` (active runs keep their mtime fresh),
* content that falls beyond the parser's traversal depth is preserved as a
  verbatim ``raw`` event, so nothing carrying content is ever dropped from the
  organized feed either.

Event schema: consumers should treat the ``run_start`` event's
``meta.schema_version`` and registry entries' ``schema_version`` as the wire
format version (``LIVE_SCHEMA_VERSION``).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Protocol, TextIO

LIVE_DIR_NAME = Path(".antigravity-reports") / "live"
# Viewer-owned settings (model selection, etc.), stored inside the live dir so
# every reader resolves it through the same ANTIGRAVITY_LIVE_DIR override.
VIEWER_SETTINGS_FILE = "viewer_settings.json"
DEFAULT_KEEP_RUNS = 20
LIVE_SCHEMA_VERSION = 1
ACTIVE_RUN_GRACE_SECONDS = 3600
REGISTRY_ENV = "ANTIGRAVITY_LIVE_REGISTRY"
REGISTRY_MAX_ENTRIES = 50
_REGISTRY_LOCK = threading.Lock()
_REGISTRY_LOCK_ATTEMPTS = 100
_REGISTRY_LOCK_RETRY_SECONDS = 0.02
_REGISTRY_TEMP_MAX_AGE_SECONDS = 3600
_REGISTRY_WRITE_ATTEMPTS = 5
_REGISTRY_WRITE_BACKOFF_SECONDS = 0.01


if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle: BinaryIO) -> bool:
        """Makes one non-blocking attempt at the sidecar lock."""
        # msvcrt.locking acts at the current file offset, and an "a+b" handle
        # starts at EOF, which moves as the file grows. Pin lock and unlock to
        # byte 0 so they always cover the same region; otherwise the lock
        # silently stops excluding other processes.
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: BinaryIO) -> bool:
        """Makes one non-blocking attempt at the sidecar lock."""
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _registry_lock_notice(reason: str) -> None:
    sys.stderr.write(
        f"[antigravity-live] {reason}; updating the live registry without the "
        "cross-process lock\n"
    )
    sys.stderr.flush()


@contextlib.contextmanager
def _registry_file_lock() -> Iterator[None]:
    """Best-effort cross-process exclusive lock over the live-directory registry.

    The in-process ``_REGISTRY_LOCK`` cannot stop two Wisp processes from
    read-modify-write racing on the same registry file, so updates are also
    serialized with an OS-level lock on a sidecar ``.lock`` file (``msvcrt``
    on Windows, ``fcntl`` on POSIX). Acquisition is non-blocking with a
    bounded retry (``_REGISTRY_LOCK_ATTEMPTS`` x
    ``_REGISTRY_LOCK_RETRY_SECONDS``), so registry bookkeeping can never stall
    a delegation. If the sidecar cannot be opened or the lock is not acquired
    in time, a one-line notice goes to stderr and the body runs under the
    thread lock alone.
    """
    lock_path = registry_path().with_name(registry_path().name + ".lock")
    handle: BinaryIO | None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
    except OSError as exc:
        _registry_lock_notice(f"cannot open {lock_path} ({exc})")
        handle = None
    locked = False
    try:
        if handle is not None:
            for attempt in range(_REGISTRY_LOCK_ATTEMPTS):
                if attempt:
                    time.sleep(_REGISTRY_LOCK_RETRY_SECONDS)
                if _try_lock(handle):
                    locked = True
                    break
            else:
                _registry_lock_notice(f"timed out waiting for {lock_path}")
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    _unlock(handle)
                except OSError:
                    pass
            try:
                handle.close()
            except OSError:
                pass


def _sweep_stale_registry_temps(path: Path) -> None:
    """Deletes registry temp files orphaned by a writer that died mid-update.

    A temp file normally lives for milliseconds before ``os.replace`` consumes
    it, so anything older than ``_REGISTRY_TEMP_MAX_AGE_SECONDS`` is debris.
    The caller holds the registry lock, so no live writer can own such a file.
    """
    prefix = path.name + ".tmp."
    cutoff = time.time() - _REGISTRY_TEMP_MAX_AGE_SECONDS
    try:
        candidates = [
            entry for entry in path.parent.iterdir() if entry.name.startswith(prefix)
        ]
    except OSError:
        return
    for candidate in candidates:
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            pass


def registry_path() -> Path:
    """Central live-directory registry path (env override wins, e.g. in tests)."""
    override = os.environ.get(REGISTRY_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / ".antigravity-reports" / "live-registry.json"


def _read_registry() -> list[dict[str, Any]]:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        last_seen = item.get("last_seen")
        schema_version = item.get("schema_version")
        entry: dict[str, Any] = {
            "path": raw_path,
            "workspace": str(item.get("workspace") or ""),
            "last_seen": (
                float(last_seen) if isinstance(last_seen, (int, float)) else 0.0
            ),
        }
        if isinstance(schema_version, (int, float)):
            entry["schema_version"] = int(schema_version)
        entries.append(entry)
    return entries


def register_live_dir(
    live_dir: Path | str, workspace: Path | str | None = None
) -> None:
    """Registers a live directory so any viewer on this machine can watch it.

    Atomic tempfile replacement with bounded retries; failures are swallowed so
    registration can never break or slow a delegation.
    """
    try:
        resolved = Path(live_dir).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    workspace_text = ""
    if workspace is not None and str(workspace).strip():
        workspace_text = str(Path(workspace).expanduser())
    with _REGISTRY_LOCK, _registry_file_lock():
        by_path: dict[str, dict[str, Any]] = {}
        for entry in _read_registry():
            by_path[entry["path"]] = entry
        existing = by_path.get(str(resolved), {})
        by_path[str(resolved)] = {
            "path": str(resolved),
            "workspace": workspace_text or str(existing.get("workspace") or ""),
            "last_seen": time.time(),
            "schema_version": LIVE_SCHEMA_VERSION,
        }
        alive = [entry for entry in by_path.values() if Path(entry["path"]).is_dir()]
        alive.sort(key=lambda entry: entry["last_seen"], reverse=True)
        alive = alive[:REGISTRY_MAX_ENTRIES]
        payload = json.dumps(alive, indent=2, ensure_ascii=False)
        path = registry_path()
        _sweep_stale_registry_temps(path)
        for attempt in range(_REGISTRY_WRITE_ATTEMPTS):
            temporary: Path | None = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + ".tmp." + uuid.uuid4().hex[:8])
                temporary.write_text(payload, encoding="utf-8")
                os.replace(temporary, path)
                return
            except OSError:
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
                time.sleep(_REGISTRY_WRITE_BACKOFF_SECONDS * (2 ** attempt))


def known_live_dirs() -> list[dict[str, Any]]:
    """Returns registered live directories that still exist, newest first."""
    with _REGISTRY_LOCK:
        entries = _read_registry()
    alive: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        key = entry["path"]
        if key in seen:
            continue
        seen.add(key)
        if Path(key).is_dir():
            alive.append(entry)
    alive.sort(key=lambda entry: entry["last_seen"], reverse=True)
    return alive


_REASONING_KEYS = frozenset(
    {
        "thinking",
        "thought",
        "reasoning",
        "reasoning_content",
        "thinking_delta",
        "thought_delta",
        "reasoning_delta",
    }
)
_TEXT_KEYS = frozenset(
    {
        "text",
        "content",
        "response",
        "delta",
        "text_delta",
        "content_delta",
        "output_delta",
        "response_delta",
    }
)
_TOOL_KEYS = frozenset({"tool_calls", "tool_call", "function_call", "tool", "actions", "action"})
_TOOL_RESULT_KEYS = frozenset(
    {"tool_result", "tool_results", "tool_output", "tool_outputs", "function_result"}
)
_NESTED_KEYS = frozenset(
    {"step_update", "events", "steps", "items", "messages", "updates", "data"}
)
# Nesting deeper than this is emitted as one verbatim ``raw`` event rather than
# walked further, which bounds recursion on adversarial or runaway payloads.
_MAX_WALK_DEPTH = 8


def new_run_id() -> str:
    """Returns a sortable run id (``YYYYmmdd-HHMMSS-<hex>``)."""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


@dataclass(frozen=True)
class LiveEvent:
    """One discrete moment in a delegation, safe to serialize as one NDJSON line."""

    run_id: str
    seq: int
    ts: float
    kind: str
    model: str = ""
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _format_tools(value: Any) -> list[tuple[str, str, dict[str, Any]]]:
    entries = value if isinstance(value, (list, tuple)) else [value]
    events: list[tuple[str, str, dict[str, Any]]] = []
    for entry in entries:
        if entry is None:
            continue
        if isinstance(entry, Mapping):
            name = entry.get("name") or entry.get("tool") or entry.get("type") or "tool"
            arguments = entry.get(
                "args",
                entry.get("arguments", entry.get("parameters", entry.get("input", {}))),
            )
            try:
                rendered = json.dumps(arguments, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                rendered = str(arguments)
            events.append(("tool_call", rendered, {"name": str(name)}))
        else:
            events.append(("tool_call", str(entry), {"name": str(entry)}))
    return events


def _format_results(value: Any) -> list[tuple[str, str, dict[str, Any]]]:
    entries = value if isinstance(value, (list, tuple)) else [value]
    events: list[tuple[str, str, dict[str, Any]]] = []
    for entry in entries:
        if entry is None:
            continue
        if isinstance(entry, str):
            if entry.strip():
                events.append(("tool_result", entry.strip(), {}))
            continue
        if isinstance(entry, Mapping):
            for key in ("output", "content", "result", "text", "message"):
                candidate = entry.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    events.append(("tool_result", candidate.strip(), {}))
                    break
            else:
                try:
                    events.append(
                        ("tool_result", json.dumps(entry, ensure_ascii=False, default=str), {})
                    )
                except (TypeError, ValueError):
                    events.append(("tool_result", str(entry), {}))
            continue
        events.append(("tool_result", str(entry), {}))
    return events


def _walk(data: Any, depth: int = 0) -> list[tuple[str, str, dict[str, Any]]]:
    if data is None or isinstance(data, str):
        return []
    if depth > _MAX_WALK_DEPTH:
        # Preserve over-depth content verbatim instead of dropping it, so the
        # "nothing that carries content is ever dropped" promise holds even in
        # the mixed case where shallow fields already matched.
        if isinstance(data, (Mapping, list, tuple)):
            try:
                return [
                    (
                        "raw",
                        json.dumps(data, ensure_ascii=False, default=str),
                        {"depth_truncated": True},
                    )
                ]
            except (TypeError, ValueError):
                return []
        return []
    events: list[tuple[str, str, dict[str, Any]]] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            lower = str(key).lower()
            if lower in _REASONING_KEYS and isinstance(value, str) and value.strip():
                events.append(("thinking", value.strip(), {}))
            elif lower in _TOOL_RESULT_KEYS:
                events.extend(_format_results(value))
            elif lower in _TOOL_KEYS:
                events.extend(_format_tools(value))
            elif lower in _TEXT_KEYS and isinstance(value, str) and value.strip():
                events.append(("text", value.strip(), {}))
            elif lower == "result":
                if isinstance(value, str) and value.strip():
                    events.append(("text", value.strip(), {}))
                elif isinstance(value, (Mapping, list, tuple)):
                    events.extend(_walk(value, depth + 1))
            elif lower in _NESTED_KEYS and isinstance(value, (Mapping, list, tuple)):
                events.extend(_walk(value, depth + 1))
        return events
    if isinstance(data, (list, tuple)):
        for item in data:
            events.extend(_walk(item, depth + 1))
    return events


def _beacon_label(data: Mapping[str, Any]) -> str:
    """Human label for content-less lifecycle envelopes (init/state/usage)."""
    event = str(data.get("event") or "").lower()
    if event == "init":
        init = data.get("init")
        model = init.get("model") if isinstance(init, Mapping) else None
        return f"session init · {model}" if model else "session init"
    if data.get("usage") is not None or data.get("duration_seconds") is not None:
        return "result envelope"
    step = data.get("step_update")
    if isinstance(step, Mapping) and step.get("state"):
        state = step["state"]
        index = step.get("step_index")
        step_type = step.get("step_type")
        label = f"step {index} · {state}" if index is not None else f"step · {state}"
        if step_type and step_type != "agent_response":
            label += f" · {step_type}"
        return label
    return event or "event"


def parse_stream_line(line: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Parses one raw ``agy`` output line into feed events.

    Returns ``(kind, text, meta)`` tuples where kind is one of ``thinking``,
    ``tool_call``, ``tool_result``, ``text``, ``meta``, or ``raw``. Known
    lifecycle envelopes (init/state/usage) become compact ``meta`` markers;
    unparseable lines are preserved verbatim as ``raw`` events. Nothing that
    carries content is ever dropped.
    """
    stripped = line.strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return [("raw", stripped, {})]
    events = _walk(data)
    if not events:
        if isinstance(data, Mapping) and data.get("event") is not None:
            return [("meta", _beacon_label(data), {})]
        return [("raw", stripped, {})]
    return events


class EventSink(Protocol):
    """Anything that can receive live events without ever raising."""

    def emit(self, event: LiveEvent) -> None: ...


class NullSink:
    """Discards every event; stands in when the live feed is disabled."""

    def emit(self, event: LiveEvent) -> None:
        return None


class CallbackSink:
    """Forwards events to a callback; callback failures are swallowed.

    The callback runs while :class:`LiveEmitter` holds its (non-reentrant)
    lock, so it must not call ``emit()`` on the same emitter; doing so would
    deadlock.
    """

    def __init__(self, callback: Callable[[LiveEvent], None]) -> None:
        self._callback = callback

    def emit(self, event: LiveEvent) -> None:
        try:
            self._callback(event)
        except Exception:
            pass


class JsonlSink:
    """Append-only NDJSON writer with bounded run retention."""

    def __init__(
        self,
        live_dir: Path | str,
        run_id: str,
        keep_runs: int = DEFAULT_KEEP_RUNS,
    ) -> None:
        self.live_dir = Path(live_dir)
        self.run_id = run_id
        self.keep_runs = max(int(keep_runs), 1)
        self.path = self.live_dir / f"run-{run_id}.jsonl"
        self._lock = threading.Lock()
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self._handle: TextIO | None = open(self.path, "a", encoding="utf-8")
        self._prune()

    def emit(self, event: LiveEvent) -> None:
        line = event.to_json() + "\n"
        with self._lock:
            if self._handle is None:
                return
            try:
                self._handle.write(line)
                self._handle.flush()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                except Exception:
                    pass
                self._handle = None

    def _prune(self) -> None:
        cutoff = time.time() - ACTIVE_RUN_GRACE_SECONDS
        try:
            runs = sorted(
                self.live_dir.glob("run-*.jsonl"),
                key=lambda path: (path.stat().st_mtime, path.name),
                reverse=True,
            )
        except OSError:
            return
        for stale in runs[self.keep_runs :]:
            if stale == self.path:
                continue
            try:
                # Never prune a run touched within the grace window: in-flight
                # runs (possibly owned by another process) keep their mtime
                # fresh.
                if stale.stat().st_mtime >= cutoff:
                    continue
                stale.unlink()
            except OSError:
                pass


class LiveEmitter:
    """Fan-out emitter that stamps run id, sequence, and timestamp on events.

    Sinks are invoked while the emitter's lock is held, so a sink (including a
    :class:`CallbackSink` callback) must not call :meth:`emit` re-entrantly.
    """

    def __init__(self, run_id: str, sinks: Iterable[EventSink]) -> None:
        self.run_id = run_id
        self._sinks = [sink for sink in sinks if sink is not None]
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, kind: str, text: str = "", model: str = "", **meta: Any) -> None:
        """Stamps and writes one event to every sink; never raises."""
        if not self._sinks:
            return
        # Stamp seq and write to every sink under one lock so on-disk order
        # always matches seq order; writing outside the lock lets concurrent
        # emitters interleave.
        with self._lock:
            self._seq += 1
            event = LiveEvent(
                run_id=self.run_id,
                seq=self._seq,
                ts=time.time(),
                kind=str(kind),
                model=str(model or ""),
                text=str(text or ""),
                meta=dict(meta),
            )
            for sink in self._sinks:
                try:
                    sink.emit(event)
                except Exception:
                    pass

    def close(self) -> None:
        for sink in self._sinks:
            close = getattr(sink, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
