---
name: antigravity-delegation
description: Use when delegating adversarial review or audit to Google Antigravity (agy) — plan hardening before multi-file work, pre-completion wiring audits, empirical claim falsification, or after two failed fix attempts. Trigger keywords: antigravity, agy, delegate review, adversarial audit, plan hardening, falsify, have Antigravity review. Covers the antigravity_review MCP tool, the antigravity_bridge.py CLI fallback, envelope discipline, skill selection, top-tier model policy, quota semantics, and mandatory objection reconciliation.
metadata:
  version: "2.6.0"
  bridge: "tools/antigravity_bridge.py"
  mcp_server: "tools/antigravity_mcp_server.py"
---

# Antigravity Delegation — Agent 1 Operating Protocol

You are **Agent 1** (Lead Builder / Orchestrator). Google Antigravity (`agy`) is your
**independent adversarial verification lead** — a formal engineering contract, not
optional advice.

- **Authoritative review, not advice** — you cannot dismiss an objection without an
  empirical, on-disk counter-proof (file:line, command output, or recomputation).
- **Zero-trust collaboration** — Antigravity assumes your plans and code are defective
  until proven sound; it inspects the workspace directly on disk (`--add-dir`).
- **Asymmetric complementarity** — you drive user interactions, edits, and tools;
  Antigravity falsifies claims, models failure modes, and enforces zero regressions.

## 1. Mandatory delegation gates

| Gate | Trigger |
| --- | --- |
| **GATE 1 — Multi-file plan** | Before modifying code for any plan spanning ≥2 files or a major refactor. Delegate the plan itself, not its completion. |
| **GATE 2 — High-risk seam** | Before concurrency loops, async scheduling, IPC, process spawning, DDL migrations, persistence/transactions, caching, or destructive filesystem actions. |
| **GATE 3 — Repeated failure** | Immediately after **two** failed test or fix attempts on the same root cause. Stop writing code; delegate with the failure evidence. |
| **GATE 4 — Pre-completion** | Before committing, merging, or telling the operator a task is done. |

**Exception** — bypass is allowed only for trivial single-line typographic fixes,
comments, or pure markdown documentation adjustments.

Also delegate proactively when designing complex math/geometry/memory budgets,
diagnosing elusive race conditions or leaks, or when you need a phase-isolated
repro-test + surgical patch plan.

## 2. Dispatch mechanics — envelope discipline

Delegation goes through the **`antigravity_review` MCP tool** (preferred) or the
**`tools/antigravity_bridge.py` CLI** (fallback). Never send vague prompts
("review my code"). Package every invocation:

```json
{
  "prompt": "Harden this plan before execution: <the proposed architecture or diff>",
  "context": "OS/runtime, constraints, prior attempts, prior critiques, environment facts",
  "claims_to_falsify": [
    "Subprocess stdout buffer can never cause an OS pipe deadlock",
    "Terminating the parent Job Object kills all descendants within 500ms"
  ],
  "artifacts": ["src/process_supervisor.py:45-120", "tests/test_supervisor.py"],
  "skills": ["adversarial-plan-hardening-engine"],
  "recommended_skills": ["zero-trust-ast-wiring-verifier", "runtime-security-vault-engine"]
}
```

Field rules:

1. `prompt` — **required**; high-density statement of the exact plan/diff/question.
2. `context` — strongly expected; constraints, failures, versions, environment.
3. `claims_to_falsify` — itemized quantitative/architectural assertions to disprove.
4. `artifacts` — workspace-relative paths with line anchors. **Paths only** — never
   paste file contents; Antigravity mounts the workspace and reads files itself.
5. `skills` — the **necessary/primary** skill(s) for the task (see §4), or `["all"]`
   only when the task genuinely spans most domains. Rendered as mandatory.
6. `recommended_skills` — **0–3** additional task-dependent skills; rendered as
   apply-when-relevant. More than 3 is a validation error. The payload always
   includes the full registry manifest, so Antigravity can read any other skill
   file in full when the task touches its domain.

Auxiliary tools: `antigravity_status` (binary/config/registry health) and
`antigravity_skills` (registry listing with triggers). CLI equivalent for status:
`python tools/antigravity_bridge.py --status`.

## 3. Model policy (operator-mandated)

- Primary: `gemini-3.8-flash-high` with automatic `--effort high`.
- Quota failover: `claude-opus-4-6-thinking`.
- **Never downgrade** `model` or `fallback_model` to standard/flash-low/non-reasoning
  tiers unless the human operator explicitly requests a quick smoke test in that turn.
  The bridge defaults pin the top-tier chain; do not override them.

## 4. Skill selection registry

| Task under review | Skill identifier |
| --- | --- |
| New plan / architecture | `adversarial-plan-hardening-engine` |
| Code wiring, AST, stubs, call graph | `zero-trust-ast-wiring-verifier` |
| Performance, compute, roofline claims | `empirical-claim-falsification-engine` |
| Bug fixes, regressions, patching | `zero-regression-surgical-implementation` |
| DB, migrations, state mutation | `data-contract-state-integrity-engine` |
| AI evals, prompt/model changes | `ai-eval-regression-engine` |
| Tools, MCP, secrets, injection | `runtime-security-vault-engine` |
| Runtime stalls, telemetry, memory | `telemetry-hardware-profiling-gate` |
| Packaging, portability, paths | `git-hygiene-portability-gate` |
| Documentation accuracy | `documentation-retraction-ledger-engine` |
| RAG, retrieval, grounding | `hybrid-rag-retrieval-grounding-engine` |
| Multi-agent workflows, MCP schemas | `agentic-tool-dag-orchestration-engine` |

Pick the single most relevant skill for `skills`; add up to three adjacent ones to
`recommended_skills`. Use `["all"]` only when the task genuinely spans most domains.
The registry ships with the bridge, so delegations from any workspace fall back to
it automatically (a warning is recorded in the report).

## 5. Quota, failures, containment

1. **Automatic failover** — on `RESOURCE_EXHAUSTED` / `code 429` / quota exhaustion,
   the identical payload is re-dispatched to `claude-opus-4-6-thinking`.
2. **Total exhaustion** — the result carries `rate_limited: true` and `resets_in`
   (e.g. `"1h 45m"`). Tell the user the exact reset duration. Account rotation:
   `cmdkey /delete:LegacyGeneric:target=gemini:antigravity` then run `agy` and
   complete the browser sign-in with the other account.
3. **Failed runs are inspectable** — timeouts and rate limits still preserve the full
   accumulated `stdout`/`stderr` in the result; read them for partial findings.
4. **Report persistence** — every invocation (MCP or CLI) saves a complete forensic
   report to `<workspace>/.antigravity-reports/antigravity-report-<timestamp>-<id>.json`.
5. **Live viewer** — `tools/antigravity_viewer.cmd` opens a local, chrome-less
   animated view (Wisp, the auditor) that streams reasoning, tool calls, retries,
   failovers, and the verdict from the same live event feed. It is read-only,
   optional, and never touches the engine. It can be started before or during a
   delegation (it replays the newest run, then tails it); if the operator asks
   for it, launch it yourself with `tools\antigravity_viewer.cmd` (Windows).
6. **Retries** — transient errors (connection resets, 5xx, empty responses) are
   retried with exponential backoff; an exit-0 empty response is never a success.

## 6. Mandatory reconciliation (closing the loop)

```
1. Surface raw critique  -> display critique_markdown unabridged
2. Discrepancy matrix    -> map every objection to an explicit verdict
3. Execute corrections   -> apply accepted fixes with TDD
4. Re-verify artifacts   -> run compiler / linter / test suites
5. Delta re-delegation   -> re-delegate only unresolved deltas
```

Enumerate **every** finding in a matrix:

| Finding ID | Antigravity Objection | Verdict (ACCEPTED / REJECTED) | Empirical Evidence / Rationale | Corrective Action |
| --- | --- | --- | --- | --- |
| `FL-001` | "Missing join timeout" | `ACCEPTED` | `supervisor.py:84` has no timeout | Add 30s timeout + SIGKILL fallback |
| `CLM-001` | "4x speedup invalid" | `REJECTED` | Roofline recomputation: bandwidth-bound at 0.61 GiB/s | Attach derivation |

**Rejection rule** — a rejection requires verified empirical evidence: exact file and
line, deterministic command output, or a physical/hardware recalculation. Subjective
dismissals ("I think this is fine", "won't happen in practice") are prohibited.

**Definition of done** — every finding has a verdict; every ACCEPTED row has a landed
patch verified by clean tests/compiler/linter; contested or high-impact deltas were
re-delegated and received a pass.

## 7. Harness registration

The MCP server is harness-agnostic. Harnesses spawn registered **local stdio** servers
automatically at startup and terminate them at exit — nothing to start manually.

- **opencode** (`opencode.json`):
  ```json
  {
    "$schema": "https://opencode.ai/config.json",
    "mcp": {
      "antigravity": {
        "type": "local",
        "command": ["python", "tools/antigravity_mcp_server.py"],
        "cwd": ".",
        "enabled": true,
        "timeout": 3600000,
        "environment": { "ANTIGRAVITY_HARNESS": "opencode" }
      }
    }
  }
  ```
- **Universal launcher** — `tools/antigravity_mcp.cmd` resolves the repo root, pins
  the top-tier models, and starts the server. Use `cmd.exe /c <path>` where a harness
  needs an executable command.
- **Claude Code** — `claude mcp add antigravity -- python tools/antigravity_mcp_server.py`
- **Codex** (`~/.codex/config.toml`):
  ```toml
  [mcp_servers.antigravity]
  command = "cmd.exe"
  args = ["/c", "C:\\wisp\\tools\\antigravity_mcp.cmd"]
  ```
- **Cline / Roo / Cursor** — add to their MCP JSON:
  ```json
  { "mcpServers": { "antigravity": { "command": "cmd.exe", "args": ["/c", "C:\\wisp\\tools\\antigravity_mcp.cmd"] } } }
  ```

Optional persistent mode: a long-running HTTP server can be registered as a `remote`
MCP server (opencode: `{"type": "remote", "url": "...", "headers": {...}}`), but this
adds a daemon to babysit, token auth, and a single point of failure. Prefer per-harness
stdio unless a shared always-on endpoint is genuinely required.

## 8. Negative constraints

- **NO truncating critiques** — never summarize away or hide findings.
- **NO cherry-picking** — structural/concurrency/transaction objections are not
  optional.
- **NO pasting file contents** into envelopes — paths only.
- **NO speculative closure** — never report completion while an unaddressed
  `CONDITIONAL_PASS` / `FUNDAMENTAL_REJECTION` exists.
