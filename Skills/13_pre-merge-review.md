---
name: pre-merge-review
aliases:
  - pre-merge-diff-audit
version: 4.0.0
description: >-
  Use when a change set (diff, branch, or pull request) is about to be declared
  done, committed, or merged and the calling agent supplies the diff plus its
  acceptance criteria for an independent bug hunt. Produces a change map, an
  acceptance-criteria coverage table, a test-adequacy assessment, and only
  those defects that have a concrete failure scenario. Not for whole-codebase
  wiring audits of existing code (use wiring-audit), not for
  writing or applying the fix (use safe-implementation),
  and not for reviewing a plan before code exists (use
  plan-review).
brief: |
  Mission: decide whether this exact change is safe to merge and actually meets its acceptance criteria.
  - Review the change that will merge: diff against the merge base, plus staged, unstaged and untracked files that changed code imports. Classify every changed file.
  - Never review a hunk in isolation: for every changed function, class, constant, flag, schema field or message name, find its callers, overrides, mocks and string-keyed or serialized consumers, and judge compatibility.
  - Probe each hunk for edge inputs, error and cancellation paths, contract and serialization breaks, ordering and concurrency, resource lifetimes, security-relevant shifts, migration and rollback compatibility, and config or flag plumbing across every front end.
  - Run the repository's own tests, linters and type checker; do not accept the author's word that they pass.
  - Test adequacy: a new test counts only if it fails without the production change (check against the base revision in a throwaway copy). Flag weakened assertions, new skips and tests that are never collected.
  - Report only defects with a specific input or state leading to a wrong outcome; put suspicious-but-fine areas under "Checked and cleared". No style nits.
  Output, before the findings: Change map, Acceptance coverage matrix (criterion, implemented at, verified by, status), Test adequacy, Verification runs.
  PASS = every criterion MET with evidence, suite green, no P0/P1. PASS_WITH_FIXES = no P0, P1 fixes are local and clear. BLOCK = any P0, a criterion claimed met that is not, a red suite, or a change that breaks an existing caller.
activation_triggers:
  task_modes:
    - PRE_MERGE_AUDIT
    - PRE_COMMIT_DIFF_REVIEW
    - ACCEPTANCE_VERIFICATION
  keywords:
    - pre-merge
    - diff audit
    - pull request
    - code review
    - acceptance criteria
    - merge gate
    - test adequacy
    - pre-commit audit
    - hunk review
    - change set
  do_not_use_when:
    - There is no diff and the question is whether existing code is connected end to end (use wiring-audit).
    - The change has failed to work two or more times and the cause is unknown (use root-cause-investigation).
    - The request is to write or apply the fix itself (use safe-implementation).
input_contract:
  requires_worktree: true
  required_inputs:
    - The change set (base revision or branch, or the diff itself)
    - The acceptance criteria or task statement the change claims to satisfy
  optional_inputs:
    - The author's own verification output and the commands used
    - Known risky areas or files the author is unsure about
    - CI configuration or the canonical test command
output_contract:
  sections:
    - Change map
    - Acceptance coverage matrix
    - Test adequacy
    - Verification runs
  findings: shared format
  verdict: shared verdict block
---

# Pre-merge diff audit

## Mission

Answer one question with evidence: if this change merges as it stands, what breaks,
and does it do what it claims? The consumer is the agent that wrote the change and
will act on every finding, so each finding must be a reproducible defect, not an
opinion. The most common failure is reading only the lines in the diff: the bug is
usually in the unchanged caller that still passes the old argument, the frontend
that still reads the old JSON key, or the test that would have passed without the
change.

## Inputs to establish first

- Base revision. Prefer `git merge-base HEAD <target>`; review
  `git diff <base>...HEAD` plus `git diff --cached` plus `git diff`, and
  `git status --porcelain` for `??` files. An untracked file that changed code
  imports means the merged tree will not build; that is a finding, not noise.
- Acceptance criteria, verbatim. If none were given, derive them from the task
  statement and commit messages, label them "inferred", and do not BLOCK solely on
  an inferred criterion.
- The canonical verification commands: read CI workflows, `Makefile`,
  `pyproject.toml`/`tox.ini`/`noxfile.py`, `package.json` scripts. Use what CI runs,
  not what you would choose.
- The author's claimed verification. Treat it as a claim to reproduce.

## Method

1. Pin the change set. List every changed path with its kind: logic, test,
   config, schema or migration, generated, lockfile, docs, asset. Note renames
   (`git diff -M --stat`) and mode changes. Done when the reviewed file set equals
   the file set that will merge.
2. Map intent. Assign each hunk to an acceptance criterion or to "collateral".
   Unexplained hunks (debug prints, reformatting, version bumps, commented-out
   code, changed defaults) get scrutiny first; they are where accidental behavior
   changes hide. Done when no hunk is unassigned.
3. Symbol impact pass. For each changed symbol, collect its consumers with
   `rg -nw <name>` and, where available, language-server references; then look for
   what grep misses: string dispatch (`getattr`, registries, route tables, IPC
   channel names, event names), subclasses overriding a changed method, mocks and
   fakes that replicate the old signature, serialized consumers (JSON read by a
   frontend, files persisted by an older version, CLI output parsed by scripts).
   Done when each changed symbol has a consumer list and a compatible / broken /
   needs-check verdict.
4. Hunk correctness. Walk the checklist below against each hunk and its consumers.
   Write down the concrete input or interleaving for every suspicion before
   promoting it to a finding.
5. Execute. Run the canonical tests, linters and type checker on the change; then
   check test adequacy by running the new or changed tests against the base
   production code in a throwaway copy (for example
   `git archive <base> | tar -x -C "$(mktemp -d)"`, copy the new tests in, run them).
   In review mode keep the workspace untouched; everything destructive happens in
   the copy. Done when each command has an exit code and a summarized result.
6. Coverage. Fill the acceptance coverage table. A criterion is MET only when the
   implementing code is identified and a test or executed command demonstrates it.
7. Adjudicate. Promote suspicions with a failure scenario to findings; move the rest
   to "Checked and cleared" with the reason they are safe.

## Checklist

Edge inputs
- Empty, single-element and duplicate collections; `None` versus missing key versus
  empty string; zero, negative and maximum values; inclusive versus exclusive range
  ends and slice off-by-ones.
- Non-ASCII and space-containing paths, Windows drive letters and backslashes, CRLF
  input, a BOM at file start, very long lines or files that exceed a buffer.
- Naive versus aware datetimes compared or subtracted; float equality; integer
  division or truncation introduced by a refactor.

Error and cancellation paths
- New raise sites whose callers catch a narrower exception type, or exception types
  changed so an existing `except SpecificError` no longer matches.
- Early returns added between acquire and release; `finally` blocks that `return`
  and swallow the in-flight exception; cleanup that itself can raise and mask the
  original error.
- Async code: un-awaited coroutines or promises, `asyncio.CancelledError` caught by a
  bare `except:` or `except BaseException` without re-raise, tasks created without a
  retained reference (they can be garbage collected mid-flight).
- Partial writes: a file or record written in place so a crash leaves it truncated;
  the safe pattern is write to a temp file in the same directory, then `os.replace`.

Contracts, APIs and serialization
- Signature changes: reordered positional parameters, changed defaults (a default
  flip is a behavior change for every caller that relied on it), removed keyword
  names, `None` returned where callers iterate.
- Renamed or retyped JSON keys, enum values added where a consumer switches
  exhaustively, changed CLI exit codes, changed stdout or log formats that another
  tool parses, changed HTTP status or error envelope.
- Persisted data written in a new format that the previous release cannot read
  (rollback breaks) or old data the new code cannot read (upgrade breaks).

Concurrency and ordering
- Module-level mutable state introduced or mutated from threads, callbacks or
  multiple processes; check-then-act on the filesystem (`exists()` then `open()`).
- Listener or handler registered after the event it waits for can already fire.
- `subprocess` with `stdout=PIPE` and `stderr=PIPE` read sequentially rather than
  via `communicate()` or concurrent readers: deadlocks once a pipe buffer fills.
- Ordering assumptions about dict or set iteration, directory listing order, or
  completion order of concurrent tasks.

Resource lifetimes
- Files, sockets, processes and temp directories opened without a context manager
  on the new path; child processes not waited on; timers, intervals, event listeners
  and watchers added without removal; caches or queues with no bound.

Security-relevant shifts
- New `shell=True` or string-built commands with external input; user-controlled
  path segments joined without containment (note `Path(base) / "/abs"` discards
  `base`); bind address widened to `0.0.0.0`; secrets newly logged or passed via
  environment to children; `yaml.load` without a safe loader, `pickle` on external
  data. Note the mechanism and route deep analysis to security-review.

Config, flags and wiring of the change itself
- A new flag or setting parsed but not plumbed to where it acts; defaults that
  differ between CLI, config file, environment and docs; a feature added to one
  front end (CLI) but not the parallel one (server, UI, MCP tool) that builds the
  same request.

Test adequacy
- Each new test fails on the base revision for the reason the criterion names, not
  for an import error or fixture mistake.
- Changed tests: removed or loosened assertions, widened tolerances, regenerated
  snapshots, new `skip`/`xfail`/`only`, deleted test cases. Inspect
  `git diff <base> -- tests/` hunk by hunk.
- Tests that mock the unit under test, assert only that a mock was called, or use
  mocks without `autospec` so a signature change still passes.
- Tests not collected: wrong file or function prefix, class with `__init__`,
  platform guards that skip on the CI platform. Confirm with the runner's
  collect-only mode.
- The risky paths (error branches, cancellation, concurrency) have at least one test
  or an explicit reason why not.

## Evidence standard

- A broken-caller finding cites the changed definition and the unchanged call site,
  with the argument or key that no longer matches.
- "Tests pass" means a command you ran in this session, its exit code, and the pass,
  fail and skip counts. A pre-existing failure is shown to exist on the base too.
- "Test is inadequate" means you ran it against base production code and it passed,
  or you show the assertion cannot observe the changed behavior.
- A concurrency finding names the interleaving step by step; "might race" without
  one is not a finding.
- Reading the code without running it caps confidence at medium.

## Severity guide

- P0: the change corrupts or loses persisted data; crashes a primary path; the
  suite is red because of the change; an acceptance criterion reported as done is
  not implemented; an untracked file the merged code imports is missing.
- P1: an existing caller, consumer or older data file breaks in realistic use; an
  error path leaks a process, handle or lock under ordinary failures; a criterion is
  implemented but its only test would pass without the change; a test was weakened
  to get green.
- P2: wrong result on a plausible edge input; missing test for a risky branch;
  flag default inconsistent between front ends on a secondary path.
- P3: leftover debug output or dead code from the change; a misleading name or
  message that will cause a future mistake.

## Skill-specific output

Change map

| File | Kind | Criterion or collateral | Consumers checked | Risk note |
| --- | --- | --- | --- | --- |

Acceptance coverage matrix

| Criterion | Implemented at (path:line) | Verified by (test id or command) | Status |
| --- | --- | --- | --- |

Status is MET, PARTIAL, UNMET or UNVERIFIABLE (say what evidence is missing).

Test adequacy

| Test | Targets criterion | Fails on base? | Gap |
| --- | --- | --- | --- |

Verification runs

| Command | Exit code | Result | Same failures on base? |
| --- | --- | --- | --- |

## Anti-patterns

- Reviewing only the visible hunks. Rule: no changed symbol leaves phase 3 without a
  consumer list, including string-keyed and serialized consumers.
- Trusting the author's green run. Rule: rerun the canonical commands; quote counts.
- Style and preference comments dressed up as findings. Rule: no failure scenario,
  no finding.
- Speculative races and "could be null" claims. Rule: show the interleaving or the
  input that produces the null, or move it to "Checked and cleared".
- Re-litigating the design. Rule: audit the change against its criteria; if the
  approach itself is unsound, say so in one finding and route the redesign to
  plan-review.
- Demanding tests for trivial plumbing while missing the untested error path. Rule:
  rank test gaps by the severity of what an undetected regression would cost.
- Blocking on missing criteria the caller never stated. Rule: mark inferred
  criteria as such and judge them at most P1.

## Done when

- [ ] The reviewed file set equals what will merge, untracked imports included.
- [ ] Every hunk is mapped to a criterion or flagged as collateral.
- [ ] Every changed symbol has a consumer list and a compatibility verdict.
- [ ] Canonical tests, lint and type check were run here, with exit codes recorded.
- [ ] New tests were checked against base production code.
- [ ] Every criterion has a status backed by a location and a verification.
- [ ] Each finding has a concrete failure scenario; cleared suspicions are listed.
