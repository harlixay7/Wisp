"""Live event feed for the Antigravity delegation bridge.

Produces structured, viewer-grade telemetry for every delegation as newline
delimited JSON (one file per run under
``<workspace>/.antigravity-reports/live/run-<id>.jsonl``). The live viewer tails
the newest run file over SSE.

Guarantees:

* nothing in this module writes to stdout (MCP JSON-RPC purity),
* sink failures are swallowed and can never break or slow a delegation,
* event text is never truncated; retention prunes whole runs, one at a time.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

LIVE_DIR_NAME = Path(".antigravity-reports") / "live"
DEFAULT_KEEP_RUNS = 20
REGISTRY_ENV = "ANTIGRAVITY_LIVE_REGISTRY"
REGISTRY_MAX_ENTRIES = 50
_REGISTRY_LOCK = threading.Lock()


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
        entries.append(
            {
                "path": raw_path,
                "workspace": str(item.get("workspace") or ""),
                "last_seen": (
                    float(last_seen) if isinstance(last_seen, (int, float)) else 0.0
                ),
            }
        )
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
    with _REGISTRY_LOCK:
        by_path: dict[str, dict[str, Any]] = {}
        for entry in _read_registry():
            by_path[entry["path"]] = entry
        existing = by_path.get(str(resolved), {})
        by_path[str(resolved)] = {
            "path": str(resolved),
            "workspace": workspace_text or str(existing.get("workspace") or ""),
            "last_seen": time.time(),
        }
        alive = [entry for entry in by_path.values() if Path(entry["path"]).is_dir()]
        alive.sort(key=lambda entry: entry["last_seen"], reverse=True)
        alive = alive[:REGISTRY_MAX_ENTRIES]
        payload = json.dumps(alive, indent=2, ensure_ascii=False)
        path = registry_path()
        for attempt in range(5):
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
                time.sleep(0.01 * (2 ** attempt))


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
    {"step_update", "events", "steps", "items", "messages", "updates", "data", "result"}
)


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
    if depth > 8 or data is None or isinstance(data, str):
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
        init = data.get("init") if isinstance(data.get("init"), Mapping) else {}
        model = init.get("model") if isinstance(init, Mapping) else None
        return f"session init{(' · ' + str(model)) if model else ''}"
    if data.get("usage") is not None or data.get("duration_seconds") is not None:
        return "result envelope"
    step = data.get("step_update") if isinstance(data.get("step_update"), Mapping) else {}
    state = step.get("state") if isinstance(step, Mapping) else None
    step_type = step.get("step_type") if isinstance(step, Mapping) else None
    index = step.get("step_index") if isinstance(step, Mapping) else None
    if state:
        label = f"step {index} · {state}" if index is not None else f"step · {state}"
        if step_type and step_type not in ("agent_response",):
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
    def emit(self, event: LiveEvent) -> None:
        return None


class CallbackSink:
    """Forwards events to a callback; callback failures are swallowed."""

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
        self._handle: Any = open(self.path, "a", encoding="utf-8")
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
                stale.unlink()
            except OSError:
                pass


class LiveEmitter:
    """Fan-out emitter that stamps run id, sequence, and timestamp on events."""

    def __init__(self, run_id: str, sinks: Iterable[EventSink]) -> None:
        self.run_id = run_id
        self._sinks = [sink for sink in sinks if sink is not None]
        self._seq = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return bool(self._sinks)

    def emit(self, kind: str, text: str = "", model: str = "", **meta: Any) -> None:
        if not self._sinks:
            return
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
