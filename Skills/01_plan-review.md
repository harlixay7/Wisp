---
name: plan-review
aliases:
  - adversarial-plan-hardening-engine
version: 4.0.0
description: >-
  Use when an implementation plan, RFC, design document or architecture proposal
  must be attacked before code is written: checking its premises against the
  repository, enumerating how each step fails, and fixing the build order.
  Produces a premise table, a failure-mode registry, EARS hardening requirements
  and a reversible build sequence. Not for choosing between alternative designs
  (use design-second-opinion), auditing code that already exists (use
  wiring-audit), or migration and schema detail (use
  data-integrity-review).
brief: |
  Mission: find how this plan fails before anyone builds it, and turn each gap into a requirement or a change of order.
  - Check every premise the plan states or silently relies on about the repository (symbols, signatures, call sites, config keys, dependency versions, platform behaviour) by reading files or running probes. A false premise outranks any speculative failure mode.
  - Attack each step across process lifecycle, state atomicity, resource exhaustion and backpressure, trust boundaries, dependencies and platform, rollout and compatibility, rollback, and observability. Every failure mode names the step, the trigger, the mechanism and the consequence; say which categories do not apply.
  - Look for an existing mitigation in the codebase before reporting a gap.
  - Name the single riskiest assumption and the cheapest experiment that would falsify it before building.
  - Reorder the work so each step ships and reverts on its own; mark one-way doors.
  - Harden, do not redesign: propose the smallest change that closes each gap.
  Output before findings: verdict rationale, premise table, failure-mode registry, riskiest assumption, EARS requirements (REQ-PLAN-NNN), recommended build order.
  PASS: premises hold and no unmitigated failure mode on the primary path. PASS_WITH_FIXES: gaps close with added requirements or reordering. BLOCK: a load-bearing premise is false, or a failure mode has no mitigation within the plan's constraints.
activation_triggers:
  task_modes:
    - PLAN_HARDENING_REVIEW
    - PRE_IMPLEMENTATION_VERIFICATION
    - RFC_REVIEW
  keywords:
    - implementation plan
    - rfc
    - design doc
    - pre-mortem
    - premise check
    - failure-mode registry
    - riskiest assumption
    - build order
    - rollout plan
    - one-way door
  do_not_use_when:
    - The caller wants alternatives compared or a direction chosen (route to design-second-opinion).
    - The code already exists and the question is whether it is wired correctly (route to wiring-audit).
    - The plan is only a schema or migration change (route to data-integrity-review).
    - The plan's core claim is a performance or capacity number (route to claim-check).
input_contract:
  requires_worktree: true
  required_inputs:
    - The plan text (steps, scope, goal), or a description precise enough to reconstruct the steps
  optional_inputs:
    - Commit or branch the plan targets
    - Deployment shape and supported platforms
    - Known constraints (compatibility promises, budgets, data that must not be lost)
    - Concerns the caller already has
output_contract:
  sections:
    - Verdict rationale
    - Premise table
    - Failure-mode registry
    - Riskiest assumption
    - Hardening requirements (EARS)
    - Recommended build order
  findings: shared format
  verdict: shared verdict block
---

# Adversarial plan hardening

## Mission

The consumer is the calling agent about to implement this plan. It needs to know
what to change in the plan before writing code. An excellent result finds the one
or two decisions that would otherwise have forced a rewrite (a false premise about
the codebase, an irreversible step placed early, a persisted format with no way
back) and converts them into testable requirements and a safer order of work. The
common failure is a generic reliability catalogue (add retries, add logging,
handle errors) that would fit any plan and changes none of this one's decisions.

## Inputs to establish first

- The plan's goal, scope and non-goals. If only a summary arrived, reconstruct the
  step list, label it as reconstructed, and review that.
- The baseline: `git rev-parse HEAD`, `git status --short`, `git log -5 --oneline`.
  Note whether the plan depends on uncommitted or unmerged work.
- Deployment shape: library, CLI, desktop app, long-running local server, hosted
  service. It decides which failure modes are real: a CLI has no rolling deploy, a
  hosted service has no user who simply restarts it, a desktop tool has many
  concurrent instances on one machine.
- Target platforms and runtime versions (read `pyproject.toml`, `package.json`,
  CI matrices, launch scripts) rather than assuming the reviewer's own platform.
- If any of these is missing, state the assumption you adopt and cap the
  confidence of findings that rest on it at medium.

## Method

1. **Decompose.** Rewrite the plan as numbered steps, each with the files it
   creates or changes and the state it leaves the system in. Done when every step
   has an observable end state.
2. **Extract premises.** For each step, list what must be true for it to work:
   "function X exists and is reachable from Y", "config key Z is read at startup",
   "library L supports cancellation in the pinned version", "only this process
   writes file F", "this works on Windows". Include the implicit ones; they are
   where plans break. Done when each step has its premise list.
3. **Verify premises on disk.** Use `git grep -n` for symbols and callers, read
   signatures and return types, read the installed dependency version (lockfile,
   `pip show`, `npm ls`) and its source under site-packages or node_modules rather
   than recalling its API, and run small probes (`python -c`, a throwaway test, a
   `--dry-run`). Done when each premise is HOLDS, FALSE or UNVERIFIABLE with
   evidence.
4. **Attack.** For each step and each relevant checklist group, ask which input,
   timing or environment leaves the system wrong after this step. Favour failures
   that cross step boundaries: step 3 assumes step 2 completed, but step 2 can
   half-complete. Search for an existing mitigation before recording a gap. Done
   when each failure mode has trigger, mechanism, consequence, likelihood and
   current mitigation.
5. **Find the riskiest assumption.** Rank premises and failure modes by
   probability of being wrong times cost of finding out late. Design the cheapest
   experiment that would falsify the top one (as a rule of thumb, under an hour:
   a spike script, a query against real data, a platform probe) with an explicit
   pass/fail criterion. Done when one assumption and one experiment are named.
6. **Sequence.** Order the work so that: the falsifying experiment runs first;
   additive changes precede destructive ones; readers that tolerate the new
   format ship before writers that produce it; new behaviour lands dark or behind
   a flag before cutover; the signal that shows success or failure exists before
   the behaviour it observes. Done when every step in the order ships alone and
   has a stated rollback, and one-way doors are marked.
7. **Specify.** Turn each accepted mitigation into an EARS requirement linked to
   its failure mode, with a verification method. Done when every P0-P2 failure
   mode maps to a requirement or an ordering change.

## Checklist

### Process lifecycle
- Startup: what happens when a dependency (database, socket, child process,
  config file, network) is not ready the first time the new code touches it? A
  fixed sleep is not readiness.
- Shutdown: on Ctrl+C, SIGTERM, window close or logoff, does in-flight work
  finish, cancel, or vanish? Windows console children do not receive SIGTERM;
  graceful stop needs `CTRL_BREAK_EVENT` and a separate process group.
- Ownership of children when the parent dies: orphans keep ports and file locks,
  so the next start fails in a way the plan never tested.
- Pipes: reading stdout to the end and then stderr deadlocks once the child fills
  the unread pipe's buffer (commonly 64 KB on Linux, smaller on Windows).
- Restart after a crash: stale pid and lock files, half-written temporaries,
  sockets still bound. Does the plan assume a clean start?

### State atomicity and consistency
- Mark every point where a crash between two writes leaves an inconsistent pair
  (file and index, row and external call, two files). The plan must name the
  source of truth and how the dependent copy is repaired.
- `os.replace` is atomic only within one filesystem; a temp file in a different
  mount turns it into an error or a copy. On Windows it fails with a permission
  error when another process holds the target open, which indexers and
  antivirus do routinely.
- Multiple instances: two terminals, two harnesses, or a UI plus a CLI writing the
  same store. A single-writer assumption needs enforcement (lock file with stale
  detection, database), not a comment.
- Read-modify-write of shared JSON or YAML without a lock loses updates.
- Derived state (caches, indexes, summaries): is there a path where the derived
  copy updates and the source write then fails?

### Resource exhaustion and backpressure
- Every queue, buffer, history, log or retained artifact the plan adds: what
  bounds it, and what happens at the bound (drop oldest, reject, block)? Retention
  bugs fill disks over weeks, never in tests.
- Producers faster than consumers: file watchers, event streams, clients that
  disconnect without the server noticing.
- Per-request cost multiplied by realistic concurrency: threads, subprocesses,
  file handles, memory per payload.
- Every blocking call has a timeout, and the plan says what the caller does when
  it fires.

### Trust boundaries
- Each new input surface (argument, endpoint, workspace file, tool output,
  clipboard, model output): who controls it, and does it reach a shell, a path
  join, a template, a deserializer or a prompt? A localhost HTTP endpoint that
  mutates state is reachable from any web page via CSRF or DNS rebinding.
- Flag these and route depth to security-review.

### Dependencies, environment, platform
- A new dependency: maintained, pinned, license-compatible, available as wheels
  or binaries for every target platform and runtime version?
- Platform-divergent behaviour: case sensitivity, path length, file locking,
  signals, default text encoding (Windows code pages versus UTF-8), line endings.
- Environment assumptions: a tool on PATH, network access, a GUI session, a
  writable working directory, a home directory.
- Version skew: the plan uses an API newer than the version the lockfile pins.

### Rollout, compatibility and rollback
- Any persisted or exchanged format that changes (config, on-disk state, report
  schema, API payload): old data must still load, and if old and new versions run
  side by side, each must tolerate the other's output.
- New settings default to current behaviour unless the change is the point.
- A switch to disable the new path without a code revert.
- For each step, the undo. Reverting code is not a rollback once data was
  migrated, files deleted, messages sent, or a format written that the old version
  cannot read. Those steps are one-way doors and go last, behind a verified gate.

### Observability
- How will the operator know the new path ran and whether it succeeded: a log
  line, a status field, a report entry?
- Failures caught and logged at debug level are invisible; each failure path needs
  a stated place where it surfaces.
- Can a failure be diagnosed afterwards from preserved artifacts alone?

## Evidence standard

- HOLDS needs a citation: path:line of the symbol, command output, or a test
  result. The plan's own text and memory of a library are not evidence; the
  installed version is.
- FALSE needs the contradicting citation, for example a different signature at
  path:line or a `git grep` with zero hits for a config key the plan says is read.
- A failure mode is credible when its mechanism traces to a named step and a named
  condition. When a five-line script can demonstrate it (pipe deadlock, a
  cross-device rename), run it and raise confidence to high.
- Likelihood comes from how the software is actually launched and used (README,
  launchers, config), not from what could happen to any system.

## Severity guide

- **P0**: a load-bearing premise is false; a step can irrecoverably lose or corrupt
  persisted data on a realistic crash; an ungated one-way door on the primary path.
- **P1**: a failure likely in normal use (second instance, restart after crash,
  a supported platform) with no mitigation; a format change with no rollback; an
  order that leaves an intermediate state unshippable.
- **P2**: failure under rarer conditions (disk full, file held by another
  process, clock change); a failure path with no visible signal; slow unbounded
  growth.
- **P3**: ordering or naming refinements; extra tests that would raise confidence.

## Skill-specific output

1. **Verdict rationale**: three to six sentences naming the deciding premises and
   failure modes.
2. **Premise table**: `| ID | Premise (stated or implicit) | Step | Evidence | Status (HOLDS / FALSE / UNVERIFIABLE) | Impact if false |`
3. **Failure-mode registry**: `| ID | Category | Step | Trigger | Mechanism -> consequence | Likelihood | Existing mitigation | Requirement |`
4. **Riskiest assumption**: the assumption, why it ranks first, the experiment
   (exact commands or code), the expected result, and what changes in the plan if
   it fails.
5. **Hardening requirements (EARS)**: `REQ-PLAN-001` onward, in WHEN / WHILE /
   IF-THEN / WHERE form, each linked to its failure mode and carrying a
   verification method. Testable responses only: "SHALL exit within 5 s and leave
   no child process", never "SHALL handle errors gracefully".
6. **Recommended build order**: numbered steps, each with what ships, its
   rollback, the gate to proceed, and whether it is a one-way door.

## Anti-patterns

- **Generic catalogue.** Failure modes not tied to a step. Rule: each entry names
  a plan step and a mechanism, or it is deleted.
- **Trusting the plan's description of the code.** Rule: every premise gets
  evidence or is marked UNVERIFIABLE.
- **Redesigning.** Replacing the plan with a preferred architecture. Rule: propose
  the minimal change; if the approach itself is unsound, BLOCK with the reason and
  recommend design-second-opinion.
- **Reporting what is already handled.** Rule: search for an existing mitigation
  (helpers, wrappers, containment code) first; if present, list it under
  "Checked and cleared".
- **Severity by imagination.** Rule: likelihood follows the deployment shape;
  exotic scenarios are P3 or omitted.
- **Ignoring order.** Rule: the build order is a deliverable; much of the risk
  reduction comes from sequencing, not mitigations.

## Done when

- [ ] Every step has its premises listed, each with a status and evidence.
- [ ] Each checklist group was applied to the steps it touches; inapplicable groups
      are named.
- [ ] The riskiest assumption has a falsifying experiment with a pass/fail
      criterion.
- [ ] Every P0-P2 failure mode maps to a REQ-PLAN requirement or an order change.
- [ ] The build order gives a rollback per step and marks one-way doors.
- [ ] The verdict follows from the premise table and the registry.
