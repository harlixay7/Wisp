---
name: zero-trust-ast-wiring-verifier
version: 4.0.0
description: >-
  Use when auditing code that already exists, a module or a whole repository,
  for whether its features are actually connected: every ingress traced to its
  effect, half-wired flags, settings, routes and handlers, stubs or mocks
  reachable in production, dead exports, swallowed errors, cross-module or
  cross-language type mismatches, and tests that never execute what they claim
  to cover. Produces a wiring map and a surface parity matrix. Not for judging
  a specific diff before merge (use pre-merge-diff-audit), not for plans that
  are not yet code (use adversarial-plan-hardening-engine), and not for
  security exploitability (use runtime-security-vault-engine).
brief: |
  Mission: establish, read-only, which features are really connected from entry point to effect, and which are half-wired, stubbed, dead or silently failing.
  - Inventory every ingress from manifests and launchers, not just source: console scripts, package bin and main fields, CLI parsers, HTTP routes, IPC and RPC channels, queue consumers, UI events, scheduled jobs, MCP tools.
  - Trace each ingress forward to its sink (state, disk, process, network, UI) and each sink backward to an ingress. Every hop cites path:line.
  - Cross-match producers and consumers by name: flags defined versus read, config keys written versus read, env vars documented versus read versus set, events emitted versus handled, routes served versus called. Diff the sets.
  - Hunt stubs, fakes and simulated behavior reachable on default paths; swallowed errors that turn failures into plausible defaults; type and unit mismatches across module and language boundaries.
  - Before calling anything dead, rule out dynamic use: string dispatch, registries, decorators, entry points, templates and reflection. Tool output alone is low confidence.
  - Execute where cheap: --help, dry runs, import every module, coverage of the suite to see what tests really run.
  Output, before the findings: Ingress inventory, Wiring map (entry point, path, sink, status), Surface parity matrix, Stub and dead-code inventory.
  PASS = primary paths WIRED, no P0/P1. PASS_WITH_FIXES = gaps are local wiring fixes. BLOCK = a documented primary feature is not wired, or a stub or fake serves production traffic.
activation_triggers:
  task_modes:
    - CODE_WIRING_AUDIT
    - SYSTEM_WIRING_INSPECTION
    - REPOSITORY_AUDIT
  keywords:
    - wiring
    - callgraph
    - dead code
    - dead export
    - half-wired
    - unregistered handler
    - unread config
    - stub detection
    - entry point
    - ingress trace
    - swallowed error
    - import graph
  do_not_use_when:
    - The subject is a specific change set awaiting merge (use pre-merge-diff-audit).
    - The artifact is a plan or design with no code on disk (use adversarial-plan-hardening-engine).
    - The question is whether data schemas, migrations or transactions are correct (use data-contract-state-integrity-engine).
input_contract:
  requires_worktree: true
  required_inputs:
    - The scope to audit (repository, package, module or named features)
  optional_inputs:
    - Specific entry points or features suspected to be broken
    - Documentation that claims the supported surface (README, help text, settings reference)
    - Known dynamic-dispatch conventions in the codebase
output_contract:
  sections:
    - Ingress inventory
    - Wiring map
    - Surface parity matrix
    - Stub and dead-code inventory
  findings: shared format
  verdict: shared verdict block
---

# Zero-trust wiring verifier

## Mission

Produce a map of how this code actually connects at runtime and the list of places
where the claimed surface and the real surface disagree. The consumer is a
maintainer or agent deciding what is safe to rely on, finish or delete. The most
common failure is a grep-only audit that declares code dead when it is reached
through a string-keyed registry, a decorator side effect, a manifest entry point or
an HTML attribute, and that declares a feature wired because a function with the
right name exists.

## Inputs to establish first

- Scope. If the request names features, audit those end to end plus their shared
  infrastructure; otherwise inventory everything and trace the primary paths first.
- The claimed surface: README, help text, settings and environment references, API
  docs, UI labels. Wiring gaps are measured against claims.
- The launch surface: `pyproject.toml` `[project.scripts]` and entry-point groups,
  `setup.cfg`, `package.json` `bin`, `main` and `scripts`, Electron main and preload
  files, HTML script tags, `Dockerfile` `CMD`, Procfile, service units, cron or CI
  schedules, shell, `.cmd` and `.ps1` launchers, MCP and editor config files.
- The dynamic-dispatch conventions in use (registries keyed by string, plugin
  discovery, framework decorators, reflection). Record them before calling
  anything unreferenced.

## Method

1. Ingress inventory. Enumerate every way execution starts, by kind, with
   path:line. Done when each launch-surface file has been read and every ingress it
   names is listed.
2. Declared surface inventory. Extract CLI options (`add_argument`, click or typer
   decorators, yargs), config keys (defaults dicts, schema models, settings
   loaders), environment variables (`os.environ`, `os.getenv`, `process.env`),
   routes, IPC channels (`ipcMain.handle`, `ipcRenderer.invoke`, `postMessage`
   types), event names (`emit`, `on`, `addEventListener`, custom event types),
   registry keys and feature flags. Done when each item has its definition site.
3. Trace. Follow each primary ingress forward hop by hop to its sinks; from each
   important sink (file write, subprocess spawn, network send, persisted state,
   rendered UI) trace backward. Use `rg -n`, language-server references, a small
   `ast` script listing definitions and name references, and import-graph tools
   (`pydeps`, `madge`, `python -X importtime`). Treat `vulture`, `knip`, `ts-prune`,
   `deadcode` and similar as candidate generators, never verdicts. Done when every
   primary ingress has a status.
4. Cross-match. For each declared item, find its consumers; for each consumer, find
   its producer. Compute set differences per channel. Done when each declared item
   is read, written and documented, or flagged.
5. Execute cheaply. Run `--help` and dry-run modes; import every module in the
   package to catch import-time failures in rarely loaded files; run the suite
   under coverage (`coverage run -m pytest`, `c8`, `nyc`) and check whether code
   that tests claim to cover is actually executed. Done when each executed check
   has recorded output.
6. Classify and report. Assign each wiring row a status and promote mismatches with
   a user-visible consequence to findings.

## Checklist

Half-wired surfaces
- A CLI option parsed but its attribute never read, or read only under a branch
  that its default can never reach.
- Several front ends (CLI, server endpoint, UI form, MCP tool) build the same
  request but expose different option sets or different defaults: build a parity
  row per option.
- A config key with a default but no reader, or a reader of a key that nothing
  writes and that has no default, so it is always the fallback.
- An environment variable documented but never read, read under a misspelled name,
  or set by a launcher script that the documented launch path does not use.
- Event, IPC or message names that differ between emitter and handler by case,
  prefix, namespace or pluralization; a handler registered after the one-time
  event it waits for has already fired.
- A frontend calling a path, method or payload shape the server does not serve.
- Registration by import side effect (decorators filling a registry) where the
  module is never imported on the production path, so the handler never exists.
- Interfaces, protocols or abstract methods with no concrete implementation, and
  implementations that a factory or selector can never choose.

Stubs, fakes and simulation on live paths
- `raise NotImplementedError`, `pass` or `...` bodies in concrete methods,
  functions returning constants or canned payloads, hard-coded sleeps that simulate
  work, `TODO` on the executed path.
- Fake or test-mode clients selected by an environment default or by a missing
  setting rather than by an explicit test opt-in; check what happens when the
  variable is absent.
- Distinguish legitimate cases and clear them: abstract bases, `Protocol` classes,
  documented null-object implementations, platform branches for other operating
  systems.

Dead and divergent code
- Definitions with no reference after accounting for dynamic use, orphan files
  never imported, branches whose condition is made impossible by upstream
  normalization, exports in `__all__` or `index` files that nothing imports.
- Two implementations of the same helper that have drifted apart, with production
  using one and tests using the other.

Swallowed and laundered errors
- `except Exception: pass`, `.catch(() => {})`, promises with no rejection handler,
  `contextlib.suppress` around more than the single expected call.
- Errors converted into plausible values: returning `[]`, `{}`, `False` or `None` on
  failure, so callers cannot distinguish "empty" from "broken".
- Subprocess results with the return code ignored (`check=False` and no
  `returncode` test), HTTP responses used without a status check.
- Log-and-continue handlers that leave partially updated state.

Type and unit boundaries
- Python to JavaScript JSON: snake_case versus camelCase keys, integer versus
  string identifiers, `null` versus absent fields, timestamp format and timezone.
- Units: seconds versus milliseconds, bytes versus kilobytes, 0-based versus 1-based.
- `bytes` versus `str` on subprocess pipes and sockets, and the encoding used to
  decode them; path types and separators crossing OS boundaries.
- Enum or status string sets defined separately in backend and frontend that have
  diverged.
- Optional returns whose callers dereference without a check.

Tests that do not test the wiring
- Tests that patch the very function under test, exercise a helper the production
  path does not call, import a copy, or assert inside a callback that never runs.
- Test files the runner does not collect, or tests skipped on the platform CI uses.
- Coverage shows the claimed module at or near zero lines executed.

## Evidence standard

- WIRED: each hop cited path:line, or the path executed with observed output.
- DEAD: the exact searches run (identifier and its string form, including manifest,
  HTML, config and docs files) with zero hits, plus a statement of which dynamic
  dispatch mechanisms were checked. Static-tool output alone is low confidence.
- Parity gaps: the definition site and the absence (search command and result) or
  the mismatching consumer site.
- Executed checks (imports, `--help`, coverage) are quoted with their command.

## Severity guide

- P0: a documented primary feature does nothing or silently uses a stub or fake in
  the default configuration; a swallowed error on a primary path loses or corrupts
  data; a module on the main path fails to import.
- P1: a documented option, setting or environment variable is accepted but ignored;
  front ends disagree on a primary option; a type or unit mismatch produces wrong
  behavior with realistic input; tests that claim to cover a primary path never
  execute it.
- P2: dead exports and orphan files; swallowed errors on secondary paths; parity
  gaps on rarely used options; documentation naming a surface that does not exist.
- P3: duplicated helpers not yet diverged; unclear registration conventions that
  invite future wiring mistakes.

## Skill-specific output

Ingress inventory

| Kind | Name | Defined at | Launched by |
| --- | --- | --- | --- |

Wiring map

| Entry point | Path (hops, path:line) | Sink | Status | Evidence |
| --- | --- | --- | --- | --- |

Status is WIRED, PARTIAL (some branches or options not connected), BROKEN (the
chain is cut), STUBBED (ends in placeholder or fake), DEAD (unreachable from any
ingress) or UNVERIFIED (say what blocked verification).

Surface parity matrix

| Item (flag, key, env var, event, route) | Defined at | Read or handled at | Documented at | Status |
| --- | --- | --- | --- | --- |

Stub and dead-code inventory: one line per item with path:line, kind, and whether
it is reachable from a production ingress.

Fixes are proposed as unified diffs or precise edit descriptions; the audit itself
changes nothing in the workspace.

## Anti-patterns

- Grep-only death certificates. Rule: search the string form and manifests, and
  name the dynamic mechanisms checked, before calling code dead.
- Wired by name. Rule: a function existing is not a connection; show the caller
  chain from an ingress or an executed run.
- Flagging intentional abstractions. Rule: abstract bases, protocols, test doubles
  in test trees and documented no-op implementations go under "Checked and cleared".
- Dumping every `TODO`. Rule: list a placeholder only if it is reachable from a
  production ingress; others are a one-line count.
- Reporting tool output verbatim. Rule: every candidate from a static tool is
  confirmed or discarded by reading the code.
- Drifting into code style or architecture opinions. Rule: report connections and
  their failures; leave redesign to plan review.

## Done when

- [ ] Every launch-surface file was read and every ingress is inventoried.
- [ ] Every primary ingress has a wiring row with a status and hop citations.
- [ ] Flags, config keys, environment variables, events and routes are cross-matched.
- [ ] Dead-code claims list the searches and the dynamic mechanisms ruled out.
- [ ] Stubs and fakes are classified as reachable or not in the default config.
- [ ] Cheap executions (imports, help, coverage) were run and quoted.
- [ ] No workspace file was modified.
