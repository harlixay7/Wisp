# Wisp Efficacy Benchmark Protocol

Wisp's mechanical claims are covered by the deterministic test suite. What the
suite cannot prove is the product claim: **delegated adversarial review
improves defect detection.** This document defines the measurement that would.

This is a protocol, not yet a result: running it consumes model quota and is
therefore an explicit operator exercise. The harness work (seeded defects,
runner script, scoring) is scoped below so anyone can execute it and reproduce
the numbers.

## Design

Four arms, same task set, same model where possible:

| Arm | Description |
| --- | --- |
| A — baseline | Agent performs the task alone. |
| B — Wisp | Agent performs the task with mandatory Wisp gates (plan hardening + pre-completion audit, per `AGENTS.md`). |
| C — self second pass | The same model re-reviews its own output without Wisp's skill payloads or adversarial framing. |
| D — human | A senior engineer reviews the same outputs (sampled subset). |

## Task set

Seeded defects across six classes, ~10 per class, planted in small real
repositories (≥1k LOC) so context pressure is realistic:

1. **Correctness** — off-by-one, inverted condition, wrong variable.
2. **Concurrency** — unsynchronized shared state, missing await, lock-order.
3. **Security** — path traversal, injection, secret in child env.
4. **Data/state** — non-atomic writes, breaking migration, float money.
5. **Architecture** — broken wiring, dead export, stub masquerading as done.
6. **Claims** — documentation numbers that contradict code (perf, geometry).

Each defect ships with: injection commit, ground-truth description, severity
(P0/P1/P2), and a mechanical detector where possible (test or grep).

## Measures

- **Defect recall** per class and overall (found / seeded).
- **Severity-weighted recall** (P0=3, P1=2, P2=1).
- **False positives** (flagged non-defects), reviewed against ground truth.
- **Unique Wisp yield** — defects found by arm B that neither A's author nor
  C's pass caught (Wisp's marginal value).
- **Cost**: wall-clock time, tokens, and quota units per arm.
- Gates honored: did arm B actually stop on `FUNDAMENTAL_REJECTION`?

## Environment fingerprint

Every run records the provenance block Wisp already emits (versions, commit,
registry hash, models) plus OS/Python, so results are attributable and
reproducible.

## Reporting

Results land in `docs/benchmark-results.md` with per-class tables, the raw
delegation reports under `.antigravity-reports/` (excluded from git), and an
explicit threats-to-validity section (model drift, seed visibility, prompt
overlap between arms).

## Skill-cost companion measurement (audit #54)

Second protocol: fix the task set, vary skill payload — `no skills` / `1
skill` / `3 skills` / `all skills` — and measure prompt tokens, latency,
critique quality (rubric), and defect recall. Answers whether the skill
architecture's context cost earns its keep, and at what dose.
