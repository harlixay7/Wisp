"""Aggregation of ``agy`` stream-json output into a Markdown critique."""

from __future__ import annotations

import io
import json
import re
from collections.abc import Mapping
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

    Maintains the accumulated text incrementally so coalescing stays linear:
    the pre-fix implementation re-joined the whole chunk list on every delta
    (quadratic in the number of fragments for genuinely incremental streams).
    ``chunks`` is retained because per-chunk notes are rendered individually.
    """

    __slots__ = ("chunks", "accumulated")

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.accumulated: str = ""

    def append(self, delta: str) -> None:
        if not delta:
            return
        if not self.chunks:
            self.chunks = [delta]
            self.accumulated = delta
            return
        if delta == self.chunks[-1]:
            return
        if delta.startswith(self.accumulated):
            self.chunks = [delta]
            self.accumulated = delta
            return
        if self.accumulated.endswith(delta):
            return
        self.chunks.append(delta)
        self.accumulated += delta

    def text(self) -> str:
        return self.accumulated

    def __bool__(self) -> bool:
        return bool(self.chunks)
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

    reasoning: list[str] = []
    actions: list[str] = []
    duplicate_actions = 0
    tool_results: list[str] = []
    duplicate_tool_results = 0
    notes: list[str] = []
    unparsed: list[str] = []
    init_labels: list[str] = []
    final_response = ""
    final_status = ""
    final_from_envelope = False
    step_meta: dict[int, dict[str, Any]] = {}
    step_texts: dict[int, _DeltaBuffer] = {}
    other_texts = _DeltaBuffer()
    tool_counts: dict[str, int] = {}
    # Companion seen-sets keep dedupe O(1) per fragment (CAN-008: membership
    # scans against the unbounded lists were quadratic in fragment count).
    seen_reasoning: set[str] = set()
    seen_actions: set[str] = set()
    seen_tool_results: set[str] = set()

    def _collect_fragments(data: Any) -> bool:
        nonlocal duplicate_actions, duplicate_tool_results
        collected = False
        for kind, fragment in _extract_fragments(data):
            if kind == "reasoning":
                if fragment not in seen_reasoning:
                    seen_reasoning.add(fragment)
                    reasoning.append(fragment)
                    collected = True
            elif kind == "action":
                if fragment not in seen_actions:
                    seen_actions.add(fragment)
                    actions.append(fragment)
                    collected = True
                else:
                    duplicate_actions += 1
            elif kind == "tool_result":
                if fragment not in seen_tool_results:
                    seen_tool_results.add(fragment)
                    tool_results.append(fragment)
                    collected = True
                else:
                    duplicate_tool_results += 1
            elif fragment not in other_texts.chunks:
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
                    final_from_envelope = True
                status = inner.get("status")
                if isinstance(status, str) and status.strip():
                    final_status = status.strip()
                if not final_response and not final_status:
                    init_labels.append(_beacon_label(data))
            elif isinstance(inner, str) and inner.strip():
                final_response = inner.strip()
                final_from_envelope = True
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
                if index is not None:
                    buffer = step_texts.get(index)
                    if buffer is None:
                        buffer = step_texts[index] = _DeltaBuffer()
                    buffer.append(delta)
                else:
                    other_texts.append(delta)
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
            if step.get("tool_calls") is not None:
                for kind, fragment in _extract_fragments(step):
                    if kind == "action":
                        if fragment not in seen_actions:
                            seen_actions.add(fragment)
                            actions.append(fragment)
                        else:
                            duplicate_actions += 1
            for kind, fragment in _extract_fragments(step):
                if kind == "reasoning" and fragment not in seen_reasoning:
                    seen_reasoning.add(fragment)
                    reasoning.append(fragment)
                elif kind == "tool_result":
                    if fragment not in seen_tool_results:
                        # AST-001 (fresh re-audit): the add was missing here,
                        # so identical tool_results repeated across step
                        # updates were never deduplicated or counted.
                        seen_tool_results.add(fragment)
                        tool_results.append(fragment)
                    else:
                        duplicate_tool_results += 1
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

    inferred_final = False
    if step_texts:
        if not final_response:
            final_index = max(step_texts)
            final_response = step_texts.pop(final_index).text().strip()
            inferred_final = True
        else:
            step_texts.pop(max(step_texts), None)
        notes = [
            f"**step {index}** \u2014 {buffer.text().strip()}"
            for index, buffer in sorted(step_texts.items())
            if buffer.text().strip()
        ]
    if other_texts and not final_response:
        final_response = other_texts.text().strip()
        inferred_final = True
        remaining = [chunk.strip() for chunk in other_texts.chunks if chunk.strip()]
        remaining = [chunk for chunk in remaining if chunk != final_response]
    elif other_texts:
        remaining = [chunk.strip() for chunk in other_texts.chunks if chunk.strip()]
    else:
        remaining = []
    notes = remaining + notes
    if final_from_envelope:
        inferred_final = False

    for index in sorted(step_meta):
        for name in step_meta[index].get("tools", []):
            tool_counts[name] = tool_counts.get(name, 0) + 1
    if tool_counts:
        actions = [
            f"- `{name}`" + (f" \u00d7{count}" if count > 1 else "")
            for name, count in sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))
        ] + actions
    if duplicate_actions:
        actions.append(
            f"- _{duplicate_actions} duplicate action fragment(s) collapsed; "
            "full detail lives in the raw `attempts[]` streams_"
        )
    if duplicate_tool_results:
        tool_results.append(
            f"- _{duplicate_tool_results} duplicate tool-result fragment(s) collapsed; "
            "full detail lives in the raw `attempts[]` streams_"
        )
    text_chars = (
        len(final_response)
        + sum(len(buffer.text()) for buffer in step_texts.values())
        + len(other_texts.text())
    )
    lifecycle = _render_lifecycle(init_labels, step_meta, final_status, text_chars)
    return _render_sections(
        final_response,
        inferred_final,
        notes,
        reasoning,
        actions,
        tool_results,
        lifecycle,
        unparsed,
    )
