# Changelog

All notable changes to Wisp are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
semantic per component with the bridge/server version as the product version
(`tools/antigravity_bridge.py::WISP_VERSION`).

Component versions are tracked together: bridge + MCP server + viewer move as
one product version; the adversarial skill registry (`Skills/`) and the
delegation skill (`integrations/antigravity-delegation/SKILL.md`) version
independently; the Electron shell versions with the viewer.

## [Unreleased]

### Fixed
- A rejected widget-server POST drains its small request body before closing,
  so Windows clients receive the error response instead of a connection reset.
- A clean review that quotes text such as `RESOURCE_EXHAUSTED` is no longer
  misclassified as rate-limited; a clean exit whose only output is a quota
  message still fails over.
- `ResumeThread` failures are detected on 64-bit Windows instead of leaving
  the child suspended until the hard timeout.
- `--dry-run` prints exactly the command and payload a real run would use.
- The quota hook runs only after a rate-limit failure.
- POSIX runs kill the whole process group, including leftover grandchildren.
- The MCP server reads the widget's model selection from the configured live
  directory, and `antigravity_status` reports the same models reviews use.
- The live-registry lock yields once and never blocks a delegation.
- Widget server: the pywebview shell's JS API is connected; `--transparent`
  works; account switching reports "unsupported" off Windows; `--host ::1`
  binds IPv6 and accepts its own origin; capture save errors return a JSON
  error; the model list is cached when `agy` is missing; chat writes are
  serialized; rejected POSTs close the connection.
- Widget UI: streamed text is no longer dropped between frames; native-shell
  handlers are registered once; sparks are not leaked under reduced motion;
  chat ignores events from other runs; Electron `set-view` no longer passes
  NaN bounds.

### Changed
- Documentation reorganized so each topic has one home. The delegation skill
  moved from `.opencode/skills/` to `integrations/antigravity-delegation/`
  (version 3.0.0): standard Agent Skills front matter, no setup material, the
  same gates, request fields, routing table and reconciliation rules, with
  the plain playbook names. `docs/integrations.md` replaces `AgentSkill.md`
  as the setup guide, with Claude Code first, then Codex, `mcpServers`
  clients (Cline, Cursor, Roo Code) and opencode, plus the MCP server's
  environment variables, every command-line option and troubleshooting.
  `AGENTS.md` now covers only work on this repository; the playbook format
  moved to `CONTRIBUTING.md`, and the credential blocklist is documented in
  `SECURITY.md` (a test checks it against the bridge).
- Setup also prints how to install the delegation skill for Claude Code.
- Review playbooks have plain names: `plan-review`, `wiring-audit`,
  `claim-check`, `safe-implementation`, `data-integrity-review`,
  `ai-eval-review`, `security-review`, `performance-profiling`,
  `portability-review`, `docs-accuracy-review`, `rag-review`,
  `agent-workflow-review`, `pre-merge-review`, `root-cause-investigation`,
  `prompt-review`, `ui-review` and `design-second-opinion`. Each file lists
  its previous name under `aliases`, so existing requests keep working.
- Widget redesigned ("Glass Dock"): one frosted panel with the owl perched on
  its corner and a floating dock for views, the command palette, settings and
  window controls; plain-language status labels; a shared motion system
  (view transitions, entry animations, verdict count-up) that honors the
  Full / Gentle / Off setting and reduced motion; settings shown inside the
  panel.
- New widget actions: Ctrl+K command palette; stream filters, per-entry copy
  and jump-to-latest; verdict summary with Ask a follow-up and Copy answer;
  chat starter prompts, copy and retry; history search, pin and delete.
- `/api/runs` reports each run's task; chat threads can be deleted.
- `antigravity_bridge` is split into `antigravity_containment` and
  `antigravity_aggregate`; `antigravity_viewer` into `viewer_shell` and
  `viewer_platform`. Public names are re-exported.
- Skill files are named after the skills they define (`NN_<name>.md`).
- The Electron shell validates IPC senders and grants only clipboard-write
  permission to the widget origin.
- Tests are organized by subject with shared helpers; `pyproject.toml`
  replaces `ruff.toml` and the lint rule set is wider.
- README rewritten; the example delegation is now synthetic.
- Setup and MCP registration docs cover Windows, macOS and Linux, with a
  table of what works and what is tested on each. MCP registrations use
  absolute paths; the old `claude mcp add antigravity -- python
  tools/antigravity_mcp_server.py` only worked from inside the Wisp folder.
  `SECURITY.md` and the delegation playbook are
  rewritten in plain language, and stale facts are corrected (skill counts,
  retired verdict names, what a malformed skill file does, and that
  `.antigravity-reports/` must be ignored in your own project).
- Skill registry v4: all twelve playbooks rewritten to one authoring contract
  (Mission, Method, Checklist, Evidence standard, Severity guide,
  Skill-specific output, Anti-patterns, Done when), each with a standalone
  `brief` and routing rules that name the right skill for adjacent tasks.
- Payloads are compact: each active skill sends its brief and the path to its
  full file, the registry is a one-line-per-skill index, and one shared review
  protocol replaces the per-skill output mandates. Selecting every skill now
  fits the Windows command-line limit with room to spare.
- Reviews use one finding format (`F-001 · P1 · high · <category>`) and end
  with a machine-readable verdict block. Report schema version 2.
- Widget chat and hotkey asks without skills get short consultation rules
  instead of the review protocol, so a quick question gets a direct answer.

### Removed
- `AgentSkill.md`, superseded by `docs/integrations.md` and the delegation
  skill.
- The repository's `opencode.json` and its Windows launcher
  `tools/antigravity_mcp.cmd`. They only worked on Windows and only inside
  the Wisp folder; register the `.venv` Python and
  `tools/antigravity_mcp_server.py` directly, as setup prints.

### Added
- One setup for every OS: `setup.bat` (Windows) and `./setup.sh` (macOS and
  Linux) find Python 3.10+ and run `tools/wisp_setup.py`. It creates `.venv`,
  installs the pinned dependencies, validates the playbooks, finds `agy` (on
  `PATH` or in `~/.gemini/bin`) and reports its version, installs the
  Electron overlay with `npm ci` when Node.js 18+ is present (by default only
  on Windows), and prints ready-to-paste MCP registrations for Claude Code,
  Codex and `mcpServers` clients with the machine's absolute paths. Options:
  `--check` (report only), `--dev` (test tools plus the suite), `--widget` /
  `--no-widget`, `--recreate-venv`. It never elevates, installs nothing
  globally and only writes `.venv/` and `tools/wisp_shell/node_modules/`.
  CI runs it from a clean checkout on Windows, Ubuntu and macOS.
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- `CONTRIBUTING.md`, issue and pull request templates, Dependabot, and
  `.editorconfig`.
- Five new playbooks: `pre-merge-review`,
  `root-cause-investigation`, `prompt-review`,
  `ui-review` and `design-second-opinion`.
- Envelope `mode` (`review`, the read-only default, or `implement`), also as
  `--mode` and the MCP `mode` parameter. A skill that requires write access
  warns when run in review mode.
- `review_verdict` (verdict, confidence, summary, P0-P3 counts, must-fix IDs)
  is parsed into results, reports and MCP output, and shown at the top of the
  critique; a missing or invalid block is a warning.

## [1.1.0] — 2026-10-07

Hardening release focused on failure semantics, trust boundaries, evidence
quality and release engineering. No new features.

### Security
- **Containment**: `AssignProcessToJobObject` return value is now checked; the
  achieved containment mode is recorded per attempt and surfaced in results,
  reports, and warnings. Children are created `CREATE_SUSPENDED` and resumed
  after job assignment, closing the assign-window race. Tree termination is
  belt-and-braces: `taskkill /F /T` (live parent→child snapshot) plus
  `TerminateJobObject` — this closed a *verified* escape where grandchildren
  did not inherit job membership in some interpreter environments.
- **Viewer artifact containment**: `/api/ask` image paths must resolve inside
  the workspace or capture directory with an image extension; arbitrary local
  paths are rejected instead of being normalized into delegation artifacts.
- **Viewer network posture**: non-loopback `--host` binding requires an
  explicit bearer token (`--auth-token` / `--generate-token`); every route is
  then token-gated, `/health` is deliberately minimal, and a `Host`-header
  check defends against DNS rebinding on loopback.
- **Viewer body limits**: `Content-Length` is validated (missing/invalid/
  negative/oversized) before any body byte is read; global 32 MB ceiling.
- **Viewer replacement safety**: the manifest carries a per-instance token;
  `--replace` uses an authenticated shutdown handshake and only falls back to
  `taskkill` after a PID-image sanity check — a stale PID can no longer kill
  an unrelated process.
- **Environment hygiene**: blocklist extended (`GOOGLE_APPLICATION_CREDENTIALS`,
  `GOOGLE_API_KEY`, `HF_`/`HUGGINGFACE`, `GIT_ASKPASS`, `SSH_ASKPASS`) plus
  interpreter/PM injection vectors (`NODE_OPTIONS`, `PYTHONPATH`,
  `PYTHONHOME`, `PYTHONSTARTUP`, `NPM_CONFIG_USERCONFIG`); docs now describe
  the policy honestly as blocklist hygiene, not a sandbox.
- **Workspace instruction trust**: the delegation payload now classifies the
  entire mounted workspace (including `AGENTS.md`) as untrusted evidence and
  removes the instruction to adopt workspace docs as protocol.
- **Electron shell**: explicit `sandbox: true`, navigation policy restricted
  to the local Wisp origin, popup/window-open denied, log rotation, and
  documented danger of `WISP_DEBUG_PORT`.
- **Viewer account exposure**: detected account email is masked and labeled
  `log-heuristic` in `/api/status`.

### Fixed
- **Success semantics**: an exit-0 run whose only output is a stderr
  diagnostic no longer counts as `SUCCESS`; stdout must carry content, and
  stderr-only runs are retried as transient failures.
- **Envelope contract**: `recommended_skills` survives the CLI JSON-envelope
  path and gained a `--recommended-skills` flag (it was silently dropped
  before, contradicting the documented contract).
- **Registry resolution parity**: `--status`, `--list-skills`, and
  `--dry-run` now resolve the skill registry exactly like the runtime
  (shipped-registry fallback included); `--status` exits non-zero when the
  registry is broken, making it usable as a health gate.
- **Report persistence**: reports are written atomically (temp file, fsync,
  `os.replace`) and pruned by count (`--report-keep`, default 50).
- **Aggregation**: `tool_result` payloads now surface in the organized
  critique; inferred final responses (no authoritative `result` envelope) are
  explicitly flagged; duplicate action fragments are counted, not silently
  dropped; delta coalescing is linear (incremental accumulation instead of
  per-delta re-join); the dead in-memory queue is gone; `combined_output` is
  cached.
- **MCP protocol**: strict JSON-RPC 2.0 handling (`-32600` for malformed
  frames and foreign versions, `-32602` for invalid params), explicit
  protocol-version negotiation against a supported list, runtime bounds
  mirroring the new schema `maxLength`/`maxItems`, and env parsing that
  degrades with `config_warnings` instead of failing the tool call.
- **Live feed**: emission happens under the sequence lock (seq order equals
  write order under thread concurrency); the registry is protected by an
  OS-level file lock across processes; retention never deletes runs modified
  within the active-run grace window; over-depth content is preserved as
  `raw` events instead of being dropped.
- **Chat**: persistence failures return `False` and log to stderr (no more
  silent loss); thread files are schema-sanitized on load; roles are
  constrained to `user|assistant`; message size and per-thread history are
  bounded; fake-thread cleanup is authoritative on `meta.fake` with content
  markers demoted to an explicit legacy mode.
- **Capture**: clipboard HGLOBAL ownership is released on every failure path
  (`GlobalFree`); capture filenames carry a random suffix (no same-second
  collisions); docs no longer claim image clipboard state is preserved.
- **Viewer replay** streams the run file line-by-line instead of loading it
  whole; selected models are validated against the inventory (custom ids
  require explicit `allow_custom`).

### Added
- `schema_version` on reports, live run-start events, and registry entries;
  full provenance block per report (Wisp version, git commit, agy version,
  platform, Python, registry hash, skill versions, timestamp).
- Real subprocess-fault integration suite (pipe flooding, grandchild-tree
  kill, interleaved streams, partial output on kill).
- Skill-registry governance tests (frontmatter, unique names, semver,
  keyword-routing collisions, banned patterns, loader round-trip) and a
  documentation-canonicalization test that fails when `AgentSkill.md` §2
  drifts from the canonical delegation skill.
- CI (Windows + Ubuntu, Python 3.10/3.13): ruff, `compileall`, skill
  validation, Node syntax check, full pytest suite.
- JSON-RPC negative-protocol test suite; cross-process registry test.
- `SECURITY.md` with the explicit trust model.

### Changed
- Pin runtime/dev dependencies exactly; `ruff.toml` gate (Pyflakes + E9).
- README wording corrected: Wisp *compacts* the wire representation and
  retains the complete raw record on disk (it does not impose a hard output
  ceiling); containment is described as process-tree termination, not a
  sandbox. Full threat model in `SECURITY.md`.
- Example delegation moved to `examples/delegation-case-study.json`; all
  documentation uses placeholders instead of machine paths.

### Fixed (follow-up)
- `tool_result` dedupe was missing in the step loop, so duplicate results
  repeated without bound.
- A resume failure of any exception class terminates the suspended child's
  tree before propagating, not only `OSError`.
- The registry lock seeks to byte 0 before `msvcrt.locking`, so lock and
  unlock always cover the same byte.
- `_prune_reports` never deletes the report it just wrote.
- Process-tree termination also walks live descendant PIDs from a process
  snapshot, so orphaned grandchildren are killed after their parent exits
  (residual limitation documented in `SECURITY.md`).
- The forced-kill fallback for `--replace` requires the viewer's command line,
  not just a Python-like image name.
- Query-string bearer tokens are accepted only on `/events`; `/api/shutdown`
  requires the operator bearer (when configured) and the instance token.
- Capture artifacts are accepted only from workspace-internal roots.
- Clipboard memory ownership is released on unexpected-exception paths.

## [1.0.0] — 2026-10-05

Initial release: delegation bridge (containment, capture, retries, quota
failover, live feed), MCP stdio server (`antigravity_review`,
`antigravity_status`, `antigravity_skills`), local viewer with SSE + replay +
chat, Electron widget shell, 12-skill adversarial registry, deterministic
test suite.
