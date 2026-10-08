"""Live event feed: stream parsing, sinks, emission order, retention, and the registry."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.helpers import ROOT
from tools import antigravity_live
from tools.antigravity_bridge import (
    AttemptResult,
    BridgeConfig,
    DelegationEnvelope,
    run_bridge,
)
from tools.antigravity_live import (
    ACTIVE_RUN_GRACE_SECONDS,
    DEFAULT_KEEP_RUNS,
    LIVE_SCHEMA_VERSION,
    REGISTRY_MAX_ENTRIES,
    CallbackSink,
    JsonlSink,
    LiveEmitter,
    _registry_file_lock,
    known_live_dirs,
    new_run_id,
    parse_stream_line,
    register_live_dir,
    registry_path,
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


def _read_lines(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


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


class TestDepthPreservation:
    @staticmethod
    def _nested_data(depth: int, leaf: dict) -> dict:
        nested: dict = leaf
        for _ in range(depth):
            nested = {"data": nested}
        return nested

    def test_over_depth_content_survives_as_raw(self) -> None:
        deep = self._nested_data(12, {"payload": "deep-value-1"})
        events = parse_stream_line(json.dumps(deep))
        raw_events = [text for kind, text, _ in events if kind == "raw"]
        assert raw_events, "over-depth content must survive"
        assert any("deep-value-1" in text for text in raw_events)
        metas = [meta for kind, _, meta in events if kind == "raw"]
        assert any(entry.get("depth_truncated") for entry in metas)

    def test_mixed_depth_event_keeps_both_levels(self) -> None:
        payload = {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "text_delta": "shallow text",
                "data": self._nested_data(10, {"payload": "deep-value-2"}),
            },
        }
        events = parse_stream_line(json.dumps(payload))
        texts = [text for kind, text, _ in events]
        assert any("shallow text" in text for text in texts)
        assert any("deep-value-2" in text for text in texts)


class TestSinks:
    def test_jsonl_sink_appends_and_stamps(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        emitter = LiveEmitter("run-a", [JsonlSink(live, "run-a", keep_runs=2)])

        emitter.emit("run_start", "", model="m1")
        emitter.emit("thinking", "deep thought")
        emitter.close()

        files = list(live.glob("run-*.jsonl"))
        assert len(files) == 1
        lines = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
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


class TestOrderedEmission:
    def test_seq_order_matches_write_order_under_threads(self, tmp_path: Path) -> None:
        sink = JsonlSink(tmp_path, "run-ordered", keep_runs=DEFAULT_KEEP_RUNS)
        emitter = LiveEmitter("run-ordered", [sink])
        start = threading.Barrier(9)

        def worker(index: int) -> None:
            start.wait()
            for round_index in range(20):
                emitter.emit("text", f"w{index}-{round_index}")

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join()
        sink.close()
        events = _read_lines(sink.path)
        seqs = [event["seq"] for event in events]
        assert seqs == sorted(seqs)
        assert len(seqs) == 160
        assert seqs == list(range(1, 161))


class TestRetentionGrace:
    def test_prune_skips_recently_active_runs(self, tmp_path: Path) -> None:
        keep = 2
        paths = []
        for index in range(keep + 2):
            run = tmp_path / f"run-20260101-0000{index}-aa.jsonl"
            run.write_text('{"kind":"text"}\n', encoding="utf-8")
            paths.append(run)
        # All runs freshly written: even beyond the keep window, nothing may be
        # pruned because they could belong to another active process.
        sink = JsonlSink(tmp_path, "run-20260101-9999-zz", keep_runs=keep)
        survivors = sorted(path.name for path in tmp_path.glob("run-*.jsonl"))
        sink.close()
        assert len(survivors) == keep + 3

    def test_prune_claims_idle_old_runs(self, tmp_path: Path) -> None:
        keep = 1
        old = tmp_path / "run-20200101-000000-old.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        stale_stamp = time.time() - (ACTIVE_RUN_GRACE_SECONDS + 120)
        os.utime(old, (stale_stamp, stale_stamp))
        fresh = tmp_path / "run-20260101-000000-new.jsonl"
        fresh.write_text("{}\n", encoding="utf-8")
        sink = JsonlSink(tmp_path, "run-20260101-111111-cur", keep_runs=keep)
        sink.close()
        names = {path.name for path in tmp_path.glob("run-*.jsonl")}
        assert old.name not in names
        assert fresh.name in names
        assert sink.path.name in names


class TestBridgeIntegration:
    def test_run_bridge_emits_full_lifecycle(self, tmp_path: Path) -> None:
        stream_lines = [
            json.dumps({"step_update": {"thinking": "analyzing"}}),
            json.dumps(
                {"step_update": {"tool_calls": [{"name": "read_file", "args": {"path": "x.py"}}]}}
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
            json.loads(line) for line in run_files[0].read_text(encoding="utf-8").splitlines()
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

    def test_task_event_shows_the_prompt_right_after_run_start(self, tmp_path: Path) -> None:
        collected: list = []
        config = BridgeConfig(
            envelope=DelegationEnvelope(prompt="Review the retry plan"),
            workspace=tmp_path,
            live_callback=collected.append,
            retry_backoff_seconds=0.0,
        )

        run_bridge(config, launcher=StreamingLauncher([], success()))

        assert [event.kind for event in collected[:2]] == ["run_start", "task"]
        assert collected[1].text == "Review the retry plan"

    def test_task_event_preview_is_capped(self, tmp_path: Path) -> None:
        from tools.antigravity_bridge import LIVE_TASK_PREVIEW_CHARS

        collected: list = []
        long_prompt = "x" * (LIVE_TASK_PREVIEW_CHARS + 50)
        config = BridgeConfig(
            envelope=DelegationEnvelope(prompt=long_prompt),
            workspace=tmp_path,
            live_callback=collected.append,
            retry_backoff_seconds=0.0,
        )

        run_bridge(config, launcher=StreamingLauncher([], success()))

        task = next(event for event in collected if event.kind == "task")
        assert task.text.startswith("x" * LIVE_TASK_PREVIEW_CHARS)
        assert "full prompt is in the report" in task.text


class TestBridgeRegistersLiveDir:
    def test_live_run_registers_its_directory(self, tmp_path: Path, monkeypatch) -> None:
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


class TestLiveRegistry:
    def test_register_and_read_with_env_isolation(self, tmp_path: Path, monkeypatch) -> None:
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

    def test_concurrent_registrations_all_present_with_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = tmp_path / "registry.json"
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(registry))
        dirs: list[Path] = []
        for index in range(10):
            directory = tmp_path / f"ws{index}" / ".antigravity-reports" / "live"
            directory.mkdir(parents=True)
            dirs.append(directory)
        threads = [
            threading.Thread(target=register_live_dir, args=(directory,)) for directory in dirs
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        entries = json.loads(registry.read_text(encoding="utf-8"))
        assert len(entries) == 10
        assert all(entry.get("schema_version") == LIVE_SCHEMA_VERSION for entry in entries)
        known = {entry["path"] for entry in known_live_dirs()}
        for directory in dirs:
            assert str(directory.resolve()) in known

    def test_prunes_missing_and_caps_entries(self, tmp_path: Path, monkeypatch) -> None:
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
        registry = tmp_path / "registry.json"
        registry.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(registry))

        assert known_live_dirs() == []


class TestRegistryFileLocking:
    def test_lock_file_helper_yields_and_creates_sidecar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(tmp_path / "registry.json"))

        with _registry_file_lock():
            assert (tmp_path / "registry.json.lock").exists()

    def test_cross_process_registrations_all_present(self, tmp_path: Path) -> None:
        """The registry lock is OS-level: separate bridge processes register concurrently."""
        registry = tmp_path / "registry.json"
        script = (
            "import json, os, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            f"os.environ['ANTIGRAVITY_LIVE_REGISTRY'] = {str(registry)!r}\n"
            "from tools.antigravity_live import register_live_dir\n"
            f"parent = {str(tmp_path)!r}\n"
            "data = json.loads(sys.argv[1])\n"
            "for name in data:\n"
            "    register_live_dir(os.path.join(parent, name), parent)\n"
        )
        child_scripts = []
        for worker in range(3):
            names = json.dumps([f"live-{worker}-{index}" for index in range(4)])
            child_scripts.append(names)
        procs = []
        for names in child_scripts:
            procs.append(subprocess.Popen([sys.executable, "-c", script, names], cwd=str(ROOT)))
        codes = [proc.wait(timeout=120) for proc in procs]
        assert codes == [0, 0, 0]
        entries = json.loads(registry.read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in entries}
        expected = {
            str(tmp_path / f"live-{worker}-{index}") for worker in range(3) for index in range(4)
        }
        missing = expected - paths
        assert not missing, f"lost registry entries under cross-process contention: {missing}"

    def test_lock_propagates_body_import_error_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(tmp_path / "registry.json"))

        with pytest.raises(ImportError, match="from the body"):
            with _registry_file_lock():
                raise ImportError("from the body")

    def test_lock_timeout_proceeds_with_stderr_notice(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(tmp_path / "registry.json"))
        monkeypatch.setattr(antigravity_live, "_try_lock", lambda handle: False)
        monkeypatch.setattr(antigravity_live, "_REGISTRY_LOCK_ATTEMPTS", 3)
        monkeypatch.setattr(antigravity_live, "_REGISTRY_LOCK_RETRY_SECONDS", 0.0)
        ran = False
        with _registry_file_lock():
            ran = True
        captured = capsys.readouterr()
        assert ran
        assert captured.out == ""
        assert "without the cross-process lock" in captured.err

    def test_registration_sweeps_only_stale_temp_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = tmp_path / "registry.json"
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(registry))
        stale = tmp_path / "registry.json.tmp.deadbeef"
        fresh = tmp_path / "registry.json.tmp.cafef00d"
        for leftover in (stale, fresh):
            leftover.write_text("[]", encoding="utf-8")
        old = time.time() - 2 * 3600
        os.utime(stale, (old, old))
        register_live_dir(tmp_path / "live")
        assert not stale.exists()
        assert fresh.exists()
