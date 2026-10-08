---
name: claim-check
aliases:
  - empirical-claim-falsification-engine
version: 4.0.0
description: >-
  Use when a quantitative claim must be checked before it is believed, merged
  or published: latency, throughput, memory, accuracy, cost, speedup or
  hardware-limit numbers in a PR, README, benchmark table, plan or chat answer.
  Produces a claim ledger that re-derives each number by reproduction or by
  first-principles bounds and rules it holds, false or unverifiable. Not for
  finding where time goes in a running system (use
  performance-profiling), for model or prompt quality evals (use
  ai-eval-review), or for prose accuracy without numbers (use
  docs-accuracy-review).
brief: |
  Mission: for every quantitative claim, decide whether the evidence supports it as stated, and recompute the number independently.
  - Pin each claim exactly: quantity, unit, statistic (mean, median, p99, best-of), workload, hardware, versions, and the baseline it is compared against. A claim missing its conditions is unverifiable as stated, not true.
  - Trace provenance before arithmetic: which script, commit and raw output produced the number.
  - Reproduce with the repository's own benchmark scripts first; when you cannot run them, say why and fall back to bounds.
  - Check measurement hygiene: warm-up excluded, steady state reached, enough runs, spread and a confidence interval reported, timer resolution adequate, frequency scaling, caches and background load controlled.
  - Check comparability: same inputs, hardware, versions and build mode; baseline configured fairly; one variable changed at a time.
  - Run sanity bounds: Little's law, Amdahl's law, bandwidth and compute peaks. A number beyond a physical bound is false whatever the log says.
  - Catch statistics misuse: mean of ratios, best run reported as typical, percentiles from too few samples, selective reporting, speedups compounded from overlapping parts.
  Emit a claim ledger (ID, as stated, pinned form, recomputed, method, HOLDS/FALSE/UNVERIFIABLE, calibrated wording) before the findings.
  PASS: every claim holds within measured noise. PASS_WITH_FIXES: numbers are directionally right but need scope, units or wording corrected. BLOCK: a headline claim is false or contradicted by a bound.
activation_triggers:
  task_modes:
    - EMPIRICAL_CLAIM_AUDIT
    - BENCHMARK_REPRODUCTION
    - PERFORMANCE_CLAIM_VERIFICATION
  keywords:
    - speedup claim
    - benchmark claim
    - roofline
    - flops
    - confidence interval
    - little's law
    - amdahl
    - back-of-envelope
    - reproduce benchmark
    - cost claim
  do_not_use_when:
    - The question is where time or memory goes in a running system (use performance-profiling).
    - The numbers are model or prompt quality scores from an eval harness (use ai-eval-review).
    - The text has no numbers and the concern is accuracy or tone of prose (use docs-accuracy-review).
input_contract:
  requires_worktree: true
  required_inputs:
    - The claims under test, quoted or located by path:line, or the document or PR that contains them
  optional_inputs:
    - Benchmark scripts, raw logs or CSVs that produced the numbers
    - Target hardware and software environment of the original measurement
    - Tolerance the claimant considers acceptable
output_contract:
  sections:
    - Environment record
    - Claim ledger
    - Bounds worksheet
  findings: shared format
  verdict: shared verdict block
---

# Empirical claim falsification

## Mission

Turn every number under review into a pinned, independently recomputed entry with an
explicit status, so the calling agent knows which figures it may repeat, which need
rewording, and which must be withdrawn. An excellent result reproduces what can be
reproduced, bounds what cannot, and never upgrades "plausible" to "verified". The most
common failure is checking the arithmetic (120 / 40 = 3x) while never asking whether
the 120 and the 40 came from comparable runs.

## Inputs to establish first

- The exact claim text and where it lives. Search the change set for numbers with
  units: `rg -n '[0-9][0-9.,]*\s?(ns|us|µs|ms|s|x|×|%|[KMGT]i?B(/s)?|req/s|ops/s|tok/s|QPS|FPS|\$)' README.md docs/ CHANGELOG*`
  plus the PR description and envelope claims.
- The producing evidence: script, command, commit and raw output. `git log -S'<number>' --oneline -- <doc>`
  shows when a figure entered the docs; compare with later commits to the code it describes.
- The environment of the original measurement (CPU/GPU model, memory, OS, runtime
  versions, build mode, power state). If unknown, record that, because it limits which
  comparisons are legitimate.
- The tolerance. If none is given, use the measured run-to-run spread as the tolerance.
- If there is no raw data and nothing can be run, adjudicate with bounds only; a claim
  that no bound falsifies stays UNVERIFIABLE, with the exact experiment that would settle it.

## Method

1. **Pin.** Rewrite each claim in canonical form: `<statistic> of <metric> = <value> <unit>
   for <workload> on <environment>, versus <baseline>`. List every slot the source leaves
   empty. Done when every claim has an ID and its missing conditions are named.
2. **Bound.** Before running anything, check the number against cheap invariants (see
   the bounds checklist). A claim that violates a hard bound is FALSE without reproduction.
   Done when each claim is marked within bounds, beyond bounds, or no applicable bound.
3. **Reproduce.** Run the repository's own harness at the cited commit when possible,
   then at HEAD. Use the stack's statistical runner: `hyperfine --warmup 3 --runs 20
   --export-json out.json 'cmd A' 'cmd B'` for commands; `python -m pyperf timeit` or
   pytest-benchmark for Python; Google Benchmark, criterion, JMH; `go test -bench . -count 10`
   piped to `benchstat`. Interleave arms (ABAB...) rather than running all of A then all
   of B, so drift hits both equally. Done when each reproducible claim has a recomputed
   value with an interval, or a stated blocker.
4. **Compare fairly.** For every comparative claim, enumerate every difference between
   the arms: inputs, data size, build flags, versions, configuration, cache state, what
   the timer includes. Done when each comparison lists its differences and states which
   one the claim attributes the effect to.
5. **Adjudicate and calibrate.** Assign HOLDS, FALSE or UNVERIFIABLE and write the
   strongest wording the evidence supports, with its conditions. Done when every ledger
   row has a status and a replacement sentence where the original overreaches.

## Checklist

**Claim definition**
- Statistic unnamed: "latency is 12 ms" could be mean, median, best or p99.
- "Up to" claims: a maximum presented as typical. Ask for the median alongside.
- Percentile resolution: a p99 needs on the order of a thousand samples per run to be stable as a rule of thumb; a "p99" from 20 samples is the maximum.
- Where the clock sits: client-observed latency includes queueing, network and serialization; server-side handler time does not. Claims often mix them.
- Open versus closed loop: a closed-loop generator (each client waits for its response before sending the next) hides queueing delay, known as coordinated omission. Throughput-at-latency claims need a constant-arrival-rate generator (wrk2, k6 arrival-rate executors, vegeta) or HDR-histogram correction.
- Percent wording: "50% faster" (1.5x throughput) and "50% less time" (2x) differ; "reduced by 200%" is meaningless.

**Measurement hygiene**
- Timer choice: wall clocks that can step (`time.time()`, `Date.now()`) versus monotonic high-resolution clocks (`time.perf_counter_ns()`, `performance.now()`, `std::chrono::steady_clock`). Browser timers are deliberately coarsened. Operations shorter than about 100x the timer resolution must be batched.
- Warm-up: JIT tiers (JVM, V8, PyPy), lazy imports, connection pools, first-call compilation (`torch.compile`, numba) belong in a separate cold-start figure.
- Asynchronous devices: GPU kernels launch asynchronously; timing without `torch.cuda.synchronize()` or CUDA events measures launch, not execution.
- Dead-code elimination: a microbenchmark whose result is unused may measure nothing; look for `black_box`, `Blackhole.consume`, `benchmark::DoNotOptimize` or equivalent.
- CPU frequency and thermals: governor (`cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor`), turbo, laptop on battery, sustained runs that throttle. `pyperf system tune` or a fixed governor removes much of this.
- Caches: OS page cache makes the second file read come from RAM; database buffer pools, HTTP caches, memoization in the code under test, and CPU caches sized to the benchmark's working set. Claims must say warm or cold, matched across arms.
- Noise: shared CI runners and virtual machines commonly show run-to-run spread of several percent or more; an effect smaller than the spread is not demonstrated.

**Comparability**
- Build mode: debug versus release, assertions on, sanitizers, `-O0` versus `-O2`, Python with tracing or coverage active.
- Unfair baseline: baseline with a pool of 1 versus candidate with 16, baseline with logging at DEBUG, baseline including process start-up while the candidate is measured in-process, baseline on an older dependency version.
- Different inputs: smaller dataset, different distribution (sorted versus random keys), different payload size, fewer concurrent clients.
- Multiple changes per comparison: the speedup is attributed to the advertised change while a version bump, flag or cache warm-up also differs.

**Bounds**
- Little's law, L = λW: 10,000 req/s at 50 ms mean latency needs about 500 requests in flight. If the server has 64 workers and no async IO, the claim is impossible.
- Amdahl: if the optimized part was 30% of runtime, the end-to-end speedup cannot exceed 1 / 0.7 ≈ 1.43x even if that part drops to zero. Obtain the original share from a profile.
- Bandwidth: bytes moved divided by claimed time must stay below the medium's sustained bandwidth (memory, PCIe, NVMe, network). Sustained figures are below datasheet peaks; STREAM-style memory tests commonly reach well under peak as a rule of thumb.
- Roofline: time ≥ max(FLOPs / attainable peak FLOP/s, bytes / attainable bandwidth). Check the precision and density of the quoted peak (FP32 versus FP16 versus INT8; vendor sheets often quote sparse figures that are double the dense ones) against the exact device SKU.
- Durability: a single-threaded loop that fsyncs every write cannot exceed the device's flush rate; claims of very high durable writes per second imply batching or a missing fsync.
- Units: bits versus bytes (8x), GB versus GiB (about 7.4%), MB/s versus Mb/s, tokens versus characters, per item versus per batch.

**Statistics**
- Mean of ratios across benchmarks; normalized speedups aggregate with the geometric mean, or report ratio of totals with the weighting explained.
- Best-of-N reported as the typical result; minimum time is legitimate only for noise-free microbenchmarks and must be labeled.
- No interval: a single number without spread cannot show an improvement. A 95% interval from a t-interval on run means, or a bootstrap on samples, is the minimum.
- Overlapping intervals presented as a win; "statistically significant" without the test named.
- Forking paths: many configurations tried, the best one reported; ask for the full sweep or the pre-declared configuration.
- Compounded gains: 2x from A and 2x from B is 4x only if they act on disjoint, sequential time; usually they overlap.
- Accuracy and rate claims on small sets: binomial standard error is sqrt(p(1-p)/n); at p = 0.8 and n = 200 that is about 2.8 points, so a 2-point "gain" is noise.

**Memory and cost**
- Memory: RSS versus heap versus virtual size; peak versus steady state; shared pages double-counted across processes; PyTorch allocated versus reserved memory.
- Cost: unit price with its date and region, measured versus assumed tokens or requests per task, cache discounts, egress and idle capacity omitted.

## Evidence standard

Proof is a command, its raw output, the run count and spread, and an environment
record (`git rev-parse HEAD`, `uname -a`, `lscpu` or equivalent, runtime versions, build
mode, governor). Arithmetic is shown inline with units carried through every step. A
datasheet figure cites the exact SKU and its source; recalled from memory it is medium
confidence. Not proof: the claimant's log without the command, a single run, a dashboard
screenshot without its time window, or your own run on different hardware used to judge
an absolute number (compare ratios there, not absolutes).

## Severity guide

- P0: a headline or published number is false beyond noise in the favorable direction, violates a physical bound, or measures a no-op (dead-code eliminated, cache hit, asynchronous launch only); a capacity figure that would make users under-provision.
- P1: the claim holds only under unstated favorable conditions (warm cache, best run, unfair baseline, different build mode); magnitude overstated by more than the reported uncertainty; an SLO set from a percentile with too few samples.
- P2: the number reproduces but lacks statistic, units, conditions or spread; a small unit confusion that does not change a decision.
- P3: reporting improvements, such as adding the interval or the raw-data path.

## Skill-specific output

**Environment record**: one short block for the reviewer's own runs: commit, hardware,
OS, runtime versions, build mode, governor or power state, background load.

**Claim ledger** (one row per claim):

| ID | Location | As stated | Pinned form | Recomputed | Method | Status | Calibrated wording |
| --- | --- | --- | --- | --- | --- | --- | --- |

- Pinned form: statistic, unit, workload, environment, baseline; missing slots marked `?`.
- Recomputed: value with interval and N, or the bound that applies.
- Method: `reproduced: <command>`, `bound: <law>`, or `arithmetic`.
- Status: HOLDS (within measured noise under the stated conditions), FALSE (contradicted
  by reproduction beyond noise or by a bound), UNVERIFIABLE (no data and no decisive
  bound; name the experiment that would settle it).

**Bounds worksheet**: for each bound used, the formula, inputs with sources, and result.
This derivation matrix lets the caller re-check the reasoning.

## Anti-patterns

- **Arithmetic without provenance.** Confirming that the ratio is computed correctly from incomparable numbers. Audit where both numbers came from before dividing them.
- **Unverifiable recorded as false.** FALSE needs counter-evidence: a reproduction outside noise or a violated bound. Without it, mark UNVERIFIABLE and name the missing evidence.
- **Absolute numbers judged on different hardware.** Your laptop being slower does not falsify a server figure. Compare relative effects, and state the environment difference.
- **Exempting your own measurements.** Your runs obey the same hygiene: warm-up, N, interval, interleaving. A single reviewer run cannot overturn a well-documented claim.
- **Unit pedantry.** GB versus GiB matters when it changes a decision or exceeds the noise; otherwise it is one P3 line, not a finding per occurrence.
- **Drifting into optimization.** This skill rules on claims. If the question becomes why the system is slow, say so and route to performance-profiling.

## Done when

- [ ] Every quantitative claim in scope has a ledger row with a pinned form.
- [ ] Each FALSE row cites a reproduction beyond noise or a violated bound.
- [ ] Each UNVERIFIABLE row names the exact experiment that would settle it.
- [ ] Every comparative claim lists the differences between its arms.
- [ ] Every reviewer measurement has N, spread and an environment record.
- [ ] Overreaching claims have calibrated replacement wording.
