"""Stream-json aggregation into the organized critique, delta folding, and verdict parsing."""

from __future__ import annotations

import json

from tools.antigravity_aggregate import (
    _DeltaBuffer,
    aggregate_stream_json,
    extract_review_verdict,
    parse_review_verdict,
    render_review_verdict,
)


class TestStreamAggregation:
    def test_preserves_very_long_text_without_truncation(self) -> None:
        long_text = "X" * 20000
        raw = json.dumps({"step_update": {"thinking": "planning", "text": long_text}})

        critique = aggregate_stream_json(raw)

        assert long_text in critique
        assert "### Reasoning & Analysis" in critique

    def test_plain_text_lines_are_kept_verbatim(self) -> None:
        critique = aggregate_stream_json("plain line one\nplain line two")

        assert "plain line one" in critique
        assert "plain line two" in critique

    def test_single_json_container_response_is_extracted(self) -> None:
        raw = json.dumps({"status": "SUCCESS", "response": "VERDICT: plan is unsound"})

        critique = aggregate_stream_json(raw)

        assert "VERDICT: plan is unsound" in critique

    def test_tool_calls_are_rendered_with_full_arguments(self) -> None:
        payload = {
            "event": "step_update",
            "step_update": {
                "tool_calls": [
                    {"name": "run_command", "args": {"command": "pytest -q", "note": "Y" * 5000}}
                ]
            },
        }

        critique = aggregate_stream_json(json.dumps(payload))

        assert "run_command" in critique
        assert "Y" * 5000 in critique

    def test_empty_stdout_is_explicit(self) -> None:
        assert "no stdout" in aggregate_stream_json("").lower()

    def test_lifecycle_beacons_render_compactly_not_as_unparsed(self) -> None:
        lines = [
            json.dumps({"event": "init", "init": {"model": "gemini-3.8-flash-high"}}),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {"step_index": 4, "state": "ACTIVE"},
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {"step_index": 4, "state": "DONE"},
                }
            ),
            json.dumps({"step_update": {"text": "the real answer"}}),
        ]

        critique = aggregate_stream_json("\n".join(lines))

        assert "the real answer" in critique
        assert "### Lifecycle" in critique
        assert "**Digest**: 1 steps" in critique
        assert "1 model" in critique
        assert "Unparsed Stream Lines" not in critique

    def test_real_agy_stream_shapes_are_aggregated(self) -> None:
        lines = [
            json.dumps(
                {
                    "event": "init",
                    "init": {"model": "gemini-3.8-flash-high", "cwd": "C:\\repo"},
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 0,
                        "state": "DONE",
                        "step_type": "user_input",
                    },
                }
            ),
        ]
        for index in (2, 4, 6):
            for state in ("ACTIVE", "DONE"):
                lines.append(
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {
                                "step_index": index,
                                "state": state,
                                "step_type": "tool",
                                "tool_name": "read_file",
                            },
                        }
                    )
                )
        lines += [
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 180,
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": "# VERDICT\n\n",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 180,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": "Claim 1 is FALSIFIED with evidence.",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "response": "# VERDICT\n\nClaim 1 is FALSIFIED with evidence.",
                    },
                }
            ),
        ]

        critique = aggregate_stream_json("\n".join(lines))

        assert "### Findings & Response" in critique
        assert "Claim 1 is FALSIFIED with evidence." in critique
        assert "3 tool" in critique
        assert "result \u00b7 SUCCESS" in critique
        assert "`read_file` \u00d73" in critique
        assert "step 180" in critique
        assert "Complete stdout" not in critique
        assert "Unparsed Stream Lines" not in critique
        assert len(critique) < 4000

    def test_duplicate_and_cumulative_deltas_are_folded(self) -> None:
        lines = [
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 9,
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": "hello ",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 9,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": "hello ",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 9,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": "hello world",
                    },
                }
            ),
        ]

        critique = aggregate_stream_json("\n".join(lines))

        assert critique.count("hello world") == 1
        assert "hello hello" not in critique


class TestToolResultsAndDuplicates:
    def test_tool_results_surface_in_organized_critique(self) -> None:
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 1,
                            "state": "DONE",
                            "tool_result": {"path": "src/x.py", "lines": "1-9"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "Final words."},
                    }
                ),
            ]
        )
        critique = aggregate_stream_json(raw)
        assert "### Tool Results" in critique
        assert "src/x.py" in critique

    def test_inferred_final_is_flagged(self) -> None:
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"step_index": 0, "text_delta": "Looked at code."},
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"step_index": 1, "text_delta": "Deep finding: bug."},
                    }
                ),
            ]
        )
        critique = aggregate_stream_json(raw)
        assert "Deep finding: bug." in critique
        assert "Inferred final response" in critique

    def test_authoritative_result_is_not_flagged_inferred(self) -> None:
        raw = json.dumps(
            {
                "event": "result",
                "result": {"status": "SUCCESS", "response": "Authoritative."},
            }
        )
        critique = aggregate_stream_json(raw)
        assert "Authoritative." in critique
        assert "Inferred final response" not in critique

    def test_duplicate_actions_are_counted_not_silently_dropped(self) -> None:
        call = json.dumps({"tool": "read_file", "args": {"path": "same.txt"}})
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"step_index": 0, "tool_calls": [json.loads(call)]},
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"step_index": 1, "tool_calls": [json.loads(call)]},
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"step_index": 2, "tool_calls": [json.loads(call)]},
                    }
                ),
            ]
        )
        critique = aggregate_stream_json(raw)
        assert "duplicate action fragment" in critique
        assert "2 duplicate action fragment(s)" in critique

    def test_duplicate_tool_results_are_counted_across_steps(self) -> None:
        """Identical tool_results in separate step_update events dedupe to one
        entry, and the duplicate is counted."""
        result_payload = json.dumps({"output": "same result text"})
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"step_index": 1, "tool_result": json.loads(result_payload)},
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"step_index": 2, "tool_result": json.loads(result_payload)},
                    }
                ),
            ]
        )
        critique = aggregate_stream_json(raw)
        assert critique.count("same result text") == 1
        assert "1 duplicate tool-result fragment(s)" in critique


class TestDeltaBuffer:
    def test_duplicate_and_cumulative_deltas_fold_into_clean_text(self) -> None:
        buffer = _DeltaBuffer()
        for delta in ("Hello", "Hello world", " world", "!", "new"):
            buffer.append(delta)
        # Duplicate-and-cumulative folding leaves one clean accumulated text.
        assert buffer.chunks == ["Hello world", "!", "new"]
        assert buffer.text() == "Hello world!new"


VALID_BLOCK = (
    "<<<WISP_VERDICT\n"
    "verdict: PASS_WITH_FIXES\n"
    "confidence: medium\n"
    "summary: One P1 race in the writer.\n"
    "counts: P0=0 P1=1 P2=2 P3=0\n"
    "must_fix: F-001\n"
    "WISP_VERDICT>>>"
)


class TestReviewVerdictParsing:
    def test_valid_block_is_parsed(self) -> None:
        verdict = parse_review_verdict(f"## Findings\n\n...\n\n{VALID_BLOCK}\n")

        assert verdict == {
            "verdict": "PASS_WITH_FIXES",
            "confidence": "medium",
            "summary": "One P1 race in the writer.",
            "counts": {"P0": 0, "P1": 1, "P2": 2, "P3": 0},
            "must_fix": ["F-001"],
        }

    def test_block_inside_a_code_fence_with_loose_formatting(self) -> None:
        text = (
            "Verdict follows.\n\n```text\n"
            "<<< wisp_verdict\n"
            "  **Verdict**:  pass with fixes \n"
            "CONFIDENCE: High\n"
            "Summary: Fix the race:\n"
            "  the writer drops a flush.\n"
            "Counts: p0 = 0, P1=2, P2: 1, P3=0\n"
            "Must fix: `F-001`, F-003\n"
            "WISP_VERDICT >>>\n```\n"
        )

        verdict = parse_review_verdict(text)

        assert verdict is not None
        assert verdict["verdict"] == "PASS_WITH_FIXES"
        assert verdict["confidence"] == "high"
        assert verdict["summary"] == "Fix the race: the writer drops a flush."
        assert verdict["counts"] == {"P0": 0, "P1": 2, "P2": 1, "P3": 0}
        assert verdict["must_fix"] == ["F-001", "F-003"]

    def test_last_block_wins(self) -> None:
        draft = VALID_BLOCK.replace("PASS_WITH_FIXES", "PASS")
        final = VALID_BLOCK.replace("PASS_WITH_FIXES", "BLOCK").replace("P0=0", "P0=1")

        verdict = parse_review_verdict(f"{draft}\n\nOn reflection:\n\n{final}")

        assert verdict is not None
        assert verdict["verdict"] == "BLOCK"
        assert verdict["counts"]["P0"] == 1

    def test_malformed_values_are_ignored_not_fatal(self) -> None:
        text = (
            "<<<WISP_VERDICT\n"
            "verdict: PASS | PASS_WITH_FIXES | BLOCK\n"
            "confidence: very sure\n"
            "counts: P0=x P1=3 P7=9\n"
            "must_fix: none\n"
            "unexpected line without a key\n"
            "WISP_VERDICT>>>"
        )

        verdict = parse_review_verdict(text)

        assert verdict is not None
        assert verdict["verdict"] is None
        assert verdict["confidence"] is None
        assert verdict["summary"] is None
        assert verdict["counts"] == {"P0": 0, "P1": 3, "P2": 0, "P3": 0}
        assert verdict["must_fix"] == []

    def test_missing_block_is_none(self) -> None:
        assert parse_review_verdict("verdict: PASS but no markers") is None
        assert parse_review_verdict("") is None
        assert parse_review_verdict(None) is None

    def test_extracts_from_the_final_response_of_a_stream(self) -> None:
        raw = "\n".join(
            [
                json.dumps({"step_update": {"step_index": 0, "text": "reading files"}}),
                json.dumps(
                    {"event": "result", "result": {"status": "SUCCESS", "response": VALID_BLOCK}}
                ),
            ]
        )

        verdict = extract_review_verdict(raw)

        assert verdict is not None and verdict["verdict"] == "PASS_WITH_FIXES"

    def test_block_quoted_in_a_tool_result_is_not_the_verdict(self) -> None:
        raw = "\n".join(
            [
                json.dumps({"tool_result": {"output": VALID_BLOCK}}),
                json.dumps(
                    {"event": "result", "result": {"status": "SUCCESS", "response": "No block."}}
                ),
            ]
        )

        assert extract_review_verdict(raw) is None

    def test_plain_text_output_is_searched(self) -> None:
        assert extract_review_verdict(f"plain critique\n{VALID_BLOCK}") is not None

    def test_rendered_section_summarizes_the_verdict(self) -> None:
        verdict = parse_review_verdict(VALID_BLOCK)
        assert verdict is not None

        section = render_review_verdict(verdict)

        assert section.startswith("## Review verdict")
        assert "PASS_WITH_FIXES (confidence: medium)" in section
        assert "P0=0 · P1=1 · P2=2 · P3=0" in section
        assert "**Must fix**: F-001" in section
