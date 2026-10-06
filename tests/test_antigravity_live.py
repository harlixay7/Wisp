"""Deterministic tests for the live event feed (parser, sinks, bridge wiring)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from tools.antigravity_bridge import (
    AttemptResult,
    BridgeConfig,
    DelegationEnvelope,
    run_bridge,
)
from tools.antigravity_live import (
    CallbackSink,
    JsonlSink,
    LiveEmitter,
    new_run_id,
    parse_stream_line,
)


class FailingSink:
    def emit(self, event) -> None:
        raise RuntimeError("sink exploded")


class StreamingLauncher:
    """Fake launcher that replays scripted stream lines through the raw sink."""

    def __init__(self, lines: list[str], result: AttemptResult) -> None:
        self.lines = lines
        self.result = result
        self.raw_line_sinks: list = []

    def __call__(self, command, cwd, env, hard_timeout_seconds, raw_line_sink=None):
        self.raw_line_sinks.append(raw_line_sink)
        if raw_line_sink is not None:
            for line in self.lines:
                raw_line_sink("stdout", line)
            raw_line_sink("stderr", "stderr warning line")
        self.result.command = tuple(str(part) for part in command)
        return self.result


def envelope() -> DelegationEnvelope:
    return DelegationEnvelope(prompt="Audit the live feed")


def success(stdout: str = "CRITIQUE") -> AttemptResult:
    return AttemptResult(exit_code=0, stdout=stdout, stderr="", duration_seconds=0.01)


class TestParsing:
    def test_thinking_tool_and_text(self) -> None:
        line = json.dumps(
            {
                "step_update": {
                    "thinking": "weighing options",
                    "tool_calls": [{"name": "run_command", "args": {"cmd": "pytest -q"}}],
                    "text": "partial answer",
                }
            }
        )

        events = parse_stream_line(line)
        kinds = [kind for kind, _, _ in events]

        assert "thinking" in kinds
        assert "tool_call" in kinds
        assert "text" in kinds
        tool = next(event for event in events if event[0] == "tool_call")
        assert tool[2]["name"] == "run_command"
        assert "pytest -q" in tool[1]

    def test_tool_result_is_classified(self) -> None:
        events = parse_stream_line(json.dumps({"tool_result": {"output": "75 passed"}}))

        assert events and events[0][0] == "tool_result"
        assert "75 passed" in events[0][1]

    def test_malformed_line_preserved_as_raw(self) -> None:
        assert parse_stream_line("not json at all") == [("raw", "not json at all", {})]

    def test_empty_line_yields_nothing(self) -> None:
        assert parse_stream_line("   ") == []

    def test_long_text_is_not_truncated(self) -> None:
        blob = "Z" * 50000
        events = parse_stream_line(json.dumps({"thinking": blob}))
        assert blob in events[0][1]

    def test_nested_event_containers_are_walked(self) -> None:
        line = json.dumps({"events": [{"thinking": "first"}, {"thinking": "second"}]})
        texts = [text for kind, text, _ in parse_stream_line(line) if kind == "thinking"]
        assert texts == ["first", "second"]

    def test_lifecycle_beacons_become_meta(self) -> None:
        init_line = json.dumps(
            {
                "event": "init",
                "init": {"model": "gemini-3.8-flash-high", "cwd": "C:\\x"},
            }
        )
        state_line = json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 90,
                    "state": "DONE",
                    "step_type": "agent_response",
                },
            }
        )
        usage_line = json.dumps({"event": "result", "duration_seconds": 1.5})

        assert parse_stream_line(init_line) == [
            ("meta", "session init \u00b7 gemini-3.8-flash-high", {})
        ]
        assert parse_stream_line(state_line) == [("meta", "step 90 \u00b7 DONE", {})]
        assert parse_stream_line(usage_line) == [("meta", "result envelope", {})]

    def test_delta_beats_beacon_classification(self) -> None:
        line = json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 90,
                    "state": "ACTIVE",
                    "text_delta": "real content",
                },
            }
        )

        events = parse_stream_line(line)

        assert events == [("text", "real content", {})]

    def test_agy_step_update_deltas_are_classified(self) -> None:
        text_line = json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "c",
                    "step_index": 42,
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": "provided\n\n3",
                },
            }
        )
        think_line = json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 43,
                    "state": "ACTIVE",
                    "step_type": "agent_thinking",
                    "thinking_delta": "weighing the tradeoff",
                },
            }
        )

        assert ("text", "provided\n\n3", {}) in parse_stream_line(text_line)
        assert ("thinking", "weighing the tradeoff", {}) in parse_stream_line(think_line)


class TestSinks:
    def test_jsonl_sink_appends_and_stamps(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        emitter = LiveEmitter("run-a", [JsonlSink(live, "run-a", keep_runs=2)])

        emitter.emit("run_start", "", model="m1")
        emitter.emit("thinking", "deep thought")
        emitter.close()

        files = list(live.glob("run-*.jsonl"))
        assert len(files) == 1
        lines = [
            json.loads(line)
            for line in files[0].read_text(encoding="utf-8").splitlines()
        ]
        assert [line["kind"] for line in lines] == ["run_start", "thinking"]
        assert lines[1]["text"] == "deep thought"
        assert [line["seq"] for line in lines] == [1, 2]
        assert all(line["run_id"] == "run-a" for line in lines)

    def test_retention_prunes_old_runs(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        live.mkdir()
        for index in range(3):
            stale = live / f"run-2026010{index}-000000-aaaaaa.jsonl"
            stale.write_text("{}\n", encoding="utf-8")
            os.utime(stale, (1000 + index, 1000 + index))

        JsonlSink(live, "new-run", keep_runs=2).close()

        names = sorted(path.name for path in live.glob("run-*.jsonl"))
        assert len(names) == 2
        assert "run-20260100-000000-aaaaaa.jsonl" not in names

    def test_sink_failures_never_raise(self) -> None:
        def exploding_callback(event) -> None:
            raise RuntimeError("boom")

        emitter = LiveEmitter("run-x", [CallbackSink(exploding_callback), FailingSink()])

        emitter.emit("thinking", "still fine")

    def test_emitter_assigns_identity(self) -> None:
        collected: list = []
        emitter = LiveEmitter("run-1", [CallbackSink(collected.append)])

        emitter.emit("thinking", "abc", model="gemini")

        event = collected[0]
        assert event.run_id == "run-1"
        assert event.kind == "thinking"
        assert event.model == "gemini"
        assert event.ts > 0

    def test_run_ids_unique(self) -> None:
        assert new_run_id() != new_run_id()


class TestBridgeIntegration:
    def test_run_bridge_emits_full_lifecycle(self, tmp_path: Path) -> None:
        stream_lines = [
            json.dumps({"step_update": {"thinking": "analyzing"}}),
            json.dumps(
                {
                    "step_update": {
                        "tool_calls": [{"name": "read_file", "args": {"path": "x.py"}}]
                    }
                }
            ),
            json.dumps({"tool_result": {"output": "file contents"}}),
            json.dumps({"step_update": {"text": "final verdict"}}),
        ]
        launcher = StreamingLauncher(stream_lines, success())
        config = BridgeConfig(
            envelope=envelope(),
            workspace=tmp_path,
            live=True,
            live_dir=tmp_path / "live",
            retry_backoff_seconds=0.0,
        )

        result = run_bridge(config, launcher=launcher)

        assert result.success
        run_files = list((tmp_path / "live").glob("run-*.jsonl"))
        assert len(run_files) == 1
        events = [
            json.loads(line)
            for line in run_files[0].read_text(encoding="utf-8").splitlines()
        ]
        kinds = [event["kind"] for event in events]
        for expected in (
            "run_start",
            "thinking",
            "tool_call",
            "tool_result",
            "text",
            "stderr",
            "attempt_start",
            "attempt_end",
            "run_end",
        ):
            assert expected in kinds
        end_event = events[-1]
        assert end_event["kind"] == "run_end"
        assert end_event["meta"]["success"] is True

    def test_live_disabled_writes_nothing(self, tmp_path: Path) -> None:
        launcher = StreamingLauncher([], success())

        run_bridge(
            BridgeConfig(envelope=envelope(), workspace=tmp_path),
            launcher=launcher,
        )

        assert not (tmp_path / ".antigravity-reports").exists()

    def test_callback_lifecycle_without_files(self, tmp_path: Path) -> None:
        collected: list = []
        config = BridgeConfig(
            envelope=envelope(),
            workspace=tmp_path,
            live_callback=collected.append,
            retry_backoff_seconds=0.0,
        )

        run_bridge(config, launcher=StreamingLauncher([], success()))

        kinds = [event.kind for event in collected]
        assert kinds[0] == "run_start"
        assert kinds[-1] == "run_end"
        assert all(event.run_id for event in collected)


class TestLiveRegistry:
    def test_register_and_read_with_env_isolation(self, tmp_path: Path, monkeypatch) -> None:
        from tools.antigravity_live import known_live_dirs, register_live_dir, registry_path

        registry = tmp_path / "registry.json"
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(registry))
        live = tmp_path / "ws" / ".antigravity-reports" / "live"
        live.mkdir(parents=True)

        register_live_dir(live, tmp_path / "ws")

        assert registry_path() == registry
        assert registry.exists()
        entries = known_live_dirs()
        assert [entry["path"] for entry in entries] == [str(live.resolve())]
        assert entries[0]["workspace"] == str(tmp_path / "ws")
        assert entries[0]["last_seen"] > 0

    def test_concurrent_registrations_all_present(self, tmp_path: Path, monkeypatch) -> None:
        import threading

        from tools.antigravity_live import known_live_dirs, register_live_dir

        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(tmp_path / "registry.json"))
        dirs: list[Path] = []
        for index in range(10):
            directory = tmp_path / f"ws{index}" / ".antigravity-reports" / "live"
            directory.mkdir(parents=True)
            dirs.append(directory)

        threads = [
            threading.Thread(target=register_live_dir, args=(directory,))
            for directory in dirs
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        known = {entry["path"] for entry in known_live_dirs()}
        for directory in dirs:
            assert str(directory.resolve()) in known

    def test_prunes_missing_and_caps_entries(self, tmp_path: Path, monkeypatch) -> None:
        from tools.antigravity_live import (
            REGISTRY_MAX_ENTRIES,
            known_live_dirs,
            register_live_dir,
            registry_path,
        )

        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(tmp_path / "registry.json"))
        for index in range(55):
            directory = tmp_path / f"d{index}" / "live"
            directory.mkdir(parents=True)
            register_live_dir(directory)

        data = json.loads(registry_path().read_text(encoding="utf-8"))
        for index in range(10):
            data.append(
                {
                    "path": str(tmp_path / f"missing{index}" / "live"),
                    "workspace": "",
                    "last_seen": 1.0,
                }
            )
        registry_path().write_text(json.dumps(data), encoding="utf-8")

        extra = tmp_path / "extra" / "live"
        extra.mkdir(parents=True)
        register_live_dir(extra)

        entries = known_live_dirs()
        assert len(entries) <= REGISTRY_MAX_ENTRIES
        assert all(Path(entry["path"]).is_dir() for entry in entries)
        assert str(extra.resolve()) in {entry["path"] for entry in entries}

    def test_corrupt_registry_is_tolerated(self, tmp_path: Path, monkeypatch) -> None:
        from tools.antigravity_live import known_live_dirs

        registry = tmp_path / "registry.json"
        registry.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(registry))

        assert known_live_dirs() == []


class TestBridgeRegistersLiveDir:
    def test_live_run_registers_its_directory(self, tmp_path: Path, monkeypatch) -> None:
        from tools.antigravity_live import known_live_dirs

        registry = tmp_path / "registry.json"
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(registry))
        config = BridgeConfig(
            envelope=DelegationEnvelope(prompt="register me"),
            workspace=tmp_path,
            live=True,
            retry_backoff_seconds=0.0,
        )

        run_bridge(config, launcher=StreamingLauncher([], success()))

        expected = str((tmp_path / ".antigravity-reports" / "live").resolve())
        assert expected in {entry["path"] for entry in known_live_dirs()}
