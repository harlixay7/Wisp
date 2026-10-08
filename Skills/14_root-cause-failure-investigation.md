---
name: root-cause-failure-investigation
version: 4.0.0
description: >-
  Use when the same problem has survived two or more fix attempts, when a
  failure is intermittent or environment-dependent, or when nobody can explain
  why a symptom appears. Produces an attempts ledger, ranked competing
  hypotheses, a log of discriminating experiments, and a causal chain from
  root cause to symptom with the fix direction and the regression test that
  proves it. Not for auditing a finished diff (use pre-merge-diff-audit), not
  for applying the fix once the cause is known (use
  zero-regression-surgical-implementation), and not for performance or latency
  regressions that need profiling (use telemetry-hardware-profiling-gate).
brief: |
  Mission: stop the fix loop by finding the cause that explains every observation, including why earlier fixes failed.
  - Start from verbatim evidence: exact error text and full trace (the first error, not the last), the reproduction command, environment and versions, failure rate, and what changed since it last worked (git log, lockfile and config diffs).
  - Build an attempts ledger: for each prior fix, the hypothesis it implied, what happened, and what that rules in or out. First verify each attempt actually ran (stale build, wrong interpreter, cached module, process not restarted).
  - Keep at least three competing hypotheses, including "environment, not code" and "the observation is wrong". Rank by prior likelihood against cost to test.
  - Run discriminating experiments cheapest first, one variable at a time, with the prediction written down before running. Use git bisect when a known-good revision exists, and differential diagnosis when it works in one place and fails in another.
  - Shrink to a minimal reproduction. Confirmed means you can switch the failure on and off by changing the suspected cause alone.
  - Reject symptom fixes: retries, longer timeouts, sleeps, broad excepts, skipped tests.
  Output, before the findings: Evidence intake, Attempts ledger, Hypotheses, Experiment log, Causal chain, Fix direction and regression test, Residual uncertainty.
  PASS = root cause confirmed and the proposed fix targets it. PASS_WITH_FIXES = probable cause with the next experiment or fix clearly defined. BLOCK = the current fix only masks the symptom, or the cause is still unknown and further patching is unsound.
activation_triggers:
  task_modes:
    - ROOT_CAUSE_ANALYSIS
    - REPEATED_FAILURE_ESCALATION
    - INTERMITTENT_FAILURE_HUNT
  keywords:
    - root cause
    - repeated failure
    - git bisect
    - minimal repro
    - differential diagnosis
    - heisenbug
    - intermittent failure
    - debugging
    - works on my machine
    - fix loop
  do_not_use_when:
    - The cause is already confirmed and only the change remains to be made (use zero-regression-surgical-implementation).
    - A completed change needs a merge-readiness review (use pre-merge-diff-audit).
    - The problem is slowness, stalls or resource pressure that needs measurement rather than a defect hunt (use telemetry-hardware-profiling-gate).
input_contract:
  requires_worktree: true
  required_inputs:
    - The symptom with exact error text or observed wrong output
    - The prior fix attempts and their results
  optional_inputs:
    - Reproduction command or steps and observed failure rate
    - Last known good revision, date or environment
    - Logs, traces, screenshots and environment details from failing and passing runs
output_contract:
  sections:
    - Evidence intake
    - Attempts ledger
    - Hypotheses
    - Experiment log
    - Causal chain
    - Fix direction and regression test
    - Residual uncertainty
  findings: shared format
  verdict: shared verdict block
---

# Root cause failure investigation

## Mission

Explain the failure, not just end it. An excellent result is a causal chain in which
every link is supported by an experiment, which also explains why each earlier fix
failed, plus a regression test that fails today and will pass only when the actual
cause is removed. The consumer is an agent that has already patched this twice and
is about to patch it again. The most common failure is a fourth fix of the same kind:
pattern-matching on the error message, editing the line in the stack trace, and
declaring success after one green run.

## Inputs to establish first

- The symptom verbatim. Paraphrased errors hide the clue. With chained tracebacks
  ("During handling of the above exception..." or "caused by"), the first exception
  is usually the cause and the last is fallout from cleanup.
- A reproduction: command, inputs, environment. If the caller gave none, build one.
  If you cannot reproduce, that is the first finding and the investigation becomes a
  differential between the reporting environment and yours.
- Failure rate for intermittent problems. As a rule of thumb, to claim a 1-in-5
  failure is gone you need about 20 consecutive passes (0.8^20 is about 1.2 percent
  chance of passing by luck); a single green run proves nothing.
- Timeline: when it last worked, what changed since. `git log --since`, `git diff
  <good>..<bad> --stat`, lockfile diffs, interpreter and OS versions, config and
  environment variables, input data.
- The prior attempts, as diffs if available.

## Method

1. Freeze the symptom. Reproduce it and capture output verbatim with versions
   (`python -VV`, `node -v`, `git rev-parse HEAD`, dependency list). Done when you
   have a command that fails deterministically or a measured failure rate.
2. Audit the attempts. For each prior fix, confirm it was actually exercised before
   concluding it failed: rebuilt artifact, restarted process, correct interpreter,
   module imported from the path you edited
   (`python -c "import m; print(m.__file__)"`), no stale bytecode, bundle or cache.
   Then record what each genuine attempt rules out. A fix that changed the symptom
   is evidence about the mechanism. Done when every attempt is classified as
   not-exercised, no-effect, changed-symptom or masked.
3. Hypothesize. Write at least three competing explanations, always including one
   environmental and one observational ("the test, log or monitoring is wrong").
   For each, state the mechanism and what it predicts for observations you have not
   yet made. Done when the hypotheses make different predictions.
4. Discriminate. Choose the experiment that splits the surviving hypotheses most
   cheaply. Change one variable, write the prediction down first, run, record. Go
   back to step 3 whenever a result fits no hypothesis. Done when one hypothesis
   survives and the others are eliminated by recorded results.
5. Minimize. Remove inputs, config, code and steps until each remaining element is
   necessary for the failure. Done when removing any element makes it pass.
6. Confirm. Toggle the suspected cause alone and show the failure follows it, on and
   off. Check the mechanism also explains the intermittency, the environment
   pattern and the earlier fix outcomes. Done when nothing in the evidence is left
   unexplained, or the unexplained items are listed.
7. Prescribe. State the fix direction at the cause and the regression test that
   fails now for that reason. If a symptom guard is also warranted (defense in
   depth), label it as such and keep it separate.

Stopping rule: stop when step 6 succeeds. Otherwise stop when the next experiment
would cost more than the caller's stated budget or needs access you lack; then
report the surviving hypotheses with their evidence and the single next experiment
that best discriminates them. Never upgrade a ranked guess to "root cause".

## Checklist

Discriminating techniques
- `git bisect run <script>` with a known-good revision: the script exits 0 for good,
  1 to 127 except 125 for bad, 125 to skip unbuildable commits, and anything above
  127 aborts the bisect (so a crashed harness must not leak its code). Make the script
  test the symptom specifically, or bisect finds an unrelated break.
- Differential diagnosis between a passing and a failing environment: diff
  interpreter path and version, dependency versions, environment variables, locale
  and encoding, working directory, user and permissions, filesystem type and case
  sensitivity, line endings, CPU count and timing.
- Test-order pollution: passes alone, fails in the suite. Bisect the preceding test
  list, run with random ordering on and off, look for global state, environment
  variables, monkeypatches or files left behind.
- Race amplification: loop the reproduction, insert a delay at the suspected window
  to make the failure deterministic, constrain CPUs (`taskset`) or load the machine.
- Observation at boundaries: log inputs and outputs at each component boundary with
  a correlation id and monotonic timestamps; `faulthandler` or `py-spy dump` for
  hangs; `strace -f -e trace=file,process` to see which files and binaries are
  actually used; asyncio debug mode for un-awaited coroutines and slow callbacks.

Failure classes that are routinely misdiagnosed
- The fix never ran: stale build output, editable install pointing at another
  checkout, two copies of a module on `sys.path`, a long-lived server or Electron
  process still running old code, cached frontend bundle.
- Wrong executable: `PATH` shadowing (`which -a <tool>`), a virtualenv not
  activated in the subprocess, a Windows `.cmd` shim resolving differently from a
  POSIX script.
- Platform defaults: `open()` without `encoding=` uses the locale encoding (often
  cp1252 on Windows); renaming or deleting an open file fails on Windows; path
  length limits; case-insensitive filesystems merging two names.
- Masked primary error: a `finally` or cleanup handler raises and hides the original
  exception; retries or broad excepts convert a deterministic error into a timeout
  far from the cause; the log shows the last error, not the first.
- Resource exhaustion presenting as logic errors: a full pipe buffer (a child writing
  to an unread `PIPE` blocks once the OS buffer fills, often around 64 KiB on
  Linux), file descriptor limits, ports in `TIME_WAIT`, disk full, inotify watch
  limits.
- Dependency drift: an unpinned transitive upgrade, an optional dependency present
  on one machine only, a native extension built against another ABI.
- Data assumptions: BOM or trailing newline in input, `None` versus missing,
  timezone-naive timestamps, ordering of directory listings.
- Heisenbugs: adding logging or a debugger changes timing or object lifetimes; treat
  "it went away when I added a print" as evidence of a race or lifetime bug.

Reasoning guards
- One variable per experiment. Changing two things at once makes both results
  uninterpretable.
- Seek the observation that would refute your favorite hypothesis, not the one that
  confirms it.
- Distrust the location in the stack trace: it is where the failure surfaced, not
  necessarily where the bad state was created. Trace the bad value backwards.

## Evidence standard

- Confirmed: the failure toggles with the cause alone, or bisect identifies a commit
  and the mechanism in that commit is shown. Confidence high.
- Probable: the mechanism fits all observations but was not toggled. Confidence
  medium, and say what toggle would confirm it.
- Correlation ("it started after the upgrade") without a toggle is a hypothesis.
- Each experiment entry records the exact command, the prediction made beforehand,
  and the observed output. Unrecorded experiments do not count.
- A claim that a fix works for an intermittent failure states the number of runs and
  the pre-fix failure rate.

## Severity guide

- P0: the root cause corrupts or loses data, or a current or proposed fix masks a
  primary-path failure (retry, timeout increase, swallowed exception, skipped test)
  while the cause stays live.
- P1: the cause will recur in realistic conditions (other platforms, load, input
  shapes) that the caller's fix does not cover; a prior fix introduced a new defect.
- P2: a contributing factor that hid the cause or slowed diagnosis (masked
  exceptions, missing context in errors, non-deterministic tests).
- P3: diagnostics that would make the next investigation faster.

## Skill-specific output

Evidence intake: symptom verbatim, reproduction command, failure rate, environment,
last known good, relevant changes since then.

Attempts ledger

| # | Change made | Implied hypothesis | Exercised? | Result | Rules in or out |
| --- | --- | --- | --- | --- | --- |

Hypotheses

| ID | Mechanism | Predicts | Prior | Test cost | Status |
| --- | --- | --- | --- | --- | --- |

Status is SURVIVING, ELIMINATED (cite the experiment) or CONFIRMED.

Experiment log

| ID | Discriminates | Command or action | Predicted | Observed | Conclusion |
| --- | --- | --- | --- | --- | --- |

Causal chain: numbered links from root cause to observed symptom, each with its
evidence reference; then one line per prior attempt explaining why it failed.

Fix direction and regression test: where the fix belongs and why there, the test
name and assertion, and confirmation that the test fails on current code for the
root-cause reason.

Residual uncertainty: what is not explained, what would change the conclusion.

## Anti-patterns

- Fixing the symptom. Rule: a retry, a longer timeout, a sleep, a broad except or a
  skipped test is never the deliverable unless the cause is shown to be external
  and transient, and then it is labeled a mitigation.
- Declaring a prior fix "failed" without checking it ran. Rule: prove the edited
  code executed before using its result as evidence.
- Confirmation bias. Rule: keep competing hypotheses alive until an experiment kills
  them; record the eliminating result.
- Shotgun changes. Rule: one variable per experiment, prediction written first.
- Trusting a single green run on an intermittent failure. Rule: state runs and
  failure rate before and after.
- Narrating a plausible story as fact. Rule: without a toggle, it is "probable" and
  carries medium confidence at most.
- Dismissing environment differences as user error. Rule: an unreproducible report
  starts a differential, not a closure.

## Done when

- [ ] The symptom is reproduced or the inability to reproduce is explained.
- [ ] Every prior attempt is classified, with exercised-or-not verified.
- [ ] At least three hypotheses were considered; eliminations cite experiments.
- [ ] The root cause toggles the failure, or the stopping rule is invoked and the
      next experiment is named.
- [ ] The causal chain explains every observation, including prior fix outcomes.
- [ ] A regression test is specified that fails today for the root-cause reason.
- [ ] Residual uncertainty is stated.
