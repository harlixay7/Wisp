"""Tests for tools/wisp_setup.py: probes, safety guards and registration snippets."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from tools import wisp_setup


class TestProbes:
    def test_python_version_gate(self) -> None:
        assert wisp_setup.python_is_supported((3, 10, 0))
        assert wisp_setup.python_is_supported((3, 13))
        assert not wisp_setup.python_is_supported((3, 9, 18))

    def test_venv_python_location_per_os(self, tmp_path: Path) -> None:
        assert wisp_setup.venv_python(tmp_path, windows=True) == (
            tmp_path / "Scripts" / "python.exe"
        )
        assert wisp_setup.venv_python(tmp_path, windows=False) == tmp_path / "bin" / "python"

    def test_node_major_parsing(self) -> None:
        assert wisp_setup.parse_node_major("v20.11.1\n") == 20
        assert wisp_setup.parse_node_major("18.0.0") == 18
        assert wisp_setup.parse_node_major("") is None
        assert wisp_setup.parse_node_major("not a version") is None

    def test_electron_binary_matches_each_platform(self, tmp_path: Path) -> None:
        dist = tmp_path / "node_modules" / "electron" / "dist"
        assert wisp_setup.electron_binary(tmp_path, "win32") == dist / "electron.exe"
        assert wisp_setup.electron_binary(tmp_path, "linux") == dist / "electron"
        assert wisp_setup.electron_binary(tmp_path, "darwin") == (
            dist / "Electron.app" / "Contents" / "MacOS" / "Electron"
        )

    def test_run_reports_unstartable_and_hung_commands_as_none(self, tmp_path: Path) -> None:
        assert wisp_setup.run(["wisp-no-such-command-xyz"], cwd=tmp_path) is None

        def hung(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)

        assert wisp_setup.run(["anything"], cwd=tmp_path, runner=hung) is None

    def test_no_color_is_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Tty:
            @staticmethod
            def isatty() -> bool:
                return True

        monkeypatch.setenv("NO_COLOR", "1")
        assert not wisp_setup.supports_color(Tty())


class TestFindAgy:
    def test_prefers_path(self, tmp_path: Path) -> None:
        found = wisp_setup.find_agy(which=lambda _: "/usr/local/bin/agy", home=tmp_path)
        assert found == ("/usr/local/bin/agy", True)

    def test_falls_back_to_gemini_bin_off_path(self, tmp_path: Path) -> None:
        binary = tmp_path / ".gemini" / "bin" / "agy"
        binary.parent.mkdir(parents=True)
        binary.write_text("", encoding="utf-8")

        found = wisp_setup.find_agy(which=lambda _: None, home=tmp_path, windows=False)

        assert found == (str(binary), False)

    def test_missing(self, tmp_path: Path) -> None:
        assert wisp_setup.find_agy(which=lambda _: None, home=tmp_path) == (None, False)


class TestVenvSafety:
    def test_recreate_never_deletes_a_directory_that_is_not_a_venv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / ".venv"
        target.mkdir()
        (target / "precious.txt").write_text("keep me", encoding="utf-8")
        monkeypatch.setattr(wisp_setup, "VENV_DIR", target)
        report = wisp_setup.Report()

        result = wisp_setup.ensure_venv(report, check_only=False, recreate=True)

        assert result is None
        assert (target / "precious.txt").read_text(encoding="utf-8") == "keep me"
        assert any("left untouched" in failure for failure in report.failures)

    def test_existing_non_venv_directory_is_not_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / ".venv"
        target.mkdir()
        monkeypatch.setattr(wisp_setup, "VENV_DIR", target)
        report = wisp_setup.Report()

        assert wisp_setup.ensure_venv(report, check_only=False, recreate=False) is None
        assert list(target.iterdir()) == []
        assert report.failures

    def test_check_mode_creates_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / ".venv"
        monkeypatch.setattr(wisp_setup, "VENV_DIR", target)

        exit_code = wisp_setup.main(["--check", "--no-widget"])

        assert exit_code == 0
        assert not target.exists()
        output = capsys.readouterr().out
        assert "no changes will be made" in output
        assert "Nothing is broken, but setup has not been run yet." in output


class TestRegistrationSnippets:
    PYTHON = Path("/home/dev/my projects/Wisp/.venv/bin/python")
    SERVER = Path("/home/dev/my projects/Wisp/tools/antigravity_mcp_server.py")

    def test_claude_command_quotes_paths_with_spaces(self) -> None:
        posix = wisp_setup.mcp_snippets(self.PYTHON, self.SERVER, windows=False)["claude"]
        windows = wisp_setup.mcp_snippets(
            Path(r"C:\Users\Dev\My Code\Wisp\.venv\Scripts\python.exe"),
            Path(r"C:\Users\Dev\My Code\Wisp\tools\antigravity_mcp_server.py"),
            windows=True,
        )["claude"]

        assert posix.startswith("claude mcp add --scope user ")
        assert f"-- '{self.PYTHON}' '{self.SERVER}'" in posix
        assert r'"C:\Users\Dev\My Code\Wisp\.venv\Scripts\python.exe"' in windows

    def test_json_snippet_is_valid_and_uses_absolute_paths(self) -> None:
        parsed = json.loads(wisp_setup.mcp_snippets(self.PYTHON, self.SERVER)["json"])
        server = parsed["mcpServers"]["antigravity"]

        assert server["command"] == str(self.PYTHON)
        assert server["args"] == [str(self.SERVER)]

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is Python 3.11+")
    def test_codex_snippet_is_valid_toml_even_with_backslashes(self) -> None:
        import tomllib

        python = Path(r"C:\Users\Dev\Wisp\.venv\Scripts\python.exe")
        snippet = wisp_setup.mcp_snippets(python, self.SERVER)["codex"]
        server = tomllib.loads(snippet)["mcp_servers"]["antigravity"]

        assert server["command"] == str(python)
        assert server["tool_timeout_sec"] == 3600


class TestSkillInstallCommands:
    def test_posix_copies_the_skill_folder_into_claude_skills(self) -> None:
        source = Path("/home/dev/my projects/Wisp/integrations/antigravity-delegation")
        commands = wisp_setup.skill_install_commands(source, windows=False)

        assert commands[0] == "mkdir -p ~/.claude/skills"
        assert commands[1] == f"cp -R '{source}' ~/.claude/skills/"

    def test_windows_uses_powershell_and_escapes_single_quotes(self) -> None:
        source = Path(r"C:\Users\O'Neil\Wisp\integrations\antigravity-delegation")
        commands = wisp_setup.skill_install_commands(source, windows=True)
        target = '"$HOME\\.claude\\skills\\antigravity-delegation"'

        assert commands[0] == f"New-Item -ItemType Directory -Force {target} | Out-Null"
        assert commands[1].startswith("Copy-Item -Recurse -Force '")
        assert "O''Neil" in commands[1]
        assert commands[1].endswith(f"\\*' {target}")

    def test_connect_summary_points_at_the_shipped_skill(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (wisp_setup.DELEGATION_SKILL_DIR / "SKILL.md").is_file()
        report = wisp_setup.Report()
        wisp_setup.print_summary(report, Path("/opt/wisp/.venv/bin/python"), connect=True)
        output = capsys.readouterr().out

        assert "install the delegation skill" in output
        assert str(wisp_setup.DELEGATION_SKILL_DIR) in output
        assert "integrations.md" in output

    def test_plain_setup_summary_leaves_assistants_alone(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wisp_setup.print_summary(wisp_setup.Report(), Path("/opt/wisp/.venv/bin/python"))
        output = capsys.readouterr().out

        assert "--connect" in output
        assert "claude mcp add" not in output
        assert "mcp_servers" not in output


class TestClaudeCommand:
    def test_server_name_comes_before_the_variadic_env_flag(self) -> None:
        # `claude mcp add -e KEY=v NAME ...` makes -e swallow NAME as a second
        # variable ("Invalid environment variable format"); the name goes first.
        args = wisp_setup.claude_add_args(PurePosixPath("/p/python"), PurePosixPath("/p/server.py"))

        assert args.index("antigravity") < args.index("-e")
        assert args[args.index("--") + 1 :] == ["/p/python", "/p/server.py"]

    def test_printed_command_matches_the_executed_one(self) -> None:
        snippet = wisp_setup.mcp_snippets(
            PurePosixPath("/p/python"), PurePosixPath("/p/s.py"), windows=False
        )

        assert snippet["claude"] == (
            "claude mcp add --scope user antigravity -e ANTIGRAVITY_HARNESS=claude-code "
            "-- /p/python /p/s.py"
        )


class TestQuestions:
    @staticmethod
    def _report(mode: str, replies: list[str]) -> wisp_setup.Report:
        answers = iter(replies)

        def reader(_prompt: str) -> str:
            try:
                return next(answers)
            except StopIteration:
                raise EOFError from None

        return wisp_setup.Report(mode=mode, reader=reader)

    def test_ask_mode_reads_the_answer_and_honours_the_default(self) -> None:
        report = self._report("ask", ["", "n", "yes", "Y"])

        assert report.ask("first?") is True
        assert report.ask("second?") is False
        assert report.ask("third?", default=False) is True
        assert report.ask("fourth?", default=False) is True
        assert report.ask("closed stdin?") is True  # EOF falls back to the default
        assert report.ask("closed stdin?", default=False) is False

    def test_yes_and_never_modes_do_not_prompt(self) -> None:
        def no_prompt(_prompt: str) -> str:
            raise AssertionError("must not prompt")

        assert wisp_setup.Report(mode="yes", reader=no_prompt).ask("install?") is True
        assert wisp_setup.Report(mode="never", reader=no_prompt).ask("install?") is False
        assert wisp_setup.Report(mode="never", reader=no_prompt).wait("press enter") == "s"

    def test_answer_mode_never_asks_without_a_terminal_or_in_check(self) -> None:
        args = wisp_setup.parse_args([])
        assert wisp_setup.answer_mode(args, interactive=True) == "ask"
        assert wisp_setup.answer_mode(args, interactive=False) == "never"
        assert wisp_setup.answer_mode(wisp_setup.parse_args(["--yes"]), False) == "yes"
        assert wisp_setup.answer_mode(wisp_setup.parse_args(["--check", "--yes"]), True) == "never"
        assert wisp_setup.answer_mode(wisp_setup.parse_args(["--no-input"]), True) == "never"


class TestConnect:
    PYTHON = Path("/opt/wisp/.venv/bin/python")

    def test_codex_entry_is_appended_with_a_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wisp_setup.Path, "home", classmethod(lambda cls: tmp_path))
        config = tmp_path / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('model = "x"\n', encoding="utf-8")
        report = wisp_setup.Report(mode="yes")

        wisp_setup.connect_codex(report, self.PYTHON, check_only=False)

        text = config.read_text(encoding="utf-8")
        assert text.startswith('model = "x"\n')
        assert "[mcp_servers.antigravity]" in text
        assert (config.parent / "config.toml.wisp-backup").read_text(encoding="utf-8") == (
            'model = "x"\n'
        )
        assert report.done == ["Codex"]

    def test_codex_is_left_alone_when_declined_or_already_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wisp_setup.Path, "home", classmethod(lambda cls: tmp_path))
        config = tmp_path / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text("", encoding="utf-8")

        wisp_setup.connect_codex(wisp_setup.Report(mode="never"), self.PYTHON, False)
        assert config.read_text(encoding="utf-8") == ""

        existing = wisp_setup.codex_block(self.PYTHON, wisp_setup.MCP_SERVER) + "\n"
        config.write_text(existing, encoding="utf-8")
        wisp_setup.connect_codex(wisp_setup.Report(mode="yes"), self.PYTHON, False)
        assert config.read_text(encoding="utf-8") == existing

    def test_claude_registration_and_skill_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wisp_setup.Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(wisp_setup, "find_tool", lambda name, extra=(): "/bin/claude")
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(list(command))
            code = 1 if command[1:3] == ["mcp", "get"] else 0
            return subprocess.CompletedProcess(command, code, "", "")

        monkeypatch.setattr(wisp_setup, "run", fake_run)
        report = wisp_setup.Report(mode="yes")

        wisp_setup.connect_claude(report, self.PYTHON, check_only=False)

        added = [c for c in calls if c[1:3] == ["mcp", "add"]]
        assert added and added[0][1:] == wisp_setup.claude_add_args(
            self.PYTHON, wisp_setup.MCP_SERVER
        )
        skill = tmp_path / ".claude" / "skills" / "antigravity-delegation" / "SKILL.md"
        assert skill.read_bytes() == (wisp_setup.DELEGATION_SKILL_DIR / "SKILL.md").read_bytes()
        assert report.done == ["Claude Code"]

    def test_plain_setup_never_connects_assistants(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wisp_setup, "LOG_PATH", tmp_path / "setup.log")
        for name in ("install_requirements", "validate_skills", "self_test"):
            monkeypatch.setattr(wisp_setup, name, lambda *a, **k: True)
        monkeypatch.setattr(wisp_setup, "ensure_venv", lambda *a, **k: self.PYTHON)
        monkeypatch.setattr(wisp_setup, "check_agy", lambda *a, **k: None)
        monkeypatch.setattr(wisp_setup, "setup_widget", lambda *a, **k: None)
        connected: list[bool] = []
        monkeypatch.setattr(
            wisp_setup, "connect_assistants", lambda *a, **k: connected.append(True)
        )

        assert wisp_setup.main(["--no-input"]) == 0
        assert connected == []
        assert wisp_setup.main(["--no-input", "--connect"]) == 0
        assert connected == [True]


class TestFailureReport:
    def test_problems_are_summarised_with_the_log_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = wisp_setup.SetupLog(tmp_path / "setup.log")
        monkeypatch.setattr(wisp_setup, "_LOG", log)
        report = wisp_setup.Report()
        report.fail("pip could not install the dependencies.", "ERROR: no network", "Try again.")
        wisp_setup.run(["wisp-no-such-command-xyz"], cwd=tmp_path)

        wisp_setup.print_summary(report, Path("/opt/python"))
        log.close()
        output = capsys.readouterr().out

        assert "Setup could not finish" in output
        assert "Problem  pip could not install the dependencies." in output
        assert "ERROR: no network" in output
        assert str(tmp_path / "setup.log") in output
        text = (tmp_path / "setup.log").read_text(encoding="utf-8")
        assert "[xx] pip could not install the dependencies." in text
        assert "$ wisp-no-such-command-xyz" in text
        assert "could not run:" in text
