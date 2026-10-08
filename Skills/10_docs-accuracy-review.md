---
name: docs-accuracy-review
aliases:
  - documentation-retraction-ledger-engine
version: 4.0.0
description: >-
  Use when documentation must be checked against the code and made trustworthy
  before a release, merge or publication: READMEs, quickstarts, CLI help,
  configuration references, changelogs, contributor guides and claims in
  them. Produces a doc-to-code parity table from executed commands and code
  lookups, a corrections ledger that records each falsified statement, and
  findings. Not for recomputing benchmark or performance numbers (use
  claim-check) or for packaging, paths and
  dependency pins (use portability-review).
brief: |
  Mission: make the documentation true and usable: every command works as written, every stated flag, default, environment variable and version matches the code, every claim is calibrated to evidence, and every correction is recorded.
  - Execute each documented command and snippet in a clean context (fresh clone or worktree, new virtual environment, project variables unset) and compare actual output and side effects with what the doc says. Skip destructive, paid or publishing commands and mark them not run with the reason.
  - Diff docs against code in both directions: flags and defaults from the argument parser, environment variables from the code that reads them, config keys, file locations, versions, error messages, public signatures; and code features the audience needs that are undocumented.
  - Behavior outranks help text: when `--help` and the code path disagree, the code path is the truth.
  - Check screenshots and diagrams against the current interface and architecture.
  - Calibrate claims: absolute wording (always, never, zero, guaranteed, secure, instant) needs proof; numbers need a source.
  - Remove marketing language and filler where it misleads or slows the stated audience; walk the newcomer path end to end.
  - Validate links and heading anchors.
  Emit a doc-to-code parity table and a corrections ledger (was, now, evidence, type, retraction-worthy) before the findings.
  PASS: all executed paths work and statements match code. PASS_WITH_FIXES: local mismatches with clear corrections. BLOCK: install or quickstart fails for the stated audience, a safety or security instruction is wrong, or a published claim is false.
activation_triggers:
  task_modes:
    - DOCUMENTATION_AUDIT
    - RETRACTION_LEDGER_CURATION
    - PUBLIC_RELEASE_AUDIT
  keywords:
    - readme audit
    - code-doc parity
    - retraction
    - corrections ledger
    - doc drift
    - broken link
    - quickstart walkthrough
    - overclaim
    - documentation accuracy
    - stale screenshot
  do_not_use_when:
    - A number in the docs needs independent recomputation or reproduction (use claim-check, then record its result here).
    - The problem is hardcoded paths, dependency pins or release packaging (use portability-review).
    - The artifact is an interface whose visual craft is under review (use ui-review).
input_contract:
  requires_worktree: true
  required_inputs:
    - The documents in scope (paths or the change set that touches them) and their intended audience
  optional_inputs:
    - Supported platforms and shells the docs must work on
    - An existing retraction or corrections ledger file
    - Network permission for external link checks
output_contract:
  sections:
    - Doc-to-code parity table
    - Corrections ledger
  findings: shared format
  verdict: shared verdict block
---

# Documentation accuracy and retraction ledger

## Mission

Establish, statement by statement, whether the documentation tells its reader the
truth about the software as it is now, and leave a durable record of every correction.
A strong result has executed the documented paths, cited the code that decides each
documented behavior, and turned vague or inflated claims into checkable ones. The most
common failure is reading docs and code side by side without running anything, which
misses missing prerequisites, shell differences and output that changed.

## Inputs to establish first

- Scope: which files (README, `docs/`, CHANGELOG, CONTRIBUTING, SECURITY, examples,
  CLI help, public docstrings) and which change set.
- Audience per document: newcomer installing for the first time, operator configuring,
  contributor, integrator calling an API. Each implies a different completeness bar.
- Supported platforms and shells. A command block for bash fails in PowerShell; if the
  project supports both, both need instructions.
- Whether commands may be executed and whether the network is available. If not,
  every command row becomes NOT_RUN with the reason, and confidence drops to medium.
- Whether the project keeps a corrections or retractions file, so ledger entries land
  in the right place in the right format.

## Method

1. **Inventory statements.** Extract every executable block, flag, environment
   variable, path, version, default and quantitative or absolute claim.
   `rg -n -- '--[a-z][a-z0-9-]+' README.md docs/`, `rg -no '\b[A-Z][A-Z0-9_]{3,}\b' README.md docs/ | sort -u`,
   fenced blocks by language tag. Done when each statement has an ID and a location.
2. **Locate the code truth.** For each statement, find what decides it:
   `rg -n 'add_argument\(|@click\.option|#\[arg|flag\.' ` for flags and defaults;
   `rg -n 'os\.environ|getenv|process\.env|env::var'` for variables; manifest files
   (`pyproject.toml`, `package.json`, `Cargo.toml`) and CI matrices for versions and
   platforms. Also search the reverse direction: required flags, new variables and
   raised errors the docs never mention. Done when each statement maps to path:line
   or is marked NOT_IN_CODE.
3. **Execute in a clean room.** `git worktree add <tmpdir> HEAD` or a fresh clone, a
   new virtual environment, project variables unset, the documented working directory.
   Follow the newcomer path literally, in order, without filling gaps from knowledge of
   the repo. Done when every block is marked ran-matches, ran-differs, failed or not-run
   with a reason.
4. **Check visuals and links.** Compare screenshots against the running interface or
   at least against UI change history (`git log -1 --format=%ci -- <image>` versus the
   UI source); check that components named in diagrams exist. Verify relative links
   resolve, anchors match generated heading slugs, and external links respond (lychee
   or `curl -sI`, when the network is permitted). Done when every image, diagram and
   link has a status.
5. **Calibrate claims and language.** Mark each absolute or quantitative claim with its
   evidence or its absence; route recomputation to claim-check
   and record the outcome. Rewrite only text that misleads or blocks the audience. Done
   when every claim has evidence, a calibrated rewrite, or a removal proposal.
6. **Write the ledger.** One entry per corrected statement, with evidence and whether
   it needs a visible retraction. Done when the ledger and parity table are consistent.

## Checklist

**Commands and snippets**
- Missing prerequisites: install step, virtual environment activation, build or
  migration step, a service that must be running, credentials the command silently needs.
- Wrong working directory or relative paths that only work from the top-level directory.
- Shell portability: `export X=1` versus `$env:X = "1"` versus `set X=1`; line
  continuation `\` versus backtick versus `^`; `python` versus `python3` versus `py`;
  path separators and quoting.
- Copy-paste hazards: `$ ` prompts inside blocks, smart quotes, non-breaking spaces,
  output lines mixed with commands, unmarked placeholders such as `YOUR_TOKEN`.
- Shown output that the command no longer prints, or exit codes that changed.
- Examples that import names that no longer exist or skip imports they need.

**Parity with code**
- Renamed or removed flags still documented; defaults changed in code but not in docs;
  choices or value ranges that differ; flags documented as optional that are required.
- Environment variable names, prefixes and precedence (flag over variable over config
  file) as implemented.
- Config file locations per OS, and what happens when the file is absent.
- Documented error messages and troubleshooting entries that match what the code raises.
- Minimum runtime versions consistent with the syntax used (`match` and `X | Y` runtime
  annotations need Python 3.10; optional chaining needs a modern Node) and with CI.
- Platform support claims backed by a CI matrix or explicit manual verification.
- CHANGELOG entries for user-visible behavior changes in the diff, including breaking ones.

**Claims**
- Absolutes: "never", "always", "zero", "guaranteed", "fully", "instant", "secure",
  "sandboxed". A hygiene measure described as a security boundary is a factual error.
- Numbers with no testbed, date or source; comparisons with no baseline.
- Compatibility statements ("works with any harness") wider than what was tested.

**Language and structure**
- Marketing adjectives (seamless, blazing, powerful, robust, effortless, cutting-edge)
  that carry no checkable content; filler openings; stacked hedges.
- Procedures written as prose instead of numbered steps; optional and required steps
  mixed; passive voice that hides who acts ("the token is configured").
- Undefined acronyms and internal jargon in newcomer docs.
- A README that does not answer, in its first screen, what this is, who it is for, how
  to install, and the first command to run.
- Tutorials, how-to guides and reference material mixed so none of them is complete.

**Retraction ledger**
- Published statements later found false (benchmarks, security properties, supported
  platforms) are corrected visibly: what was claimed, what is true, since when, the
  evidence and the fixing commit. Silent edits erase information users relied on.
- Existing ledger entries still accurate, and none reverted by later doc edits
  (`git log -p -- <doc>` around the corrected lines).

## Evidence standard

A parity row cites the doc location and either the code location that decides the
behavior or the executed command with its actual output. A failed walkthrough step
quotes the exact command, the environment (OS, shell, runtime version) and the error.
A stale screenshot cites the current rendering or the UI commit that changed it. Not
evidence: help text alone when the code path disagrees, the reviewer's configured
environment standing in for a clean one, or "this reads as outdated" without a source.

## Severity guide

- P0: the documented install or quickstart fails for the stated audience on a supported
  platform; a security or safety instruction is wrong (claims a protection that does
  not exist, omits a warning on a destructive command); a published claim is false.
- P1: a documented flag, variable, default or path disagrees with code and leads to
  wrong behavior; a screenshot or diagram of a primary flow no longer matches; a
  prominent absolute claim has no evidence; a breaking change missing from the changelog.
- P2: broken links or anchors; stale examples in secondary docs; a missing prerequisite
  on a secondary path; undocumented options the audience needs.
- P3: wording, filler and structure improvements that do not change correctness;
  group these into one finding rather than one per sentence.

## Skill-specific output

**Doc-to-code parity table**

| ID | Doc location | Statement | Code truth | Status | Fix |
| --- | --- | --- | --- | --- | --- |

Status is one of MATCH, MISMATCH, UNDOCUMENTED (in code, missing from docs),
NOT_IN_CODE (documented, does not exist), RAN_OK, RAN_DIFFERS, FAILED, NOT_RUN (with
reason). Code truth is path:line or the command and its observed output.

**Corrections ledger**

| ID | Location | Was | Now | Evidence | Type | Retraction-worthy |
| --- | --- | --- | --- | --- | --- | --- |

Type is factual error, stale, overclaim, ambiguity or omission. Retraction-worthy is
yes when the statement was published and users may have acted on it; those entries
belong in the project's visible corrections record, not only in the diff.

## Anti-patterns

- **Reading without running.** Commands are executed or explicitly marked NOT_RUN with
  a reason; "looks correct" is not a status.
- **Using your configured environment.** Your shell already has the variables, tools and
  caches the newcomer lacks. Run in a clean context.
- **Style policing as findings.** Language is a finding only when it misleads or blocks
  the audience; minor style goes into a single P3.
- **Rewriting wholesale.** Propose the smallest correction that makes each statement
  true; do not restructure documents nobody asked to restructure.
- **Silent deletion of falsified claims.** A published falsehood gets a ledger entry
  and a visible correction, not just a removed sentence.
- **README-only review.** Help output, examples, docstrings and the changelog drift too,
  and readers reach them directly.
- **Trusting help text.** Help strings are documentation too; verify them against the
  code path that implements the behavior.

## Done when

- [ ] Every executable block has a run status from a clean context or a reason it was not run.
- [ ] Every flag, variable, default, path and version in scope maps to code or is marked NOT_IN_CODE.
- [ ] The reverse pass for undocumented, audience-relevant behavior is done.
- [ ] Screenshots, diagrams and links each have a status.
- [ ] Every absolute or quantitative claim has evidence, a calibrated rewrite or a removal proposal.
- [ ] Each correction is in the ledger, with retraction-worthy entries identified.
