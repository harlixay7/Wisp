---
name: ai-eval-review
aliases:
  - ai-eval-regression-engine
version: 4.0.0
description: >-
  Use when deciding whether a prompt, model, retrieval, tool or agent change is
  better, worse or indistinguishable from the baseline, or when reviewing an
  eval harness: the claim under test, golden set slices and contamination,
  deterministic graders before LLM judges, judge calibration, repeated paired
  runs with uncertainty, cost and latency, overfitting, and the promotion gate.
  Produces an eval plan matrix, a results table with confidence intervals and a
  promotion decision. Not for auditing prompt wording (use
  prompt-review), retrieval pipeline design (use
  rag-review), or hardware and throughput claims
  (use claim-check).
brief: |
  Mission: decide a stated claim about a model-system change with quantified uncertainty, so the calling agent knows whether to ship, hold or reject.
  - Write the claim and decision rule before looking at results: primary metric, guardrail metrics that must not regress (format validity, safety, cost, latency), the minimum effect worth shipping, and a non-inferiority margin for "no regression" claims.
  - The golden set has slices (typical cases sampled from real use, edge, adversarial, regression cases from past failures) and is checked for leakage: no overlap with prompt examples or the set used while iterating, near-duplicates removed.
  - Grade deterministically wherever possible (exact or normalized match, schema checks, executed tests, tool-argument match); use an LLM judge only for the remainder, with a per-criterion rubric, measured agreement with human labels, and position and verbosity bias controls.
  - Run both arms on the same items, with repeated samples when outputs are stochastic; the item is the unit of analysis. Report the paired difference with a confidence interval, not two separate scores.
  - Inspect raw transcripts from both arms; harness bugs are the most common cause of wrong eval conclusions.
  - Account for tokens, cost and p50/p95 latency per item.
  Output before findings: eval plan matrix, results table with paired differences and intervals, promotion decision (PROMOTE, HOLD with the data needed, REJECT). PASS = the decision is supported by the evidence; PASS_WITH_FIXES = supported after local fixes to analysis or reporting; BLOCK = the decision rests on a harness bug, contamination, a difference within noise, or an ignored guardrail regression.
activation_triggers:
  task_modes:
    - EVAL_DESIGN
    - MODEL_CHANGE_EVALUATION
    - PROMOTION_DECISION
  keywords:
    - golden set
    - eval harness
    - llm judge
    - judge calibration
    - paired comparison
    - promotion gate
    - eval contamination
    - prompt regression
    - model migration
    - pass@k
  do_not_use_when:
    - The prompt text itself needs review or rewriting (use prompt-review).
    - The change is to chunking, retrieval or reranking design rather than its measured outcome (use rag-review).
    - The numbers are throughput, FLOPs or hardware claims (use claim-check).
input_contract:
  requires_worktree: true
  required_inputs:
    - The baseline and candidate configurations, or the eval harness under review
  optional_inputs:
    - The claim and decision the eval must support
    - Existing golden set, per-item results and judge prompts
    - Production failures or traffic samples
    - Budget for model calls and the minimum effect worth shipping
output_contract:
  sections:
    - Eval plan matrix
    - Results table with uncertainty
    - Promotion decision
  findings: shared format
  verdict: shared verdict block
---

# AI evaluation and regression gate

## Mission
An excellent result answers one pre-stated question, such as "the candidate prompt is non-inferior overall within two points and better on the adversarial slice, at no more than ten percent extra cost", with intervals that make the strength of evidence obvious. The calling agent uses it to ship, hold or reject. The most common failure is comparing two aggregate numbers from a single run on a small convenience set graded by an uncalibrated judge and declaring a winner, when the difference is within run-to-run noise or produced by a harness bug.

## Inputs to establish first
- Exact configurations for both arms: model identifier and version, prompt version or hash, sampling settings, tools, retrieval index version, harness commit. A difference not pinned down is not a comparison.
- The claim and the decision it drives. If none is given, write one from the change description and state it as an assumption.
- What exists: golden set location and provenance, grader code, judge prompts, per-item results from earlier runs.
- Budget and stakes: the call budget, and the smallest effect that would change the decision. A low-risk change can be decided on a wider non-inferiority margin; a high-impact model migration needs more items.

## Method
1. Frame the claim. Name the primary metric, guardrails, minimum effect of interest and the decision rule (for example: promote if the lower bound of the 95% interval on the paired difference exceeds minus two points and no guardrail regresses). Done when the rule is written before results are inspected.
2. Audit or design the golden set (checklist). Done when each slice has a size, a source, labels with known quality, and a contamination check result.
3. Audit the graders. Read the grading code; run it on hand-made correct and incorrect outputs, including valid answers in unexpected formats. Done when grader false negatives and false positives are known for each output shape.
4. Calibrate any LLM judge against human labels on a stratified sample. Done when agreement and bias checks are reported.
5. Run or inspect the runs. Both arms on identical items, with repeated samples per item when temperature is above zero or the system is agentic, interleaved in time to avoid provider drift, with infrastructure failures (timeouts, rate limits, crashes) recorded separately from wrong answers. Done when per-item results for both arms exist.
6. Analyze paired differences with intervals, per slice and overall; read raw transcripts of a sample of wins, losses and ties in both arms. Done when the results table is filled and the transcripts confirm the grader judged what it claims to.
7. Decide and record. Apply the pre-stated rule, state PROMOTE, HOLD or REJECT, and specify what is stored for regression tracking. Done when the decision cites the rule and the numbers.

## Checklist

### Claim and metrics
- The primary metric measures the user-relevant outcome (task success, correct extraction), not a proxy that moves with style (length, politeness, format).
- Guardrails are explicit and measured in the same run: format validity, refusal rate on legitimate requests, unsafe output rate, cost per item, p95 latency.
- "No regression" claims use non-inferiority with a stated margin; failing to find a significant difference is not evidence of equivalence.

### Golden set
- Slices: typical (sampled from real usage, not invented), edge (empty, very long, multilingual, malformed input), adversarial (injection attempts, contradictory instructions, out-of-scope requests), regression (every past production failure, kept forever).
- Labels: a double-labeled subset with measured agreement; disagreements adjudicated; ambiguous items fixed or removed before the run and logged, never during the analysis.
- Contamination: no overlap with prompt examples or few-shot blocks (exact and near-duplicate, via n-gram overlap or embedding similarity); a held-out portion never seen while iterating on the prompt; public benchmark items treated as possibly memorized (check with paraphrased variants or a fresh time-split sample).
- Size against effect: for a pass/fail metric on paired items, the standard error of the difference is roughly sqrt(b + c) / n, where b and c count items that flipped in each direction. With 100 items and 10 flips the 95% interval is about plus or minus six points, so a three-point gain on that set is noise. Use this to size the set before running.

### Graders and judges
- Deterministic first: normalized exact match, numeric tolerance, JSON schema validation, executing generated code against tests, set-based precision and recall for extraction, tool-call name and argument match, final environment state for agents.
- Grader bugs to look for: valid answers rejected for formatting, truncated outputs graded as wrong without being flagged, answer keys with errors, cached responses reused across arms, a configuration flag that never reached the candidate.
- LLM judges: one criterion per question, binary or short ordinal scales with anchored descriptions, reference answers when they exist, the judge model and prompt version fixed across arms and runs.
- Calibration: agreement with human majority labels on a stratified sample (as a rule of thumb, a few dozen items per slice that matters), reported as accuracy with a confusion matrix or Cohen's kappa.
- Bias controls: for pairwise judging, run both orders and count only consistent verdicts (report the inconsistency rate as position bias); check the correlation of score with length and test padded variants of the same answer for verbosity bias; avoid a judge from the same model family as one arm when self-preference could decide the result.

### Statistics
- The item is the unit: with k samples per item, aggregate per item first or use a cluster bootstrap over items; treating samples as independent inflates n.
- Paired analysis: McNemar's test or a paired bootstrap for binary outcomes, paired bootstrap or Wilcoxon signed-rank for scores; report the interval of the difference.
- Non-overlapping per-arm intervals are not the test, and overlapping ones do not prove equivalence.
- Many slices produce chance wins and losses: treat slice results as diagnostic unless pre-registered, or correct (Holm). Look closely at any slice that regresses by more than the overall interval width.
- Agents: report pass@k (any of k attempts succeeds) only for settings where retries are free; use pass^k (all k succeed) or per-attempt success for reliability; reset the environment between trials.

### Cost, latency and overfitting
- Per item: input and output tokens, tool calls or steps, cost, p50 and p95 latency, timeout and loop rate. A quality gain that triples cost is a trade-off for the caller to decide, not an automatic win.
- Overfitting: when many prompt variants were tried on the same set, the best one is optimistic (winner's curse); confirm on held-out items. Gains concentrated on items inspected during iteration point to fitting the eval, not the task.
- Regression tracking: store per-item results with configuration hashes and set version; move new production failures into the regression slice; re-baseline whenever the set, grader or judge changes, and never compare scores across set versions.

## Evidence standard
Proof is per-item result files for both arms, the grader code read and exercised on known cases, judge-human agreement numbers, intervals computed by a script the reviewer ran or inspected, and sampled raw transcripts. Not proof: an aggregate score from one run, a dashboard screenshot, a judge score without calibration, a public leaderboard position, or "it looked better in the playground".

## Severity guide
- P0: the promotion decision is wrong or unsupported: a harness bug invalidates results, the candidate configuration was not actually applied, the test items overlap prompt examples, a high-impact change is promoted on a difference inside the interval, or a guardrail regression was observed and ignored.
- P1: single-run comparison of a stochastic system; an uncalibrated judge deciding the primary metric; failed or timed-out items silently dropped; the iteration set reused as the test set; no slice for a known risk.
- P2: cost or latency not measured; no regression tracking; bias checks missing on secondary judged metrics; label quality unmeasured on a small subset.
- P3: reporting improvements (clearer tables, missing per-slice counts) with no decision impact.

## Skill-specific output
1. Eval plan matrix: Claim or metric | Slice | Items | Samples per item | Grader (deterministic or judge, with calibration status) | Decision rule | Status (ready, missing, invalid).
2. Results table: Metric | Slice | Baseline | Candidate | Paired difference | 95% interval | Items (and flips for binary metrics) | Reading (better, worse, indistinguishable, non-inferior). Include cost and latency rows.
3. Promotion decision: PROMOTE, HOLD or REJECT, the rule applied, and for HOLD the specific additional data needed (items per slice, estimated from the observed flip rate). PROMOTE with no P0/P1 findings corresponds to PASS in the verdict block; a decision the evidence cannot support corresponds to BLOCK.

## Anti-patterns
- Two-number comparisons. Corrective: paired per-item differences with an interval, sized against the stated minimum effect.
- Judge as ground truth. Corrective: calibrate against human labels and report agreement before trusting judged metrics.
- Aggregates hiding slices. Corrective: a small overall gain can hide a large adversarial regression; report each slice and flag any slice regression wider than the overall interval.
- Moving the goalposts. Corrective: relabeling, dropping items or changing the rule after seeing results is logged and applied to both arms, or the analysis is redone on fresh items.
- Treating "indistinguishable" as failure. Corrective: a cheaper or faster candidate that is non-inferior within the margin is a valid promotion.
- Demanding huge sets for every change. Corrective: size uncertainty to the decision; a reversible low-risk change can ship on a wider margin with monitoring.
- Trusting the harness. Corrective: read transcripts from both arms before believing any number.

## Done when
- [ ] The claim, guardrails and decision rule were stated before results.
- [ ] Each slice has a source, size, label quality and a contamination check.
- [ ] Graders were exercised on known correct and incorrect outputs; judges are calibrated with bias checks.
- [ ] Results are paired, per item, with intervals, per slice and overall, including cost and latency.
- [ ] Raw transcripts from both arms were sampled and agree with the grades.
- [ ] The promotion decision cites the rule, and the verdict block matches it.
