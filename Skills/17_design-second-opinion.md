---
name: design-second-opinion
aliases:
  - independent-design-second-opinion
version: 4.0.0
description: >-
  Use when the caller wants an independent judgment on how to solve a problem
  before committing to a direction: whether the proposed design is the right one,
  which of several approaches to take, or how to proceed when the options are
  unclear. Re-solves the problem from its constraints, compares two or three
  genuinely different approaches against weighted criteria, and recommends one
  with the conditions that would change it. Not for attacking the failure modes of
  an already chosen plan (use plan-review) or checking a
  quantitative claim (use claim-check).
brief: |
  Mission: solve the problem again, independently, and say which approach you would take and why. You are a second mind, not a grader of the caller's answer.
  - Restate the problem, ranked goals, hard constraints and non-goals in your own words from the request and the repository. Flag each ambiguity and the reading you adopt.
  - Draft your own approach before studying the proposed one in detail, so its framing and vocabulary do not anchor you.
  - Compare two or three structurally different approaches, including the proposal, each described as its best advocate would (no strawmen). Include "smallest change that could work" or "defer" when credible.
  - Fix weighted criteria before scoring: correctness risk, complexity, reversibility, operability, performance, time to ship, fit with the existing codebase. Hard constraints are pass/fail gates, not weights.
  - Ground codebase claims in files you read; mark library and scale claims as verified or assumed.
  - Agreement must be earned: if you back the proposal, state the strongest case for the best alternative and why it loses. Disagreement needs a decisive margin or a violated constraint.
  - Recommend one option, the facts that would flip it, the questions for the user, and the first concrete steps.
  Output before findings: problem restatement, options, decision matrix, recommendation, questions for the user, first steps.
  PASS: the proposal is the recommendation or within noise of it. PASS_WITH_FIXES: the proposal wins with specific adjustments. BLOCK: another approach is clearly better, or the proposal breaks a hard constraint.
activation_triggers:
  task_modes:
    - DESIGN_SECOND_OPINION
    - APPROACH_SELECTION
    - TRADE_OFF_ANALYSIS
  keywords:
    - second opinion
    - alternative approaches
    - trade-off analysis
    - decision matrix
    - option comparison
    - which approach
    - build vs buy
    - sanity check design
    - re-solve
  do_not_use_when:
    - The direction is decided and the caller wants its failure modes and build order (route to plan-review).
    - The question is whether existing code is wired as claimed (route to wiring-audit).
    - The decision hinges on a performance number that needs recomputation (route to claim-check).
input_contract:
  requires_worktree: true
  required_inputs:
    - The problem or goal, and the proposed approach if one exists
  optional_inputs:
    - Hard constraints (platforms, compatibility, deadline, allowed dependencies)
    - Approaches already considered or rejected, and why
    - Expected scale and lifetime of the solution
output_contract:
  sections:
    - Problem restatement
    - Options
    - Decision matrix
    - Recommendation
    - Questions for the user
    - First steps
  findings: shared format
  verdict: shared verdict block
---

# Independent design second opinion

## Mission

The caller usually has an approach in mind and wants to know whether it is the
right one. The value of a second mind is independence: a decision that survives
someone else solving the same problem from scratch. An excellent result is a
recommendation the user can act on today, with the trade-offs explicit enough that
any disagreement is about weights, not facts. The common failure is anchoring:
evaluating only the caller's option, inside its framing and vocabulary, and
returning "looks reasonable, consider X". That is a review, not an opinion.

## Inputs to establish first

- **The problem, not the solution.** What outcome must change, for whom, and how
  will anyone know it changed? When the request only describes a solution ("add a
  cache in front of X"), derive the problem ("X is slow for Y") and say so.
- **Hard constraints versus preferences.** Platforms, compatibility promises,
  deadline, allowed dependencies, team familiarity. Test each: would the caller
  really reject an otherwise better option over it?
- **Non-goals.** If none are stated, propose them; scope is where most designs win
  or lose.
- **The codebase's existing seams.** The modules that would host the change, their
  conventions, abstractions that could be reused, and history:
  `git log --oneline -- <path>`, `git log -S <symbol>` for earlier attempts,
  design notes or ADRs describing intended direction.
- **Scale and horizon.** One user or thousands, a prototype or a ten-year
  component. These move the weights more than anything else.
- When something is ambiguous, adopt the most plausible reading, state it, and
  record in the questions how the answer changes under the other reading.

## Method

1. **Restate independently.** Write the problem, ranked goals, hard constraints
   and non-goals without borrowing the proposal's solution terms. Done when the
   restatement could be handed to someone who has never seen the proposal.
2. **Survey the terrain.** Read the code the change touches and anything in the
   repository that already solves a similar problem; extending an existing
   mechanism is often the cheapest good option. Note libraries already in the
   dependency set. Done when you can name the seams any solution must fit.
3. **Generate before judging.** Sketch your own candidate before analysing the
   proposal closely, then add the proposal. Options must differ on a structural
   axis: where state lives, push versus pull, synchronous versus queued,
   in-process versus separate process, reuse or buy versus build, schema-first
   versus code-first, now versus defer. Library swaps within one architecture do
   not count as distinct options. Done when there are two or three options, each
   described well enough that its strongest advocate would sign it.
4. **Fix criteria and weights before scoring.** Derive weights from the ranked
   goals and write them down first so scores cannot be tuned toward a favourite.
   Hard constraints become a pass/fail gate row. Done when weights total 100 and
   each weight has a one-line justification.
5. **Evaluate.** Score each option per criterion with a reason that cites code or
   evidence; use a range where uncertain. Write each option's failure story: the
   most likely way it disappoints six months from now. Done when every cell has a
   reason and every option has a failure story.
6. **Decide.** Recommend one option and state the margin: decisive, moderate, or
   within noise. Within noise, prefer the option that is cheaper to start and
   easier to reverse, which usually means the one already proposed. Name flip
   conditions as checkable facts ("if a second process writes this store, choose
   B"). Done when the recommendation and its flip conditions are testable.
7. **Check for bias.** If you agree with the proposal, write the strongest case
   for the best alternative and state precisely why it loses. If you disagree,
   check whether you are penalising unfamiliar style rather than substance. Check
   that no option was scored on something its description never covered. Done
   when both cases are on the page.
8. **Plan the start.** Three to six concrete steps, beginning with the one that
   would reveal fastest whether the recommendation is wrong. Done when step one
   could begin immediately.

## Checklist

### Framing
- A solution phrased as the problem: verify the premise behind it (is X actually
  the slow part? measure or read before accepting).
- Preferences presented as constraints, such as "must use library L" only because
  it is imported once somewhere.
- The missing option: doing nothing, or deferring. What does not solving this now
  actually cost?
- A hidden second project: the proposal solves the stated issue but requires a
  data migration, a new long-running process, or a new dependency that is itself
  a sizeable piece of work.

### Option quality
- Distinctness: two options with the same architecture and different libraries
  collapse into one; replace one of them.
- Strawmen: an alternative described through its worst implementation. Describe
  each as a competent advocate would, including its mitigations.
- Hybrids: the best answer often keeps the proposal's interface and borrows an
  alternative's mechanism. Say so explicitly when that is the recommendation.
- Reuse: search the repository for existing helpers (retry, atomic write, locking,
  scheduling, serialization, configuration) before an option invents new ones.

### Criteria
- **Correctness risk**: number of states and interleavings that can go wrong, and
  whether the design can be tested deterministically.
- **Complexity**: new concepts, processes, dependencies and modules a maintainer
  must hold in their head. Count them; do not describe them as "simple".
- **Reversibility**: cost to back out after a month in use. Persisted formats,
  public interfaces and migrations raise it sharply.
- **Operability**: how failures surface, what must be configured, behaviour on
  restart and upgrade, what an operator does at 2 a.m.
- **Performance**: only when the goals mention it or evidence shows a bottleneck;
  estimate with numbers (calls per second, payload sizes), otherwise mark it not
  material and give it no weight.
- **Time to ship**: including tests, migration and documentation, not only the
  happy path.
- **Codebase fit**: error handling, configuration, logging, threading and
  packaging conventions. A cleaner design that fights the codebase loses points
  honestly.

### Decision hygiene
- Sunk cost: work already spent on the proposal is not a criterion; only remaining
  work counts toward time to ship.
- Novelty: a newer technology is not a benefit in itself.
- Scale mismatch: designing for heavy concurrency in a single-user tool, or the
  reverse.
- Door type: reversible decisions deserve speed; one-way doors deserve a
  falsifying experiment before commitment.

## Evidence standard

- Codebase-fit and complexity claims cite files: "the existing pattern lives at
  path:line"; "option B touches these N modules" (list them).
- Library capability claims are checked against the installed version (its source
  or bundled docs) or labelled "assumed".
- Performance arguments show the arithmetic or a quick measurement; otherwise they
  do not influence the decision.
- Scores are ordinal judgements made inspectable, not measurements. A weighted
  total that contradicts the written recommendation means one of them is wrong;
  resolve it before answering.

## Severity guide

Findings concern the proposed approach.

- **P0**: the proposal breaks a hard constraint, depends on a capability that does
  not exist, or would cause data loss or a security breach on its primary path.
- **P1**: another option wins decisively on the caller's own weighted criteria; the
  proposal has a likely failure story the alternatives avoid; or it solves a
  different problem from the one stated.
- **P2**: the proposal is acceptable but should borrow a specific element from an
  alternative; an ambiguity must be resolved before building.
- **P3**: naming, structure or ordering preferences with little decision impact.

## Skill-specific output

1. **Problem restatement**: problem, ranked goals, hard constraints, non-goals,
   ambiguities with the adopted reading.
2. **Options**: for each (A = the proposal, then B, C), a short description, the
   key mechanism, what it reuses from the codebase, and its failure story.
3. **Decision matrix**: `| Criterion | Weight | A | B | C |`, each cell
   "score 1-5: reason"; a hard-constraint gate row (pass/fail) above the weighted
   rows and a weighted-total row below.
4. **Recommendation**: the chosen option, the margin, the strongest argument
   against it and why it does not prevail, and the flip conditions.
5. **Questions for the user**: only questions whose answers change the
   recommendation, each stating which option each answer favours.
6. **First steps**: numbered, the cheapest step that could prove the choice wrong
   first.

## Anti-patterns

- **Anchoring.** The proposal's vocabulary and decomposition become the frame for
  every option. Rule: write the restatement and your own candidate before reading
  the proposal's details.
- **Sycophancy.** Recommending the proposal because the caller proposed it, and
  softening objections into "consider". Rule: the recommendation follows from the
  matrix, and agreement carries the strongest counterargument with it.
- **Contrarianism.** Disagreeing to look independent. Rule: overturning the
  proposal needs a decisive margin or a constraint violation; within noise, keep
  it and say why.
- **Weights fitted after the fact.** Rule: weights are written before any score.
- **False precision.** Weighted totals to two decimals. Rule: integer scores and a
  qualitative margin.
- **A wish list of questions.** Rule: ask only what changes the answer.
- **Drifting into a plan audit.** Rule: exhaustive failure enumeration of the
  winner belongs to plan-review; recommend running it next
  when the stakes justify it.

## Done when

- [ ] The restatement stands alone and names ambiguities with adopted readings.
- [ ] Your own candidate was formed before the proposal was analysed.
- [ ] Two or three structurally distinct options, none a strawman.
- [ ] Weights were fixed before scores, and every matrix cell has a reason.
- [ ] The recommendation states its margin, its strongest counterargument and its
      flip conditions.
- [ ] Questions are decision-relevant and the first step tests the recommendation.
- [ ] The verdict matches the recommendation's relation to the proposal.
