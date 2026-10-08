# Connecting Wisp to your coding assistant

Wisp reaches your assistant in two parts:

1. **The MCP server** gives the assistant three tools: `antigravity_review`,
   `antigravity_status` and `antigravity_skills`.
2. **The delegation skill**,
   [`integrations/antigravity-delegation/SKILL.md`](../integrations/antigravity-delegation/SKILL.md),
   tells the assistant when to ask for a review, how to write the request,
   which review playbook to pick, and how to answer every finding.

The server alone works, but an assistant without the skill tends to ask for
reviews late and to skim the answer. Install both.

This page covers each assistant, the server's settings, the command-line
fallback, day-to-day operation and troubleshooting. For writing requests that
get sharp answers, see [delegation-playbook.md](delegation-playbook.md).

## Before you start

- Run setup (`setup.bat` on Windows, `./setup.sh` on macOS and Linux). At the
  end it prints the registrations below with the absolute paths on your
  machine already filled in, plus the command that installs the skill for
  Claude Code. Run it again with `--check` to print them again without
  changing anything.
- Sign in by running `agy` once. Then run `agy` inside each project you want
  reviewed and accept its workspace-trust prompt, or add the project to
  `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json`.
- In the examples, `<wisp>` is the folder you cloned Wisp into. Paths must be
  absolute: your assistant starts the server from whatever project it is
  working in, so a relative `tools/...` path only works inside the Wisp
  folder. On Windows the Python path is `<wisp>\.venv\Scripts\python.exe`
  instead of `<wisp>/.venv/bin/python`.
- Restart the assistant after registering. Every assistant reads its MCP
  servers once, at startup, and starts and stops the server itself; there is
  nothing to run in the background.
- The server reviews the folder your assistant runs it in. A request can name
  another folder in its `workspace` field, or you can pin one with
  `ANTIGRAVITY_WORKSPACE` (see [Server settings](#server-settings)). Don't pin
  it to the Wisp folder unless Wisp itself is what you want reviewed.

## Claude Code

Register the server for every project (`--scope user`):

```bash
claude mcp add --scope user -e ANTIGRAVITY_HARNESS=claude-code antigravity -- \
  <wisp>/.venv/bin/python <wisp>/tools/antigravity_mcp_server.py
```

Install the delegation skill by copying its folder into your skills folder.
For every project, on macOS or Linux:

```bash
mkdir -p ~/.claude/skills
cp -R <wisp>/integrations/antigravity-delegation ~/.claude/skills/
```

On Windows, in PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\.claude\skills\antigravity-delegation" | Out-Null
Copy-Item -Recurse -Force "<wisp>\integrations\antigravity-delegation\*" "$HOME\.claude\skills\antigravity-delegation"
```

For a single project, copy the folder to `<project>/.claude/skills/` instead.
Claude Code loads the skill when a task matches its description. Run the copy
again after you update Wisp.

## Codex

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.antigravity]
command = "<wisp>/.venv/bin/python"
args = ["<wisp>/tools/antigravity_mcp_server.py"]
tool_timeout_sec = 3600
env = { ANTIGRAVITY_HARNESS = "codex" }
```

A review often takes several minutes, and Wisp lets one attempt run for up to
20 minutes by default. The long `tool_timeout_sec` keeps Codex from giving up
first.

Codex follows the `AGENTS.md` in your project. Add a line there that tells it
to read and follow `<wisp>/integrations/antigravity-delegation/SKILL.md` when
delegating reviews.

## Cline, Cursor, Roo Code and other MCP clients

These take an `mcpServers` block: Cline and Roo Code in their MCP settings
JSON, Cursor in `.cursor/mcp.json`.

```json
{
  "mcpServers": {
    "antigravity": {
      "command": "<wisp>/.venv/bin/python",
      "args": ["<wisp>/tools/antigravity_mcp_server.py"],
      "env": { "ANTIGRAVITY_HARNESS": "mcp-client" }
    }
  }
}
```

For the skill, Cursor reads rules from `.cursor/rules/` (for example
`.cursor/rules/antigravity-delegation.mdc`); Cline and Roo Code read a rules
file such as `.clinerules`. Copy the body of `SKILL.md` there, or add a rule
that points at the file.

## opencode

opencode registers MCP servers under `"mcp"` (not `"mcpServers"`), in your
project's `opencode.json` or in your global opencode config:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "antigravity": {
      "type": "local",
      "command": ["<wisp>/.venv/bin/python", "<wisp>/tools/antigravity_mcp_server.py"],
      "enabled": true,
      "timeout": 3600000,
      "environment": { "ANTIGRAVITY_HARNESS": "opencode" }
    }
  }
}
```

For the skill, copy the `antigravity-delegation` folder to
`.opencode/skills/` in a project, or to `~/.config/opencode/skills/` for every
project. opencode also picks up skills from `~/.agents/skills/`.

## Assistants without MCP

Assistants such as Aider can run the command line instead (see
[Command line](#command-line)). Point them at `SKILL.md` from their
instructions file so they follow the same rules. The command line saves the
same complete report as the MCP tool.

## Letting your assistant do the setup

You can ask an assistant to "register the Wisp MCP server and install the
delegation skill using docs/integrations.md". It can run setup, copy the
registration it prints, write config files (global ones such as
`~/.codex/config.toml` may need your approval), copy the skill folder, and
check the result with `--status`. It cannot restart itself, so the tools only
appear after you restart it, and it can't reliably tell which assistant it is
running in, so name it.

## Three kinds of "skill"

The word means three different things here:

1. **Review playbooks** in `<wisp>/Skills/`. Wisp sends these to the reviewer.
2. **Antigravity's own skills** in `~/.gemini/config/skills/`, which `agy`
   loads by itself. Wisp doesn't touch them.
3. **The delegation skill** in `integrations/antigravity-delegation/`. It is
   for your coding assistant, not for the reviewer.

## Server settings

The MCP server reads these environment variables. Set them in the `env` (or
`environment`) part of the registration.

| Variable | Default | Effect |
| --- | --- | --- |
| `ANTIGRAVITY_HARNESS` | `mcp-client` | Label for the assistant, shown in the request and the widget. |
| `ANTIGRAVITY_WORKSPACE` | the server's working folder | Folder to review when a request doesn't name one. |
| `ANTIGRAVITY_SKILL_DIR` | `<workspace>/Skills`, else Wisp's own | Playbook folder. When set, there is no fallback. |
| `ANTIGRAVITY_MODEL` | `gemini-3.8-flash-high` | Primary model. A model chosen in the widget's settings takes precedence. |
| `ANTIGRAVITY_FALLBACK_MODEL` | `claude-opus-4-6-thinking` | Model used when the primary is out of quota. Same precedence. |
| `ANTIGRAVITY_PRINT_TIMEOUT` | `1200` | Seconds `agy` may run per attempt. |
| `ANTIGRAVITY_RETRIES` | `2` | Retries per model after a transient failure. |
| `ANTIGRAVITY_RETRY_BACKOFF` | `5` | Base seconds for exponential backoff between retries. |
| `ANTIGRAVITY_QUOTA_WAIT` | `0` | Longest quota reset, in seconds, worth waiting for before failing over. |
| `ANTIGRAVITY_QUOTA_HOOK` | none | Command to run when quota runs out, such as an account switch. |
| `ANTIGRAVITY_LIVE` | `1` | `0` turns off the live feed for the widget. |
| `ANTIGRAVITY_LIVE_DIR` | `<workspace>/.antigravity-reports/live` | Where live events are written. |
| `ANTIGRAVITY_LIVE_KEEP` | `20` | Live runs kept per folder. |
| `ANTIGRAVITY_LIVE_REGISTRY` | `~/.antigravity-reports/live-registry.json` | List of live folders the widget watches. |

## Command line

The bridge is the same engine the MCP server uses:

```bash
<wisp>/.venv/bin/python <wisp>/tools/antigravity_bridge.py \
  --workspace ~/code/my-app \
  --prompt "Review this migration plan" \
  --context "SQLite in WAL mode, single writer" \
  --claim "No migration takes longer than 5 seconds" \
  --artifact "migrations/0042_add_index.sql" \
  --skills data-integrity-review
```

`--envelope request.json` reads the whole request from a JSON file with the
same fields as the MCP tool, plus `harness` and `notes`; unknown fields are
ignored. [examples/delegation-case-study.json](../examples/delegation-case-study.json)
is a complete example.

| Option | Effect |
| --- | --- |
| `--prompt`, `-p` / `--envelope FILE` | The request, or a JSON file holding it. |
| `--context`, `--claim`, `--artifact` | Request fields. `--claim` and `--artifact` can repeat. |
| `--skills a,b` / `--skills all` | Primary playbooks. |
| `--recommended-skills a,b` | Up to three playbooks applied where relevant. |
| `--mode review\|implement` | Read-only review (default), or let the reviewer edit files. |
| `--workspace DIR` | Project to review (default: the current folder). |
| `--skill-dir DIR` | Playbook folder (default: `<workspace>/Skills`, else Wisp's own). |
| `--model`, `--fallback-model` | Model chain (default `gemini-3.8-flash-high`, then `claude-opus-4-6-thinking`). |
| `--print-timeout`, `--grace-seconds` | Seconds `agy` may run (default 1200), and extra seconds before Wisp kills it (default 60). |
| `--retries`, `--retry-backoff` | Transient retries per model (default 2) and base backoff seconds (default 5). |
| `--quota-wait`, `--quota-hook` | Wait for a quota reset up to N seconds, or run a command when quota runs out. |
| `--live` / `--no-live`, `--live-dir`, `--live-keep-runs` | Live events for the widget (on by default), their folder, and how many runs to keep (default 20). |
| `--report-keep N` | Reports kept per project (default 50; 0 keeps all). |
| `--executable PATH` | Use this `agy` instead of searching for it. |
| `--harness NAME` | Label recorded in the request. |
| `--json` | Print the complete result, both raw streams included, as JSON. |
| `--dry-run` | Print the exact command and request without running `agy`. Works before `agy` is installed. |
| `--status`, `--list-skills` | Print a health check, or the playbooks, and exit. |

## Checking that it works

```bash
<wisp>/.venv/bin/python <wisp>/tools/antigravity_bridge.py --status
<wisp>/.venv/bin/python <wisp>/tools/antigravity_bridge.py --list-skills
<wisp>/.venv/bin/python <wisp>/tools/antigravity_bridge.py --prompt "Reply with PONG" --dry-run
```

`--status` should show `executable_found: true`, the two models, all
seventeen playbooks and no warnings. Then ask your assistant to call
`antigravity_status`; if the tool is missing, see
[Troubleshooting](#troubleshooting). A real review of a one-line prompt with no
playbooks is a cheap end-to-end test, and its report should appear in
`.antigravity-reports/`.

## How runs behave

**Models and quota.** Reviews use `gemini-3.8-flash-high` with
`--effort high`. When `agy` reports a quota error (`RESOURCE_EXHAUSTED`,
`code 429`, "Individual quota reached", "Rate limit exceeded"), Wisp first
waits for the reset if it is no longer than `--quota-wait`, then runs
`--quota-hook` and retries if the hook exits with 0, and finally sends the
unchanged request to `claude-opus-4-6-thinking`. The fallback also takes over
when the primary keeps failing after its retries. If both models are out of
quota, the result has `rate_limited: true` with `resets_in` (for example
`1h 45m`) and `reset_seconds`. Keep the default models unless you only want a quick smoke
test; cheaper models make for weaker reviews.

**Switching Google accounts.** On Windows, the widget's account switch does
this for you. By hand: run
`cmdkey /delete:LegacyGeneric:target=gemini:antigravity`, run `agy`, sign in
with the other account, and confirm with `agy models`. Elsewhere, switch
accounts through `agy` itself.

**Retries and timeouts.** Connection resets, server errors and empty answers
are retried with exponential backoff, and a run that exits with 0 but prints
nothing counts as a failure. Wisp kills `agy` and everything it started at
`--print-timeout` plus `--grace-seconds`, and still returns every line it
captured.

**Reports.** Every run, from MCP or the command line, writes
`<workspace>/.antigravity-reports/antigravity-report-<timestamp>-<id>.json`
with the critique, the parsed verdict, both raw output streams and the
details of each attempt. The MCP response leaves out the raw streams to stay
compact and gives the report's path instead. If your assistant cuts long tool
output short, read the report.

**Playbook registry.** Wisp uses the project's own `Skills/` folder when it
has one and its own registry otherwise. In that case it records a warning and
also gives the reviewer read access to Wisp's `Skills/` folder. A single
`.md` file without YAML front matter breaks the whole registry, so keep
anything that isn't a playbook out of `Skills/`.

**Watching from any project.** Each review registers its live folder in
`~/.antigravity-reports/live-registry.json` (up to 50 folders; missing ones
are pruned). The widget watches its own folder and every registered one, so
reviews started from any project, by any assistant, appear in the stream,
History and replay. It follows the active run until that run ends or is quiet
for 15 seconds, so two runs at once don't make the view jump.

**Antigravity's own MCP servers.** `agy` manages its own MCP servers
(`agy mcp add`, `remove`, `list`, `enable`, `disable`; settings in
`~/.gemini/config/mcp_config.json`). Wisp leaves that configuration alone, and
stopping a review also stops any server `agy` started.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| The tools don't appear in the assistant | Restart the assistant. Check that the paths are absolute and point at `.venv`'s Python. opencode needs `"mcp"`, not `"mcpServers"`. |
| `agy` not found | Wisp looks on `PATH`, then in `~/.gemini/bin/` (`agy.exe` on Windows). `setup --check` and `--status` show what was found; `--executable` overrides it. |
| The reviewer can't read the project | Accept `agy`'s trust prompt in that project, or add it to `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json`. |
| Reviews look at the Wisp folder instead of your project | `ANTIGRAVITY_WORKSPACE` points at Wisp, or the assistant starts servers in the Wisp folder. Remove the variable or pass `workspace`. |
| `Skill registry directory not found` | `--skill-dir` or `ANTIGRAVITY_SKILL_DIR` points at a folder that doesn't exist. |
| `antigravity_status` shows no skills | A file in the `Skills/` folder in use is malformed; `skill_error` names it. Fix it or move it out. |
| Setup can't create `.venv` on Debian or Ubuntu | Install `python3-venv` and run setup again. |
| `Delegation payload too large for the Windows command line` | Windows caps the command line. Move long material into files and list them as artifacts, or use fewer playbooks. |
| Quota errors | Wait for `resets_in`, switch accounts, or set a quota hook. |
| Timeouts | Raise `--print-timeout` (or `ANTIGRAVITY_PRINT_TIMEOUT`) and, for Codex, `tool_timeout_sec`. |
| Exit 0 but no critique | Treated as a failure and retried; the report lists each attempt. |
