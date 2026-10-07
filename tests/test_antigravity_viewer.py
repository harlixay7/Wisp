"""Deterministic tests for the live viewer server (discovery, HTTP, SSE)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tools.antigravity_viewer import list_runs, newest_run

ROOT = Path(__file__).resolve().parent.parent


def _write_run(
    live_dir: Path,
    name: str,
    events: list[dict],
    mtime: float | None = None,
) -> Path:
    live_dir.mkdir(parents=True, exist_ok=True)
    path = live_dir / name
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestPortBinding:
    def test_busy_port_falls_back_to_next(self, tmp_path: Path) -> None:
        import socket

        from tools.antigravity_viewer import ViewerContext, bind_server

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            preferred = probe.getsockname()[1]

        def factory(port: int) -> ViewerContext:
            return ViewerContext(
                workspace=tmp_path,
                live_dir=tmp_path / "live",
                port=port,
            )

        first, first_port = bind_server("127.0.0.1", preferred, factory, max_attempts=10)
        try:
            second, second_port = bind_server(
                "127.0.0.1", preferred, factory, max_attempts=10
            )
        finally:
            first.server_close()
        second.server_close()

        assert first_port == preferred
        assert second_port == preferred + 1


class TestRunDiscovery:
    def test_newest_run_picks_latest_mtime(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        _write_run(live, "run-a.jsonl", [{"kind": "run_start"}], mtime=1000)
        _write_run(live, "run-b.jsonl", [{"kind": "run_start"}], mtime=2000)

        assert newest_run(live).name == "run-b.jsonl"

    def test_newest_run_none_when_missing(self, tmp_path: Path) -> None:
        assert newest_run(tmp_path / "missing") is None

    def test_list_runs_sorted_desc(self, tmp_path: Path) -> None:
        live = tmp_path / "live"
        _write_run(live, "run-a.jsonl", [], mtime=1000)
        _write_run(live, "run-b.jsonl", [], mtime=2000)

        runs = list_runs(live)

        assert [run["file"] for run in runs] == ["run-b.jsonl", "run-a.jsonl"]


class ViewerProcess:
    def __init__(
        self,
        tmp_path: Path,
        extra_env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.extra_env = extra_env or {}
        self.extra_args = extra_args or []
        self.proc: subprocess.Popen | None = None
        self.base = ""

    def __enter__(self) -> tuple[str, Path]:
        live = self.tmp_path / ".antigravity-reports" / "live"
        _write_run(
            live,
            "run-20260101-000000-test01.jsonl",
            [{"kind": "run_start", "run_id": "r", "seq": 1, "ts": time.time(), "text": ""}],
        )
        env = dict(os.environ)
        env.update(self.extra_env)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tools" / "antigravity_viewer.py"),
                "--port",
                "0",
                "--workspace",
                str(self.tmp_path),
                "--no-open",
                *self.extra_args,
            ],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        manifest = live / "viewer.json"
        deadline = time.time() + 20
        while time.time() < deadline and not manifest.exists():
            if self.proc.poll() is not None:
                error = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"viewer exited early: {error}")
            time.sleep(0.1)
        if not manifest.exists():
            raise RuntimeError("viewer.json was never written")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.base = f"http://127.0.0.1:{data['port']}"
        return self.base, live

    def __exit__(self, *args) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()


@pytest.fixture()
def viewer(tmp_path: Path):
    with ViewerProcess(tmp_path) as context:
        yield context


@pytest.fixture()
def fake_viewer(tmp_path: Path):
    with ViewerProcess(
        tmp_path,
        extra_env={
            "WISP_ASK_FAKE": "1",
            "WISP_ASK_FAKE_DELAY": "1.5",
            "WISP_CAPTURE_FAKE": "text:selected text from clipboard",
            "WISP_CAPTURE_FAKE_DELAY": "1.2",
        },
    ) as context:
        yield context


def _post_json(base: str, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + path,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=30)
        return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


class TestCaptureEndpoints:
    def test_concurrent_captures_rejected(self, fake_viewer) -> None:
        import threading

        base, _ = fake_viewer
        results: list[int] = []

        def first_capture() -> None:
            request = urllib.request.Request(
                base + "/api/capture",
                method="POST",
                data=json.dumps({"mode": "auto"}).encode("utf-8"),
                headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    results.append(response.status)
            except urllib.error.HTTPError as exc:
                results.append(exc.code)

        worker = threading.Thread(target=first_capture)
        worker.start()
        time.sleep(0.4)
        status, data = _post_json(base, "/api/capture", {"mode": "auto"})
        worker.join(timeout=30)

        assert status == 409
        assert data["error"] == "busy"
        assert results and results[0] == 200

    def test_capture_text_fake(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, data = _post_json(base, "/api/capture", {"mode": "auto"})

        assert status == 200
        assert data["kind"] == "text"
        assert "clipboard" in data["text"]

    def test_capture_diagnostic_contains_no_secret_material(self, fake_viewer) -> None:
        base, _ = fake_viewer

        _, data = _post_json(base, "/api/capture", {})

        assert "password" not in json.dumps(data).lower()


PNG_BLOB = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class TestImageAttachments:
    def _upload(self, base: str, blob: bytes) -> tuple[int, dict]:
        return _post_json(
            base,
            "/api/chat/upload",
            {"data": base64.b64encode(blob).decode("ascii")},
        )

    def test_upload_and_serve_roundtrip(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, data = self._upload(base, PNG_BLOB)

        assert status == 200
        assert data["kind"] == "image"
        assert data["name"].startswith("paste-")
        assert data["rel_path"].startswith(".antigravity-reports/captures/")
        saved = Path(data["path"])
        assert saved.is_file()
        assert saved.read_bytes() == PNG_BLOB
        with urllib.request.urlopen(
            base + "/captures/" + data["name"], timeout=10
        ) as response:
            assert response.status == 200
            assert response.headers.get("Content-Type", "").startswith("image/png")
            assert response.read() == PNG_BLOB

    def test_upload_rejects_non_image_bytes(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, data = self._upload(base, b"just some text")

        assert status == 400
        assert "unsupported" in data["error"]

    def test_upload_rejects_invalid_base64(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, _ = _post_json(
            base, "/api/chat/upload", {"data": "!!!not-base64!!!"}
        )

        assert status == 400

    def test_upload_rejects_oversize(self, fake_viewer) -> None:
        base, _ = fake_viewer
        blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * (12 * 1024 * 1024)

        status, data = self._upload(base, blob)

        assert status == 413
        assert "12 MB" in data["error"]

    def test_capture_route_blocks_traversal(self, fake_viewer) -> None:
        base, _ = fake_viewer

        for path in (
            "/captures/..%2F..%2Fviewer.json",
            "/captures/",
            "/captures/nope.exe",
        ):
            try:
                with urllib.request.urlopen(base + path, timeout=10) as response:
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            assert status == 404, path

    def test_ask_records_image_paths(self, fake_viewer) -> None:
        base, live = fake_viewer
        capture_dir = live.parent / "captures"
        capture_dir.mkdir(parents=True, exist_ok=True)
        first = capture_dir / "paste-test-a.png"
        second = capture_dir / "paste-test-b.png"
        first.write_bytes(PNG_BLOB)
        second.write_bytes(PNG_BLOB)

        status, data = _post_json(
            base,
            "/api/ask",
            {"prompt": "two images", "images": [str(first), str(second)]},
        )
        assert status == 200
        thread_id = data["thread_id"]
        deadline = time.time() + 30
        thread = None
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + thread_id, timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        # Contained artifacts are recorded workspace-relative (audit #2/#33).
        expected_rel = [
            str(first.relative_to(fake_viewer[1].parent.parent)).replace("\\", "/"),
            str(second.relative_to(fake_viewer[1].parent.parent)).replace("\\", "/"),
        ]
        user_meta = thread["messages"][0]["meta"]
        assert user_meta["image_paths"] == expected_rel
        assert user_meta["image_path"] == expected_rel[0]

    def test_ask_rejects_outside_artifact_paths(self, fake_viewer) -> None:
        base, _ = fake_viewer
        outside = Path(fake_viewer[1].parent.parent) / "outside-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        status, data = _post_json(
            base,
            "/api/ask",
            {"prompt": "peek", "images": [str(outside)]},
        )
        assert status == 400
        assert data["rejected"] == [str(outside)]


class TestImagePasteUnit:
    def test_sniff_formats(self) -> None:
        from tools.wisp_capture import sniff_image_suffix

        assert sniff_image_suffix(b"\x89PNG\r\n\x1a\nrest") == ".png"
        assert sniff_image_suffix(b"\xff\xd8\xff\xe0rest") == ".jpg"
        assert sniff_image_suffix(b"GIF89a....") == ".gif"
        assert sniff_image_suffix(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp"
        assert sniff_image_suffix(b"nope") is None

    def test_save_pasted_image(self, tmp_path: Path) -> None:
        from tools.wisp_capture import save_pasted_image

        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)

        saved = save_pasted_image(live, PNG_BLOB)

        assert saved is not None
        assert saved.parent.name == "captures"
        assert saved.suffix == ".png"
        assert saved.read_bytes() == PNG_BLOB
        assert save_pasted_image(live, b"junk") is None

    def test_prompt_lists_all_images(self) -> None:
        from tools.antigravity_viewer import build_ask_prompt

        prompt = build_ask_prompt("check", "", ("a/b.png", "c/d.jpg"))

        assert "## ATTACHED IMAGES" in prompt
        assert "`a/b.png`" in prompt
        assert "`c/d.jpg`" in prompt
        assert "ATTACHED IMAGES" not in build_ask_prompt("check", "", ())


class TestAskFlow:
    def test_ask_roundtrip_with_fake_runner(self, fake_viewer) -> None:
        base, live = fake_viewer

        status, data = _post_json(
            base,
            "/api/ask",
            {"prompt": "Check this snippet", "context_text": "x = 1"},
        )
        assert status == 200
        thread_id = data["thread_id"]
        assert data["status"] == "running"

        deadline = time.time() + 30
        thread = None
        while time.time() < deadline:
            raw = urllib.request.urlopen(
                base + "/api/chat/thread/" + thread_id, timeout=10
            ).read()
            thread = json.loads(raw)["thread"]
            if (
                len(thread["messages"]) >= 2
                and thread["run"]["status"] in ("done", "failed")
            ):
                break
            time.sleep(0.3)

        assert thread is not None
        assert thread["messages"][0]["role"] == "user"
        assistant = thread["messages"][1]
        assert assistant["role"] == "assistant"
        assert "Quick review" in assistant["content"]
        assert assistant["meta"]["success"] is True
        assert thread["run"]["status"] == "done"
        run_files = list(live.glob("run-*.jsonl"))
        assert len(run_files) >= 2

    def test_ask_busy_returns_409(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status_a, data_a = _post_json(base, "/api/ask", {"prompt": "first"})
        assert status_a == 200
        status_b, data_b = _post_json(base, "/api/ask", {"prompt": "second"})
        assert status_b == 409
        assert data_b["error"] == "busy"
        assert data_b["thread_id"] == data_a["thread_id"]

    def test_follow_up_reuses_thread(self, fake_viewer) -> None:
        base, _ = fake_viewer

        _, data = _post_json(base, "/api/ask", {"prompt": "first question"})
        thread_id = data["thread_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + thread_id, timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        status, data2 = _post_json(
            base,
            "/api/ask",
            {"prompt": "follow-up question", "thread_id": thread_id},
        )
        assert status == 200
        assert data2["thread_id"] == thread_id

        deadline = time.time() + 30
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + thread_id, timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 4:
                break
            time.sleep(0.3)
        assert len(thread["messages"]) >= 4
        roles = [message["role"] for message in thread["messages"]]
        assert roles[:4] == ["user", "assistant", "user", "assistant"]

    def test_thread_pin_and_ordering(self, fake_viewer) -> None:
        base, _ = fake_viewer

        _, first = _post_json(base, "/api/ask", {"prompt": "first"})
        deadline = time.time() + 30
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + first["thread_id"], timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        status, pinned = _post_json(
            base,
            "/api/chat/thread/" + first["thread_id"] + "/pin",
            {"pinned": True},
        )
        assert status == 200
        assert pinned["thread"]["pinned"] is True

        threads = json.loads(
            urllib.request.urlopen(base + "/api/chat/threads", timeout=10).read()
        )["threads"]
        assert threads[0]["id"] == first["thread_id"]
        assert threads[0]["pinned"] is True

    def test_thread_persisted_to_disk(self, fake_viewer) -> None:
        base, live = fake_viewer

        _, data = _post_json(base, "/api/ask", {"prompt": "persist me"})

        thread_path = live / "chat" / f"thread-{data['thread_id']}.json"
        assert thread_path.exists()
        document = json.loads(thread_path.read_text(encoding="utf-8"))
        assert document["messages"][0]["content"] == "persist me"


class TestChatAnswerExtraction:
    def test_prefers_critique_section(self) -> None:
        from tools.antigravity_viewer import extract_chat_answer

        report = "\n".join(
            [
                "# ANTIGRAVITY ADVERSARIAL DELEGATION REPORT",
                "- **Verdict**: SUCCESS",
                "",
                "## Antigravity Critique â€” `gemini-3.8-flash-high` (PRIMARY)",
                "",
                "### Findings & Response",
                "",
                "Bottom line: looks fine.",
                "",
                "### Lifecycle",
                "- step 4 Â· DONE",
                "",
                "## Complete stdout (verbatim) â€” `gemini-3.8-flash-high`",
                "```text",
                json.dumps({"event": "step_update", "step_update": {"text_delta": "raw"}}),
                "```",
            ]
        )

        answer = extract_chat_answer(report)

        assert "Bottom line: looks fine." in answer
        assert "step 4" in answer
        assert "Complete stdout" not in answer
        assert "step_update" not in answer
        assert not answer.startswith("## Antigravity Critique")

    def test_fallback_trim_without_marker(self) -> None:
        from tools.antigravity_viewer import extract_chat_answer

        answer = extract_chat_answer("plain answer\n## Complete stderr\njunk")

        assert answer == "plain answer"

    def test_empty_report_is_explicit(self) -> None:
        from tools.antigravity_viewer import extract_chat_answer

        assert "No answer" in extract_chat_answer("")


class TestExistingViewerDetection:
    def test_dead_manifest_returns_none(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import existing_viewer

        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        (live / "viewer.json").write_text(
            json.dumps({"port": 59999, "pid": 12345}), encoding="utf-8"
        )

        assert existing_viewer(live) is None

    def test_missing_manifest_returns_none(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import existing_viewer

        assert existing_viewer(tmp_path) is None


class TestChatStoreUnit:
    def test_invalid_thread_id_rejected(self, tmp_path: Path) -> None:
        from tools.wisp_chat import ChatStore

        store = ChatStore(tmp_path)
        assert store.load("../../etc/passwd") is None
        store.append_message("../../etc/passwd", "user", "nope")

    def test_thread_lifecycle(self, tmp_path: Path) -> None:
        from tools.wisp_chat import ChatStore

        store = ChatStore(tmp_path)
        thread = store.create("Hello world, this is a long enough prompt for titling")
        assert thread["title"].startswith("Hello world")

        store.append_message(thread["id"], "user", "hi")
        store.append_message(thread["id"], "assistant", "hello", {"success": True})
        loaded = store.load(thread["id"])
        assert [message["role"] for message in loaded["messages"]] == ["user", "assistant"]

        summaries = store.list_threads()
        assert summaries[0]["id"] == thread["id"]
        assert summaries[0]["message_count"] == 2
        assert "hello" in summaries[0]["preview"]


class TestHttpEndpoints:
    def test_index_serves_wisp_ui(self, viewer) -> None:
        base, _ = viewer

        html = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")

        assert "Wisp" in html
        assert "/events" in html
        assert "modelBtn" in html
        assert "streamScroll" in html

    def test_health_reports_watching(self, viewer) -> None:
        base, _ = viewer

        health = json.loads(urllib.request.urlopen(base + "/health", timeout=10).read())
        details = json.loads(
            urllib.request.urlopen(base + "/health/details", timeout=10).read()
        )

        # /health is deliberately minimal (unauthenticated liveness probe).
        assert health["status"] == "ok"
        assert "watching" not in health
        assert "live_dir" not in health
        assert details["watching"].startswith("run-")

    def test_assets_and_runs_endpoints(self, viewer) -> None:
        base, _ = viewer

        assets = json.loads(urllib.request.urlopen(base + "/api/assets", timeout=10).read())
        runs = json.loads(urllib.request.urlopen(base + "/api/runs", timeout=10).read())

        assert "assets" in assets
        assert any(run["file"].startswith("run-20260101") for run in runs["runs"])

    def test_post_requires_session_header(self, viewer) -> None:
        base, _ = viewer
        request = urllib.request.Request(base + "/api/account/recheck", method="POST")

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)

        assert excinfo.value.code == 403

    def test_unknown_route_404(self, viewer) -> None:
        base, _ = viewer

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(base + "/nope", timeout=10)

        assert excinfo.value.code == 404

    def test_model_selection_persists(self, viewer) -> None:
        base, live = viewer

        defaults = json.loads(urllib.request.urlopen(base + "/api/model", timeout=10).read())
        assert defaults["model"] == "gemini-3.8-flash-high"

        request = urllib.request.Request(
            base + "/api/model",
            method="POST",
            data=json.dumps({"model": "claude-sonnet-4-6"}).encode("utf-8"),
            headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
        )
        saved = json.loads(urllib.request.urlopen(request, timeout=10).read())
        assert saved["model"] == "claude-sonnet-4-6"
        assert saved["model_known"] is True

        again = json.loads(urllib.request.urlopen(base + "/api/model", timeout=10).read())
        assert again["model"] == "claude-sonnet-4-6"
        assert (live / "viewer_settings.json").exists()

        # Malformed names are rejected outright (audit #34).
        malformed = urllib.request.Request(
            base + "/api/model",
            method="POST",
            data=json.dumps({"model": "not a valid name!"}).encode("utf-8"),
            headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(malformed, timeout=10)
        assert excinfo.value.code == 400

        # Well-formed but unknown ids are rejected unless allow_custom is set.
        unknown = urllib.request.Request(
            base + "/api/model",
            method="POST",
            data=json.dumps({"model": "zzz-not-a-real-model"}).encode("utf-8"),
            headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(unknown, timeout=10)
        assert excinfo.value.code == 400
        body = json.loads(excinfo.value.read().decode("utf-8"))
        assert "known_models" in body

        forced = urllib.request.Request(
            base + "/api/model",
            method="POST",
            data=json.dumps({"model": "zzz-not-a-real-model", "allow_custom": True}).encode("utf-8"),
            headers={"X-Wisp-Request": "1", "Content-Type": "application/json"},
        )
        saved_custom = json.loads(urllib.request.urlopen(forced, timeout=10).read())
        assert saved_custom["model"] == "zzz-not-a-real-model"
        assert saved_custom["model_known"] is False

    def test_ask_rejects_unknown_model(self, fake_viewer) -> None:
        base, _ = fake_viewer
        status, data = _post_json(
            base,
            "/api/ask",
            {"prompt": "hi", "model": "zzz-not-a-real-model"},
        )
        assert status == 400
        assert "unknown model" in data["error"]

    def test_model_post_requires_session_header(self, viewer) -> None:
        base, _ = viewer
        request = urllib.request.Request(
            base + "/api/model",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)

        assert excinfo.value.code == 403


class TestRunMetadata:
    def test_describe_run_parses_lifecycle(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import describe_run

        live = tmp_path / "live"
        live.mkdir()
        path = live / "run-x.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "kind": "run_start",
                            "run_id": "x",
                            "seq": 1,
                            "ts": 1000.0,
                            "model": "m",
                            "text": "",
                            "meta": {"primary_model": "gemini-3.8-flash-high"},
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "thinking",
                            "run_id": "x",
                            "seq": 2,
                            "ts": 1001.0,
                            "model": "m",
                            "text": "t",
                            "meta": {},
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "run_end",
                            "run_id": "x",
                            "seq": 3,
                            "ts": 1010.0,
                            "model": "m",
                            "text": "",
                            "meta": {
                                "success": True,
                                "elapsed_seconds": 9.5,
                                "failover_used": False,
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        record = describe_run(path)

        assert record["model"] == "gemini-3.8-flash-high"
        assert record["finished"] is True
        assert record["success"] is True
        assert record["elapsed_seconds"] == 9.5
        assert record["failover_used"] is False

    def test_describe_run_unfinished(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import describe_run

        live = tmp_path / "live"
        live.mkdir()
        path = live / "run-y.jsonl"
        path.write_text(
            json.dumps(
                {
                    "kind": "run_start",
                    "run_id": "y",
                    "seq": 1,
                    "ts": 2000.0,
                    "model": "m",
                    "text": "",
                    "meta": {"primary_model": "claude-sonnet-4-6"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        record = describe_run(path)

        assert record["finished"] is False
        assert record["success"] is None
        assert record["model"] == "claude-sonnet-4-6"


class TestModelParsing:
    def test_parse_model_list(self) -> None:
        from tools.antigravity_viewer import parse_model_list

        output = (
            "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
            "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
            "\n"
        )

        assert parse_model_list(output) == [
            "gemini-3.8-flash-high",
            "claude-sonnet-4-6",
        ]

    def test_selected_models_prefer_viewer_settings(self, tmp_path: Path) -> None:
        from tools.antigravity_mcp_server import _selected_models

        live = tmp_path / ".antigravity-reports" / "live"
        live.mkdir(parents=True)
        (live / "viewer_settings.json").write_text(
            json.dumps({"model": "claude-sonnet-4-6"}),
            encoding="utf-8",
        )

        selected = _selected_models(tmp_path)

        assert selected["model"] == "claude-sonnet-4-6"
        assert selected["fallback_model"] == "claude-opus-4-6-thinking"


class TestServerSentEvents:
    def test_streams_hello_and_new_events(self, viewer) -> None:
        base, live = viewer
        response = urllib.request.urlopen(base + "/events", timeout=15)

        assert "text/event-stream" in response.headers.get("Content-Type", "")
        buffer = b""
        deadline = time.time() + 10
        while b"event: hello" not in buffer and time.time() < deadline:
            buffer += response.readline()
        assert b"event: hello" in buffer

        run_file = newest_run(live)
        with open(run_file, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": "thinking",
                        "run_id": "r",
                        "seq": 2,
                        "ts": time.time(),
                        "text": "live thought",
                    }
                )
                + "\n"
            )

        deadline = time.time() + 10
        while b"live thought" not in buffer and time.time() < deadline:
            buffer += response.readline()
        assert b"live thought" in buffer
        response.close()


class TestCaptureHygiene:
    @pytest.mark.skipif(os.name != "nt", reason="Windows-only console flags")
    def test_powershell_helpers_never_show_a_console(self, monkeypatch) -> None:
        import subprocess as sp

        from tools import wisp_capture

        captured = {}

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            return sp.CompletedProcess(args, 0, b"", b"")

        monkeypatch.setattr(wisp_capture.subprocess, "run", fake_run)
        wisp_capture._run_powershell(["-File", "x.ps1"], timeout=1)

        assert captured.get("creationflags") == wisp_capture.CREATE_NO_WINDOW

    def test_snip_wait_stays_bounded(self) -> None:
        from tools import wisp_capture

        assert wisp_capture.SNIP_TIMEOUT_SECONDS <= 60

    def test_ui_ships_pending_reconciliation(self) -> None:
        html = (ROOT / "tools" / "antigravity_viewer.html").read_text(
            encoding="utf-8"
        )

        assert "threadHasPendingAsk" in html
        assert "reconcilePendingAsk" in html


class TestSelectionCaptureModifiers:
    @pytest.mark.skipif(os.name != "nt", reason="Windows-only capture path")
    def test_modifier_wait_returns_when_keys_are_up(self) -> None:
        import time as _time

        from tools.wisp_capture import _wait_for_modifier_release

        started = _time.monotonic()
        _wait_for_modifier_release(timeout_ms=500)

        assert _time.monotonic() - started < 0.8


class TestChatModelScoping:
    def test_ask_uses_chat_model_when_set(self, fake_viewer) -> None:
        base, _ = fake_viewer
        status, _ = _post_json(
            base, "/api/model", {"chat_model": "claude-opus-4-6-thinking"}
        )
        assert status == 200

        status, data = _post_json(base, "/api/ask", {"prompt": "quick one"})
        assert status == 200
        deadline = time.time() + 30
        thread = None
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + data["thread_id"], timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        assert thread["messages"][1]["meta"]["model"] == "claude-opus-4-6-thinking"

    def test_ask_falls_back_to_global_model(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, data = _post_json(base, "/api/ask", {"prompt": "quick two"})
        assert status == 200
        deadline = time.time() + 30
        thread = None
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + data["thread_id"], timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        assert thread["messages"][1]["meta"]["model"] == "gemini-3.8-flash-high"

    def test_ask_prompt_demands_brevity(self) -> None:
        from tools.antigravity_viewer import build_ask_prompt

        prompt = build_ask_prompt("what is a list comprehension?", "", ())

        assert "Be brief" in prompt
        assert "fewest words" in prompt

    def test_ui_ships_collapsible_run_details(self) -> None:
        html = (ROOT / "tools" / "antigravity_viewer.html").read_text(
            encoding="utf-8"
        )

        assert "splitAssistantDetails" in html
        assert "SHOW RUN DETAILS" in html
        assert "'### Lifecycle'" in html


class TestTestModeHygiene:
    def test_fake_launcher_marks_output(self) -> None:
        from tools.antigravity_viewer import _fake_ask_launcher

        result = _fake_ask_launcher(["agy"], Path("."), {}, 30)

        assert result.stdout
        assert "[TEST MODE]" in result.stdout

    def test_status_reports_test_mode_on(self, fake_viewer) -> None:
        base, _ = fake_viewer

        data = json.loads(
            urllib.request.urlopen(base + "/api/status", timeout=10).read()
        )

        assert data["test_mode"] is True

    def test_status_reports_test_mode_off(self, viewer) -> None:
        base, _ = viewer

        data = json.loads(
            urllib.request.urlopen(base + "/api/status", timeout=10).read()
        )

        assert data["test_mode"] is False

    def test_fake_ask_marks_message_meta(self, fake_viewer) -> None:
        base, _ = fake_viewer

        status, data = _post_json(base, "/api/ask", {"prompt": "meta check"})
        assert status == 200
        deadline = time.time() + 30
        thread = None
        while time.time() < deadline:
            thread = json.loads(
                urllib.request.urlopen(
                    base + "/api/chat/thread/" + data["thread_id"], timeout=10
                ).read()
            )["thread"]
            if len(thread["messages"]) >= 2:
                break
            time.sleep(0.3)

        assistant = thread["messages"][1]
        assert assistant["meta"]["fake"] is True
        assert "[TEST MODE]" in assistant["content"]


class TestChatStoreIntegrity:
    def test_delete_thread(self, tmp_path: Path) -> None:
        from tools.wisp_chat import ChatStore

        store = ChatStore(tmp_path)
        thread = store.create("hello")

        assert store.delete_thread(thread["id"]) is True
        assert store.load(thread["id"]) is None
        assert store.delete_thread(thread["id"]) is False

    def test_clean_fake_threads_matches_all_markers(self, tmp_path: Path) -> None:
        from tools.wisp_chat import ChatStore

        store = ChatStore(tmp_path)
        legacy = store.create("legacy")
        store.append_message(
            legacy["id"], "assistant", "Risk: the retry path has no backoff bound"
        )
        modern = store.create("modern")
        store.append_message(modern["id"], "assistant", "[TEST MODE] Canned critique")
        flagged = store.create("flagged")
        store.append_message(
            flagged["id"], "assistant", "looks real", {"fake": True}
        )
        genuine = store.create("genuine")
        store.append_message(
            genuine["id"], "assistant", "A genuine critique with findings"
        )
        quoted = store.create("quoted")
        store.append_message(
            quoted["id"],
            "assistant",
            "Assessing the claim: 'Risk: the retry path has no backoff bound'",
        )

        # Default cleanup is authoritative on meta.fake only (audit #29):
        # genuine text that merely quotes a canned test phrase must survive.
        removed = store.clean_fake_threads()
        assert set(removed) == {flagged["id"]}
        for survivor in (legacy, modern, genuine, quoted):
            assert store.load(survivor["id"]) is not None

        # Legacy content matching is an explicit migration mode: it matches
        # every canned-marker thread (including quoted text) by design — that
        # is exactly why it is no longer the default.
        removed_legacy = store.clean_fake_threads(include_legacy_markers=True)
        assert set(removed_legacy) == {legacy["id"], modern["id"], quoted["id"]}
        assert store.load(genuine["id"]) is not None

    def test_save_leaves_no_temp_files(self, tmp_path: Path) -> None:
        from tools.wisp_chat import ChatStore

        store = ChatStore(tmp_path)
        thread = store.create("atomic")
        store.append_message(thread["id"], "user", "again")

        chat_dir = tmp_path / "chat"
        assert list(chat_dir.glob("*.tmp.*")) == []
        document = json.loads(
            (chat_dir / f"thread-{thread['id']}.json").read_text(encoding="utf-8")
        )
        assert len(document["messages"]) == 1


class TestMultiDirWatch:
    def test_newest_run_across_dirs(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import newest_run_across

        older = tmp_path / "a"
        newer = tmp_path / "b"
        _write_run(older, "run-20260101-000000-aaaaaa.jsonl", [])
        _write_run(newer, "run-20260102-000000-bbbbbb.jsonl", [])

        assert newest_run_across([older, newer]).name == "run-20260102-000000-bbbbbb.jsonl"
        assert newest_run_across([]) is None

    def test_newest_run_across_prefers_run_id_order(self, tmp_path: Path) -> None:
        from tools.antigravity_viewer import newest_run_across

        first = tmp_path / "a"
        second = tmp_path / "b"
        _write_run(first, "run-20260201-000000-aaaaaa.jsonl", [], mtime=5000)
        _write_run(second, "run-20260101-000000-bbbbbb.jsonl", [], mtime=9000)

        assert newest_run_across([first, second]).name == "run-20260201-000000-aaaaaa.jsonl"


class TestForeignWorkspaceVisibility:
    def test_runs_merge_and_foreign_replay(self, tmp_path: Path) -> None:
        registry = tmp_path / "registry.json"
        foreign_workspace = tmp_path / "foreign"
        foreign_live = foreign_workspace / ".antigravity-reports" / "live"
        foreign_live.mkdir(parents=True)
        _write_run(
            foreign_live,
            "run-20260102-000000-foreign.jsonl",
            [
                {
                    "kind": "run_start",
                    "run_id": "foreign-run",
                    "seq": 1,
                    "ts": time.time(),
                    "text": "",
                }
            ],
        )
        registry.write_text(
            json.dumps(
                [
                    {
                        "path": str(foreign_live.resolve()),
                        "workspace": str(foreign_workspace),
                        "last_seen": time.time(),
                    }
                ]
            ),
            encoding="utf-8",
        )

        with ViewerProcess(
            tmp_path / "own",
            extra_env={"ANTIGRAVITY_LIVE_REGISTRY": str(registry)},
        ) as (base, _live):
            data = json.loads(
                urllib.request.urlopen(base + "/api/runs", timeout=10).read()
            )
            records = [
                record
                for record in data["runs"]
                if record["file"] == "run-20260102-000000-foreign.jsonl"
            ]
            assert records
            assert records[0]["is_foreign"] is True
            assert records[0]["workspace"] == str(foreign_workspace)

            status = json.loads(
                urllib.request.urlopen(base + "/api/status", timeout=10).read()
            )
            assert str(foreign_live.resolve()) in status["watched_dirs"]

            response = urllib.request.urlopen(
                base + "/events?file=run-20260102-000000-foreign.jsonl", timeout=15
            )
            buffer = b""
            deadline = time.time() + 10
            while b"replay_end" not in buffer and time.time() < deadline:
                buffer += response.readline()
            response.close()

            assert b"run file not found" not in buffer
            assert b"replay_end" in buffer
