---
name: agentic-tool-dag-orchestration-engine
version: 4.0.0
description: >-
  Use when reviewing agent loops, tool or MCP server definitions, multi-step tool
  graphs, multi-agent handoffs, or the runtime that executes model tool calls:
  schema clarity, error contracts, retries of side-effecting tools, budgets and
  termination, parallel calls, cancellation, context growth and approval gates.
  Produces a tool contract registry, a control-flow and budget matrix, a
  side-effect ledger and EARS orchestration requirements. Not for prompt
  injection or secret exposure through tools (use runtime-security-vault-engine),
  retrieval quality (use hybrid-rag-retrieval-grounding-engine), or database
  transaction integrity (use data-contract-state-integrity-engine).
brief: |
  Mission: make sure the model can call these tools correctly, the runtime always stops, and every failure leaves the world in a known state.
  - Schemas: complete `required`, enums for closed sets, `additionalProperties: false`, stated defaults and units, and descriptions that say when to use this tool rather than its neighbours. Validate arguments server-side anyway.
  - Errors return as tool results the model can act on (what failed, whether retrying helps, what to change), never as exceptions that end the loop or success payloads that hide failure.
  - Side effects: each mutating tool is idempotent or keyed by a stable step ID so a retry after a timeout cannot apply twice; irreversible actions pass a runtime-enforced approval bound to the exact arguments.
  - Termination: step, token and wall-clock budgets checked every iteration, owned by the runtime, shared with sub-agents, and never resettable by the model; repeated identical calls detected; each stop carries a reason.
  - Graphs: cycles rejected before execution; joins define what happens to siblings and completed effects when one branch fails.
  - Concurrency and cancellation: parallel calls on shared state are serialized or isolated; cancellation reaches subprocesses and still yields a result for every call ID.
  - Context: tool output is bounded with a visible truncation marker; handoffs carry an explicit contract, not the transcript.
  Output before findings: tool contract registry, control-flow and budget matrix, side-effect ledger, EARS requirements (REQ-ORCH-NNN).
  PASS: bounded, recoverable, unambiguous. PASS_WITH_FIXES: local schema, guard or error-shape fixes. BLOCK: an unbounded loop, an ungated irreversible action, or a retry that duplicates side effects.
activation_triggers:
  task_modes:
    - AGENT_LOOP_AUDIT
    - TOOL_SCHEMA_REVIEW
    - DAG_ORCHESTRATION_CHECK
    - MULTI_AGENT_HANDOFF_REVIEW
  keywords:
    - agent loop
    - tool schema
    - mcp tool
    - inputschema
    - tool call retry
    - loop guard
    - step budget
    - dag
    - fan-in join
    - parallel tool calls
    - agent handoff
    - approval gate
  do_not_use_when:
    - The question is whether tool inputs or outputs can be abused by an attacker (route to runtime-security-vault-engine).
    - The question is retrieval, chunking or grounding quality (route to hybrid-rag-retrieval-grounding-engine).
    - The state at risk is database rows and transactions rather than agent workflow state (route to data-contract-state-integrity-engine).
input_contract:
  requires_worktree: true
  required_inputs:
    - The agent loop, tool definitions, MCP server, or graph runner under review (paths or diff)
  optional_inputs:
    - Model provider and SDK version, and whether strict tool schemas are enabled
    - Expected task sizes (typical steps, tokens, duration per run)
    - Which tools have external side effects
output_contract:
  sections:
    - Tool contract registry
    - Control-flow and budget matrix
    - Side-effect ledger
    - Orchestration requirements (EARS)
  findings: shared format
  verdict: shared verdict block
---

# Agentic tool and orchestration contracts

## Mission

The consumer is the engineer who owns an agent runtime or tool surface and needs
to know where a model, acting in good faith but imperfectly, will pick the wrong
tool, loop, double-apply an effect, or leave work half-done. An excellent review
traces real control flow: who counts steps, what happens on timeout, what the
model sees when a tool fails. The common failure is reviewing schemas as static
JSON and stopping there, while the real defects live in the loop, the retry
middleware and the cancellation path.

## Inputs to establish first

- The execution path end to end: where model output is parsed into tool calls,
  where calls are dispatched, where results re-enter history, where the loop
  decides to continue. Locate each with `git grep -n` for the provider SDK's tool
  types, `tools/call`, `stop_reason` or `finish_reason`.
- Provider and SDK version, and whether strict schema mode is on. Strict modes
  accept only a schema subset (OpenAI strict function calling, for example,
  requires every property in `required` and `additionalProperties: false`, with
  optional fields expressed as nullable).
- For MCP servers, the protocol revision the SDK implements; `outputSchema`,
  `structuredContent` and tool annotations exist only in newer revisions.
- Which tools mutate anything outside the process, and which are irreversible.
- Typical run size, so budget findings can say whether a limit is plausible.

## Method

1. **Map the loop.** Draw iteration: model call, tool dispatch, result append,
   continue or stop. Mark every exit and the counter or condition behind it. Done
   when each exit has a path:line.
2. **Inventory tools.** For each tool record schema, side-effect class (read-only,
   idempotent write, non-idempotent write, irreversible), error behaviour, and
   timeout. Prefer the schema the server actually emits (run `tools/list` or the
   registration code) over hand-written docs. Done when the registry is complete.
3. **Test the error path.** For each tool class, follow what happens on bad
   arguments, an exception, a timeout and an empty result, up to what the model
   sees next. Done when each yields an actionable result or a defined stop.
4. **Prove termination.** Identify the decreasing measure (remaining steps, tokens,
   seconds) and show no path resets or bypasses it, including sub-agents and
   retries. Done when every loop has a bound the model cannot influence.
5. **Check graphs and concurrency.** For graph runners: cycle check, join
   semantics, partial-failure handling, compensation. For parallel calls: shared
   state and ordering. Done when each fan-out has defined failure behaviour.
6. **Trace cancellation.** Follow a user cancel or deadline from the top to every
   in-flight call and child process. Done when nothing outlives its parent and
   every call ID receives a result.
7. **Specify.** Write EARS requirements for accepted fixes. Done when every P0-P2
   finding maps to a REQ-ORCH requirement.

## Checklist

### Tool schemas and selection
- Overlapping tools (`search_files` and `grep`, `run` and `exec`) whose
  descriptions both match a request; each description should state when not to
  use it.
- Ambiguous parameters: `path` relative to what, `timeout` in which unit. Put the
  unit in the name (`timeout_seconds`) and the base in the description.
- Optional parameters whose default is undocumented, so the model guesses or
  always passes one.
- Nested `oneOf` unions and JSON-encoded strings inside string parameters are
  frequent sources of malformed calls; prefer flat objects.
- Enumerations of hundreds of values belong behind a lookup tool.
- Tool count: selection accuracy falls as the active set grows (as a rule of
  thumb, beyond a few dozen); scope tools per task or agent.
- The server validates arguments against the schema even when the provider
  claims to; non-strict modes do emit invalid arguments.

### Error contracts
- Tool failures return in-band so the model can react: MCP uses a result with
  `isError: true`; JSON-RPC errors (-32602 invalid params, -32601 unknown method)
  are for protocol faults, not for "file not found".
- The error text says what failed, whether retrying can help, and what to change;
  a multi-kilobyte stack trace wastes context and teaches nothing.
- "No results" is distinguishable from "failed".
- An uncaught exception in a handler must not crash the server or end the run.
- MCP stdio servers must write nothing but protocol messages to stdout; a stray
  `print` or library banner corrupts the stream. Logs go to stderr.

### Side effects and retries
- A client-side timeout does not stop the server: a retried `create_issue`,
  `send_message`, `git push` or payment runs twice.
- Idempotency keys derive from stable identifiers (run ID plus step ID), never a
  UUID generated per attempt.
- Generic retry middleware wrapped around every tool retries non-idempotent ones;
  check decorators and SDK retry settings.
- MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) are hints
  from the server, not enforcement; the client cannot rely on them for safety.

### Budgets and termination
- Counters live in runtime state the model cannot write; a "continue" tool, a
  self-reflection step or a sub-agent must not reset them.
- Token budgets include tool results; wall-clock includes tool time.
- Sub-agents draw from the parent's remaining budget and carry a depth limit;
  fresh budgets per child make total cost unbounded.
- Repeated identical calls (same tool, same arguments, same result) within a
  window are detected and break the loop or change strategy.
- The stop reason (done, budget, error, cancelled) reaches the caller.
- A response cut off at the output token limit can contain truncated tool-call
  JSON; handle the truncation stop reason before parsing.

### Graphs and joins
- Cycle detection runs at build time and when nodes are added dynamically.
- Each join declares its policy (all, any, quorum) and what happens to running
  siblings when one fails: cancel or let finish.
- Completed side effects in a failed graph have compensations, run in reverse
  order and themselves idempotent.
- Fan-out has a concurrency cap and respects provider rate limits.
- Fan-in that appends in completion order (`as_completed`) makes prompts and
  outputs nondeterministic; sort by node ID.
- Resume after a crash skips completed nodes by stable ID without re-running
  their effects.

### Parallel tool calls and shared state
- Parallel calls editing the same file or record race; serialize by resource or
  reject conflicting batches.
- Handlers sharing process-global state (`os.chdir`, environment variables, a
  module-level session) corrupt each other under concurrency.
- Every tool call ID gets exactly one result, including failed and cancelled ones;
  major provider APIs reject the next request otherwise.

### Cancellation and timeouts
- Cancelling an asyncio task does not kill subprocesses it started; threads
  cannot be cancelled at all. Each needs explicit termination.
- Inner timeouts are shorter than outer ones so the specific error surfaces.
- MCP cancellation uses `notifications/cancelled` with the request ID; the server
  should stop work and the client must ignore late responses.

### Context and handoffs
- Full history is resent each turn, so cost grows roughly quadratically with turn
  count; large tool outputs stay forever unless bounded on entry.
- Truncation is marked in the text and points to the full output (a file path or
  handle) so the model knows what it has not seen.
- Compaction keeps pending obligations and open tool call IDs.
- A handoff carries goal, constraints, artifacts, definition of done and budget,
  and returns a structured result; who owns cleanup afterwards is stated.

### Approval gates and observability
- Irreversible actions (delete, force push, send, pay, deploy) are gated in
  runtime code, not by a prompt instruction; approval is bound to a hash of the
  exact arguments and denied on timeout.
- A trace per step records run ID, step ID, parent span, tool, redacted
  arguments, duration, result size, error class and tokens, enough to replay a
  run.

## Evidence standard

- Run the real surface: list tools from the running server (a short stdio
  JSON-RPC script or the SDK client) and validate each `inputSchema` with a JSON
  Schema validator.
- Prove termination with a scripted fake model that always requests the same
  call; the run must stop at the budget with the right stop reason.
- Prove retry safety by injecting a timeout after the side effect and counting
  effects.
- Reading the loop counts as medium confidence; a failing or passing scripted run
  is high.

## Severity guide

- **P0**: a loop with no bound the model cannot defeat on a primary path; an
  irreversible action without a runtime gate; a retry path that duplicates
  external effects; stdout pollution that breaks an MCP stdio server.
- **P1**: tool exceptions that end the run; a missing result for a cancelled call
  ID; budgets reset by sub-agents; parallel calls clobbering shared state;
  cancellation leaving orphan processes.
- **P2**: overlapping descriptions causing plausible misselection; unbounded
  tool output entering history; undocumented defaults; missing trace fields.
- **P3**: naming and description polish.

## Skill-specific output

1. **Tool contract registry**: `| Tool | Side-effect class | Schema issues | Error contract | Retry-safe | Approval gate |`
2. **Control-flow and budget matrix**: `| Loop or graph | Step limit | Token limit | Wall clock | Checked at (path:line) | Model can reset | Stop reason surfaced | Repeat detection |`
3. **Side-effect ledger**: `| Effect | Tool or node | Idempotency mechanism | Compensation | On cancel |`
4. **Orchestration requirements (EARS)**: `REQ-ORCH-001` onward, e.g. "IF a tool
   call times out after dispatch, THEN the runtime SHALL reuse the step's
   idempotency key on retry", each with a verification method.

## Anti-patterns

- **Static-only review.** Rule: follow the loop, retries and cancellation in code,
  not just the JSON.
- **Calling the agent loop a cycle defect.** The loop is iterative by design;
  acyclicity applies to task graphs, bounds apply to loops.
- **Trusting hints and prompts.** Rule: annotations and "never delete without
  asking" in a prompt are not enforcement.
- **Arbitrary limits.** Rule: tie step and token limits to observed run sizes.
- **Schema absolutism.** Rule: demand enums only for closed sets, and check strict
  mode compatibility before demanding schema changes.
- **Security drift.** Rule: route injection and secret findings to
  runtime-security-vault-engine.

## Done when

- [ ] Every loop exit and budget check is cited by path:line.
- [ ] Every tool has a side-effect class, error behaviour and retry verdict.
- [ ] Termination is shown against a measure the model cannot reset.
- [ ] Every fan-out and join has defined partial-failure behaviour.
- [ ] Cancellation reaches children and every call ID gets a result.
- [ ] Every irreversible action has a runtime gate or a finding.
- [ ] Every P0-P2 finding has a REQ-ORCH requirement with a verification method.
