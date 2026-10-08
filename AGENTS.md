# AGENTS.md

Wisp hands a coding assistant's plan or change to an independent reviewer, the
Google Antigravity CLI (`agy`), and returns its complete critique. It ships a
Python bridge, an MCP server, seventeen review playbooks in `Skills/` and a
desktop widget.

This file is for agents changing this repository. To use Wisp from another
project, follow
[integrations/antigravity-delegation/SKILL.md](integrations/antigravity-delegation/SKILL.md).

## Module map

| Path | Responsibility |
| --- | --- |
| `tools/antigravity_bridge.py` | Request, payload and command building, environment sanitizing, retries, quota failover, reports, CLI. |
| `tools/antigravity_containment.py` | Process containment: Windows Job Objects and descendant kill, POSIX process groups. |
| `tools/antigravity_aggregate.py` | Turns the `stream-json` event stream into the critique and parses the `WISP_VERDICT` block. |
| `tools/antigravity_mcp_server.py` | MCP stdio server: `antigravity_review`, `antigravity_status`, `antigravity_skills`. |
| `tools/antigravity_live.py` | Live event feed: stream parser, NDJSON sinks, retention, cross-project registry. |
| `tools/antigravity_viewer.py`, `.html`, `.cmd` | Widget server and UI (SSE, six views, chat, settings, replay). Read-only with respect to the engine. |
| `tools/viewer_shell.py`, `tools/viewer_platform.py` | Widget windows (Electron, pywebview, Chromium app window); OS process helpers and account switching. |
| `tools/wisp_chat.py` | Persistent chat threads for the widget. |
| `tools/wisp_capture.py`, `tools/capture/*.ps1` | Selection, snip and pasted-image capture (Windows). |
| `tools/wisp_shell/` | Electron shell that hosts the widget as a transparent overlay. |
| `tools/viewer_assets/*.png` | Owl artwork (black background, screen-blended). |
| `tools/skill_loader.py` | Playbook discovery, metadata validation, name and alias resolution, prompt rendering. |
| `tools/wisp_setup.py`, `setup.bat`, `setup.sh` | Cross-platform setup, self-test, `--connect` and `--check`. Standard library only; asks before every install or outside change. |
| `Skills/NN_<name>.md` | Review playbooks (Markdown with YAML front matter). |
| `integrations/antigravity-delegation/SKILL.md` | The delegation skill assistants install. Single source for the request format, skill routing and reconciliation rules. |
| `docs/integrations.md` | Human setup guide for each assistant, server settings, CLI options, troubleshooting. |
| `tests/` | Offline suite; no test runs the real `agy`. |

## Hard rules

- Never truncate `agy` output anywhere between the process and the caller.
- No `TODO` or stub code. Every function is implemented and typed.
- No machine-specific absolute paths. Resolve with `shutil.which` and
  `Path.home()`.
- No API keys in child environments; `agy` handles Google sign-in. The variables
  stripped are `BLOCKED_ENV_PREFIXES` and `BLOCKED_ENV_EXACT` in
  `tools/antigravity_bridge.py`; `SECURITY.md` lists them and a test keeps the
  two in sync.
- Runtime dependencies stay at PyYAML. `tools/wisp_setup.py` uses only the
  standard library because it runs before anything is installed.
- Tests use scripted launchers and cost no quota. `WISP_ASK_FAKE=1` makes the
  widget answer with canned `[TEST MODE]` replies;
  `python tools/wisp_chat.py --live-dir <dir> --clean-fake` removes those threads.
- Widget input (Electron): the shell keeps a fixed transparent canvas and
  toggles `setIgnoreMouseEvents` from the cursor feed, so empty canvas passes
  clicks to the desktop. The owl, panel header and dock are drag regions; every
  button inside them and every overlay must be an explicit `no-drag` hole.
  Never center transformed elements inside a drag region: Chromium computes the
  holes from untransformed layout. pywebview drags over IPC (`wisp:move`).
- Docs: the skill routing table lives in `SKILL.md` and the user-facing one in
  `README.md`; both must name every shipped playbook. Use the plain playbook
  names; the old long names are aliases only.

## Checks

Set up once with `./setup.sh --dev` (Windows: `setup.bat --dev`), then use the
Python in `.venv`:

```bash
python -m pytest tests/ -q                 # full offline suite
ruff check .                               # lint
python -m tools.skill_loader --validate    # playbook registry
python tools/antigravity_bridge.py --prompt "..." --skills plan-review --dry-run
node --check tools/wisp_shell/main.js      # only if the shell changed
```

`README.md` quotes the number of collected tests (`# N test cases`); update it
when you add or remove tests (`python -m pytest tests/ --collect-only -q`).

## Review gates for changes here

Delegate through Wisp itself when a gate fires. The request format, skill
choice and how to answer each finding are in
[SKILL.md](integrations/antigravity-delegation/SKILL.md); follow it rather
than a copy here.

| Gate | When | Skill |
| --- | --- | --- |
| Plan | Before any multi-file feature, refactor or architectural change | `plan-review` (add `design-second-opinion` when the approach is open) |
| High-risk seam | Containment and process spawning, the live registry and other file locking, chat and report persistence, the MCP protocol, environment sanitizing, the widget server's network surface | `security-review`, `data-integrity-review` or `agent-workflow-review`, whichever matches |
| Repeated failure | After two failed fixes for the same root cause | `root-cause-investigation` |
| Pre-commit | Before declaring a task done or merging | `pre-merge-review` with the diff and acceptance criteria |

Skip delegation for typos, formatting and wording-only doc edits, or when a
critique from the last 30 minutes already covers the exact change.

Writing or changing a playbook: see
[CONTRIBUTING.md](CONTRIBUTING.md#adding-a-review-playbook).
