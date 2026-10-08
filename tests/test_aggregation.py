"""Stream-json aggregation into the organized critique, including delta folding."""

from __future__ import annotations

import json

from tools.antigravity_aggregate import _DeltaBuffer, aggregate_stream_json


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
