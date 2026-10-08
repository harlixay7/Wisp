---
name: telemetry-hardware-profiling-gate
version: 4.0.0
description: >-
  Use when a system is slow, resource-hungry or regressing and the cause must
  be found by measurement, or when a performance-sensitive change needs a
  before/after gate: CPU hotspots, allocation and GC pressure, lock
  contention, IO wait, event-loop or UI-thread blocking, N+1 queries,
  serialization cost, cold starts and tail latency. Produces a measurement
  plan, a profiler-backed hotspot table and changes with predicted effects and
  confirmation steps. Not for adjudicating numbers someone already published
  (use empirical-claim-falsification-engine) or for model quality regressions
  (use ai-eval-regression-engine).
brief: |
  Mission: find where time and resources actually go, prove it with profiler or trace evidence, and recommend changes whose effect can be confirmed.
  - Measure before optimizing. A proposed optimization whose target is not shown hot in a profile or trace is unsupported.
  - Run a USE pass first (utilization, saturation, errors for CPU, memory, disk, network, locks, pools) to name the limiting resource.
  - Pick the profiler for the stack and question: sampling profilers (py-spy, perf, async-profiler, pprof, Chrome DevTools, Perfetto) for where CPU goes; wall-clock or off-CPU views for waiting; allocation profilers for memory and GC; tracing for causality across async and IO. A CPU profile cannot explain time spent blocked.
  - Classify the bottleneck by mechanism: compute, allocation/GC, lock contention, IO wait, event-loop blocking, N+1 queries, serialization, cold start.
  - Report latency as distributions per phase (N, p50, p95, p99, max), never averages alone; account for tail amplification under fan-out and queueing near saturation.
  - Predict each change's effect from its measured cost share, and confirm with a before/after protocol: same workload, build and environment, interleaved runs, a regression budget.
  Emit a measurement plan, a hotspot table (location, cost share, mechanism, evidence), a latency table, and recommended changes with expected effect and how to confirm.
  PASS: budgets met with evidence and no regression. PASS_WITH_FIXES: hotspots identified with clear local fixes. BLOCK: the change breaches a budget on a primary path, leaks resources, blocks a loop for user-visible time, or optimizes without measurement at a correctness cost.
activation_triggers:
  task_modes:
    - PERFORMANCE_PROFILING
    - LATENCY_DIAGNOSIS
    - RUNTIME_PERFORMANCE_GATE
  keywords:
    - profiler
    - flame graph
    - py-spy
    - perf record
    - pprof
    - hotspot
    - p99
    - tail latency
    - event loop lag
    - lock contention
    - allocation profiling
    - cold start
  do_not_use_when:
    - The task is checking whether a published or claimed number is true (use empirical-claim-falsification-engine).
    - The regression is in model or prompt output quality rather than speed or resources (use ai-eval-regression-engine).
    - The concern is retrieval relevance rather than retrieval latency (use hybrid-rag-retrieval-grounding-engine).
input_contract:
  requires_worktree: true
  required_inputs:
    - The symptom or the performance-sensitive change, and the code path or entry point involved
  optional_inputs:
    - A command or load profile that reproduces the workload
    - Latency or resource budgets (SLOs) and the target environment
    - Existing profiles, traces or metrics dashboards exports
output_contract:
  sections:
    - Measurement plan
    - Hotspot table
    - Latency distribution table
    - Recommended changes
  findings: shared format
  verdict: shared verdict block
---

# Profiling and performance gate

## Mission

Locate the real bottleneck with evidence a skeptic would accept, explain its mechanism,
and hand the calling agent changes ranked by measured payoff with a way to confirm each.
A strong result attributes most of the gap between observed and budgeted performance to
named code locations. The most common failure is optimizing what looks slow in the
source (a nested loop, a regex) while the time is actually spent waiting on a lock, a
query or the network.

## Inputs to establish first

- Symptom in measurable terms: which operation, which statistic, how far from budget.
  "Slow" becomes "p99 of `GET /search` is 1.8 s against a 400 ms budget at 50 req/s".
- A reproducible workload: a command, test, load script or captured request set. If
  none exists, build the smallest one that shows the symptom and state its limits.
- Environment: hardware, core count, memory, runtime versions, build mode (profiling
  a debug build misleads), container limits (`cat /sys/fs/cgroup/cpu.max`), whether
  the system is shared.
- Budgets: given SLOs, or a regression budget relative to the baseline commit. If
  none is given, propose one (for example, p95 must not regress by more than the
  measured noise plus 5%, as a rule of thumb) and say it is proposed.
- If nothing can be executed, do static analysis only, label every hotspot claim medium
  or low confidence, and make the measurement plan the main deliverable.

## Method

1. **Reproduce and baseline.** Run the workload at the baseline commit, capture a
   latency distribution and resource use. Done when the symptom reproduces with N and
   percentiles recorded, or the failure to reproduce is itself reported.
2. **USE pass.** For each resource, check utilization, saturation and errors:
   `vmstat 1` (run queue `r` above core count means CPU saturation; `si`/`so` means
   swapping), `iostat -x 1` (`%util`, `await`), `pidstat -u -w -p <pid> 1` (CPU and
   context switches), `ss -s`, connection-pool and thread-pool wait metrics, GPU via
   `nvidia-smi dmon`. Done when the limiting resource class is named or each is ruled out.
3. **Profile the right dimension.** On-CPU sampling for compute; wall-clock or off-CPU
   for waits; allocation profiles for memory; traces for cross-service or async
   causality. Capture steady state separately from start-up. Done when one profile
   covers steady state and, where relevant, one covers the slow tail or cold start.
4. **Attribute.** Read self versus inclusive cost; confirm each top hotspot with a
   second, independent view (call counts, a targeted microbenchmark, a query log, a
   trace span). Done when most of the gap to budget is attributed to named locations,
   with any unexplained remainder stated.
5. **Predict.** For each candidate change, estimate the effect from the measured share
   (removing a 30% self-time hotspot entirely caps the gain at about 1.43x) and list
   side effects (memory, complexity, correctness risk). Done when each change has an
   expected effect with its derivation.
6. **Confirm.** Before/after on the same workload, build and machine, interleaving runs,
   comparing full distributions and resource use, and checking that cost did not just
   move (to another thread, process, or the database). Done when each applied or
   proposed change has a confirmation result or a precise confirmation protocol.

## Checklist

**Choosing and trusting the profiler**
- Python: `py-spy record -o out.svg --rate 200 -- python app.py` (or `--pid`); add `--idle` to see waiting threads, `--native` for C extensions, `--subprocesses` for workers; `py-spy dump --pid` for a hung process. Scalene separates Python, native and system time. memray or tracemalloc for allocations. cProfile is deterministic and inflates small, frequently called functions; use it for call counts, not time shares.
- asyncio: `loop.set_debug(True)` with `loop.slow_callback_duration` logs callbacks that block the loop; Node: `perf_hooks.monitorEventLoopDelay()`, `node --cpu-prof`.
- Native Linux: `perf record -F 99 -g --call-graph dwarf -p <pid> -- sleep 30`, then `perf report` or a flame graph; `perf stat -e cycles,instructions,cache-misses` for IPC (well below 1 suggests memory stalls); off-CPU time via bcc `offcputime`.
- JVM: async-profiler modes `cpu`, `alloc`, `lock`, `wall`; JFR. Avoid profilers that only sample at safepoints.
- Go: pprof CPU, heap, mutex and block profiles; the latter two record nothing unless `runtime.SetMutexProfileFraction` and `runtime.SetBlockProfileRate` are set. `go tool trace` for scheduler delays.
- Browser and Electron: DevTools Performance panel, long tasks over 50 ms, forced synchronous layout (reading `offsetHeight` after a style write inside a loop), Perfetto for system traces.
- Databases: `EXPLAIN (ANALYZE, BUFFERS)`, `pg_stat_statements`, slow-query logs; count queries per request.
- GPU: Nsight Systems or `torch.profiler`; synchronize before stopping a timer.
- Validity: enough samples (99 Hz for 30 s is about 3,000; short runs give noisy shares); symbols resolved (`[unknown]` frames, stripped binaries, JIT frames need perf maps or frame pointers); profiler overhead small relative to the effect; a release build.

**Compute**
- Work repeated per item that could be hoisted: regex compilation, config parsing, client construction, sorting inside loops.
- Quadratic patterns hidden by small test inputs: string concatenation in loops, `list.remove` or `in list` inside loops, repeated full scans.
- Logging cost when the level is disabled (eager f-string formatting, expensive `repr`), exceptions used for control flow on hot paths.

**Memory and GC**
- Allocation rate on the hot path; GC frequency and pause times (`gc.callbacks` in Python, `--trace-gc` in Node, GC logs on the JVM).
- Leaks: RSS or heap that keeps rising across at least three warm cycles of the same workload. Fragmentation shows as high RSS with a modest live heap.
- Unbounded caches: `functools.lru_cache` on methods (holds `self`), dictionaries keyed by request data, caches with no size or TTL limit.

**Contention and concurrency**
- Python GIL: CPU-bound threads give flat or worse throughput as threads increase; move to processes or native code that releases the GIL.
- Locks held across IO; one global lock around a shared client; pool exhaustion visible as time waiting for a connection rather than in the query.
- Thundering herd on cache expiry; retries without jitter that amplify load; false sharing in native code.

**IO and data access**
- Synchronous IO inside an event loop; fsync on every write; small unbuffered reads.
- No connection reuse (a TLS handshake per request), DNS resolved per call, chatty request/response protocols.
- N+1: query count grows with result rows. Missing index: sequential scan with a selective filter on a large table. Over-fetching columns or rows. ORM hydration or schema validation dominating serialization.

**Start-up**
- `python -X importtime -c 'import app' 2> imports.txt` for import cost; eager initialization of clients or models on import; container image size and pull time; missing compile or JIT caches.

**Latency distributions**
- Percentiles cannot be averaged across hosts or time windows; merge histograms (HDR histogram, t-digest) instead.
- Queueing: latency grows sharply as the bottleneck nears full utilization; latency-sensitive paths typically keep sustained utilization well below saturation, around 70-80% as a rule of thumb.
- Fan-out amplification: with 100 parallel sub-requests that are each slow 1% of the time, about 63% of requests (1 - 0.99^100) wait for a slow one; hedged requests or lower per-call tails fix this, not a faster median.
- Bimodal distributions usually mean two paths (cache hit and miss, GC and no GC); analyze them separately.

## Evidence standard

A hotspot claim cites the profile artifact (flame graph, pprof file, perf report
summary, trace export), the exact capture command, duration and sample count, and the
frame or span with its self and inclusive share. A latency claim has N, percentiles and
the load profile. A before/after claim has both distributions from interleaved runs on
the same machine. Not evidence: intuition about slow-looking code, Big-O reasoning
without real input sizes, one timing per arm, or a profile of a different workload.

## Severity guide

- P0: the change makes a primary path breach its budget (for example, p99 doubles); unbounded memory or handle growth that ends in OOM or exhaustion; a call that blocks an event loop or UI thread for seconds.
- P1: budget breach under realistic load; N+1 on a list endpoint; pool sizing that serializes requests; an optimization with no measurement that adds complexity or correctness risk to a critical path.
- P2: a significant hotspot off the primary path; missing performance regression guard for a path with a budget; cold start well above what imports and initialization require.
- P3: micro-optimizations on code with a small share of total cost.

## Skill-specific output

**Measurement plan**: workload command and load profile, environment, profilers with
exact commands and durations, metrics captured, budgets, run counts.

**Hotspot table**

| Rank | Location | Cost share | Mechanism | Evidence |
| --- | --- | --- | --- | --- |

Location is symbol plus path:line. Cost share states self and inclusive percentages and
which profile (CPU, wall, alloc). Mechanism is the resource and why (lock wait, GC,
quadratic loop, round trips). Evidence names the artifact and the confirming view.

**Latency distribution table**: Phase | N | p50 | p95 | p99 | max | Budget | Status.

**Recommended changes**: Change | Hotspot addressed | Expected effect (with derivation)
| Risk | How to confirm (command and pass criterion). This is the decision matrix the
calling agent acts on.

## Anti-patterns

- **No hotspot, no change.** Recommending optimizations for code never shown to be hot. Every recommendation must point to a hotspot row.
- **CPU profile for a waiting problem.** Low CPU with high latency means off-CPU time; capture wall-clock, off-CPU or trace data instead of reading the CPU flame graph harder.
- **Inclusive confused with self.** `main` is 100% inclusive; the actionable cost is self time and the narrow subtrees beneath it.
- **Averages hiding tails.** A mean that meets budget while p99 is four times the budget is a failure. Report distributions.
- **Warm-cache gate for a cold-start fix.** Measure the phase the change targets, in the state users meet.
- **Caches as a reflex.** A cache needs a hit-rate estimate, a memory bound and an invalidation rule; without them it is a new bug class, not a fix.
- **One run before, one run after.** Improvements smaller than run-to-run noise are not demonstrated.

## Done when

- [ ] The symptom is reproduced (or its non-reproduction reported) with N and percentiles.
- [ ] The limiting resource is named from a USE pass.
- [ ] Every hotspot row cites an artifact, a capture command and a second confirming view.
- [ ] Every recommendation maps to a hotspot and carries a derived expected effect.
- [ ] Before/after evidence or an exact confirmation protocol exists for each change.
- [ ] Memory, handles and threads were checked for growth across repeated cycles.
