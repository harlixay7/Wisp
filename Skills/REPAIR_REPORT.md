# Skill Family Repair Report

**Date:** 2026-10-07
**Scope:** All 12 skills in the `Skills/` registry — repaired, completed, and hardened to v3.0.0 using a six-part prompt-engineering guide set.

---

## 1. Defects Found and Fixed

| # | Defect | Files Affected | Fix |
|---|---|---|---|
| 1 | Gemini `[cite: N]` citation artifacts (hundreds of dead references) | 02–12 | Stripped; replaced with self-contained reasoning mandates |
| 2 | Escaped/broken markdown (`\*\*`, `\_`, `&#x20;`, double blank lines) | 02–12 | Full rewrite with clean markdown; ~40% token bloat removed |
| 3 | Unparseable YAML frontmatter (escaped delimiters/keys) | 02–12 | Valid YAML frontmatter, machine-verified with PyYAML (12/12 parse clean) |
| 4 | **Truncated file — TDD lifecycle missing** (ended mid-sentence at "four isolated phases:") | 04 | Reconstructed the full five-phase lifecycle (REPLICATE → IMPLEMENT → REGRESS → REFACTOR → REPORT) with hard phase guardrails, edit-format calibration, and dependency sanitation |
| 5 | **Truncated file — half the skill missing** (ended mid code block; Vectors 2–4, EARS, prohibitions, deliverables absent) | 09 | Reconstructed all four vectors (path portability, git tree/binary exclusion, dependency pinning/lockfiles, pre-flight doctor) plus EARS matrix, prohibitions, acceptance contract, and deliverables |
| 6 | Claimed "five core failure vectors" but listed only four | 01 | Added Vector 5: Dependency & Environment Assumptions |
| 7 | Technical error: WAL prescribed for JSON config caches (WAL is a SQLite mode) | 05 | Corrected: WAL for SQLite; tempfile + `os.replace` + fsync for JSON/state files |
| 8 | Statistically unsound "verifiable 0.0% hallucination rate" | 06 | Rewritten as "zero hallucination events on the golden set (0/N with Wilson confidence interval)" |
| 9 | Author-environment leakage (local credential entries and machine-specific hardware presented as mandates) | 07, 03 | Generalized; worked examples relabeled as illustrative; datasheet grounding re-verified per-device |
| 10 | Pseudo-precise scoring rubric doubling as the release gate | 02 (pattern applied family-wide) | Rubric demoted to an overridable calibration default; release gate moved to binary, machine-checkable acceptance contracts |
| 11 | Massive keyword collisions (`regression` ×4, `benchmark`/`latency` ×2, `schema`/`transaction` ×2, `mcp` ×2, etc.) | all | Curated discriminating keyword sets; added `do_not_use_when` routing to every skill; verified zero remaining cross-skill keyword overlap |
| 12 | Dangling references ("Write-Select-Isolate protocol", "Agent 1") never defined | all | WSCI now defined canonically in the shared kernel; upstream-agent references generalized with compatibility note |
| 13 | Duplicated EARS primers / Format-Tax preambles bloating every file | all | Compressed into a shared-protocol kernel (common core, domain-adapted per skill) |
| 14 | All-negative constraint lists (known to cause constraint collapse and weaker adherence) | all | Directional framing: positive mandates first, bans paired with required behavior, constraints grouped into categorized sections |
| 15 | JSON-RPC error code list incomplete | 12 | Completed standard set (−32700, −32600, −32601, −32602, −32603) |
| 16 | SSRF blocklist incomplete | 07 | Full private-range coverage incl. `172.16/12`, link-local, `::1`, `fc00::/7`, plus post-resolution DNS re-validation |

## 2. Prompting-Guide Application Map (all six guides used)

| Guide | Where It Was Applied |
|---|---|
| **G1 — Meta-Prompt Architect** | Every skill restructured under the Universal Prompt Blueprint: `[ROLE & OBJECTIVE] → [GROUND TRUTH] → [DIRECTIONAL MANDATES & PROHIBITIONS] → [ACCEPTANCE CONTRACT] → [OUTPUT SHAPE]`. Declarative scoping throughout (goals + constraints, no procedural hand-holding). Instruction density kept well under the ~150–200 rule saturation boundary by deduplication. Tier-4 principle baked in: deterministic verification (grep/AST/tests/exit codes) is never LLM-judged. |
| **G2 — Anti-Slop Interface Engine** | Its meta-methodology generalized from UI to audit skills: (a) Phase-0 brief inference → every skill now opens with a mandatory one-line pre-flight declaration ("Plan Read" / "Audit Read" / "Eval Read" / "Profile Read" / "Remediation Read"); (b) the Three-Dial parametric framework → every skill carries three calibration dials with defaults (scan breadth, rigor/intensity, report compression); (c) ban/mandate pairing — no prohibition ships without the required replacement behavior; (d) zero-emoji and craft-discipline rules carried into the kernel. |
| **G3 — Spec-Driven Rigor** | EARS syntax retained and strengthened (five variants, immutable REQ-xxx IDs per skill prefix). Skill 04 completed directly from G3's phase-isolated TDD: RED-phase production-file prohibition, GREEN-phase test-tampering prohibition, REFACTOR signature lock. Dependency sanitation (zero hallucinated packages, no speculative libraries) and error-handling discipline embedded in 04 and 09. |
| **G4 — Context Lifecycle & Cache Optimization** | The Write-Select-Compress-Isolate protocol — previously referenced but never defined — is now canonically defined in every kernel (Write/Select/Compress/Isolate with per-skill domain adaptation). KV-cache tiering honored structurally: static contract (frontmatter, mandate, kernel) ordered before dynamic content, and skills instruct the orchestrator not to interpolate per-run data into mandate sections. Tool-registry ceiling (<20–30 definitions) enforced in skill 12's kernel. |
| **G5 — Cognitive De-taxing & Adversarial Hardening** | Format-Tax handling made explicit: Pattern B (single-pass scratchpad buffering) is the default execution mode; Pattern A (two-pass freeform reasoning → grammar-constrained transduction) documented as the high-stakes orchestrator option. Directional framing converted negative-only lists into positive formulations. Defense-in-depth added family-wide: instruction hierarchy (system contract outranks content directives), untrusted-content quarantine (`<untrusted_evidence>` = data, never instructions), deterministic action gating (audits emit proposals/EARS requirements; only skill 04 may mutate, and only behind RED-phase proof). |
| **G6 — Agent Governance & Pipeline Compilation** | Charter discipline: every skill kept under the length ceiling (~11–13 KB, down from 5–13 KB unclean). Edit-format calibration table (search/replace <30 lines, whole-file, unified diff) restored into skill 04. Closed-loop verification protocol (replicate → fail-verify → surgical patch → regression run → self-debugging loop with escalation after two failed fixes) forms skill 04's Phase 3. Protocol-reasoning decoupling: machine-parsed contracts (frontmatter, verdict enums, registry well-formedness rules) separated from prose instructions. |

## 3. Validation Evidence

- **YAML parse:** 12/12 files parse with PyYAML (01 as full document; 02–12 frontmatter). `ALL VALID`.
- **Residual artifacts:** 0 occurrences of `[cite:`, `&#x20;`, `\*\*`, `\_` across the directory.
- **Truncation:** every file terminates with its final deliverable section (verified programmatically).
- **Keyword routing:** 0 overlapping keyword pairs across the 12 skills.
- **Size uniformity:** all files 10.5–13.1 KB (previously 04/09 were conspicuously half-size at ~5 KB).

## 4. Remaining Recommendations (not applied — orchestrator decisions)

1. **Runtime conformance:** if these target a ZCode/Claude-style skill loader rather than the existing orchestrator schema, each needs a `SKILL.md` rename with description-driven triggering. The current custom schema (task_modes/input_contract/output_contract) was preserved deliberately.
2. **Optional shared-kernel file:** the kernel is intentionally inlined (standalone loadability). If the orchestrator supports includes, a canonical `00_SHARED_PROTOCOL` with per-skill references would eliminate the remaining inlined duplication.
3. **Pipeline manifest:** the implied lifecycle (01 plan → {02,03,05,07,08,11,12} audit → 04 implement → 09 release → 10 docs) is now documented in each `do_not_use_when` routing, but an explicit pipeline manifest for the orchestrator would make execution order first-class.
4. **Ground-truth testing:** the acceptance contracts are now machine-checkable by design; a small harness asserting verdict-enum validity and registry well-formedness per skill would close the loop on the skills themselves.
