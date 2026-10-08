---
name: prompt-review
aliases:
  - prompt-context-engineering-audit
version: 4.0.0
description: >-
  Use when reviewing or rewriting a system prompt, prompt template, agent
  charter (AGENTS.md, CLAUDE.md), skill file or tool description that steers an
  LLM: conflicting or unverifiable rules, missing goals and acceptance
  criteria, context ordering for prefix caching, progressive disclosure,
  untrusted-content handling, reasoning-before-format, examples, output
  contracts and token budget. Produces a rule inventory, a context layout, the
  rewritten prompt or a diff, and a plan to measure the change. Not for whether
  agent tools are exploitable (use security-review), running and
  scoring the evaluation (use ai-eval-review), or tool-schema and
  orchestration correctness (use agent-workflow-review).
brief: |
  Mission: make every instruction in the prompt earn its tokens, remove conflicts and ambiguity, and leave a measurable way to tell whether the rewrite is better.
  - Audit the rendered prompt, not the template: the exact text and order the model receives, including interpolated parts and other layers (harness prompt, tool definitions).
  - Inventory atomic rules; mark each verifiable or not, duplicated, conflicting, already default model behavior, or stale (names a file, tool or flag that no longer exists; grep to confirm).
  - Require a declarative core: goal, consumer, constraints, acceptance criteria. Procedure only where order truly matters.
  - Order context static to semi-static to dynamic so prefix caching holds; nothing per-request (dates, IDs, retrieved text) above stable content. Replace bulky inline reference with pointers the model can load on demand.
  - Untrusted content is delimited, source-labeled, placed after instructions and given no authority; flag a prompt that is the only control over a dangerous tool.
  - Let the model reason before strict formatting; the output contract must match the consumer's parser.
  - Prefer positive target behavior with reasons over long negative lists and emphasis inflation; examples must agree with the rules.
  Output before findings: rule inventory, context layout diagram, rewritten prompt or unified diff, evaluation plan. PASS = no P0/P1; PASS_WITH_FIXES = local wording, ordering or contract fixes; BLOCK = output unparseable by its consumer, hard rules contradict on the main path, or untrusted text carries instruction authority over consequential tools.
activation_triggers:
  task_modes:
    - PROMPT_AUDIT
    - CONTEXT_LAYOUT_REVIEW
    - CHARTER_REWRITE
  keywords:
    - system prompt
    - context engineering
    - prompt caching
    - agents.md
    - claude.md
    - few-shot
    - instruction conflict
    - tool description
    - progressive disclosure
    - prompt rewrite
  do_not_use_when:
    - The question is what an agent's tools can do once hijacked (use security-review).
    - The task is to run an eval, compute uncertainty or decide promotion (use ai-eval-review).
    - The concern is tool JSON schema validity, step budgets or DAG ordering (use agent-workflow-review).
input_contract:
  requires_worktree: false
  required_inputs:
    - The prompt, charter, skill or tool description text, or its path
  optional_inputs:
    - The code that assembles the prompt and the code that parses the output
    - Target model class and settings (reasoning or not, structured outputs, temperature)
    - Transcripts showing observed failures
    - Token or latency budget
output_contract:
  sections:
    - Rule inventory
    - Conflict matrix (when conflicts exist)
    - Context layout diagram
    - Rewritten prompt or unified diff
    - Evaluation plan
  findings: shared format
  verdict: shared verdict block
---

# Prompt and context engineering audit

## Mission
A prompt is a program run by a probabilistic interpreter. An excellent audit leaves it no longer than before, every rule either changing behavior or cut, conflicts resolved, goal and done-criteria unmistakable, output parseable by its consumer, and a concrete test showing whether the rewrite helps. The calling agent applies the diff and runs the evaluation. The most common failure is rewriting for taste: adding emphasis, persona and more rules without knowing which rules the model actually breaks, and with no way to measure the result.

## Inputs to establish first
- The rendered prompt. If it is assembled in code, read the builder and render it with a representative input (use a dry-run or render function if one exists). Identify which segments are static, which vary per session, and which vary per request.
- The layers around it: harness or platform system prompt, tool definitions, charters loaded automatically, skills loaded on demand. Rules in one layer can contradict another.
- The target model class: a reasoning model with internal deliberation, a fast non-reasoning model, or unknown; whether native structured outputs or tool calling are used.
- The consumer: the parser, the human reader, or a downstream model. Read the parsing code when it exists.
- Observed failures: transcripts or bug reports. Without them, say the audit is static and rank changes by risk.

## Method
1. Render and segment: the exact sequence of blocks with approximate sizes (a tokenizer, or bytes divided by four labeled as an estimate). Done when the context layout diagram can be drawn.
2. Build the rule inventory. Split the text into atomic rules and classify each: goal, constraint, output format, procedure, style, persona, fact, example. For each, decide verifiable (a checker or reviewer could decide compliance), duplicate, conflicting, default (a capable model does it unprompted), aspirational ("be thorough", "never hallucinate"), or stale (grep the repository for every path, command, flag, tool and skill name the prompt mentions). Done when every rule has a disposition and a reason.
3. Find conflicts and ambiguity. For each pair of rules that cannot both hold for some input, write that input (for example "cite path:line for every claim" against "answer in under 100 words" on a ten-finding review). Look for undefined qualifiers ("short", "important", "when appropriate") and missing precedence statements between layers. Done when each conflict has a resolution: precedence rule, scoping condition, or deletion.
4. Check layout and loading. Verify static-to-dynamic order and progressive disclosure (checklist). Done when each block is placed or moved with a reason.
5. Check authority and trust: which segments are instructions and which are data, and how untrusted data is fenced. Done when every data-bearing segment has a source label and an authority statement.
6. Check the output contract against the consumer: field names, enums, sentinels, refusal and partial-result behavior, truncation under the output token limit. Done when sample outputs (real or constructed) have been fed through the parser or traced through its code.
7. Rewrite and plan the evaluation. Produce a diff (or a full rewrite when restructuring) where every change maps to an inventory row, then the evaluation plan. Done when the plan names the cases that would reveal a regression.

## Checklist

### Goal, scope and acceptance
- The goal is an outcome with a named consumer, not an activity ("produce a reconciliation table the calling agent can act on", not "analyze the code").
- Acceptance criteria are checkable; "done" is defined; scope boundaries (what not to touch) appear once, in one place.
- For reasoning models, state goal, constraints and acceptance criteria and let the model plan; generic process nudges ("think step by step") add nothing. Keep procedure where order is a real requirement (read before edit, reproduce before fix). For fast non-reasoning models, explicit decomposition can still help; check the target class before removing it.
- A one-line role naming domain and audience suffices; stacked superlative titles rarely change output.

### Instruction load and wording
- Count atomic rules. As a rule of thumb, adherence to any single rule falls as the number of simultaneous constraints grows, and low-salience rules in the middle of long prompts are dropped first. Merge duplicates and delete defaults.
- Emphasis inflation: when many rules carry capitals, "CRITICAL" or "MUST", none stands out, and current models tend to over-apply emphatic rules to cases they were not meant for. Reserve emphasis for the few true invariants and state why each matters.
- A rule with its reason generalizes to unanticipated cases; a bare command does not.
- Long negative lists: restate as target behavior ("write plain paragraphs separated by blank lines" instead of five "do not" clauses). Keep a prohibition only for a specific observed failure; quoting an unwanted phrase can prime it.
- Aspirational rules ("be accurate", "never hallucinate") become checkable behaviors ("cite path:line for each claim; label unverified statements as such").
- When constraints are many, group them under labeled sections (format, scope, evidence); scattered constraints are dropped more often.

### Layout, caching and disclosure
- Prefix caches match exact leading bytes. Anything that varies (timestamps, request IDs, user names, retrieved documents, randomized example or tool order, non-deterministic JSON key order) placed above stable content invalidates the cache on every call. Tool definitions are usually part of the prefix, so editing them invalidates everything after.
- Recommended order: stable instructions and tool definitions, then semi-static reference (charter, skill briefs, project facts), then session state, then per-request documents, then the specific task or question last. For long documents, putting the material before the question tends to work better than the reverse.
- Progressive disclosure: inline only what every request needs; for the rest give an index of pointers (path plus one-line purpose) and say when to load each. Verify each pointer resolves and the model has a tool to read it.
- Charters such as AGENTS.md and CLAUDE.md are paid on every turn: keep what (module map), why (invariants) and how (exact runnable commands); move rare procedures into skills; do not inline file trees or API references that drift.
- For long prompts, restate the output contract briefly at the end; constraints stated only in the middle of long context are the most likely to be missed.

### Authority and untrusted content
- The hierarchy is explicit: platform or developer instructions, then user, then tool results and documents as data.
- Untrusted text sits inside delimiters labeled with its source, after the instructions, with a statement that it carries no authority. Check whether content can contain the closing delimiter and whether the assembler escapes it.
- Prompt-level quarantine reduces but does not prevent injection. When the prompt alone stands between injected text and a consequential tool, record a finding and route the capability question to security-review.

### Reasoning, format and examples
- Demanding a strict schema as the very first output degrades reasoning-heavy tasks. Prefer reasoning first (native thinking, or a notes section) and a structured block last; or two passes (free analysis, then extraction into the schema); or native structured outputs for pure extraction. In single-pass JSON, put evidence and reasoning fields before verdict fields.
- Machine-read blocks use unique sentinels and fixed enums the parser checks; specify behavior for refusal, missing data and partial results; make sure the output token limit cannot cut off the block the consumer needs.
- Examples override prose when they disagree; check every example against every rule. Examples should vary along the dimensions that matter and include an edge or null case. Reasoning models can over-anchor on worked examples, so prefer describing the format plus one minimal example; fast models benefit more from several. When the class is unknown, describe the format explicitly and keep one example.
- Examples and the evaluation set do not overlap.

### Tool descriptions
- Each tool says what it does, when to use it, and when to prefer a sibling; names are distinct; parameters state format, units and an example; errors say how to recover.
- Many overlapping tools reduce selection accuracy; expose only the relevant subset where the harness allows.

### Budget
- Static and typical dynamic token cost per call is stated; length targets are measurable (word or item counts) and consistent with the output limit.

## Evidence standard
Proof is the rendered prompt bytes, grep output for stale references, token or byte counts, a parser run on sample outputs, or transcripts showing the failure. General claims ("models ignore rules past N", "caching saves X percent") are not evidence; state the mechanism and measure it here.

## Severity guide
- P0: the consumer cannot parse the output contract on the main path; two hard rules contradict on the primary task and the observed or certain result is wrong behavior; untrusted text is placed with instruction authority while the agent holds consequential tools; the prompt depends on a tool, file or flag that does not exist.
- P1: per-request content ahead of stable content (cache miss on every call at material cost); a critical constraint ambiguous or buried; examples contradicting rules; the central behavior governed only by unverifiable rules; output truncation risk for the machine-read block.
- P2: duplicated or default rules, emphasis inflation, long negative lists, role inflation, missing reasons, stale but harmless references.
- P3: wording and ordering polish without behavioral effect.

## Skill-specific output
1. Rule inventory: ID | Rule (quoted or paraphrased, with location) | Type | Disposition (kept, merged, removed, rewritten) | Why (evidence or mechanism).
2. Conflict matrix, only for conflicting pairs: Rule A | Rule B | Input where both cannot hold | Resolution.
3. Context layout diagram in text: one line per block in order, tagged static, semi-static or dynamic, with approximate size and the cache boundary marked; show before and after when the order changes.
4. Rewritten prompt or unified diff. Prefer a diff for files in the repository; give a full rewrite when restructuring. State the net change in rule count and estimated tokens.
5. Evaluation plan: cases (observed failures, one per changed rule, adversarial inputs for trust boundaries), the deterministic checks (parser acceptance, regex or schema checks), paired comparison of old and new prompt on the same inputs with repeated samples, and the result that would trigger a revert. Statistical promotion belongs to ai-eval-review.

## Anti-patterns
- Rewriting by taste. Corrective: every change maps to an inventory row with a reason.
- Fixing rules by adding rules. Corrective: prefer deletion, merging and scoping; the net rule count should usually fall.
- Quoting research figures as laws. Corrective: name the mechanism and propose a measurement on this prompt.
- Auditing the template instead of what the model sees. Corrective: render it with real inputs, including every layer.
- Treating the prompt as the security boundary. Corrective: flag it and route capability limits to code-level controls.
- Copying advice across model classes, such as few-shot rules for reasoning models applied to fast models or the reverse. Corrective: state the target class; when unknown, choose defaults that work for both and test.
- Over-compression: deleting reasons and examples that carried the behavior. Corrective: removals need justification too.

## Done when
- [ ] The audited text is the rendered prompt, with all layers identified.
- [ ] Every rule has a disposition; every stale reference was grepped.
- [ ] Every conflict has an input that triggers it and a resolution.
- [ ] The layout is static to dynamic, with the cache boundary marked.
- [ ] Untrusted content is fenced and labeled, and dangerous capability gaps are routed.
- [ ] The output contract was checked against the consumer's parser.
- [ ] The diff and an evaluation plan with revert criteria are present, and the verdict block reflects the severity counts.
