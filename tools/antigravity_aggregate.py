"""Aggregation of ``agy`` stream-json output into a Markdown critique.

Also parses the reviewer's machine-readable verdict block
(``<<<WISP_VERDICT ... WISP_VERDICT>>>``, defined by the bridge's review
protocol) so callers get PASS / PASS_WITH_FIXES / BLOCK, severity counts and
the must-fix list without re-reading the whole critique.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tools.antigravity_live import _beacon_label

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
        "output",
        "delta",
        "message",
        "text_delta",
        "content_delta",
        "output_delta",
        "response_delta",
        "error",
        "detail",
        "details",
    }
)
_TOOL_KEYS = frozenset(
    {"tool_calls", "tool_call", "function_call", "tool", "actions", "action"}
)
_TOOL_RESULT_KEYS = frozenset(
    {
        "tool_result",
        "tool_results",
        "tool_output",
        "tool_outputs",
        "function_result",
        "observation",
    }
)
_NESTED_KEYS = frozenset(
    {"events", "steps", "items", "messages", "updates", "step_update", "error", "details", "data"}
)


def _fence(text: str, language: str = "text") -> str:
    longest = 0
    for match in re.finditer(r"`+", text):
        longest = max(longest, len(match.group(0)))
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _iter_tool_entries(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [entry for entry in value if entry is not None]
    return [value]


def _format_tool(tool: Any) -> str:
    if isinstance(tool, Mapping):
        name = tool.get("name") or tool.get("tool") or tool.get("type") or "tool"
        arguments = tool.get(
            "args",
            tool.get("arguments", tool.get("parameters", tool.get("input", {}))),
        )
        return f"**{name}**\n\n{_fence(_dumps_compact(arguments), 'json')}"
    return f"`{tool}`"


def _format_payload(value: Any, label: str) -> str:
    return f"**{label}**\n\n{_fence(_dumps_compact(value), 'json')}"


def _dumps_compact(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def _extract_fragments(data: Any, depth: int = 0) -> list[tuple[str, str]]:
    if data is None or depth > 8:
        return []
    if isinstance(data, str):
        return []
    fragments: list[tuple[str, str]] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            lower = str(key).lower()
            if lower == "step_update" and isinstance(value, Mapping):
                fragments.extend(_extract_fragments(value, depth + 1))
                continue
            if lower == "result":
                if isinstance(value, str) and value.strip():
                    fragments.append(("text", value.strip()))
                elif isinstance(value, (Mapping, list, tuple)):
                    fragments.extend(_extract_fragments(value, depth + 1))
                continue
            if lower in _REASONING_KEYS and isinstance(value, str) and value.strip():
                fragments.append(("reasoning", value.strip()))
            elif lower in _TEXT_KEYS and isinstance(value, str) and value.strip():
                fragments.append(("text", value.strip()))
            elif lower in _TOOL_KEYS:
                for tool in _iter_tool_entries(value):
                    fragments.append(("action", _format_tool(tool)))
            elif lower in _TOOL_RESULT_KEYS:
                for entry in _iter_tool_entries(value):
                    fragments.append(("tool_result", _format_payload(entry, "Result")))
            elif lower in _NESTED_KEYS and isinstance(value, (list, tuple, Mapping)):
                if isinstance(value, Mapping):
                    fragments.extend(_extract_fragments(value, depth + 1))
                else:
                    for item in value:
                        fragments.extend(_extract_fragments(item, depth + 1))
        return fragments
    if isinstance(data, (list, tuple)):
        for item in data:
            fragments.extend(_extract_fragments(item, depth + 1))
    return fragments


_STEP_TEXT_KEYS = (
    "text_delta",
    "content_delta",
    "output_delta",
    "response_delta",
    "text",
    "content",
    "response",
    "delta",
)


def _step_delta(step: Mapping[str, Any]) -> str:
    for key in _STEP_TEXT_KEYS:
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


class _DeltaBuffer:
    """Accumulates stream deltas, folding duplicate and cumulative resends.

    accumulated is maintained incrementally, so each append extends one string
    instead of re-joining every chunk. chunks is kept because pre-final notes
    render each chunk separately.
    """

    __slots__ = ("chunks", "accumulated", "_chunk_set")

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.accumulated: str = ""
        self._chunk_set: set[str] = set()

    def append(self, delta: str) -> None:
        if not delta:
            return
        if not self.chunks or delta.startswith(self.accumulated):
            # First delta, or a cumulative resend that supersedes everything.
            self._reset(delta)
            return
        if delta == self.chunks[-1] or self.accumulated.endswith(delta):
            return
        self.chunks.append(delta)
        self._chunk_set.add(delta)
        self.accumulated += delta

    def _reset(self, delta: str) -> None:
        self.chunks = [delta]
        self._chunk_set = {delta}
        self.accumulated = delta

    def text(self) -> str:
        return self.accumulated

    def __contains__(self, chunk: object) -> bool:
        """O(1) membership test against the current chunks."""
        return chunk in self._chunk_set

    def __bool__(self) -> bool:
        return bool(self.chunks)


class _FragmentSet:
    """Ordered, de-duplicated fragments plus a count of collapsed repeats."""

    __slots__ = ("items", "duplicates", "_seen")

    def __init__(self) -> None:
        self.items: list[str] = []
        self.duplicates = 0
        self._seen: set[str] = set()

    def add(self, fragment: str) -> bool:
        """Records ``fragment``; returns False (and counts it) when already seen."""
        if fragment in self._seen:
            self.duplicates += 1
            return False
        self._seen.add(fragment)
        self.items.append(fragment)
        return True


def _render_lifecycle(
    init_labels: list[str],
    step_meta: dict[int, dict[str, Any]],
    final_status: str,
    text_chars: int,
) -> list[str]:
    type_counts: dict[str, int] = {}
    notable: list[str] = []
    for index in sorted(step_meta):
        meta = step_meta[index]
        step_type = str(meta.get("type") or "model")
        type_counts[step_type] = type_counts.get(step_type, 0) + 1
        if step_type not in ("tool", "model"):
            state = str(meta.get("state") or "")
            suffix = f" · {state}" if state else ""
            notable.append(f"- step {index} · {step_type}{suffix}")
    total_steps = sum(type_counts.values())
    digest = (
        "- **Digest**: "
        + f"{total_steps} steps"
        + "".join(f" · {count} {name}" for name, count in sorted(type_counts.items()))
        + f" · {text_chars} chars of agent text"
    )
    lines = [digest]
    lines += [f"- {label}" for label in init_labels]
    lines += notable
    if final_status:
        lines.append(f"- result · {final_status}")
    return lines


def _render_sections(
    final_text: str,
    inferred_final: bool,
    notes: list[str],
    reasoning: list[str],
    actions: list[str],
    tool_results: list[str],
    lifecycle: list[str],
    unparsed: list[str],
) -> str:
    sections: list[str] = []
    if final_text:
        header = "### Findings & Response"
        if inferred_final:
            header += (
                "\n\n_Inferred final response: the stream carried no authoritative "
                "`result` envelope, so the highest-index step text is presented as the "
                "response. Verify against the raw streams in `attempts[]`._"
            )
        sections.append(header + "\n\n" + final_text)
    else:
        sections.append(
            "### Findings & Response\n\n"
            "_The stream contained no final response text; see the lifecycle digest "
            "and the report's raw streams in `attempts[]`._"
        )
    if notes:
        sections.append("### Working Notes (pre-final)\n\n" + "\n\n".join(notes))
    if reasoning:
        sections.append("### Reasoning & Analysis\n\n" + "\n\n".join(reasoning))
    if actions:
        sections.append("### Tool Activity\n\n" + "\n".join(actions))
    if tool_results:
        sections.append("### Tool Results\n\n" + "\n\n".join(tool_results))
    if lifecycle:
        sections.append("### Lifecycle\n\n" + "\n".join(lifecycle))
    if unparsed:
        sections.append("### Unparsed Stream Lines (verbatim)\n\n" + _fence("\n".join(unparsed)))
    return "\n\n".join(sections)


@dataclass
class StreamAnalysis:
    """Everything ``aggregate_stream_json`` renders, kept structured for reuse.

    ``final_response`` is the authoritative (or inferred) answer text; the
    verdict parser reads it so a block quoted inside a tool result can never
    be mistaken for the reviewer's verdict.
    """

    final_response: str = ""
    inferred_final: bool = False
    notes: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    tool_results: list[str] = field(default_factory=list)
    lifecycle: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


def aggregate_stream_json(raw: str) -> str:
    """Aggregates raw ``stream-json`` output into a cohesive Markdown critique.

    The final ``result`` response is authoritative and leads the output; when no
    ``result`` envelope exists, the highest-index step text is presented as an
    explicitly flagged *inferred* response. Streamed per-step text is coalesced
    (duplicate and cumulative resends are folded via ``_DeltaBuffer``), tool
    calls and tool results are surfaced, and lifecycle beacons collapse into a
    compact digest. Unparseable lines are preserved verbatim. Nothing is
    truncated: the complete raw streams remain available in ``attempts[]`` of
    the report.

    The organized critique is a **derived summary**, not a complete semantic
    representation of the stream; the raw ``attempts[].stdout`` record is the
    authoritative forensic artifact.
    """
    if not raw or not raw.strip():
        return "_Antigravity produced no stdout._"
    analysis = analyze_stream_json(raw)
    return _render_sections(
        analysis.final_response,
        analysis.inferred_final,
        analysis.notes,
        analysis.reasoning,
        analysis.actions,
        analysis.tool_results,
        analysis.lifecycle,
        analysis.unparsed,
    )


def analyze_stream_json(raw: str) -> StreamAnalysis:
    """Parses raw ``stream-json`` output into a :class:`StreamAnalysis`.

    See :func:`aggregate_stream_json` for how the final response is chosen.
    """
    if not raw or not raw.strip():
        return StreamAnalysis()

    reasoning = _FragmentSet()
    actions = _FragmentSet()
    tool_results = _FragmentSet()
    unparsed: list[str] = []
    init_labels: list[str] = []
    final_response = ""
    final_status = ""
    step_meta: dict[int, dict[str, Any]] = {}
    step_texts: dict[int, _DeltaBuffer] = {}
    other_texts = _DeltaBuffer()
    tool_counts: dict[str, int] = {}

    def _collect_fragments(data: Any) -> bool:
        """Files every fragment of a non-step event; True when any was new."""
        collected = False
        for kind, fragment in _extract_fragments(data):
            if kind == "reasoning":
                collected |= reasoning.add(fragment)
            elif kind == "action":
                collected |= actions.add(fragment)
            elif kind == "tool_result":
                collected |= tool_results.add(fragment)
            elif fragment not in other_texts:
                other_texts.append(fragment)
                collected = True
        return collected

    for line in io.StringIO(raw):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            unparsed.append(candidate)
            continue
        if not isinstance(data, Mapping):
            if not _collect_fragments(data):
                unparsed.append(candidate)
            continue

        event = str(data.get("event") or "").lower()
        if event == "init":
            init_labels.append(_beacon_label(data))
            continue

        inner = data.get("result")
        if event == "result" or (
            isinstance(inner, Mapping) and "status" in inner and "response" in inner
        ):
            if isinstance(inner, Mapping):
                response = inner.get("response")
                if isinstance(response, str) and response.strip():
                    final_response = response.strip()
                status = inner.get("status")
                if isinstance(status, str) and status.strip():
                    final_status = status.strip()
                if not final_response and not final_status:
                    init_labels.append(_beacon_label(data))
            elif isinstance(inner, str) and inner.strip():
                final_response = inner.strip()
            else:
                init_labels.append(_beacon_label(data))
            continue

        step = data.get("step_update")
        if isinstance(step, Mapping):
            index = step.get("step_index")
            index = index if isinstance(index, int) else None
            state = step.get("state")
            step_type = str(step.get("step_type") or "")
            delta = _step_delta(step)
            if delta:
                if index is None:
                    other_texts.append(delta)
                else:
                    step_texts.setdefault(index, _DeltaBuffer()).append(delta)
            tool_name = step.get("tool_name")
            if isinstance(tool_name, str) and tool_name.strip():
                name = tool_name.strip()
                if index is not None:
                    meta = step_meta.setdefault(index, {})
                    names = meta.setdefault("tools", [])
                    if name not in names:
                        names.append(name)
                else:
                    tool_counts[name] = tool_counts.get(name, 0) + 1
            # Step text arrives through the delta above. Action fragments are
            # taken only from steps that carry ``tool_calls``; other steps are
            # summarized by ``tool_name``.
            has_tool_calls = step.get("tool_calls") is not None
            for kind, fragment in _extract_fragments(step):
                if kind == "action":
                    if has_tool_calls:
                        actions.add(fragment)
                elif kind == "reasoning":
                    reasoning.add(fragment)
                elif kind == "tool_result":
                    tool_results.add(fragment)
            if index is not None and (state or step_type):
                meta = step_meta.setdefault(index, {})
                if state:
                    meta["state"] = str(state)
                if step_type:
                    meta["type"] = step_type
            elif index is None and not delta and not tool_name:
                if data.get("event") is not None:
                    init_labels.append(_beacon_label(data))
            continue

        if not _collect_fragments(data):
            if data.get("event") is not None:
                init_labels.append(_beacon_label(data))
            else:
                unparsed.append(candidate)

    # Without a ``result`` envelope, the highest-index step (or, failing that,
    # the un-indexed text) stands in as the response and is flagged inferred.
    inferred_final = False
    step_notes: list[str] = []
    if step_texts:
        final_step = step_texts.pop(max(step_texts))
        if not final_response:
            final_response = final_step.text().strip()
            inferred_final = True
        step_notes = [
            f"**step {index}** — {buffer.text().strip()}"
            for index, buffer in sorted(step_texts.items())
            if buffer.text().strip()
        ]
    other_notes = [chunk.strip() for chunk in other_texts.chunks if chunk.strip()]
    if other_texts and not final_response:
        final_response = other_texts.text().strip()
        inferred_final = True
        other_notes = [chunk for chunk in other_notes if chunk != final_response]
    notes = other_notes + step_notes

    for index in sorted(step_meta):
        for name in step_meta[index].get("tools", []):
            tool_counts[name] = tool_counts.get(name, 0) + 1
    action_lines = [
        f"- `{name}`" + (f" ×{count}" if count > 1 else "")
        for name, count in sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))
    ] + actions.items
    if actions.duplicates:
        action_lines.append(
            f"- _{actions.duplicates} duplicate action fragment(s) collapsed; "
            "full detail lives in the raw `attempts[]` streams_"
        )
    tool_result_lines = list(tool_results.items)
    if tool_results.duplicates:
        tool_result_lines.append(
            f"- _{tool_results.duplicates} duplicate tool-result fragment(s) collapsed; "
            "full detail lives in the raw `attempts[]` streams_"
        )
    text_chars = (
        len(final_response)
        + sum(len(buffer.text()) for buffer in step_texts.values())
        + len(other_texts.text())
    )
    lifecycle = _render_lifecycle(init_labels, step_meta, final_status, text_chars)
    return StreamAnalysis(
        final_response=final_response,
        inferred_final=inferred_final,
        notes=notes,
        reasoning=reasoning.items,
        actions=action_lines,
        tool_results=tool_result_lines,
        lifecycle=lifecycle,
        unparsed=unparsed,
    )


VERDICT_VALUES: tuple[str, ...] = ("PASS", "PASS_WITH_FIXES", "BLOCK")
CONFIDENCE_VALUES: tuple[str, ...] = ("high", "medium", "low")
SEVERITY_KEYS: tuple[str, ...] = ("P0", "P1", "P2", "P3")

_VERDICT_BLOCK_PATTERN = re.compile(
    r"<<<\s*WISP_VERDICT\b(.*?)\bWISP_VERDICT\s*>>>", re.IGNORECASE | re.DOTALL
)
# "key: value" with optional list markers, blockquotes or emphasis around the key.
_VERDICT_FIELD_PATTERN = re.compile(r"^[\s>*_`#-]*([A-Za-z][A-Za-z _-]*?)[\s*_`]*[:=]\s*(.*)$")
_COUNT_PATTERN = re.compile(r"\bP\s*([0-3])\s*[=:]\s*(\d+)", re.IGNORECASE)
_FINDING_ID_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9]*-\d+\b")
_NO_IDS = frozenset({"", "none", "n/a", "na", "nil", "-", "\u2014", "null", "[]"})
_VERDICT_KEYS = frozenset({"verdict", "confidence", "summary", "counts", "must_fix"})


def _clean_value(value: str) -> str:
    return value.strip().strip("*_`").strip()


def _parse_must_fix(value: str) -> list[str]:
    text = _clean_value(value)
    if text.lower() in _NO_IDS:
        return []
    ids = _FINDING_ID_PATTERN.findall(text)
    if not ids:
        ids = [part.strip("`*[]().'\"") for part in re.split(r"[,;\s]+", text)]
    unique: list[str] = []
    for item in ids:
        if item and item.lower() not in _NO_IDS and item not in unique:
            unique.append(item)
    return unique


def parse_review_verdict(text: str | None) -> dict[str, Any] | None:
    """Parses the LAST ``<<<WISP_VERDICT ... WISP_VERDICT>>>`` block in ``text``.

    Returns ``None`` when no block exists. Otherwise returns ``{verdict,
    confidence, summary, counts: {P0..P3}, must_fix: [ids]}``. The parser is
    lenient about presentation (code fences, emphasis, list markers, key case,
    ``must fix`` vs ``must_fix``) and strict about values: a verdict or
    confidence outside the protocol's enum becomes ``None`` and an unreadable
    count stays 0, so a sloppy block degrades instead of raising. The last
    block wins because the protocol puts the verdict at the very end; earlier
    blocks are usually quotes or drafts.
    """
    if not text:
        return None
    blocks = _VERDICT_BLOCK_PATTERN.findall(text)
    if not blocks:
        return None
    verdict: dict[str, Any] = {
        "verdict": None,
        "confidence": None,
        "summary": None,
        "counts": {key: 0 for key in SEVERITY_KEYS},
        "must_fix": [],
    }
    summary_lines: list[str] = []
    current: str | None = None
    for raw_line in blocks[-1].splitlines():
        line = raw_line.strip()
        if not line or set(line) <= set("`~"):
            continue
        match = _VERDICT_FIELD_PATTERN.match(line)
        key = (
            re.sub(r"[\s-]+", "_", match.group(1).strip().lower()) if match else None
        )
        if match is None or key not in _VERDICT_KEYS:
            # Only the summary may wrap onto continuation lines.
            if current == "summary":
                summary_lines.append(_clean_value(line))
            continue
        current = key
        value = match.group(2)
        if key == "verdict":
            candidate = re.sub(r"[\s-]+", "_", _clean_value(value).upper())
            verdict["verdict"] = candidate if candidate in VERDICT_VALUES else None
        elif key == "confidence":
            candidate = _clean_value(value).lower()
            verdict["confidence"] = candidate if candidate in CONFIDENCE_VALUES else None
        elif key == "summary":
            summary_lines = [_clean_value(value)]
        elif key == "counts":
            for severity, number in _COUNT_PATTERN.findall(value):
                verdict["counts"][f"P{severity}"] = int(number)
        elif key == "must_fix":
            verdict["must_fix"] = _parse_must_fix(value)
    summary = " ".join(part for part in summary_lines if part).strip()
    verdict["summary"] = summary or None
    return verdict


def extract_review_verdict(raw: str) -> dict[str, Any] | None:
    """Finds the reviewer's verdict in a raw ``stream-json`` stdout.

    The final response is authoritative. Lines that are not JSON (plain-text
    output) are searched only when the final response carries no block.
    """
    analysis = analyze_stream_json(raw)
    verdict = parse_review_verdict(analysis.final_response)
    if verdict is None and analysis.unparsed:
        verdict = parse_review_verdict("\n".join(analysis.unparsed))
    return verdict


def render_review_verdict(verdict: Mapping[str, Any]) -> str:
    """Short Markdown section that leads the critique with the parsed verdict."""
    label = verdict.get("verdict") or "not stated (missing or invalid value)"
    confidence = verdict.get("confidence") or "not stated"
    counts = verdict.get("counts") or {}
    must_fix = verdict.get("must_fix") or []
    lines = [
        "## Review verdict",
        "",
        f"- **Verdict**: {label} (confidence: {confidence})",
        f"- **Summary**: {verdict.get('summary') or 'not stated'}",
        "- **Findings**: "
        + " \u00b7 ".join(f"{key}={counts.get(key, 0)}" for key in SEVERITY_KEYS),
        f"- **Must fix**: {', '.join(must_fix) if must_fix else 'none'}",
    ]
    return "\n".join(lines)
