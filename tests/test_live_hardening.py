"""Live-feed hardening regression tests from the external audit (2026-10).

* sequence order equals file write order under thread concurrency (#15),
* the doc guarantee about sinks is honest about backpressure (#16),
* the registry is protected across processes, not just threads (#17/#40),
* retention never claims a recently-active run (#18),
* content beyond traversal depth survives as a raw event (#19),
* registry entries carry a schema version (#63).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from tools.antigravity_live import (
    ACTIVE_RUN_GRACE_SECONDS,
    DEFAULT_KEEP_RUNS,
    LIVE_SCHEMA_VERSION,
    JsonlSink,
    LiveEmitter,
    parse_stream_line,
    register_live_dir,
    registry_path,
)


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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

    def test_concurrent_registrations_all_present_with_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = tmp_path / "registry.json"
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(registry))
        dirs = [tmp_path / f"live-{index}" for index in range(10)]
        threads = [
            threading.Thread(target=register_live_dir, args=(directory,))
            for directory in dirs
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        entries = json.loads(registry.read_text(encoding="utf-8"))
        assert len(entries) == 10
        assert all(entry.get("schema_version") == LIVE_SCHEMA_VERSION for entry in entries)


class TestRegistryFileLocking:
    def test_lock_file_helper_yields_and_creates_sidecar(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(tmp_path / "registry.json"))
        from tools.antigravity_live import _registry_file_lock

        with _registry_file_lock():
            assert (tmp_path / "registry.json.lock").exists()

    def test_cross_process_registrations_all_present(self, tmp_path: Path) -> None:
        """Multiprocessing regression for audit #40: the registry lock must be
        OS-level, because two bridge processes register concurrently."""
        registry = tmp_path / "registry.json"
        script = (
            "import json, os, sys\n"
            "sys.path.insert(0, r'%s')\n"
            "os.environ['ANTIGRAVITY_LIVE_REGISTRY'] = r'%s'\n"
            "from tools.antigravity_live import register_live_dir\n"
            "parent = r'%s'\n"
            "data = json.loads(sys.argv[1])\n"
            "for name in data:\n"
            "    register_live_dir(os.path.join(parent, name), parent)\n"
        ) % (str(Path(__file__).resolve().parent.parent), str(registry), str(tmp_path))
        child_scripts = []
        for worker in range(3):
            names = json.dumps([f"live-{worker}-{index}" for index in range(4)])
            child_scripts.append(names)
        procs = []
        for names in child_scripts:
            procs.append(
                __import__("subprocess").Popen(
                    ["python" if os.name != "nt" else "python", "-c", script, names],
                    cwd=str(Path(__file__).resolve().parent.parent),
                )
            )
        codes = [proc.wait(timeout=120) for proc in procs]
        assert codes == [0, 0, 0]
        entries = json.loads(registry.read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in entries}
        expected = {str(tmp_path / f"live-{worker}-{index}") for worker in range(3) for index in range(4)}
        missing = expected - paths
        assert not missing, f"lost registry entries under cross-process contention: {missing}"

    def test_lock_propagates_body_import_error_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(tmp_path / "registry.json"))
        from tools.antigravity_live import _registry_file_lock

        with pytest.raises(ImportError, match="from the body"):
            with _registry_file_lock():
                raise ImportError("from the body")

    def test_lock_timeout_proceeds_with_stderr_notice(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import tools.antigravity_live as live

        monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(tmp_path / "registry.json"))
        monkeypatch.setattr(live, "_try_lock", lambda handle: False)
        monkeypatch.setattr(live, "_REGISTRY_LOCK_ATTEMPTS", 3)
        monkeypatch.setattr(live, "_REGISTRY_LOCK_RETRY_SECONDS", 0.0)
        ran = False
        with live._registry_file_lock():
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


class TestRetentionGrace:
    def test_prune_skips_recently_active_runs(self, tmp_path: Path) -> None:
        keep = 2
        paths = []
        for index in range(keep + 2):
            run = tmp_path / f"run-20260101-0000{index}-aa.jsonl"
            run.write_text('{"kind":"text"}\n', encoding="utf-8")
            paths.append(run)
        # All runs freshly written: even beyond the keep window, nothing may be
        # pruned because they could belong to another active process (#18).
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
