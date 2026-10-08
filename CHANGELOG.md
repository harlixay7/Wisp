# Changelog

All notable changes to Wisp are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
semantic per component with the bridge/server version as the product version
(`tools/antigravity_bridge.py::WISP_VERSION`).

Component versions are tracked together: bridge + MCP server + viewer move as
one product version; the adversarial skill registry (`Skills/`) and the
delegation skill (`integrations/antigravity-delegation/SKILL.md`) version
independently; the Electron shell versions with the viewer.

## [Unreleased]

## [1.0.0] — 2026-10-08

First public release.

### Added
- **Delegation bridge** (`tools/antigravity_bridge.py`). Builds a review
  request from a JSON envelope or command-line flags, runs the Google
  Antigravity CLI (`agy`) as a contained child process with credential
  variables removed from its environment, retries transient failures, and
  fails over to a backup model when quota runs out (optionally waiting for
  the reset or running an account-switch hook). The complete critique is
  returned untruncated and saved as a report under `.antigravity-reports/`.
- **Review and implement modes.** Reviews are read-only by default and
  propose changes as diffs; `implement` lets the reviewer edit files in the
  workspace.
- **Structured findings and verdicts.** Every finding uses one format
  (`F-001 · P1 · high · <category>`), and every review ends with a
  `WISP_VERDICT` block (PASS, PASS_WITH_FIXES or BLOCK, severity counts and
  the must-fix list) that is parsed into results and reports.
- **Seventeen review playbooks** in `Skills/`: `plan-review`,
  `design-second-opinion`, `pre-merge-review`, `root-cause-investigation`,
  `wiring-audit`, `safe-implementation`, `data-integrity-review`,
  `security-review`, `claim-check`, `performance-profiling`,
  `agent-workflow-review`, `prompt-review`, `ai-eval-review`, `rag-review`,
  `ui-review`, `docs-accuracy-review` and `portability-review`. Each has a
  standalone brief and a full procedure; the loader validates every file
  and resolves names, aliases and file names.
- **Compact requests.** Each selected playbook travels as its brief plus the
  path to its full file, with a one-line index of the rest, so any selection
  fits the Windows command-line limit.
- **MCP server** (`tools/antigravity_mcp_server.py`) with
  `antigravity_review`, `antigravity_status` and `antigravity_skills`, for
  Claude Code, Codex, Cursor, Cline and any other MCP client.
- **Desktop widget.** A local server and UI with six views (owl, focus,
  stream, chat, verdict and history), eleven owl moods, a live feed across
  every project on the machine, replay, a Ctrl+K command palette, chat with
  pasted images, and screen-capture hotkeys on Windows. It runs as a
  transparent Electron overlay or in a browser window.
- **Process containment.** Windows Job Objects with whole-tree termination;
  process groups on macOS and Linux.
- **One-command setup** for Windows, macOS and Linux (`setup.bat`,
  `setup.sh`, `tools/wisp_setup.py`). It offers to install what is missing
  (Python, Node.js for the overlay, and a guided wait while you install the
  Antigravity CLI), installs the pinned dependencies, and runs a self-test of
  the engine and the MCP server. `--connect` registers Wisp with Claude Code
  or Codex and installs the delegation skill, asking for each one. Every
  install or outside change needs a yes (`--yes` accepts all); failures end
  with a plain summary and a full log in `.wisp-setup.log`.
- **Delegation skill** for coding assistants
  (`integrations/antigravity-delegation/SKILL.md`) and a setup guide for each
  assistant (`docs/integrations.md`).
- **Continuous integration**: the offline test suite on Windows and Ubuntu
  with Python 3.10 and 3.13, the setup from a clean checkout on Windows,
  macOS and Linux, and a release workflow that publishes source archives
  with checksums.
