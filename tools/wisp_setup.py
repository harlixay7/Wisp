"""Sets up Wisp on Windows, macOS or Linux, or checks an existing setup.

Run it with any Python 3.10+ interpreter (``setup.bat`` and ``setup.sh`` find
one for you, and offer to install it when there is none)::

    python tools/wisp_setup.py            # install and verify Wisp
    python tools/wisp_setup.py --connect  # connect Claude Code, Codex, ... (optional)
    python tools/wisp_setup.py --check    # report only; changes nothing
    python tools/wisp_setup.py --dev      # also install pytest/ruff and run the suite

Setup creates ``.venv``, installs the pinned dependencies, checks the
playbooks and runs a self-test that starts the engine and the MCP server.
When something is missing it offers to fix it: open the Antigravity CLI
download page and wait for the install, or install Node.js for the desktop
overlay. It never touches a coding assistant; ``--connect`` does that, one
assistant at a time and only after a yes. Every offer is a question, and
``--yes`` accepts them all. Without a terminal (CI, piped input) or with
``--no-input`` it asks nothing and prints the remaining steps instead.

Every command it runs is recorded in ``.wisp-setup.log`` at the repository
root, so a failure can be diagnosed from one file. The script only uses the
standard library because it runs before any dependency is installed, and it
never reads or touches the Antigravity sign-in.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    Runner = Callable[..., subprocess.CompletedProcess[str]]

MIN_PYTHON = (3, 10)
# Electron 33 (tools/wisp_shell/package.json) needs a current Node.js LTS.
MIN_NODE_MAJOR = 18
REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".venv"
SHELL_DIR = REPO_ROOT / "tools" / "wisp_shell"
DELEGATION_SKILL_DIR = REPO_ROOT / "integrations" / "antigravity-delegation"
MCP_SERVER = REPO_ROOT / "tools" / "antigravity_mcp_server.py"
LOG_PATH = REPO_ROOT / ".wisp-setup.log"
MCP_NAME = "antigravity"
AGY_INSTALL_URL = "https://antigravity.google"
PYTHON_DOWNLOAD_URL = "https://www.python.org/downloads/"
NODE_DOWNLOAD_URL = "https://nodejs.org/"
NODE_WINGET_ID = "OpenJS.NodeJS.LTS"
ISSUES_URL = "https://github.com/harlixay7/Wisp/issues/new/choose"

# Generous ceilings: they only exist so a hung tool can never hang setup.
PROBE_TIMEOUT_SECONDS = 20
INSTALL_TIMEOUT_SECONDS = 900
TEST_TIMEOUT_SECONDS = 1200

IS_WINDOWS = os.name == "nt"
STEP_COUNT = 7


# --------------------------------------------------------------------------
# Log file


class SetupLog:
    """Writes every step, question and command (with full output) to one file."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._file: TextIO | None = None
        if path is not None:
            try:
                self._file = path.open("w", encoding="utf-8")
            except OSError:
                self.path = None
        self.write(f"Wisp setup log, {datetime.datetime.now().isoformat(timespec='seconds')}")
        self.write(f"System: {platform.platform()} ({platform.machine()})")
        self.write(f"Python: {sys.version.split()[0]} at {sys.executable}")
        self.write(f"Repository: {REPO_ROOT}")
        self.write(f"Arguments: {' '.join(sys.argv[1:]) or '(none)'}")

    def write(self, text: str) -> None:
        if self._file is not None:
            self._file.write(text.rstrip("\n") + "\n")
            self._file.flush()

    def command(
        self,
        command: Sequence[str],
        cwd: Path,
        result: subprocess.CompletedProcess[str] | None,
        error: str = "",
    ) -> None:
        self.write(f"\n$ {' '.join(command)}\n  (in {cwd})")
        if result is None:
            self.write(f"  could not run: {error or 'unknown error'}")
            return
        self.write(f"  exit code {result.returncode}")
        for name, text in (("stdout", result.stdout), ("stderr", result.stderr)):
            if text and text.strip():
                self.write(f"  --- {name} ---")
                self.write(text)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


_LOG = SetupLog(None)


# --------------------------------------------------------------------------
# Console output and questions


@dataclass
class Problem:
    """A failure, with what went wrong and what to try next."""

    message: str
    hints: tuple[str, ...] = ()


@dataclass
class Report:
    """Prints check results as they happen and remembers what needs attention.

    ``mode`` decides how questions are answered: ``ask`` prompts on the
    terminal, ``yes`` accepts every offer, ``never`` declines every offer.
    """

    color: bool = False
    mode: str = "never"
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    reader: Callable[[str], str] = input

    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def section(self, title: str, number: int | None = None) -> None:
        label = f"[{number}/{STEP_COUNT}] {title}" if number else title
        print(f"\n{self._paint(label, '1')}")
        _LOG.write(f"\n=== {label} ===")

    def ok(self, message: str) -> None:
        print(f"  {self._paint('[ok]', '32')} {message}")
        _LOG.write(f"[ok] {message}")

    def info(self, message: str) -> None:
        print(f"  {self._paint('[..]', '36')} {message}")
        _LOG.write(f"[..] {message}")

    def warn(self, message: str, *hints: str) -> None:
        self.warnings.append(message)
        print(f"  {self._paint('[!!]', '33')} {message}")
        for hint in hints:
            print(f"       {hint}")
        _LOG.write(f"[!!] {message}" + "".join(f"\n     {h}" for h in hints))

    def fail(self, message: str, *hints: str) -> None:
        self.failures.append(message)
        self.problems.append(Problem(message, tuple(h for h in hints if h)))
        print(f"  {self._paint('[xx]', '31')} {message}")
        for hint in hints:
            if hint:
                for line in hint.splitlines():
                    print(f"       {line}")
        _LOG.write(f"[xx] {message}" + "".join(f"\n     {h}" for h in hints if h))

    def ask(self, question: str, default: bool = True) -> bool:
        """Asks a yes/no question; the answer is logged."""
        if self.mode == "yes":
            answer = True
        elif self.mode == "never":
            answer = False
        else:
            suffix = "[Y/n]" if default else "[y/N]"
            try:
                reply = self.reader(f"  {self._paint('[??]', '35')} {question} {suffix} ")
            except EOFError:
                reply = ""
            reply = reply.strip().lower()
            answer = default if not reply else reply in ("y", "yes")
        _LOG.write(f"[??] {question} -> {'yes' if answer else 'no'}")
        return answer

    def wait(self, message: str) -> str:
        """Waits for Enter; returns what was typed (lowercased)."""
        if self.mode != "ask":
            return "s"
        try:
            reply = self.reader(f"  {self._paint('[..]', '36')} {message} ")
        except EOFError:
            reply = "s"
        _LOG.write(f"[..] {message} -> {reply.strip() or '(enter)'}")
        return reply.strip().lower()


def supports_color(stream: object = sys.stdout) -> bool:
    if os.environ.get("NO_COLOR") or not getattr(stream, "isatty", lambda: False)():
        return False
    if not IS_WINDOWS:
        return True
    return _enable_windows_vt_mode()


def _enable_windows_vt_mode() -> bool:
    """Turns on ANSI colour handling in the classic Windows console."""
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


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


def find_tool(name: str, extra_dirs: Sequence[Path] = ()) -> str | None:
    """``shutil.which`` plus well-known install folders, for tools installed during this run."""
    found = shutil.which(name)
    if found:
        return found
    suffixes = (".cmd", ".exe", "") if IS_WINDOWS else ("",)
    for folder in extra_dirs:
        for suffix in suffixes:
            candidate = folder / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def node_dirs() -> list[Path]:
    if IS_WINDOWS:
        return [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / "nodejs",
        ]
    return [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]


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
    """Runs a command without a shell and logs it; None when it cannot start or times out."""
    try:
        result = runner(
            list(command),
            cwd=str(cwd),
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _LOG.command(command, cwd, None, f"timed out after {timeout:.0f} seconds")
        return None
    except OSError as exc:
        _LOG.command(command, cwd, None, str(exc))
        return None
    _LOG.command(command, cwd, result)
    return result


def first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def tail(text: str | None, lines: int = 15) -> str:
    return "\n".join((text or "").rstrip().splitlines()[-lines:])


def output_of(result: subprocess.CompletedProcess[str] | None, fallback: str) -> str:
    if result is None:
        return fallback
    return tail((result.stderr or "") + (result.stdout or "")) or fallback


def refresh_path() -> None:
    """Picks up PATH changes made by an installer that ran during setup (Windows)."""
    if not IS_WINDOWS:
        return
    try:
        import winreg
    except ImportError:
        return
    parts: list[str] = []
    for root, key in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Path")
        except OSError:
            continue
        parts.extend(os.path.expandvars(p) for p in str(value).split(";") if p)
    current = os.environ.get("PATH", "").split(os.pathsep)
    merged = current + [p for p in parts if p not in current]
    os.environ["PATH"] = os.pathsep.join(merged)


# --------------------------------------------------------------------------
# Registration snippets


def quote_arg(value: str, windows: bool = IS_WINDOWS) -> str:
    if windows:
        return f'"{value}"' if any(ch in value for ch in " \t&()^%!;,") else value
    return shlex.quote(value)


def claude_add_args(python: Path, server: Path) -> list[str]:
    """``claude mcp add`` arguments. The name must come before ``-e``, which is variadic."""
    return [
        "mcp", "add", "--scope", "user", MCP_NAME,
        "-e", "ANTIGRAVITY_HARNESS=claude-code",
        "--", str(python), str(server),
    ]  # fmt: skip


def codex_block(python: Path, server: Path) -> str:
    return "\n".join(
        [
            f"[mcp_servers.{MCP_NAME}]",
            f"command = {json.dumps(str(python))}",
            f"args = [{json.dumps(str(server))}]",
            "tool_timeout_sec = 3600",
            'env = { ANTIGRAVITY_HARNESS = "codex" }',
        ]
    )


def mcp_snippets(python: Path, server: Path, windows: bool = IS_WINDOWS) -> dict[str, str]:
    """Ready-to-paste MCP registrations that use this machine's absolute paths."""
    claude = "claude " + " ".join(quote_arg(a, windows) for a in claude_add_args(python, server))
    generic = json.dumps(
        {
            "mcpServers": {
                MCP_NAME: {
                    "command": str(python),
                    "args": [str(server)],
                    "env": {"ANTIGRAVITY_HARNESS": "mcp-client"},
                }
            }
        },
        indent=2,
    )
    return {"claude": claude, "codex": codex_block(python, server), "json": generic}


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
# Installers (each one runs only after a yes)


def install_with_winget(report: Report, package_id: str, label: str) -> bool:
    winget = shutil.which("winget")
    if not winget:
        return False
    report.info(f"Installing {label} with winget. Windows may ask for permission ...")
    result = run(
        [winget, "install", "--exact", "--id", package_id,
         "--accept-package-agreements", "--accept-source-agreements"],
        timeout=INSTALL_TIMEOUT_SECONDS,
    )  # fmt: skip
    refresh_path()
    if result is None or result.returncode != 0:
        report.warn(
            f"winget could not install {label}.", output_of(result, "winget did not start.")
        )
        return False
    report.ok(f"Installed {label}")
    return True


def install_with_brew(report: Report, formula: str, label: str) -> bool:
    brew = find_tool("brew", [Path("/opt/homebrew/bin"), Path("/usr/local/bin")])
    if not brew:
        return False
    report.info(f"Installing {label} with Homebrew ...")
    result = run([brew, "install", formula], timeout=INSTALL_TIMEOUT_SECONDS)
    if result is None or result.returncode != 0:
        report.warn(
            f"Homebrew could not install {label}.", output_of(result, "brew did not start.")
        )
        return False
    report.ok(f"Installed {label}")
    return True


def linux_package_command(packages: Sequence[str]) -> list[str] | None:
    """The distribution's install command for ``packages`` (run with sudo), or None."""
    for manager, base in (
        ("apt-get", ["apt-get", "install", "-y"]),
        ("dnf", ["dnf", "install", "-y"]),
        ("pacman", ["pacman", "-S", "--needed", "--noconfirm"]),
        ("zypper", ["zypper", "install", "-y"]),
    ):
        if shutil.which(manager):
            prefix = [] if os.geteuid() == 0 else ["sudo"]  # type: ignore[attr-defined]
            return [*prefix, *base, *packages]
    return None


# --------------------------------------------------------------------------
# Steps


def check_python(report: Report) -> bool:
    report.section("Python", 1)
    version = platform.python_version()
    if not python_is_supported():
        report.fail(
            f"Python {version} is too old; Wisp needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.",
            f"Install a current Python from {PYTHON_DOWNLOAD_URL} and run setup again.",
        )
        return False
    report.ok(f"Python {version} ({sys.executable})")
    report.info(f"{platform.system()} {platform.release()} ({platform.machine()})")
    return True


def _create_venv(report: Report) -> subprocess.CompletedProcess[str] | None:
    created = run([sys.executable, "-m", "venv", str(VENV_DIR)], timeout=INSTALL_TIMEOUT_SECONDS)
    if created is not None and created.returncode == 0 and venv_python(VENV_DIR).is_file():
        return created
    if not sys.platform.startswith("linux"):
        return created
    # Debian and Ubuntu ship Python without the venv module.
    major, minor = sys.version_info[:2]
    command = linux_package_command([f"python{major}.{minor}-venv"])
    if command and report.ask(
        f"Python's venv module is missing. Install it now ({' '.join(command)})?"
    ):
        installed = run(command, timeout=INSTALL_TIMEOUT_SECONDS, capture=False)
        if installed is not None and installed.returncode == 0:
            if VENV_DIR.exists() and is_wisp_venv(VENV_DIR):
                shutil.rmtree(VENV_DIR)
            return run(
                [sys.executable, "-m", "venv", str(VENV_DIR)], timeout=INSTALL_TIMEOUT_SECONDS
            )
    return created


def ensure_venv(report: Report, check_only: bool, recreate: bool) -> Path | None:
    report.section("Virtual environment", 2)
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
        if (
            not check_only
            and is_wisp_venv(VENV_DIR)
            and report.ask(".venv is broken (its Python does not start). Rebuild it?")
        ):
            shutil.rmtree(VENV_DIR)
        else:
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
    created = _create_venv(report)
    if created is None or created.returncode != 0 or not python.is_file():
        hints = [output_of(created, "Python could not create a virtual environment.")]
        if sys.platform.startswith("linux"):
            hints.append(
                "On Debian or Ubuntu, install the venv module first: sudo apt install python3-venv"
            )
        report.fail("Could not create .venv.", *hints)
        return None
    report.ok("Created .venv")
    return python


def install_requirements(report: Report, python: Path, dev: bool, check_only: bool) -> bool:
    report.section("Python dependencies", 3)
    requirements = REPO_ROOT / ("requirements-dev.txt" if dev else "requirements.txt")
    if not check_only:
        report.info(f"Installing {requirements.name} into .venv ...")
        base = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--quiet"]
        installed = run([*base, "-r", str(requirements)], timeout=INSTALL_TIMEOUT_SECONDS)
        if installed is None or installed.returncode != 0:
            # A corrupt download cache is the usual cause of a second failure mode.
            report.info("First attempt failed; retrying without pip's cache ...")
            installed = run(
                [*base, "--no-cache-dir", "-r", str(requirements)], timeout=INSTALL_TIMEOUT_SECONDS
            )
        if installed is None or installed.returncode != 0:
            report.fail(
                "pip could not install the dependencies.",
                output_of(installed, "pip did not start or timed out."),
                "Check your internet connection. Behind a proxy, set HTTPS_PROXY and run "
                "setup again.",
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
    report.section("Review playbooks", 4)
    result = run([str(python), "-m", "tools.skill_loader", "--validate"])
    if result is None or result.returncode != 0:
        report.fail(
            "The playbooks in Skills/ did not validate.",
            output_of(result, "The validator did not start."),
        )
        return False
    report.ok(first_line(result.stdout).removeprefix("OK: ") or "Playbooks valid")
    return True


def check_agy(report: Report, check_only: bool) -> str | None:
    report.section("Antigravity CLI (agy)", 5)
    path, on_path = find_agy()
    if path is None and not check_only and report.mode == "ask":
        report.warn("The Antigravity CLI (agy) is not installed. Wisp uses it as the reviewer.")
        if report.ask("Open the Antigravity download page in your browser?"):
            webbrowser.open(AGY_INSTALL_URL)
        while path is None:
            reply = report.wait(
                "Install it, then press Enter to check again (or type s and Enter to skip):"
            )
            refresh_path()
            path, on_path = find_agy()
            if path is None and reply == "s":
                break
            if path is None:
                report.info("Still not found. The installer may need a moment to finish.")
    if path is None:
        report.warn(
            "agy was not found, so reviews cannot run yet.",
            f"Install the Google Antigravity CLI from {AGY_INSTALL_URL}, then run setup again.",
        )
        report.todo.append(
            f"Install the Google Antigravity CLI ({AGY_INSTALL_URL}) and run setup again."
        )
        return None
    version = run([path, "--version"])
    label = first_line(version.stdout or version.stderr) if version else ""
    if not label.lower().startswith("agy"):
        label = f"agy {label}".strip() if label else "agy"
    report.ok(f"{label} ({path})")
    if not on_path:
        if not check_only and report.ask("agy is not on your PATH. Run `agy install` to add it?"):
            installed = run([path, "install"], timeout=INSTALL_TIMEOUT_SECONDS)
            if installed is not None and installed.returncode == 0:
                report.ok("agy added to your PATH (open a new terminal to use it there)")
            else:
                report.warn("`agy install` did not finish.", output_of(installed, ""))
        else:
            report.warn(
                "agy is not on your PATH. Wisp still finds it, but your terminal will not.",
                "Run `agy install` once to add it.",
            )
    report.todo.append(
        "Sign in to Antigravity: run `agy` once and follow the prompt (skip if you already have)."
    )
    report.todo.append(
        "Open `agy` once inside each project you want reviewed and accept its trust prompt."
    )
    return path


MCP_HANDSHAKE = (
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
    '"capabilities":{},"clientInfo":{"name":"wisp-setup","version":"1"}}}\n'
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
)


def self_test(report: Report, python: Path) -> bool:
    """Starts the real engine and MCP server once, the way an assistant would."""
    report.section("Self-test", 6)
    bridge = REPO_ROOT / "tools" / "antigravity_bridge.py"
    result = run([str(python), str(bridge), "--status"])
    if result is None or result.returncode != 0:
        report.fail("The engine did not start.", output_of(result, "The bridge did not start."))
        return False
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        report.fail("The engine's status output was not valid JSON.", tail(result.stdout))
        return False
    report.ok(
        f"Engine: Wisp {status.get('wisp_version', '?')} with "
        f"{len(status.get('skills', []))} playbooks"
    )
    for warning in status.get("warnings", []):
        # A missing agy is already explained in the Antigravity CLI step.
        if status.get("executable_found") is False and "was not found" in str(warning):
            continue
        report.warn(str(warning))

    dry = run([str(python), str(bridge), "--prompt", "Setup self-test", "--skills",
               "plan-review", "--dry-run"])  # fmt: skip
    try:
        payload = json.loads(dry.stdout)["payload"] if dry and dry.returncode == 0 else ""
    except (json.JSONDecodeError, KeyError, TypeError):
        payload = ""
    if "Setup self-test" not in payload:
        report.fail("A test review request could not be built.", output_of(dry, "No output."))
        return False
    report.ok("Review requests build correctly")

    try:
        mcp = subprocess.run(
            [str(python), str(MCP_SERVER)], input=MCP_HANDSHAKE, capture_output=True,
            text=True, timeout=PROBE_TIMEOUT_SECONDS, cwd=str(REPO_ROOT), check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOG.command([str(python), str(MCP_SERVER)], REPO_ROOT, None, str(exc))
        mcp = None
    else:
        _LOG.command([str(python), str(MCP_SERVER), "(handshake)"], REPO_ROOT, mcp)
    if mcp is None or '"antigravity_review"' not in (mcp.stdout or ""):
        report.fail(
            "The MCP server did not answer a test handshake.",
            output_of(mcp, "The server did not start."),
        )
        return False
    report.ok("MCP server answers and lists its three tools")
    return True


def _install_node(report: Report) -> bool:
    if IS_WINDOWS and shutil.which("winget"):
        if report.ask(
            "Node.js is needed for the desktop overlay. Install Node.js LTS with winget?"
        ):
            return install_with_winget(report, NODE_WINGET_ID, "Node.js LTS")
    elif sys.platform == "darwin" and find_tool("brew", [Path("/opt/homebrew/bin")]):
        if report.ask("Node.js is needed for the desktop overlay. Install it with Homebrew?"):
            return install_with_brew(report, "node", "Node.js")
    return False


def setup_widget(report: Report, mode: str, check_only: bool) -> None:
    report.section("Desktop widget", 7)
    if electron_binary().is_file():
        report.ok("Electron overlay installed")
        return
    if mode == "skip":
        report.info("Skipped. The widget opens in a browser window instead.")
        return
    if mode == "auto" and not IS_WINDOWS:
        report.info(
            "The transparent overlay is built for Windows, so the widget opens in a browser "
            "window here. Pass --widget to install the overlay anyway."
        )
        return
    node = find_tool("node", node_dirs())
    npm = find_tool("npm", node_dirs())
    major = None
    if node:
        version = run([node, "--version"])
        major = parse_node_major(version.stdout if version else "")
    if (not node or not npm or major is None or major < MIN_NODE_MAJOR) and not check_only:
        if _install_node(report):
            node, npm = find_tool("node", node_dirs()), find_tool("npm", node_dirs())
            version = run([node, "--version"]) if node else None
            major = parse_node_major(version.stdout if version else "")
    if not node or not npm or major is None or major < MIN_NODE_MAJOR:
        report.warn(
            f"Node.js {MIN_NODE_MAJOR} or newer was not found, so the widget opens in a browser "
            "window.",
            f"Install Node.js from {NODE_DOWNLOAD_URL} and run setup again for the overlay.",
        )
        return
    if check_only:
        report.warn("The Electron overlay is not installed.", "Run setup without --check.")
        return
    report.info("Installing the Electron overlay (one time, about 100 MB) ...")
    # npm ci installs exactly what package-lock.json pins; npm install is the fallback
    # when the lock file and the local npm disagree.
    installed = run([npm, "ci", "--no-audit", "--no-fund"], cwd=SHELL_DIR,
                    timeout=INSTALL_TIMEOUT_SECONDS)  # fmt: skip
    if installed is None or installed.returncode != 0:
        report.info("npm ci failed; retrying with npm install ...")
        installed = run([npm, "install", "--no-audit", "--no-fund"], cwd=SHELL_DIR,
                        timeout=INSTALL_TIMEOUT_SECONDS)  # fmt: skip
    if installed is None or installed.returncode != 0 or not electron_binary().is_file():
        report.warn(
            "The Electron overlay could not be installed; the widget will use a browser window.",
            output_of(installed, "npm did not start or timed out."),
            "Electron downloads its binary during install: check your network or proxy, then "
            "run setup again.",
        )
        return
    report.ok("Electron overlay installed")


def _claude_registered(claude: str) -> tuple[bool, str]:
    result = run([claude, "mcp", "get", MCP_NAME])
    if result is None or result.returncode != 0:
        return False, ""
    return True, (result.stdout or "") + (result.stderr or "")


def connect_claude(report: Report, python: Path, check_only: bool) -> None:
    claude = find_tool(
        "claude", [Path.home() / ".local" / "bin", Path.home() / ".claude" / "local"]
    )
    skills_home = Path.home() / ".claude" / "skills"
    if claude is None and not (Path.home() / ".claude").is_dir():
        report.info("Claude Code was not found; skipped.")
        return
    if claude is not None:
        registered, details = _claude_registered(claude)
        current = str(MCP_SERVER) in details
        if registered and current:
            report.ok("Claude Code: Wisp's MCP server is registered")
        elif check_only:
            report.warn("Claude Code: Wisp's MCP server is not registered (or points elsewhere).")
        else:
            question = (
                "Claude Code has an older 'antigravity' server. Replace it with this Wisp?"
                if registered
                else "Register Wisp's MCP server with Claude Code (for all your projects)?"
            )
            if report.ask(question):
                if registered:
                    run([claude, "mcp", "remove", MCP_NAME, "--scope", "user"])
                added = run([claude, *claude_add_args(python, MCP_SERVER)])
                if added is not None and added.returncode == 0:
                    report.ok("Claude Code: registered Wisp's MCP server")
                    report.done.append("Claude Code")
                else:
                    report.fail(
                        "Claude Code could not register the MCP server.",
                        output_of(added, "The claude command did not start."),
                        "Run the command from the end of this output by hand.",
                    )
                    report.todo.append("Register Wisp with Claude Code (command below).")
            else:
                report.todo.append("Register Wisp with Claude Code (command below).")
    else:
        report.info("The claude command was not found, so the MCP server was not registered.")
        report.todo.append("Register Wisp with Claude Code (command below).")

    target = skills_home / DELEGATION_SKILL_DIR.name
    source_skill = DELEGATION_SKILL_DIR / "SKILL.md"
    installed_skill = target / "SKILL.md"
    up_to_date = (
        installed_skill.is_file() and installed_skill.read_bytes() == source_skill.read_bytes()
    )
    if up_to_date:
        report.ok("Claude Code: delegation skill installed")
    elif check_only:
        report.warn("Claude Code: the delegation skill is not installed or is out of date.")
    elif report.ask(
        "Install the delegation skill for Claude Code (it tells Claude when to ask for a review)?"
    ):
        try:
            shutil.copytree(DELEGATION_SKILL_DIR, target, dirs_exist_ok=True)
        except OSError as exc:
            report.fail("Could not copy the delegation skill.", str(exc))
        else:
            report.ok(f"Claude Code: delegation skill installed in {target}")
    else:
        report.todo.append("Install the delegation skill for Claude Code (command below).")


def connect_codex(report: Report, python: Path, check_only: bool) -> None:
    codex_home = Path.home() / ".codex"
    if not shutil.which("codex") and not codex_home.is_dir():
        return
    config = codex_home / "config.toml"
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    if f"[mcp_servers.{MCP_NAME}]" in text:
        if str(MCP_SERVER) in text or json.dumps(str(MCP_SERVER)) in text:
            report.ok("Codex: Wisp's MCP server is configured")
        else:
            report.warn(
                f"Codex: {config} already has an [mcp_servers.{MCP_NAME}] entry for another path.",
                "Update it by hand with the block printed below.",
            )
            report.todo.append("Update the Codex entry (block below).")
        return
    if check_only:
        report.warn("Codex: Wisp's MCP server is not configured.")
        return
    if not report.ask(f"Add Wisp's MCP server to Codex ({config})? A backup is kept."):
        report.todo.append("Add Wisp to Codex (block below).")
        return
    try:
        codex_home.mkdir(parents=True, exist_ok=True)
        if config.is_file():
            shutil.copy2(config, config.with_name("config.toml.wisp-backup"))
        block = codex_block(python, MCP_SERVER)
        new_text = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
        try:
            import tomllib

            tomllib.loads(new_text)
        except ImportError:
            pass
        config.write_text(new_text, encoding="utf-8")
    except (OSError, ValueError) as exc:
        report.fail("Could not update the Codex configuration; it was left unchanged.", str(exc))
        return
    report.ok(f"Codex: added Wisp's MCP server to {config}")
    report.done.append("Codex")


def connect_assistants(report: Report, python: Path, check_only: bool) -> None:
    report.section("Connect coding assistants")
    connect_claude(report, python, check_only)
    connect_codex(report, python, check_only)
    report.info(
        "Cline, Cursor, Roo Code, opencode and others: see docs/integrations.md "
        "(the JSON entry is printed below)."
    )
    if report.done:
        names = " and ".join(report.done)
        verb = "loads" if len(report.done) == 1 else "load"
        report.todo.append(
            f"Restart {names} so {'it' if len(report.done) == 1 else 'they'} {verb} Wisp's tools."
        )


def run_test_suite(report: Report, python: Path) -> None:
    report.section("Test suite")
    report.info("Running the offline test suite (about a minute) ...")
    result = run([str(python), "-m", "pytest", "tests", "-q"], timeout=TEST_TIMEOUT_SECONDS,
                 capture=False)  # fmt: skip
    if result is None or result.returncode != 0:
        report.fail("Some tests failed; see the output above.")
        return
    report.ok("All tests passed")


# --------------------------------------------------------------------------
# Summary


def print_summary(report: Report, python: Path | None, connect: bool = False) -> None:
    bar = "-" * 72
    if report.problems:
        print(f"\n{bar}\n{report._paint('Setup could not finish', '1;31')}\n")
        for problem in report.problems:
            print(f"  {report._paint('Problem', '1')}  {problem.message}")
            for hint in problem.hints:
                for line in hint.splitlines():
                    print(f"           {line}")
            print()
        if _LOG.path is not None:
            print(f"  The full log is in {_LOG.path}")
            print(f"  If you report an issue ({ISSUES_URL}), attach that file.")
        print(bar)
        return

    if python is None:
        print("\nNothing is broken, but setup has not been run yet.")
        return
    print(f"\n{bar}")
    headline = "Setup finished" + (
        f" with {len(report.warnings)} note(s)" if report.warnings else ""
    )
    print(report._paint(headline, "1;32"))
    if report.todo:
        print("\nStill to do:")
        for i, item in enumerate(dict.fromkeys(report.todo), 1):
            print(f"  {i}. {item}")
    print("\nOpen the widget:")
    if IS_WINDOWS:
        print(f"  {REPO_ROOT / 'tools' / 'antigravity_viewer.cmd'}")
    else:
        print(
            f"  {quote_arg(str(python))} {quote_arg(str(REPO_ROOT / 'tools' / 'antigravity_viewer.py'))}"
        )
    setup_cmd = "setup.bat" if IS_WINDOWS else "./setup.sh"
    if not connect:
        print("\nConnect a coding assistant (optional):")
        print(f"  {setup_cmd} --connect   sets up Claude Code or Codex, asking first")
        print(f"  Any other MCP client: {REPO_ROOT / 'docs' / 'integrations.md'}")
        if _LOG.path is not None:
            print(f"\nSetup log: {_LOG.path}")
        print(f"Run {setup_cmd} --check at any time to re-check everything.")
        print(bar)
        return
    snippets = mcp_snippets(python, MCP_SERVER)
    if "Claude Code" not in report.done:
        print("\nClaude Code (register the server):")
        print(f"  {snippets['claude']}")
        shell = "PowerShell" if IS_WINDOWS else "a terminal"
        print(f"Claude Code (install the delegation skill, in {shell}):")
        for line in skill_install_commands(DELEGATION_SKILL_DIR):
            print(f"  {line}")
    if "Codex" not in report.done:
        print("\nCodex (~/.codex/config.toml):")
        for line in snippets["codex"].splitlines():
            print(f"  {line}")
    print("\nCline, Cursor, Roo Code and other MCP clients (mcpServers JSON):")
    for line in snippets["json"].splitlines():
        print(f"  {line}")
    print(f"\nMore detail for each assistant: {REPO_ROOT / 'docs' / 'integrations.md'}")
    if _LOG.path is not None:
        print(f"Setup log: {_LOG.path}")
    print(f"Run {setup_cmd} --check at any time to re-check everything.")
    print(bar)


# --------------------------------------------------------------------------
# Entry point


def skipped(report: Report, steps: Sequence[tuple[str, int]]) -> None:
    """Shows steps that cannot run yet, so the numbering never jumps."""
    for title, number in steps:
        report.section(title, number)
        report.info("Skipped until the steps above succeed.")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wisp_setup",
        description="Set up Wisp, or check an existing setup with --check.",
    )
    parser.add_argument(
        "--check", action="store_true", help="report on the setup without changing anything"
    )
    answers = parser.add_mutually_exclusive_group()
    answers.add_argument(
        "-y", "--yes", action="store_true", help="accept every offer without asking"
    )
    answers.add_argument(
        "--no-input",
        action="store_true",
        help="never ask; skip optional installs and print the remaining steps",
    )
    parser.add_argument(
        "--connect",
        action="store_true",
        help="connect Claude Code, Codex or other coding assistants (asks for each one)",
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


def answer_mode(args: argparse.Namespace, interactive: bool) -> str:
    if args.check or args.no_input:
        return "never"
    if args.yes:
        return "yes"
    return "ask" if interactive else "never"


def main(argv: Sequence[str] | None = None) -> int:
    global _LOG
    args = parse_args(argv)
    interactive = sys.stdin is not None and sys.stdin.isatty()
    _LOG = SetupLog(None if args.check else LOG_PATH)
    report = Report(color=supports_color(), mode=answer_mode(args, interactive))
    title = "Wisp setup" + (" check (no changes will be made)" if args.check else "")
    print(report._paint(title, "1"))
    print(f"Folder: {REPO_ROOT}")
    if report.mode == "ask":
        print("Setup asks before it installs or changes anything outside this folder.")
    try:
        if not check_python(report):
            return 1
        python = ensure_venv(report, args.check, args.recreate_venv)
        ready = python is not None and install_requirements(report, python, args.dev, args.check)
        if ready and python is not None:
            ready = validate_skills(report, python)
        elif python is None:
            skipped(report, [("Python dependencies", 3), ("Review playbooks", 4)])
        else:
            skipped(report, [("Review playbooks", 4)])
        check_agy(report, args.check)
        if ready and python is not None:
            ready = self_test(report, python)
        else:
            skipped(report, [("Self-test", 6)])
        setup_widget(report, args.widget, args.check)
        if args.connect and ready and python is not None:
            connect_assistants(report, python, args.check)
        if args.dev and ready and python is not None and not report.failures:
            run_test_suite(report, python)
    except KeyboardInterrupt:
        print("\nSetup was interrupted. Run it again to continue where it stopped.")
        _LOG.write("Interrupted by the user.")
        return 130
    except Exception as exc:  # last-resort report that points to the log
        _LOG.write(f"Unexpected error: {exc!r}")
        import traceback

        _LOG.write(traceback.format_exc())
        report.fail(f"Setup hit an unexpected error: {exc}", "The full traceback is in the log.")
        print_summary(report, None)
        return 1
    finally:
        _LOG.close()
    print_summary(report, python, connect=args.connect)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
