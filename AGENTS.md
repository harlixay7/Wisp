# AGENTS.md — Antigravity Delegation Charter

Harness-agnostic governance for the calling agent (any coding harness) delegating adversarial
review to the Google Antigravity CLI (`agy`) through `tools/antigravity_bridge.py`.

---

## 1. The Core Triad

| Axis | Definition |
| --- | --- |
| **WHAT** | A delegation bridge that spawns `agy` as a contained sub-process and returns its complete, untruncated critique to the calling agent. |
| **WHY** | The calling agent is optimistic by nature and writes the code it grades. Antigravity is an independent adversary: it stress-tests plans, audits ASTs and wiring on disk, hunts crash/concurrency failure modes, falsifies empirical claims, and produces fix lists. |
| **HOW** | The calling agent authors a JSON delegation envelope → the bridge assembles the payload (skills + context + claims + artifacts) → `agy` runs inside an OS containment boundary with a credential-hygienic environment → stdout/stderr are captured in full, aggregated into Markdown, and every raw line is surfaced back. |

A green test suite is necessary but not sufficient. **Process success ≠ task success.**
Verification succeeds only when the deterministic gates pass AND the adversarial
critique has been ingested, reconciled, and resolved.

---

## 2. Module Boundaries

| Path | Responsibility |
| --- | --- |
| `tools/antigravity_bridge.py` | Envelope, payload and command building, env sanitization, retries, quota failover, reports, CLI. |
| `tools/antigravity_containment.py` | Process containment: Windows Job Objects and descendant tree kill, POSIX process groups. |
| `tools/antigravity_aggregate.py` | Turns the `stream-json` event stream into the organized critique. |
| `tools/antigravity_mcp_server.py` | MCP stdio server exposing `antigravity_review`, `antigravity_status`, `antigravity_skills`. |
| `tools/antigravity_live.py` | Live event feed (stream parser, NDJSON sinks, retention, cross-workspace registry). |
| `tools/antigravity_viewer.py` + `.html` + `.cmd` | Local widget server and UI (SSE, 6 modes, chat, settings, replay); optional and read-only with respect to the engine. |
| `tools/viewer_shell.py` | Widget window shells: Electron, pywebview and Chromium app windows. |
| `tools/viewer_platform.py` | OS process helpers and account switching used by the widget server. |
| `tools/wisp_chat.py` | Persistent chat threads for operator asks. |
| `tools/wisp_capture.py`, `tools/capture/*.ps1` | Selection, snip and pasted-image capture (Windows). |
| `tools/wisp_shell/` | Electron shell hosting the widget as a transparent desktop overlay. |
| `tools/antigravity_mcp.cmd` | Windows launcher for the MCP server. |
| `setup.bat`, `setup.sh`, `tools/wisp_setup.py` | Cross-platform setup and environment check (standard library only; `--check` changes nothing). |
| `tools/viewer_assets/*.png` | Creature state art (black-background, screen-blended). |
| `tools/skill_loader.py` | Registry discovery, metadata-contract validation, identifier resolution, prompt rendering. |
| `Skills/NN_<name>.md` | Adversarial skill definitions (Markdown with YAML front matter; YAML also accepted). |
| `.opencode/skills/antigravity-delegation/SKILL.md` | Delegation skill: MCP-first invocation, envelope fields, reconciliation rules. |
| `opencode.json` | Registers the delegation MCP server with a long-running tool timeout. |
| `tests/` | Deterministic, offline suite; no test invokes the real `agy`. |

Hard rules for these modules:

- No truncation of `agy` output anywhere in the data path.
- No `TODO`/stub code; every function is fully implemented and typed.
- No machine-specific absolute paths; resolution uses `shutil.which` + `Path.home()`.
- No API keys in child environments; authentication is Google OAuth managed by `agy`.

---

## 3. Mandatory Delegation Gates

The calling agent **MUST** invoke the bridge before proceeding when any gate fires:

1. **Plan generation gate** — before starting any multi-file feature, refactor, or
   architectural change. Delegate the plan itself, not its completion
   (`adversarial-plan-hardening-engine`; add `independent-design-second-opinion`
   when the approach itself is open).
2. **High-risk seam gate** — any change touching concurrency, async scheduling,
   persistence/transactions, process spawning, IPC, caching, or destructive
   filesystem operations (the matching domain skill from §6).
3. **Pre-commit audit gate** — before declaring a task complete or merging, delegate
   the diff plus the acceptance criteria (`pre-merge-diff-audit`).
4. **Repeated-failure gate** — after **two** consecutive failed attempts to fix the
   same root-cause area, stop writing code and delegate with the failure evidence
   (`root-cause-failure-investigation`).

Delegation is **not** required for trivial edits (docs, typos, formatting) or when the
critique received in the last 30 minutes already covers the exact change set.

---

## 4. Invocation Envelope Schema

The standardized JSON prompt wrapper (`--envelope envelope.json`):

```json
{
  "prompt": "Required. The plan, question, diff summary, or claim under review.",
  "harness": "opencode | claude-code | cline | agent-1 (default)",
  "context": "Prior art, constraints, failed attempts, or relevant briefing.",
  "claims_to_falsify": [
    "Enqueue is O(1) under 10k concurrent producers",
    "The server survives 100 concurrent websocket reconnects"
  ],
  "artifacts": [
    "src/scheduler.py:1-240",
    "tests/test_scheduler.py",
    "evidence/screenshot-1.png"
  ],
  "skills": ["adversarial-plan-hardening-engine"],
  "recommended_skills": ["zero-trust-ast-wiring-verifier"],
  "mode": "review",
  "notes": "Operator steering that must not be ignored."
}
```

Field rules:

- `prompt` is mandatory and non-empty; the envelope is rejected otherwise.
- `claims_to_falsify` and `artifacts` accept a string or a list of strings.
- `skills` is optional: the necessary/primary selection (one or more names, or
  `"all"`); these render as mandatory instructions.
- `recommended_skills` is optional: up to 3 task-dependent names; they render as
  apply-when-relevant guidance and are deduplicated against `skills`. Each selected
  skill travels as its `brief` plus the absolute path of its full file, which
  Antigravity reads from disk; the payload also carries a compact index of every
  skill (name, purpose, path). Briefs beyond the inline budget degrade to
  path-only entries with a warning, so no selection can overflow the command line.
- `mode` is optional: `review` (default; read-only, changes are proposed as
  unified diffs) or `implement` (the reviewer may modify files inside the
  workspace). A skill whose `input_contract` declares `write_access: required`
  stays in review mode unless `implement` is requested, and a warning is recorded.
- Unknown fields are ignored (forward compatible).

Preferred transport: the MCP tool `antigravity_review` (registered via `opencode.json`),
which wraps the same engine and persists every full report under
`<workspace>/.antigravity-reports/`. The CLI below remains the fallback and the
reference implementation.

Direct CLI invocation (same semantics):

```bash
python tools/antigravity_bridge.py \
  --prompt "Review this migration plan" \
  --context "SQLite WAL, single writer" \
  --claim "No migration takes longer than 5s" \
  --artifact "migrations/0042_add_index.sql" \
  --skills all --workspace . --json
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--skills all` / `--skills a,b` | Activate registry skills by selector. |
| `--json` | Emit the complete result (critique + both raw streams) as JSON. |
| `--dry-run` | Print the exact command and payload without executing `agy`. |
| `--list-skills` | List registry skills and exit. |
| `--model` / `--fallback-model` | Override the default `gemini-3.8-flash-high` → `claude-opus-4-6-thinking` chain (do not downgrade). |
| `--print-timeout` / `--grace-seconds` | `agy --print-timeout` and the hard-kill grace window. |
| `--retries` / `--retry-backoff` | Transient-failure retries per model (default 2, exponential backoff). |
| `--quota-wait` / `--quota-hook` | Wait for a parsed quota reset window, or run an operator account-switch command on exhaustion. |

### Hosting the MCP server in any harness

Register `<wisp>/.venv/bin/python` (Windows: `<wisp>\.venv\Scripts\python.exe`)
running `<wisp>/tools/antigravity_mcp_server.py` as a local stdio MCP server, by
absolute path, in each harness you use — opencode: `opencode.json`; Codex:
`~/.codex/config.toml`; Claude Code: `claude mcp add --scope user`; Cline: its MCP
settings JSON. `setup.bat` / `./setup.sh` print these entries with the machine's
real paths. The server reviews its working directory unless `ANTIGRAVITY_WORKSPACE`
is set or the call passes `workspace`. On Windows, `tools/antigravity_mcp.cmd`
(run via `cmd.exe /c`) also works; it pins the top-tier model chain and defaults
`ANTIGRAVITY_WORKSPACE` to the Wisp repo. The repo's `opencode.json` uses that
launcher, so it is Windows-only. Harnesses spawn registered servers automatically at startup and terminate
them at exit; there is nothing to start manually and no background service.
Harnesses without MCP support use the CLI fallback, which also persists the
complete report under `.antigravity-reports/`.

---

## 5. Ingestion & Reconciliation Rules

When Antigravity returns, the calling agent MUST:

1. **Start from the verdict.** The reviewer ends with a `WISP_VERDICT` block that
   the bridge parses into `review_verdict` (PASS, PASS_WITH_FIXES or BLOCK, the
   severity counts and the `must_fix` list) and shows at the top of the critique.
   A successful run without it is an incomplete review: re-delegate.
2. **Display the complete raw critique** in its working context. Never truncate,
   summarize away, or elide the critique, stdout, or stderr. The bridge already
   appends the verbatim streams; present them.
3. **Enumerate every objection** as an explicit reconciliation row keyed by its
   finding ID: `F-00N → VERDICT (ACCEPTED | REJECTED) → EVIDENCE → ACTION`. Every
   `must_fix` ID must end ACCEPTED with a landed fix or REJECTED with evidence.
4. **Never silently drop an objection.** A rejection requires a counter-citation
   (file:line, command output, or recomputation), not an assertion.
5. **Apply accepted fixes** before re-running the deterministic verification gates.
6. **Re-delegate** only the unresolved deltas, citing the prior critique.
7. **Fail loudly** if the bridge reports `FAILED`: surface the full exit code, the
   bridge error, and the preserved streams — do not proceed as if review happened.

A delegation is only complete when every row has a verdict and every ACCEPTED row has
a landed fix with fresh verification evidence.

---

## 6. Skill Registry

Location: `Skills/` (override with `--skill-dir`). When a workspace has no
`Skills/` directory, the bridge falls back to the registry shipped with it
(`<bridge repo>/Skills`) and records a warning in the report; the fallback
registry is also mounted via an extra `--add-dir` so Antigravity can read the
files. Definitions may be YAML
(`.yaml`/`.yml`) or Markdown (`.md`/`.markdown`) with a YAML front-matter block; for
Markdown the body becomes the `instructions_payload` unless the front matter declares
one. Rich-text export artifacts (escaped markdown, `&#x20;` spaces) are normalized
automatically. Empty placeholder files are skipped with a warning, never fatal.

Every definition must carry all mandatory metadata fields (`name`, `version`,
`description`, `activation_triggers`, `input_contract`, `output_contract`,
`instructions_payload`) or loading fails loudly. `activation_triggers` may be a
string, a list, or a mapping of lists (e.g. `task_modes` + `keywords`); mapping
values are flattened.

Core roster:

| Situation | Primary skill |
| --- | --- |
| Plan, RFC or architecture before code is written (plan gate) | `adversarial-plan-hardening-engine` |
| You want an independent alternative and a recommendation, not an attack | `independent-design-second-opinion` |
| A diff is ready to commit or merge (pre-commit gate) | `pre-merge-diff-audit` |
| Two failed fixes on the same problem (repeated-failure gate) | `root-cause-failure-investigation` |
| Existing code: is every feature really wired end to end | `zero-trust-ast-wiring-verifier` |
| Make the change itself (implement mode; diffs only in review mode) | `zero-regression-surgical-implementation` |
| Schemas, migrations, serialization, transactions, state machines | `data-contract-state-integrity-engine` |
| Trust boundaries, injection, secrets, authorization, agent permissions | `runtime-security-vault-engine` |
| A performance, cost or accuracy number that needs re-deriving | `empirical-claim-falsification-engine` |
| Slow code, stalls, memory or tail latency that needs profiling | `telemetry-hardware-profiling-gate` |
| Agent loops, tool schemas, MCP surfaces, budgets, handoffs | `agentic-tool-dag-orchestration-engine` |
| Prompts, system prompts, AGENTS.md / CLAUDE.md, tool descriptions, skills | `prompt-context-engineering-audit` |
| A prompt or model change that needs eval evidence before shipping | `ai-eval-regression-engine` |
| Retrieval-augmented generation: chunking, retrieval, grounding | `hybrid-rag-retrieval-grounding-engine` |
| UI changes, screenshots, visual and interaction quality | `interface-craft-audit` |
| README, guides and quickstarts versus actual behavior | `documentation-retraction-ledger-engine` |
| Fresh-clone setup, portability, lockfiles, release hygiene | `git-hygiene-portability-gate` |

Authoring a new skill: create `Skills/NN_<name>.md` with YAML front matter, fill
every required field plus a standalone `brief` (600–1800 characters, defining what
PASS, PASS_WITH_FIXES and BLOCK mean for the task), and write the body in the
standard sections (Mission, Inputs to establish first, Method, Checklist, Evidence
standard, Severity guide, Skill-specific output, Anti-patterns, Done when). Skills
never restate the shared review protocol; the bridge sends it once. Files marked `kind: template` are never auto-selected by
`--skills all`.

The bridge embeds the registry path in the Antigravity payload; the workspace is
mounted via `--add-dir`, so the agent can read any raw skill file in full.

Validate the registry at any time:

```bash
python -m tools.skill_loader --validate
python tools/antigravity_bridge.py --list-skills
```

---

## 7. Operational Safety Guarantees

- **Containment** — Windows: Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`;
  POSIX: new session + process-group kill. Killing the bridge kills the whole tree
  (including servers or test harnesses `agy` spawned).
- **Credential hygiene (blocklist)** — credential-bearing variables matching
  `AWS_*`, `AZURE_*`, `GITHUB_*`, `GH_*`, `SSH_*`, `OPENAI_*`, `ANTHROPIC_*`,
  `GEMINI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_API_KEY`,
  `HF_*`/`HUGGINGFACE*`, `GIT_ASKPASS`, `SSH_ASKPASS`, plus the
  interpreter/package-manager injection vectors `NODE_OPTIONS`, `PYTHONPATH`,
  `PYTHONHOME`, `PYTHONSTARTUP`, and `NPM_CONFIG_USERCONFIG`, are stripped
  from the child environment. The authoritative enumeration is
  `tools/antigravity_bridge.py::BLOCKED_ENV_PREFIXES` / `BLOCKED_ENV_EXACT`;
  this is hygiene hardening, not a sandbox (see `SECURITY.md`).
  `~/.gemini/bin` is prepended to `PATH`. OAuth state stays in the OS
  credential store / `~/.gemini` cache; the bridge never reads or transmits it.
- **Transient recovery** — connection resets, 5xx, and empty responses are retried
  with exponential backoff. An exit-0 run with no output is treated as a failure,
  never as success.
- **Quota failover** — signatures `RESOURCE_EXHAUSTED`, `code 429`,
  `Individual quota reached`, `Rate limit exceeded` trigger an automatic re-dispatch
  to `claude-opus-4-6-thinking` with the payload unchanged. `--quota-wait` waits for a
  parseable reset window; `--quota-hook` runs an operator credential-rotation command
  (e.g. switching the Google account). Reports surface `resets_in`/`reset_seconds`.
  Exhaustion on both models fails loudly with all output preserved.
- **Timeouts** — `agy --print-timeout` governs the model; the bridge hard-kills at
  `print-timeout + grace-seconds` and still returns every captured line.
- **MCP inheritance** — `agy` manages MCP servers itself (`agy mcp add/remove/list/enable/disable`,
  config merged from `~/.gemini/config/mcp_config.json`). The bridge never strips
  config-discovery variables and containment kills any stdio MCP server `agy` spawns.
- **Operator hotkey asks** — while the Wisp widget runs, `Ctrl+Alt+Q` sends the
  current text selection (or a snip) to Antigravity, and `Ctrl+Alt+E` pre-fills the
  chat composer with the capture. These delegate through the same engine and live
  feed; the MCP server and agent workflow are unchanged. Screenshots are saved under
  `.antigravity-reports/captures/` (last 50 kept) and conversations persist under
  `.antigravity-reports/live/chat/`. The chat composer also accepts pasted images
  (`Ctrl+V`): each is stored under `captures/` and attached to the ask as an
  artifact alongside the prompt.
- **Widget input model** — the Electron shell keeps a fixed transparent canvas and
  toggles `setIgnoreMouseEvents` from the cursor feed (interactive only over visible
  UI), so translucent pixels stay clickable and empty canvas clicks pass through to
  the desktop. Navigation lives on the widget itself: the Wisp (emotion) tab shows
  the six modes as glass nodes orbiting the creature (the orbit container is the
  native drag surface; each node is a `no-drag` button so clicks always land —
  never center transformed elements inside drag regions, Chromium computes region
  holes from untransformed layout). The drawer, menus, backdrop, replay bar, and
  toast punch explicit `no-drag` holes so overlaid buttons always receive clicks.
  Elsewhere the creature and controls bar drag natively; the pywebview shell falls
  back to IPC dragging (`wisp:move`).
- **Cross-workspace live visibility** — every live-enabled delegation registers
  its live directory in `~/.antigravity-reports/live-registry.json`
  (`ANTIGRAVITY_LIVE_REGISTRY` overrides; atomic writes, missing dirs pruned,
  50-entry cap). The widget watches its own live dir plus every registered dir,
  so runs from any workspace (any harness, any cwd) appear in the live stream,
  the History list (labelled by workspace), and replay. The SSE tail latches
  onto the active run until it ends or goes idle for 15s, so concurrent runs
  cannot thrash the view.
- **Test-mode hygiene** — when `WISP_ASK_FAKE=1`, answers are prefixed with
  `[TEST MODE]` and flagged in message metadata; `/api/status` reports
  `test_mode` and the widget shows a TEST MODE badge. `python tools/wisp_chat.py
  --live-dir <dir> --clean-fake` deletes canned threads (legacy markers included).
- **Zero quota in CI** — tests use scripted launchers; no test invokes real `agy`.

---

## 8. Quick Reference

```bash
./setup.sh --check                                   # environment report (Windows: setup.bat --check)
python -m tools.skill_loader --validate              # validate registry
python tools/antigravity_bridge.py --list-skills     # list skills
python tools/antigravity_bridge.py --prompt "..." --skills all --dry-run   # inspect dispatch
python tools/antigravity_bridge.py --prompt "..." --skills all            # delegate
python tools/antigravity_bridge.py --envelope envelope.json --json        # machine-readable
python -m pytest tests/ -q                           # deterministic suite (~1 min)
```

Reconciliation checklist before completion:

- [ ] All four delegation gates respected.
- [ ] Antigravity's complete critique displayed, none of it dropped.
- [ ] Every objection adjudicated with verdict + evidence.
- [ ] Accepted fixes landed; deterministic suite re-run and green.
- [ ] Bridge verdict is `SUCCESS`; any `FAILED` was escalated, not ignored.
