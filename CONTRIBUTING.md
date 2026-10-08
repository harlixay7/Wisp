# Contributing to Wisp

Thanks for taking the time to improve Wisp. This page covers how to get a
working setup, how the code is laid out, and what a change needs before it
can be merged.

## Set up

You need Python 3.10 or newer. Node.js is only needed if you work on the
Electron widget shell.

**Windows**

```bat
setup.bat
```

This creates `.venv`, installs the pinned dependencies, validates the skill
registry and runs the test suite. Pass `--no-pause` to run it unattended.

**Linux or macOS**

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

You don't need the Antigravity CLI or a Google account to develop. The test
suite uses scripted launchers and never calls the real `agy`.

## Check your change

Run these before you open a pull request. CI runs the same steps on Windows
and Ubuntu with Python 3.10 and 3.13.

```bash
python -m pytest tests/ -q
ruff check .
python -m tools.skill_loader --validate
node --check tools/wisp_shell/main.js   # only if you touched the shell
```

If you change the widget, also run it (`tools\antigravity_viewer.cmd`, or
`python tools/antigravity_viewer.py --shell browser` elsewhere) and click
through the views you touched.

## How the code is organized

| Path | What lives there |
| --- | --- |
| `tools/antigravity_*.py` | The delegation engine: the bridge that runs `agy`, the MCP server, the live event feed, and the widget's local HTTP server (`antigravity_viewer.py`). |
| `tools/wisp_*.py` | Widget features: the chat store and screen capture. |
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
