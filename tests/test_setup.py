"""Tests for tools/wisp_setup.py: probes, safety guards and registration snippets."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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

    def test_next_steps_point_at_the_shipped_skill(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (wisp_setup.DELEGATION_SKILL_DIR / "SKILL.md").is_file()
        wisp_setup.print_next_steps(Path("/opt/wisp/.venv/bin/python"), "agy")
        output = capsys.readouterr().out

        assert "Install the delegation skill for Claude Code" in output
        assert str(wisp_setup.DELEGATION_SKILL_DIR) in output
        assert "integrations.md" in output
