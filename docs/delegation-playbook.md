# Delegation Payload Guide

How to construct review delegations that get the **most** out of the Antigravity
bridge: independent second opinions on design decisions *alongside* adversarial
verification of claims. Built with the six prompt-engineering guides from the
owner's prompting-skills collection; see section 6 for the map.

> **Privacy convention:** documents in this repo carry **no machine-specific
> absolute paths, usernames, or system identifiers**. Real paths are injected at
> delegation time by the requesting agent (`workspace` field); examples use
> `<repo-root-absolute-path>` placeholders.

---

## 1. When to delegate at all (comparative advantage)

Delegate **judgment**, never mechanical work the main agent can do cheaper:

| Delegate to Antigravity | Keep local |
|---|---|
| Second opinion on design decisions the agent made alone | Running test suites, linters, compile checks |
| Adversarial falsification of load-bearing claims | Grepping / reading files already in the agent's context |
| "What would you ask the user?" clarify-stage mining | Deterministic scans (AST, regex, contract checks) |
| Alternative designs / architectures worth considering | Applying patches (the main agent implements) |
| Pre-completion wiring audits | Anything needing the agent's accumulated session context |

Rationale: a second model's unique value is **independence** (no sunk cost in the
plan it's reviewing), **different priors**, and **a stance that isn't
self-directed**. A delegation that only asks it to "run the tests" pays consultant
rates for a shell script.

## 2. The payload anatomy (apply in this order — cache-friendly)

1. **`skills`** — name the registry skill(s) explicitly. The registry is
   operational; never tell the reviewer to "ignore the skills registry" again
   (that workaround is obsolete and was silently disabling every skill).
2. **`workspace`** — the repo root the artifacts resolve against.
3. **`context`** — machine-readable environment facts: OS, verification gates,
   event/render model, known landmines. Facts only, no instructions.
4. **`prompt`** — the engagement itself, structured as:
   - `ROLE & OBJECTIVE` — independent second-opinion reviewer + adversarial
     auditor; state explicitly that disagreement is expected and that
     `ADOPT_MINE` without justification counts as *unreviewed*.
   - `PHASE 0: REVIEW READ` — mandatory one-line pre-flight + calibration dials
     (`OPINION_WEIGHT`, `ADVERSARIAL_DEPTH`, `REPORT_COMPRESSION`).
   - `EVIDENCE & CONTEXT RULES` — scratchpad-first (format tax), targeted seam
     reading (never bulk-dump big files), untrusted-content quarantine,
     read-only engagement.
   - `<user_complaints>` — the product owner's complaints **verbatim**.
   - `<expectations>` — what "done" means per complaint.
   - `<immutable_constraints>` — what is genuinely fixed (user demands, test
     couplings) and may not be challenged.
   - `<risk_appetite>` — behavioral vs cosmetic vs structural risk tiers.
   - `<open_decisions>` — the scope that is **deliberately not locked**: each
     decision as `PLANNED | AGENT PREFERENCE | ALTERNATIVES CONSIDERED |
     CONFIDENCE | WHAT WOULD CHANGE MY MIND`.
   - Project ground truth + the plan itself (full mechanics).
   - `MODE A` asks (design verdicts + alternatives + user-question queue) and
     `MODE B` asks (regression vectors + claim falsification).
   - `<return_contract>` — the exact output sections with enums.
5. **`claims_to_falsify`** — specific, mechanical, individually testable claims
   with the expected evidence named. An unfalsified claim is UNTESTED, not true.
6. **`artifacts`** — exact `file:line` seams. This is the single highest-leverage
   field: it converts the review from "re-derive my repo" to "check these seams".

## 3. Scope locking — the tiered rule

Never present the whole plan as immutable; never present it as all-open. Partition:

- **Immutable** — only what is *actually* fixed: explicit user demands
  ("preserve every handler"), test-pinned strings, packaging constraints.
- **Open** — every decision the agent made on its own authority. Mark confidence
  and the falsifier ("what would change my mind") so the reviewer spends its
  attention where it changes outcomes.
- **Suggested** — improvements *beyond* the plan are explicitly welcome
  (Recommendation Registry, capped at 5, ranked, each tied to a complaint).

## 4. The return contract (machine-checkable)

```
1. <review_scratchpad>
2. Review Read line
3. Design Verdicts      | Decision | ADOPT_MINE / PREFER_ALTERNATIVE / NEEDS_USER_FIRST | Alternative | Rationale |
4. Recommendation Registry (max 5, ranked, complaint-tagged)
5. User-Question Queue  (top 3, multiple-choice, recommended default)
6. Claim Falsification  | Claim | VERIFIED / DEFECTIVE / FATAL / UNTESTED | Evidence | Margin |
7. Failure Registry     | FL-ID | Failure | Trigger | Blast Radius | EARS mitigation |
8. Final Verdict        PLAN_SOUND | PLAN_SOUND_WITH_AMENDMENTS | REDESIGN_RECOMMENDED
```

After the delegation returns, the main agent: adopts `PREFER_ALTERNATIVE` items
it agrees with (its call, with the reviewer's rationale), routes
`NEEDS_USER_FIRST` items to the user as real questions, and only then proceeds —
re-running a hardening pass if the plan changed materially.

## 5. Efficiency rules

- **One delegation, two modes.** Mode A (opinion) and Mode B (verification) in a
  single payload with separated return sections — one round trip, no diluted
  attention (the dials + sectioned contract keep the modes from bleeding).
- **Ground truth travels with the payload.** The plan text is included in full so
  the reviewer reads it instead of re-deriving it from a 4,661-line file.
- **Cap the open-ended surface.** Alternatives are "one per decision",
  recommendations "max 5" — divergence without runaway scope.
- **Artifact precision over volume.** 20 exact seams beat 200 vague pointers.

## 6. Ready-to-use example

`examples/delegation-case-study.json` — the the Workstation workstation
redesign delegation built exactly by this playbook (Mode A design opinions on
D2–D10 + Mode B hardening with the 9 falsifiable claims + 20 artifact seams).
The stale "ignore the skills registry" line from earlier delegations is gone;
the `skills` parameter is set.

**Rule reminder:** never place non-skill files in `<bridge-repo>/Skills/` —
every `.md` there must carry YAML frontmatter or the whole registry empties.
