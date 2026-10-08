# Contributing to Wisp

Thanks for taking the time to improve Wisp. This page covers how to get a
working setup, how the code is laid out, and what a change needs before it
can be merged.

## Set up

You need Python 3.10 or newer and Git. Node.js 18 or newer is only needed if
you work on the Electron widget shell.

On Windows:

```bat
setup.bat --dev
```

On macOS or Linux:

```bash
./setup.sh --dev
```

This creates `.venv`, installs the pinned runtime and test dependencies,
validates the playbooks in `Skills/` and runs the test suite. Run it with
`--check` instead to see what is set up without changing anything. On
Windows, add `--no-pause` to run it unattended. To work on the Electron shell
on macOS or Linux, add `--widget` (Windows installs it by default when Node.js
is present).

You don't need the Antigravity CLI or a Google account to develop. The test
suite uses scripted stand-ins and never calls the real `agy`.

## Check your change

Run these before you open a pull request, with the Python in `.venv`
(activate it first, or call `.venv/bin/python` or `.venv\Scripts\python`
directly). CI runs the same steps on Windows and Ubuntu with Python 3.10 and
3.13, and runs the setup itself on Windows, Ubuntu and macOS.

```bash
python -m pytest tests/ -q
ruff check .
python -m tools.skill_loader --validate
node --check tools/wisp_shell/main.js   # only if you touched the shell
```

If you change the widget, also run it (`tools\antigravity_viewer.cmd` on
Windows, `python tools/antigravity_viewer.py --shell browser` elsewhere) and
click through the views you touched. If you change setup, run
`python tools/wisp_setup.py --check` on the systems you can reach and say in
the pull request which ones you tried.

## How the code is organized

| Path | What lives there |
| --- | --- |
| `tools/antigravity_*.py` | The delegation engine: the bridge that runs `agy`, the MCP server, the live event feed, and the widget's local HTTP server (`antigravity_viewer.py`). |
| `tools/viewer_shell.py`, `tools/viewer_platform.py` | Widget windows (Electron, pywebview, browser) and OS helpers for the widget server. |
| `tools/wisp_chat.py`, `tools/wisp_capture.py`, `tools/capture/` | Widget features: the chat store and screen capture (capture is Windows-only). |
| `tools/wisp_setup.py` | The cross-platform setup and environment check behind `setup.bat` and `setup.sh`. It uses only the standard library, because it runs before anything is installed. |
| `tools/skill_loader.py` | Loads and validates the review playbooks in `Skills/`. |
| `tools/antigravity_viewer.html` | The widget UI (HTML, CSS and JavaScript in one file). |
| `tools/wisp_shell/` | The Electron shell that hosts the widget as a desktop overlay. |
| `Skills/` | The review playbooks, one Markdown file each. |
| `tests/` | The test suite. |

The runtime has one third-party dependency, PyYAML. Please keep it that way
unless there's a strong reason; everything else uses the standard library.

## Guidelines

- **Tests come with behavior changes.** A bug fix should include a test that
  fails without it.
- **Comments explain why.** Describe the constraint or failure mode that made
  the code look the way it does. Don't reference issue numbers or describe
  the change history; that belongs in the commit message.
- **Nothing is truncated.** The bridge must never drop or shorten reviewer
  output anywhere between `agy` and the caller. Tests enforce this.
- **No machine-specific paths and no credentials.** Resolve paths with
  `shutil.which` and `Path.home()`. Don't pass API keys to child processes.
- **Keep commits focused.** One logical change per commit, with a message
  that says what changed and why.

## Adding a review playbook

Create `Skills/NN_<name>.md` with YAML front matter that includes every
required field (`name`, `version`, `description`, `activation_triggers`,
`input_contract`, `output_contract`) plus a `brief`: a standalone summary of
600 to 1,800 characters that is sent to the reviewer inline and must define what
PASS, PASS_WITH_FIXES and BLOCK mean for the task. The full body is read from
disk and uses the standard sections: Mission, Inputs to establish first, Method,
Checklist, Evidence standard, Severity guide, Skill-specific output,
Anti-patterns and Done when.

Write for the domain: concrete checks a strong generalist would miss, what counts
as proof, and the specific ways reviewers go wrong. Don't restate the shared
review protocol (finding format, verdict block, evidence rules); the bridge sends
it once for every review. Run `python -m tools.skill_loader --validate` and the
registry tests (`python -m pytest tests/test_skill_registry.py -q`).
[AGENTS.md](AGENTS.md#6-skill-registry) describes the format in full.

## Reporting security issues

Please don't open a public issue for a vulnerability. Use
[a private security advisory](https://github.com/harlixay7/Wisp/security/advisories/new)
instead. [SECURITY.md](SECURITY.md) explains Wisp's trust model.

## Code of conduct

Everyone taking part in Wisp is expected to follow the
[code of conduct](CODE_OF_CONDUCT.md).
