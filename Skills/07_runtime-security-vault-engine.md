---
name: runtime-security-vault-engine
version: 4.0.0
description: >-
  Use when reviewing code, services, CLIs, local servers, MCP servers or
  LLM agent tool surfaces for exploitable security defects: injection of any
  kind, broken authorization, secret exposure, SSRF and egress, file-system
  escape, crypto misuse, supply-chain exposure, and over-privileged agents.
  Produces a threat-model-driven review with a trust-boundary map, attack paths
  and minimal fixes. Not for prompt wording quality (use
  prompt-context-engineering-audit), tool-schema or DAG correctness (use
  agentic-tool-dag-orchestration-engine), or clone and release hygiene (use
  git-hygiene-portability-gate).
brief: |
  Mission: report only vulnerabilities an attacker can actually reach, ranked by exploitability and impact, each with the smallest fix that closes the path.
  - Threat model first: state the deployment model and attacker profiles, then list every attacker-controlled input (requests, files, IPC, CLI args, tool results, retrieved documents, model output) and the sinks it reaches. No reachable source, no finding.
  - Trace source to sink for SQL, shell and argv, path traversal, template, deserialization, header/log and prompt injection that can trigger tools.
  - Check authorization on every action, including secondary routes (export, bulk, websocket, debug, download); test object-level access and confused-deputy flows.
  - Find secrets in code, logs, error messages, child-process environments and git history; a pushed live credential needs rotation, not just deletion.
  - Check SSRF and egress, symlink/TOCTOU file access, crypto misuse, and dependency and CI supply chain.
  - For agents assume the model is fully hijacked by injected text and ask what its tools can then do; gate irreversible actions in code or with a human, not in the prompt.
  Output before findings: trust-boundary map and attack-path table (attacker, entry, steps, impact, preconditions, fix). PASS = no reachable P0/P1; PASS_WITH_FIXES = P1 paths with local sink-level fixes; BLOCK = any P0 path (remote or cross-user code execution, data access, live secret, unguarded agent exfiltration) or a design that local fixes cannot secure.
activation_triggers:
  task_modes:
    - SECURITY_REVIEW
    - THREAT_MODELING
    - AGENT_PERMISSION_AUDIT
  keywords:
    - threat model
    - trust boundary
    - attack path
    - command injection
    - idor
    - ssrf
    - path traversal
    - deserialization
    - confused deputy
    - secret exposure
    - supply chain attack
    - prompt injection
  do_not_use_when:
    - The question is whether a prompt or charter is well written rather than whether its tools are dangerous (use prompt-context-engineering-audit).
    - The concern is tool-schema validity, DAG cycles or step budgets (use agentic-tool-dag-orchestration-engine).
    - The concern is reproducible setup, lockfile drift or tree hygiene without an attacker in the picture (use git-hygiene-portability-gate).
input_contract:
  requires_worktree: true
  required_inputs:
    - The code, diff or component under review
  optional_inputs:
    - Deployment model and intended users
    - Known assets and data classification
    - Agent tool manifest or MCP configuration
    - Prior findings or reported incidents
output_contract:
  sections:
    - Trust-boundary map
    - Attack-path table
    - Authorization matrix (when more than one role exists)
  findings: shared format
  verdict: shared verdict block
---

# Runtime security review

## Mission
An excellent review is a short list of real vulnerabilities: each has an attacker-controlled source, a traced path to a dangerous sink, an impact on named assets, and a sink-level fix that keeps the feature working. The calling agent uses it to decide what must change before merge. The most common failure is pattern matching: flagging every `subprocess` or `eval` whether or not attacker data reaches it, burying the one exploitable bug under twenty theoretical ones.

## Inputs to establish first
- Deployment model: single-user CLI, desktop app on a loopback port, multi-tenant server, or CI job processing pull requests. Infer it from bind addresses, auth middleware and packaging when not given; if still unclear, rate under the most plausible model and say how severity shifts under the alternative.
- Assets: credentials, user data, the host, other tenants, publishing or money-moving actions, CI secrets.
- Attacker profiles: unauthenticated network client, low-privilege user, another local user, a malicious web page in the user's browser, an author of content an agent reads, a compromised dependency.
- Scope: for a diff, the changed lines plus every path that now reaches them; for a full audit, entry points first.

## Method
1. Enumerate entry points: routes, CLI parsers, socket and pipe listeners, Electron `ipcMain` and preload bridges, MCP tool handlers, webhook and queue consumers, file watchers, and every place model output becomes an action. Done when each one is in the trust-boundary map with its authentication requirement.
2. Enumerate sinks: process spawn, raw SQL, file write/delete/extract, outbound fetch, template render, deserializers, dynamic code, headers and redirects, logs, tool dispatch. Done when sinks are listed per module.
3. Trace each source to sinks through every transformation. Note where validation happens relative to use: validating one representation and using another (decoded vs raw, resolved vs unresolved path, parsed URL vs string) is itself a bug. Read the callers of a sink, not only the sink. Done when each candidate path is either an attack path or cleared with the named control (path:line) that stops it.
4. Authorization pass: build an authorization matrix with actions as rows and roles or ownership as columns; each cell names where the check is enforced or marks it missing. Done when every state-changing or data-returning action has a filled cell.
5. Secrets, egress, file-system and supply-chain passes from the checklist.
6. Agent pass whenever a model can invoke tools: list each tool's reach and gating.
7. Confirm P0/P1 candidates with a minimal reproduction in the workspace (unit test, local request, fixture script) when safe; never against external systems or real credentials. If not executed, say why and cap confidence at medium.

## Checklist

### Injection
- Shell: `shell=True`, `os.system`, `child_process.exec`, `sh -c` with interpolated strings. With argv arrays, look for argument injection: a value starting with `-` reaching `git` (`--upload-pack`, `-c core.sshCommand=`), `ssh -oProxyCommand`, `curl -o`, `tar --checkpoint-action`, `rsync -e`; the fix is a `--` separator plus a leading-dash check. On Windows, argv reaching a `.bat` or `.cmd` target is reparsed by cmd.exe, so quoting rules change and injection returns.
- SQL: string formatting into `execute`, ORM `raw()`/`text()`, identifiers in `ORDER BY` or column lists (cannot be bound; need an allowlist), unescaped `LIKE` wildcards.
- Paths: `os.path.join(base, user)` silently discards `base` when `user` is absolute; checks done before URL or double decoding; Windows drive letters, UNC paths, `\` separators, alternate data streams and device names; prefix checks with `startswith` (`/srv/app` admits `/srv/app-secrets`) instead of `resolve()` plus `is_relative_to`; archive extraction without member validation (zip slip; Python `tarfile` without `filter="data"`).
- Templates and markup: user text used as template source (`Template(user)`, `render_template_string`), autoescape off, `|safe`, `dangerouslySetInnerHTML`, `v-html`, Markdown passing raw HTML. In Electron, renderer XSS becomes code execution if `nodeIntegration` is on, `contextIsolation` is off, or the preload exposes generic file or shell APIs.
- Deserialization: `pickle`, `marshal`, `shelve`, `yaml.load` without a safe loader, `torch.load` without `weights_only=True`, `jsonpickle`, Java and .NET native serializers, on any data an attacker can supply or swap on disk.
- Header and log: CRLF in headers, open redirects via `next=`, forged log lines or ANSI escapes reaching an operator terminal, formula injection in CSV exports.
- Prompt injection that reaches tools: untrusted text (web pages, files, issues, emails, tool output) in the same context as tool access. Determine whether injected text can cause a tool call with attacker-chosen arguments, and whether rendered Markdown images or links can carry data out in a URL.

### Authentication and authorization
- Object-level access: handlers loading by request ID without scoping to the caller. Check list, search, export, bulk, download and GraphQL resolver paths, not only the main read.
- Secondary routes: websocket and SSE upgrades, debug and metrics endpoints, old API versions, static servers exposing source or `.env`.
- Local servers: binding `0.0.0.0` instead of loopback; loopback services reachable from any browser tab via CSRF or DNS rebinding unless they check `Host` and `Origin` and require a token.
- Confused deputy: a privileged component acting on a caller-supplied path, URL or account using its own authority; an agent using the operator's credentials on instructions found in content.
- Tokens: non-constant-time MAC comparison, JWT algorithm confusion or missing `exp`/`aud`, credentials in URLs.

### Secrets
- Hardcoded in code, fixtures, notebooks and configs; present in history (`git log -p --all -S'<prefix>'`, deleted env files via `git log --all --diff-filter=D --name-only`, gitleaks or trufflehog when installed).
- In logs, exception messages and error responses (connection strings, request dumps with `Authorization`).
- In child processes: environments inherited wholesale; blocklists miss new variable names, so prefer an allowlist; argv is readable by other local users through `ps` and `/proc/<pid>/cmdline`.
- In client bundles (`NEXT_PUBLIC_`, `VITE_`, `REACT_APP_` prefixes ship to browsers), crash dumps and telemetry payloads.

### SSRF and egress
- Any fetch of a user- or model-supplied URL: block loopback, private, link-local (including cloud metadata at 169.254.169.254) and IPv4-mapped IPv6 ranges; resolve once and connect to that address to defeat DNS rebinding; revalidate on every redirect; restrict schemes. URL parsers disagree on inputs like `http://a@b` and backslashes, so validate the parsed object actually used to connect.
- Indirect fetchers: headless renderers, image proxies, webhook senders, XML external entities.

### File-system scope and races
- Check-then-use on paths an attacker can modify (symlink swap between validation and open); prefer `O_NOFOLLOW`, directory file descriptors, or a directory only the process can write.
- Predictable names in shared temp directories, `tempfile.mktemp`; recursive deletes on input-derived paths or through symlinks.

### Crypto
- Disabled TLS verification (`verify=False`, `rejectUnauthorized: false`), nonce reuse with GCM or CTR, ECB, unauthenticated encryption, fast password hashes instead of argon2, scrypt or bcrypt, `random` instead of `secrets`.

### Supply chain
- Release or CI installs without a lockfile or hashes; packages new in the diff with unfamiliar names or low adoption (verify on the registry when network allows, otherwise flag as unverified); install-time code (`postinstall`, executing `setup.py`); `--extra-index-url` enabling dependency confusion.
- CI: actions pinned by tag rather than commit SHA, `pull_request_target` workflows that check out the PR head, `${{ github.event.* }}` interpolated into `run:` steps, secrets exposed to fork builds.
- Advisory scanners (`pip-audit`, `npm audit`, `osv-scanner`) are leads; report only advisories with a plausible path to the vulnerable code.

### LLM agent permissions
- Tool scope: read versus write, path scopes, network allowlists, per-tool credentials. A generic shell or fetch tool nullifies narrower controls elsewhere.
- Command allowlists that admit dangerous forms (a `git` prefix rule admitting `git -c core.sshCommand=...`, a `python` rule admitting `-c`).
- The dangerous triad: private data access, exposure to untrusted content, and an outbound channel (network, rendered links, writes to public places) in one session. Any two may be acceptable; all three without gating is a P0 or P1 design defect.
- Irreversible actions (delete, send, pay, push, deploy, merge) gated by program checks or by a human shown the exact arguments.
- Untrusted-content quarantine (labeled delimiters, no authority) is defense in depth, never the boundary; third-party MCP tool descriptions and outputs are untrusted content too.

## Evidence standard
Proof is a traced path (path:line at source, transforms and sink) plus a reproduction (test or command with output) or the exact remaining precondition. A cleared candidate names the control that stops it. Not proof: scanner output alone, an advisory without reachability, "uses subprocess", "could be vulnerable if" without a concrete input.

## Severity guide
- P0: unauthenticated or low-privilege code execution; reading or modifying another user's data; a live credential committed, logged to a shared place, or passed to an untrusted child; SSRF reaching cloud metadata or internal admin services; an agent that reads untrusted content and can run shell commands or send data out without gating.
- P1: needs a realistic precondition (authenticated user, victim opening a link, local user on a shared host); IDOR on less sensitive objects; stored XSS in an operator view; zip slip in an import; deserializing uploaded files.
- P2: missing defense in depth with no current path (no `--` where input is already validated, blocklist environment filtering that currently covers known secrets, unpinned build-only tooling).
- P3: hardening with no plausible exploit (headers, redaction polish).
Downgrade when the attacker already holds the capability the exploit grants: a local user injecting a command into a CLI they run as themselves gains nothing.

## Skill-specific output
1. Trust-boundary map: one line stating the deployment model and attacker profiles, then a table with columns Entry point | Input controlled by | Auth required | Sinks reached | Controls in place (path:line).
2. Attack-path table: ID | Attacker | Entry | Steps (source, transforms, sink) | Impact | Preconditions | Fix | Finding ID. One row per P0-P2 path.
3. Authorization matrix when the system has more than one role or ownership boundary: rows are actions, columns are roles, cells are the enforcing path:line or MISSING.

## Anti-patterns
- Severity by pattern: flagging `shell=True` on a constant command. Corrective: no finding without an attacker-controlled source and a stated attacker profile.
- Self-attack: calling "the user can inject into their own local CLI" code execution. Corrective: the attacker must gain a capability they did not already have.
- Prompt-only fixes: "tell the model to ignore instructions in documents". Corrective: fix capability (narrow scope, gate the action, cut egress); prompt quarantine is supplementary.
- Scanner dumps: pasting forty advisories. Corrective: report reachable ones; one line for the rest under "Checked and cleared".
- Wrong-layer fixes: HTML-escaping data bound for a shell, or sanitizing input instead of using the sink's safe API. Corrective: parameterization, argv arrays, safe loaders, path containment at the sink.
- Stopping at the first instance. Corrective: grep every handler of the same shape and list all affected sites in one finding.
- Over-engineering: demanding a vault, signing or a rewrite when one validated argument closes the path. Corrective: minimal fix first; larger hardening as P3 unless no local fix exists.

## Done when
- [ ] Deployment model, attacker profiles and every entry point are in the trust-boundary map.
- [ ] Every P0/P1 has a full attack path, a reproduction or a reason it was not run, and a sink-level fix.
- [ ] Secondary routes and sibling handlers were checked, not only the primary path.
- [ ] Secrets were checked in code, logs, child environments and history.
- [ ] Agent tools were assessed under the fully-hijacked-model assumption.
- [ ] Cleared candidates name the control that stops them, and the verdict block matches the severity counts.
