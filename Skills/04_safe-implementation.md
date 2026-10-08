---
name: safe-implementation
aliases:
  - zero-regression-surgical-implementation
version: 4.0.0
description: >-
  Use when a confirmed defect must be fixed, a specified feature built, or
  accepted review findings applied as code: reproduce first, map every
  consumer of the symbols to change, land the smallest patch, and prove it
  with fresh test, type-check and lint output. In review mode it produces the
  complete patch as unified diffs plus the exact test plan instead of editing.
  The only skill that changes code. Not for finding the cause of a failure
  that is not yet understood (use root-cause-investigation), not for
  judging a finished diff (use pre-merge-review), and not for designing
  the approach (use plan-review).
brief: |
  Mission: make the requirement true with the smallest change, proven by a test that failed before and passes after, with no regressions against a recorded baseline.
  - Mode: in implement mode, edit the workspace. In review mode (read-only), change nothing; deliver complete unified diffs that apply cleanly, the new tests in full, and the exact commands, validated in a throwaway copy if possible.
  - Baseline first: record HEAD and run the repository's own test, type-check and lint commands, so pre-existing failures are known.
  - Write requirements as EARS statements with IDs, including the error-path behavior.
  - Reproduce before fixing: a test or deterministic repro that fails for the reason the requirement names, output captured.
  - Map every caller and consumer of each symbol you will change, including string-keyed, serialized and mocked consumers; keep public contracts unless the task changes them, then update every consumer in the same patch.
  - Minimal patch: no drive-by refactors, no placeholders, no new dependency unless it is declared in the manifest and lockfile and justified.
  - Never weaken tests: no edited assertions, tolerances, snapshots, skips or xfails to get green.
  - Finish with a fresh full run after the last edit. After two failed attempts on the same cause, stop widening the diff and report.
  Output, before the findings: Requirements, Phase ledger, Patch registry, Verification log (plus Proposed patch and Test plan in review mode).
  PASS = all requirements verified, no regressions against the baseline. PASS_WITH_FIXES = done with a named, justified gap. BLOCK = green not reached under the escalation rule, or only reachable by weakening tests or breaking a contract.
activation_triggers:
  task_modes:
    - SURGICAL_IMPLEMENTATION
    - DEFECT_REMEDIATION
    - FINDINGS_APPLICATION
  keywords:
    - implement
    - bug fix
    - apply patch
    - tdd
    - red green
    - failing test first
    - refactor
    - minimal diff
    - write code
    - apply findings
  do_not_use_when:
    - The cause of the failure is not yet understood or two fixes have already failed (use root-cause-investigation).
    - The request is to review a completed change before merge (use pre-merge-review).
    - The design or approach is still undecided (use plan-review first).
input_contract:
  requires_worktree: true
  write_access: required
  required_inputs:
    - The requirement, confirmed defect or accepted findings to implement
    - Acceptance criteria or the observable behavior that defines done
  optional_inputs:
    - A reproduction command or failing test already identified
    - Prior critique or root-cause report to build on
    - Files or areas the caller wants left untouched
output_contract:
  sections:
    - Requirements
    - Phase ledger
    - Patch registry
    - Verification log
    - Proposed patch and test plan (review mode only)
  findings: shared format
  verdict: shared verdict block
---

# Zero-regression surgical implementation

## Mission

Deliver a change a maintainer can merge without re-deriving it: every behavior
change is tied to a requirement, every requirement to a test that was seen failing
and then passing, and every touched symbol to a list of consumers verified still
compatible. The consumer is the calling agent, which will either keep the edits
(implement mode) or apply the diffs verbatim (review mode). The most common failure
is reaching green the cheap way: loosening a test, rewriting more than needed, or
reporting success from a test run that predates the final edit.

## Inputs to establish first

- Mode. Write access comes from the envelope. Without it, the workspace is
  read-only: produce the patch, do not apply it.
- The requirement in testable form. If the request is vague, write the
  interpretation as EARS statements and mark assumptions; do not silently pick one.
- Canonical commands from CI configuration, `Makefile`, `pyproject.toml`,
  `tox.ini` or `package.json` scripts: tests, type checker, linter, formatter in
  check mode, build.
- Baseline: `git rev-parse HEAD`, `git status --porcelain`, and a full run of those
  commands with pre-existing failures recorded verbatim. Without a baseline, a red
  test after your change cannot be attributed.
- Constraints from the caller: files not to touch, compatibility promises, target
  platforms.

## Method

1. Baseline. Record revision, working-tree state and the canonical command results.
   Done when every pre-existing failure is listed with its test id.
2. Requirements. Express each behavior as EARS with IDs (REQ-IMP-001...): event
   driven ("WHEN <trigger>, the <component> SHALL <response>"), state driven
   ("WHILE <state>, ..."), and unwanted behavior ("IF <fault>, THEN the <component>
   SHALL <safe outcome>") for every error path the change touches. Done when every
   acceptance criterion maps to at least one requirement.
3. Reproduce (red). Write the test at the lowest level that still exercises the real
   behavior, run it, and capture the failure. The failure must come from the
   asserted behavior, not from a typo, fixture error or missing import in the test.
   For a brand-new API, an attribute or import failure is acceptable only if the
   test also asserts the specific behavior. Done when each requirement has a test id
   and a captured failure whose message matches the requirement.
4. Consumer map. For every symbol, file format, flag, message or route you will
   change, list consumers: `rg -nw`, language-server references, string dispatch,
   subclasses, serialized readers (frontend, persisted files, other processes),
   tests and mocks that copy its signature. Decide the change shape that preserves
   each contract. Done when every consumer is marked unaffected, adapted in this
   patch, or deliberately broken with the task's authorization.
5. Patch (green). Make the minimal edit that satisfies the requirements, following
   local conventions for errors, logging, typing and naming. Run the new tests, then
   the surrounding module's tests. Done when the red tests pass and nothing nearby
   regressed.
6. Full verification. Run the full canonical set after the final edit. Compare
   against the baseline: any new failure is a regression to fix, not to explain
   away. Confirm the new tests are load-bearing by reverting only the production
   hunks (for example `git diff -- <src files> > p.diff && git apply -R p.diff`, run,
   then `git apply p.diff`) and watching them fail. Done when fresh output shows no
   new failures and the revert check fails as expected.
7. Escalation rule. If two attempts to reach green on the same cause fail, stop.
   Revert speculative edits that did not help, keep the red test, and report the
   attempts with their output, recommending root-cause-investigation.
   Widening the diff in search of green is not allowed.
8. Review mode variant. Perform steps 1 to 4 read-only. Produce the patch as unified
   diffs against the recorded revision and validate it in a disposable copy
   (`git archive HEAD | tar -x -C "$(mktemp -d)"`, then `git apply --check`, apply,
   run the tests there). If no disposable copy is possible, state that the patch is
   unexecuted and lower confidence accordingly.

## Checklist

Test integrity
- No changes to existing assertions, expected values, tolerances, timeouts,
  snapshot or golden files, `skip`/`xfail`/`only` markers, deselect lists or CI
  test filters, unless the requirement itself changes that expected output; then
  name the requirement next to each such hunk.
- Shared fixtures are not repurposed for the new test in a way that changes other
  tests' inputs.
- Mocks use `autospec=True` or `create_autospec` (or typed fakes) so signature
  drift fails loudly; the unit under test is never itself mocked.
- Tests are deterministic: time frozen or injected, random seeded, temporary
  directories instead of the real home directory, no network, no fixed sleeps
  (poll a condition with a deadline instead).

Patch minimality
- No renames, reformatting or import reordering of untouched code; run the
  formatter only on changed files if the repository does not format everything.
- New parameters are keyword arguments with defaults that preserve old behavior,
  appended rather than inserted.
- No speculative generality: no new abstraction layer, option or config key the
  requirement does not need.

Contract preservation
- Same exception types raised for the same conditions; same `None`-versus-raise
  behavior; same ordering of returned collections when callers may depend on it.
- CLI exit codes, stdout formats and log lines that other tools parse stay stable.
- On-disk and wire formats: new code reads old data; if the format changes, older
  readers either still work or the incompatibility is called out.

Error paths and resources
- Each new failure mode has defined behavior under a REQ "IF ... THEN" statement;
  no broad `except` that hides it.
- Cleanup runs on every exit path (context managers, `try`/`finally`); files that
  must not be left truncated are written to a temp file in the same directory and
  moved into place with `os.replace`.
- Subprocesses get timeouts, reaped children and `communicate()` (or concurrent
  readers) when both pipes are captured.

Portability
- `encoding="utf-8"` on text I/O (the default is the locale encoding, which differs
  on Windows); `pathlib` instead of string joins; executables resolved with
  `shutil.which`; no `shell=True`; no assumption of case-sensitive filenames or
  POSIX-only signals.

Dependencies
- Any import is already declared in the manifest and lockfile, or its addition is
  justified (standard library cannot do it) and both files are updated.
- APIs used exist in the installed version: check the installed package's source
  or `help()`, not memory of a newer release.

Completeness
- `git diff | rg '^\+.*(TODO|FIXME|XXX|NotImplementedError)'` finds nothing the
  patch introduced; no ellipsized or elided code in delivered diffs.
- User-visible behavior changes update help text, docs and every front end that
  exposes the same option.

## Evidence standard

- Red and green outputs come from the same test id, before and after the patch.
- The final verification run is newer than the last edit; any edit after it
  invalidates the claim until rerun.
- Regressions are judged against the recorded baseline, quoting test ids.
- The patch registry matches `git diff --stat` file for file.
- In review mode, `git apply --check` output on a clean copy, or an explicit
  statement that the patch was not executed.

## Severity guide

Findings here are defects discovered while implementing (in the request, in
neighboring code, or residual risk in the patch).

- P0: the change as requested would corrupt persisted data or break a primary path;
  the requirement conflicts with an existing contract that other components rely
  on; green was only reachable by weakening tests.
- P1: a consumer outside the patch will break or needs a coordinated change; a
  requirement could not be covered by a test that fails without the patch; a needed
  dependency is undeclared.
- P2: a pre-existing defect found next to the change; an edge case the requirement
  leaves undefined; a platform assumption on a secondary path.
- P3: follow-up cleanup deliberately left out to keep the diff minimal.

## Skill-specific output

Requirements: EARS statements with IDs; mark inferred ones.

Phase ledger

| Phase | Action | Command or edit | Evidence (output excerpt or reference) | Status |
| --- | --- | --- | --- | --- |

Patch registry

| File:line | Change | Requirement | Consumers verified |
| --- | --- | --- | --- |

Verification log

| Command | Exit code | Pass / fail / skip | New versus baseline |
| --- | --- | --- | --- |

Review mode only: Proposed patch (complete unified diffs, new files included in
full) and Test plan (test id, requirement, expected failure before, expected pass
after, command to run).

## Anti-patterns

- Green by editing the test. Rule: test expectations change only when the
  requirement changes them, and each such change is cited.
- Fixing before reproducing. Rule: no production edit until a captured failure
  exists for the requirement.
- Drive-by refactoring. Rule: anything not needed by a requirement goes to a P3
  follow-up note, not the diff.
- Stale verification. Rule: rerun the full set after the last edit; quote it.
- Blaming pre-existing failures without a baseline. Rule: a failure is
  pre-existing only if the baseline run shows it.
- Dependency grabbing. Rule: prefer the standard library and declared packages.
- Thrashing. Rule: two failed attempts on one cause trigger escalation, not a wider
  diff.
- Partial patches in review mode. Rule: diffs are complete and apply cleanly; no
  "rest unchanged" elisions.

## Done when

- [ ] Baseline recorded with pre-existing failures.
- [ ] Every acceptance criterion maps to an EARS requirement with a test id.
- [ ] Each test was seen failing for the right reason, then passing.
- [ ] Every changed symbol's consumers are listed and verified.
- [ ] No test was weakened; no placeholder or undeclared dependency introduced.
- [ ] Full tests, type check and lint ran after the last edit with no new failures.
- [ ] Reverting the production hunks makes the new tests fail.
- [ ] In review mode, the diffs are complete and checked with `git apply --check`.
