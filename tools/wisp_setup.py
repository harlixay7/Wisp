"""Sets up Wisp on Windows, macOS or Linux, or checks an existing setup.

Run it with any Python 3.10+ interpreter (``setup.bat`` and ``setup.sh`` find
one for you)::

    python tools/wisp_setup.py            # set up: .venv, dependencies, checks
    python tools/wisp_setup.py --check    # report only; changes nothing
    python tools/wisp_setup.py --dev      # also install pytest/ruff and run the suite

The script only uses the standard library because it runs before any
dependency is installed. It never uses elevated privileges, never installs
anything globally, never reads or touches the Antigravity sign-in, and only
writes inside the repository: ``.venv/`` and, for the desktop widget,
``tools/wisp_shell/node_modules/``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    Runner = Callable[..., subprocess.CompletedProcess[str]]

MIN_PYTHON = (3, 10)
# Electron 33 (tools/wisp_shell/package.json) needs a current Node.js LTS.
MIN_NODE_MAJOR = 18
REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".venv"
SHELL_DIR = REPO_ROOT / "tools" / "wisp_shell"
DELEGATION_SKILL_DIR = REPO_ROOT / "integrations" / "antigravity-delegation"
AGY_INSTALL_URL = "https://antigravity.google"
PYTHON_DOWNLOAD_URL = "https://www.python.org/downloads/"
NODE_DOWNLOAD_URL = "https://nodejs.org/"

# Generous ceilings: they only exist so a hung tool can never hang setup.
PROBE_TIMEOUT_SECONDS = 20
INSTALL_TIMEOUT_SECONDS = 900
TEST_TIMEOUT_SECONDS = 1200

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------
# Reporting


@dataclass
class Report:
    """Collects check results and prints them as they happen."""

    color: bool = False
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def _mark(self, label: str, code: str) -> str:
        return f"\033[{code}m{label}\033[0m" if self.color else label

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def ok(self, message: str) -> None:
        print(f"  {self._mark('[ok]', '32')} {message}")

    def info(self, message: str) -> None:
        print(f"  {self._mark('[..]', '36')} {message}")

    def warn(self, message: str, *hints: str) -> None:
        self.warnings.append(message)
        print(f"  {self._mark('[!!]', '33')} {message}")
        for hint in hints:
            print(f"       {hint}")

    def fail(self, message: str, *hints: str) -> None:
        self.failures.append(message)
        print(f"  {self._mark('[xx]', '31')} {message}")
        for hint in hints:
            print(f"       {hint}")


def supports_color(stream: object = sys.stdout) -> bool:
    if os.environ.get("NO_COLOR") or not getattr(stream, "isatty", lambda: False)():
        return False
    if not IS_WINDOWS:
        return True
    # Classic conhost only renders ANSI escapes when VT mode is on; Windows
    # Terminal and VS Code always do.
    return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"))


# --------------------------------------------------------------------------
# Probes (pure where possible so they can be tested without side effects)


def python_is_supported(version: Sequence[int] = sys.version_info) -> bool:
    return tuple(version[:2]) >= MIN_PYTHON


def venv_python(venv_dir: Path = VENV_DIR, windows: bool = IS_WINDOWS) -> Path:
    if windows:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def is_wisp_venv(venv_dir: Path) -> bool:
    """True only for a directory that is unmistakably a virtual environment."""
    return venv_dir.is_dir() and (venv_dir / "pyvenv.cfg").is_file()


def find_agy(
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
    windows: bool = IS_WINDOWS,
) -> tuple[str | None, bool]:
    """Locates ``agy`` the same way the bridge does.

    Returns ``(path, on_path)``. ``on_path`` is False when the CLI was only
    found in ``~/.gemini/bin``, where the bridge still finds it but a
    terminal does not.
    """
    found = which("agy")
    if found:
        return found, True
    base = (home if home is not None else Path.home()) / ".gemini" / "bin"
    for name in ("agy.exe", "agy") if windows else ("agy", "agy.exe"):
        candidate = base / name
        if candidate.is_file():
            return str(candidate), False
    return None, False


def electron_binary(shell_dir: Path = SHELL_DIR, system: str = sys.platform) -> Path:
    """Where ``npm ci`` puts the Electron binary; mirrors viewer_shell.electron_executable."""
    dist = shell_dir / "node_modules" / "electron" / "dist"
    if system.startswith("win"):
        return dist / "electron.exe"
    if system == "darwin":
        return dist / "Electron.app" / "Contents" / "MacOS" / "Electron"
    return dist / "electron"


def parse_node_major(version_text: str) -> int | None:
    """``"v20.11.1"`` -> 20; anything unparseable -> None."""
    text = version_text.strip().lstrip("v")
    head = text.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def run(
    command: Sequence[str],
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    cwd: Path = REPO_ROOT,
    capture: bool = True,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str] | None:
    """Runs a command without a shell; None when it cannot be started or times out."""
    try:
        return runner(
            list(command),
            cwd=str(cwd),
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def tail(text: str | None, lines: int = 15) -> str:
    return "\n".join((text or "").rstrip().splitlines()[-lines:])


# --------------------------------------------------------------------------
# Registration snippets


def quote_arg(value: str, windows: bool = IS_WINDOWS) -> str:
    if windows:
        return f'"{value}"' if any(ch in value for ch in ' \t&()^%!;,') else value
    return shlex.quote(value)


def mcp_snippets(python: Path, server: Path, windows: bool = IS_WINDOWS) -> dict[str, str]:
    """Ready-to-paste MCP registrations that use this machine's absolute paths."""
    claude = " ".join(
        [
            "claude mcp add --scope user -e ANTIGRAVITY_HARNESS=claude-code antigravity --",
            quote_arg(str(python), windows),
            quote_arg(str(server), windows),
        ]
    )
    codex = "\n".join(
        [
            "[mcp_servers.antigravity]",
            f"command = {json.dumps(str(python))}",
            f"args = [{json.dumps(str(server))}]",
            "tool_timeout_sec = 3600",
            'env = { ANTIGRAVITY_HARNESS = "codex" }',
        ]
    )
    generic = json.dumps(
        {
            "mcpServers": {
                "antigravity": {
                    "command": str(python),
                    "args": [str(server)],
                    "env": {"ANTIGRAVITY_HARNESS": "mcp-client"},
                }
            }
        },
        indent=2,
    )
    return {"claude": claude, "codex": codex, "json": generic}


def skill_install_commands(source: Path, windows: bool = IS_WINDOWS) -> list[str]:
    """Commands that copy the delegation skill into Claude Code's user skills folder.

    Windows gets PowerShell. It copies the folder's contents into a target it
    creates first, because ``Copy-Item -Recurse`` of a folder onto an existing
    folder nests a second copy inside it instead of updating it.
    """
    if windows:
        contents = "'" + f"{source}\\*".replace("'", "''") + "'"
        target = '"$HOME\\.claude\\skills\\antigravity-delegation"'
        return [
            f"New-Item -ItemType Directory -Force {target} | Out-Null",
            f"Copy-Item -Recurse -Force {contents} {target}",
        ]
    return [
        "mkdir -p ~/.claude/skills",
        f"cp -R {quote_arg(str(source), windows=False)} ~/.claude/skills/",
    ]


# --------------------------------------------------------------------------
# Steps


def check_python(report: Report) -> bool:
    report.section("Python")
    version = platform.python_version()
    if not python_is_supported():
        report.fail(
            f"Python {version} is too old; Wisp needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.",
            f"Install a current Python from {PYTHON_DOWNLOAD_URL} and run setup again.",
        )
        return False
    report.ok(f"Python {version} ({sys.executable})")
    return True


def ensure_venv(report: Report, check_only: bool, recreate: bool) -> Path | None:
    report.section("Virtual environment")
    python = venv_python(VENV_DIR)
    if recreate and VENV_DIR.exists() and not check_only:
        if not is_wisp_venv(VENV_DIR):
            report.fail(
                f"{VENV_DIR} exists but has no pyvenv.cfg, so it was left untouched.",
                "Move or delete it yourself if it is safe to do so, then run setup again.",
            )
            return None
        report.info(f"Removing {VENV_DIR} ...")
        shutil.rmtree(VENV_DIR)

    if python.is_file():
        probe = run([str(python), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
        found = first_line(probe.stdout) if probe and probe.returncode == 0 else ""
        parts = tuple(int(p) for p in found.split(".") if p.isdigit())
        if len(parts) == 2 and python_is_supported(parts):
            report.ok(f".venv uses Python {found}")
            return python
        report.fail(
            ".venv exists but its Python does not start or is older than 3.10.",
            "Run setup again with --recreate-venv to rebuild it.",
        )
        return None

    if check_only:
        report.warn(".venv has not been created yet.", "Run setup without --check to create it.")
        return None
    if VENV_DIR.exists() and not is_wisp_venv(VENV_DIR):
        report.fail(
            f"{VENV_DIR} exists but is not a virtual environment, so it was left untouched.",
            "Move it out of the way and run setup again.",
        )
        return None

    report.info("Creating .venv ...")
    created = run(
        [sys.executable, "-m", "venv", str(VENV_DIR)], timeout=INSTALL_TIMEOUT_SECONDS
    )
    if created is None or created.returncode != 0 or not python.is_file():
        hints = ["Python could not create a virtual environment."]
        detail = tail(created.stderr if created else "")
        if detail:
            hints.append(detail)
        if sys.platform.startswith("linux"):
            hints.append(
                "On Debian or Ubuntu, install the venv module first: "
                "sudo apt install python3-venv"
            )
        report.fail("Could not create .venv.", *hints)
        return None
    report.ok("Created .venv")
    return python


def install_requirements(report: Report, python: Path, dev: bool, check_only: bool) -> bool:
    report.section("Python dependencies")
    requirements = REPO_ROOT / ("requirements-dev.txt" if dev else "requirements.txt")
    if not check_only:
        report.info(f"Installing {requirements.name} into .venv ...")
        installed = run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--quiet",
                "-r",
                str(requirements),
            ],
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
        if installed is None or installed.returncode != 0:
            report.fail(
                "pip could not install the dependencies.",
                tail(installed.stderr if installed else "pip did not start or timed out."),
                "If you are behind a proxy, set HTTPS_PROXY and run setup again.",
            )
            return False
    probe = run([str(python), "-c", "import yaml; print(yaml.__version__)"])
    if probe is None or probe.returncode != 0:
        report.fail("PyYAML is not installed in .venv.", "Run setup without --check.")
        return False
    report.ok(f"PyYAML {first_line(probe.stdout)}")
    if dev:
        tools_probe = run([str(python), "-m", "pytest", "--version"])
        if tools_probe is None or tools_probe.returncode != 0:
            report.fail("pytest is not installed in .venv.", "Run setup with --dev.")
            return False
        report.ok(first_line(tools_probe.stdout) or "pytest installed")
    return True


def validate_skills(report: Report, python: Path) -> bool:
    report.section("Review playbooks")
    result = run([str(python), "-m", "tools.skill_loader", "--validate"])
    if result is None or result.returncode != 0:
        report.fail(
            "The playbooks in Skills/ did not validate.",
            tail((result.stdout + result.stderr) if result else "The validator did not start."),
        )
        return False
    report.ok(first_line(result.stdout).removeprefix("OK: ") or "Playbooks valid")
    return True


def check_agy(report: Report) -> str | None:
    report.section("Antigravity CLI (agy)")
    path, on_path = find_agy()
    if path is None:
        report.warn(
            "agy was not found, so reviews cannot run yet.",
            f"Install the Google Antigravity CLI from {AGY_INSTALL_URL}, run `agy` once",
            "to sign in with your Google account, then run this check again.",
        )
        return None
    version = run([path, "--version"])
    label = first_line(version.stdout or version.stderr) if version else ""
    report.ok(f"agy {label or 'found'} ({path})")
    if not on_path:
        report.warn(
            "agy is not on your PATH. Wisp still finds it, but your terminal will not.",
            "Run `agy install` once to add it to your PATH.",
        )
    report.info(
        "Open `agy` once inside each project you review and accept its "
        "workspace-trust prompt."
    )
    return path


def setup_widget(report: Report, mode: str, check_only: bool) -> None:
    report.section("Desktop widget (optional)")
    if electron_binary().is_file():
        report.ok("Electron shell installed")
        return
    if mode == "skip":
        report.info("Skipped. The widget opens in a browser window instead.")
        return
    if mode == "auto" and not IS_WINDOWS:
        report.info(
            "Skipped on this system: the transparent overlay is built for Windows. "
            "The widget opens in a browser window; pass --widget to install it anyway."
        )
        return
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        report.warn(
            "Node.js was not found, so the widget will open in a browser window.",
            f"Install Node.js {MIN_NODE_MAJOR} or newer from {NODE_DOWNLOAD_URL} "
            "for the desktop overlay.",
        )
        return
    node_version = run([node, "--version"])
    major = parse_node_major(node_version.stdout if node_version else "")
    if major is None or major < MIN_NODE_MAJOR:
        report.warn(
            f"Node.js {MIN_NODE_MAJOR} or newer is needed for the desktop overlay.",
            f"Update Node.js from {NODE_DOWNLOAD_URL}; the browser window works meanwhile.",
        )
        return
    if check_only:
        report.warn("The Electron shell is not installed.", "Run setup without --check.")
        return
    report.info("Installing the Electron shell (one time, about 100 MB) ...")
    # npm ci installs exactly what package-lock.json pins.
    installed = run(
        [npm, "ci", "--no-audit", "--no-fund"],
        cwd=SHELL_DIR,
        timeout=INSTALL_TIMEOUT_SECONDS,
    )
    if installed is None or installed.returncode != 0:
        report.warn(
            "npm could not install the Electron shell; the widget will use a browser window.",
            tail(installed.stderr if installed else "npm did not start or timed out."),
        )
        return
    if not electron_binary().is_file():
        report.warn(
            "npm finished but the Electron binary is missing; the widget will use a "
            "browser window.",
            "Electron downloads its binary after install; check your network or proxy "
            "and run setup again.",
        )
        return
    report.ok("Electron shell installed")


def check_platform(report: Report) -> None:
    report.section("This system")
    report.ok(f"{platform.system()} {platform.release()} ({platform.machine()})")
    if not IS_WINDOWS:
        report.info(
            "The screen-capture hotkeys (Ctrl+Alt+Q / Ctrl+Alt+E) are Windows-only. "
            "Reviews, the MCP server and the widget work here."
        )


def check_engine(report: Report, python: Path) -> None:
    report.section("Engine")
    result = run([str(python), str(REPO_ROOT / "tools" / "antigravity_bridge.py"), "--status"])
    if result is None or result.returncode != 0:
        report.fail(
            "The bridge status check failed.",
            tail((result.stdout + result.stderr) if result else "The bridge did not start."),
        )
        return
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        report.fail("The bridge status output was not valid JSON.", tail(result.stdout))
        return
    report.ok(
        f"Wisp {status.get('wisp_version', '?')} with {len(status.get('skills', []))} "
        "playbooks"
    )
    for warning in status.get("warnings", []):
        # A missing agy is already explained in the Antigravity CLI section.
        if status.get("executable_found") is False and "was not found" in str(warning):
            continue
        report.warn(str(warning))


def run_test_suite(report: Report, python: Path) -> None:
    report.section("Test suite")
    report.info("Running the offline test suite (about a minute) ...")
    result = run(
        [str(python), "-m", "pytest", "tests", "-q"],
        timeout=TEST_TIMEOUT_SECONDS,
        capture=False,
    )
    if result is None or result.returncode != 0:
        report.fail("Some tests failed; see the output above.")
        return
    report.ok("All tests passed")


def print_next_steps(python: Path, agy: str | None) -> None:
    server = REPO_ROOT / "tools" / "antigravity_mcp_server.py"
    viewer = REPO_ROOT / "tools" / "antigravity_viewer.py"
    bridge = REPO_ROOT / "tools" / "antigravity_bridge.py"
    snippets = mcp_snippets(python, server)
    py = quote_arg(str(python))
    print("\nNext steps")
    if agy is None:
        print(f"  1. Install the Antigravity CLI ({AGY_INSTALL_URL}) and sign in by running `agy`.")
    else:
        print("  1. The Antigravity CLI is ready.")
    if IS_WINDOWS:
        print(f"  2. Open the widget:   {REPO_ROOT / 'tools' / 'antigravity_viewer.cmd'}")
    else:
        print(f"  2. Open the widget:   {py} {quote_arg(str(viewer))}")
    print(
        f"  3. Try a review:      {py} {quote_arg(str(bridge))} --prompt \"Review my plan\" "
        "--dry-run"
    )
    print("  4. Connect your coding assistant (then restart it):")
    print("\n     Claude Code:")
    print(f"       {snippets['claude']}")
    print("\n     Codex (~/.codex/config.toml):")
    for line in snippets["codex"].splitlines():
        print(f"       {line}")
    print("\n     Cline, Cursor, Roo Code and other MCP clients (mcpServers JSON):")
    for line in snippets["json"].splitlines():
        print(f"       {line}")
    shell = "PowerShell" if IS_WINDOWS else "a terminal"
    print(f"\n  5. Install the delegation skill for Claude Code (in {shell}):")
    for line in skill_install_commands(DELEGATION_SKILL_DIR):
        print(f"       {line}")
    print(f"     Other assistants: see {REPO_ROOT / 'docs' / 'integrations.md'}")
    print("\n  Run this script with --check at any time to re-check your setup.")


# --------------------------------------------------------------------------
# Entry point


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wisp_setup",
        description="Set up Wisp, or check an existing setup with --check.",
    )
    parser.add_argument(
        "--check", action="store_true", help="report on the setup without changing anything"
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="also install the test and lint tools and run the test suite",
    )
    widget = parser.add_mutually_exclusive_group()
    widget.add_argument(
        "--widget",
        dest="widget",
        action="store_const",
        const="install",
        default="auto",
        help="install the Electron desktop overlay on any system (default: Windows only)",
    )
    widget.add_argument(
        "--no-widget",
        dest="widget",
        action="store_const",
        const="skip",
        help="never install the Electron desktop overlay",
    )
    parser.add_argument(
        "--recreate-venv", action="store_true", help="delete and rebuild .venv first"
    )
    # Consumed by setup.bat (keep the window open); accepted here so it can be
    # forwarded unchanged.
    parser.add_argument("--no-pause", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = Report(color=supports_color())
    print("Wisp setup" + (" check (no changes will be made)" if args.check else ""))
    print(f"Repository: {REPO_ROOT}")

    if not check_python(report):
        return 1
    check_platform(report)
    python = ensure_venv(report, args.check, args.recreate_venv)
    ready = python is not None and install_requirements(report, python, args.dev, args.check)
    if ready and python is not None:
        ready = validate_skills(report, python)
    agy = check_agy(report)
    if ready and python is not None:
        check_engine(report, python)
    setup_widget(report, args.widget, args.check)
    if args.dev and ready and python is not None and not report.failures:
        run_test_suite(report, python)

    print()
    if report.failures:
        print(f"Setup is not complete: {len(report.failures)} problem(s) above need attention.")
        return 1
    if python is None:
        print("Nothing is broken, but setup has not been run yet.")
        return 0
    if report.warnings:
        print(f"Setup finished with {len(report.warnings)} note(s) above.")
    else:
        print("Setup finished. Everything looks good.")
    print_next_steps(python, agy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
