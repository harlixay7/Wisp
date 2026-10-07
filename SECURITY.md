# Security Policy & Threat Model

Wisp automates a full-privilege coding agent (`agy`) inside a **process
containment boundary**. This document states exactly what that means — what is
trusted, what is not, and what the boundary does and does not guarantee.

> **One-sentence posture:** Wisp provides process-tree *containment* and
> evidence hygiene — it is **not a security sandbox**, not least-privilege
> execution, and not a defense against malicious model output.

## Trust model

**Trusted:**

- The local operator and their harness (the agent configuring delegations).
- The Wisp code itself and its pinned dependencies.
- The installed `agy` binary (Google OAuth via `agy`; Wisp never handles
  credentials — the quota hook is the one operator-authored escape hatch, run
  only from operator channels (`--quota-hook` / `ANTIGRAVITY_QUOTA_HOOK`),
  never from envelopes or workspace data).
- The local filesystem *inside the operator's intent*: the workspace the
  operator points Wisp at is read and mounted for the reviewer by design.

**Not trusted:**

- Repository content under review — including `AGENTS.md`, READMEs, source
  comments, and skill-adjacent docs. The delegation payload classifies the
  entire workspace as **untrusted evidence**; workspace text cannot alter the
  reviewer's mandate, and prompt-injection attempts inside reviewed content
  are findings, not instructions.
- Tool output and model output — captured verbatim, surfaced verbatim, never
  executed by Wisp itself.
- Captured clipboard text and pasted images — stored under
  `.antigravity-reports/captures/` and attached to delegations only from the
  approved roots.
- Arbitrary MCP callers — the server has no authentication by design because
  it binds stdio to a locally-spawned child; do not expose it over a network
  transport without adding your own gate.
- Foreign live directories on the same machine (the viewer watches them
  read-only).

## What the containment boundary guarantees

1. **Process-tree termination.** On Windows the child is created suspended,
   assigned to a Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), and
   resumed; termination applies `taskkill /F /T` (live parent→child snapshot)
   *and* `TerminateJobObject`. On POSIX the child runs in its own session and
   is killed by process group. The achieved containment mode is recorded on
   every attempt and appears in reports and warnings.
2. **Credential hygiene (blocklist).** Credential-bearing environment
   variables (`AWS_*`, `AZURE_*`, `GITHUB_*`, `GH_*`, `SSH_*`, `OPENAI_*`,
   `ANTHROPIC_*`, `GEMINI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`,
   `GOOGLE_API_KEY`, `HF_*`/`HUGGINGFACE*`, `GIT_ASKPASS`, `SSH_ASKPASS`) and
   interpreter/PM injection vectors (`NODE_OPTIONS`, `PYTHONPATH`,
   `PYTHONHOME`, `PYTHONSTARTUP`, `NPM_CONFIG_USERCONFIG`) are stripped before
   launch. The full policy lives in
   `tools/antigravity_bridge.py::BLOCKED_ENV_PREFIXES` / `BLOCKED_ENV_EXACT`.
3. **Evidence durability.** Complete stdout/stderr and a provenance block
   (versions, commit, registry hash, timestamp) are persisted atomically per
   delegation under `.antigravity-reports/`.

## What the boundary does NOT do

- **Not least privilege.** `agy` runs with `--dangerously-skip-permissions`:
  the reviewed agent can read and write within its granted scopes and run
  commands. Wisp contains the *process tree*, not the agent's *authority*.
- **Not a sandbox for untrusted code.** Never point Wisp at a workspace whose
  build/test execution you would not run yourself; the reviewer may execute
  repository code as part of verification.
- **Not immune to breakaway processes.** A process that deliberately detaches
  from its parent (re-exec chains, breakaway jobs) may escape both tree-kill
  mechanisms; the empirical containment mode is reported so degraded runs are
  visible. Verified environment note (2026-10-07): interpreters that re-exec
  through the WindowsApps Store alias break parent-PID chains entirely —
  grandchildren are re-parented to an intermediate host, so snapshot walks
  from the recorded root find nothing. Termination from a *live* direct
  child (the bridge's actual failure path: timeout, error, interruption)
  is covered by taskkill /T plus TerminateJobObject; cleanup after a child
  has already exited on its own is best-effort in such environments.
- **Not a network control.** The reviewer can make network calls. Restrict at
  the OS/firewall layer if your threat model requires it.

## Local services

- **Viewer** binds `127.0.0.1` by default with no authentication (a local
  operator tool). Non-loopback binding requires `--auth-token` /
  `--generate-token`; every route is then bearer-gated, `/health` is minimal,
  and `Host` headers are validated. `/api/ask` accepts image artifacts only
  from the workspace and capture directory.
- **Live reports** (`.antigravity-reports/`) contain full delegation prompts —
  treat that directory as sensitive and never commit or share it
  (`.gitignore` excludes it).

## Reporting

Open a GitHub issue for anything you find; for sensitive disclosures, use the
repository's private security-advisory feature rather than a public issue.
