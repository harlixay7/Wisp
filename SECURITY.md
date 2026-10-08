# Security

Wisp starts a coding agent, the Antigravity CLI (`agy`), and lets it read
your project and run commands so it can check your assistant's work. This
page explains what Wisp does to keep that tidy, what it does not protect you
from, and how to report a problem.

> **The short version:** Wisp keeps the reviewer's processes together so they
> can all be stopped, keeps your credentials out of its environment, and
> saves a complete record of every review. It is **not a sandbox**. The
> reviewer runs with the same rights as you, and Wisp cannot protect you from
> harmful code in the project you point it at.

## What Wisp trusts

- **You, and the coding assistant you set up** to send review requests.
- **Wisp's own code** and its pinned dependencies (PyYAML at runtime;
  pytest, ruff and Electron for development and the overlay).
- **The `agy` program you installed.** It handles the Google sign-in. Wisp
  never reads or stores your credentials. The one exception you control is
  the quota hook: a command you configure yourself (`--quota-hook` or
  `ANTIGRAVITY_QUOTA_HOOK`) that Wisp runs when quota runs out. It is never
  taken from a review request or from files in the project.
- **The folder you point Wisp at.** Mounting it for the reviewer is the whole
  point, so Wisp assumes you meant to share it.

## What Wisp does not trust

- **The content of the project under review**, including its `AGENTS.md`,
  READMEs and code comments. Every review request tells the reviewer to treat
  the workspace as evidence, not instructions, and to report attempts to
  steer it as findings. That makes prompt injection harder; it doesn't make
  it impossible.
- **Output from tools and models.** Wisp saves and shows it word for word,
  and never runs it.
- **Captured text and pasted images.** They are stored under
  `.antigravity-reports/captures/` and attached to a request only from
  approved folders.
- **Anyone who can talk to the MCP server.** It has no login because it only
  talks over stdin and stdout to the assistant that started it. Don't expose
  it over a network without adding your own access control.
- **Other projects' live folders** on the same machine. The widget reads them
  but never writes to them.

## What Wisp guarantees

1. **Stopping a review stops everything it started.** On Windows, `agy` is
   started suspended, placed in a Job Object
   (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) and only then resumed. To stop it,
   Wisp kills every descendant it can find in a process snapshot, runs
   `taskkill /F /T`, and terminates the Job Object. On macOS and Linux, `agy`
   runs in its own session and Wisp kills the whole process group. Each
   review records which of these methods actually worked, and reports and
   warnings show it.
2. **Credentials are removed from the reviewer's environment.** Before
   starting `agy`, Wisp removes variables that commonly hold secrets
   (`AWS_*`, `AZURE_*`, `GITHUB_*`, `GH_*`, `SSH_*`, `OPENAI_*`,
   `ANTHROPIC_*`, `GEMINI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`,
   `GOOGLE_API_KEY`, `HF_*`/`HUGGINGFACE*`, `GIT_ASKPASS`, `SSH_ASKPASS`) and
   variables that can inject code into interpreters or package managers
   (`NODE_OPTIONS`, `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`,
   `NPM_CONFIG_USERCONFIG`). This is a blocklist, so a secret stored under
   another name is passed through. The exact list is
   `BLOCKED_ENV_PREFIXES` and `BLOCKED_ENV_EXACT` in
   `tools/antigravity_bridge.py`.
3. **Every review leaves a complete record.** The full output of `agy` and a
   provenance block (Wisp and `agy` versions, git commit, a hash of the
   playbooks, timestamp) are written to `.antigravity-reports/` in the
   reviewed project. The file is written to a temporary name first and then
   renamed, so a crash never leaves half a report.

## What Wisp does not do

- **It doesn't limit what the reviewer may do.** `agy` runs with
  `--dangerously-skip-permissions`, so it can read and write wherever it has
  access and run commands without asking. In the default review mode Wisp
  *tells* it not to change files, but nothing enforces that. Wisp controls
  the reviewer's processes, not its authority.
- **It isn't a sandbox for untrusted code.** The reviewer may build and run
  the project's code to check a claim. Never point Wisp at a project you
  wouldn't build and test yourself.
- **It can't catch every runaway process.** A program that deliberately
  detaches from its parent can escape both the Job Object and the process
  group. One case we have seen on Windows: Python launched through the
  Microsoft Store alias in `WindowsApps` re-launches itself, so its children
  are no longer linked to the process Wisp started. Wisp still cleans up
  reliably when it stops a review that is running (timeout, error or
  interruption). Cleaning up after `agy` has already exited on its own is
  best effort in that setup.
- **It doesn't control the network.** The reviewer can make network
  requests. If that matters to you, restrict it with your firewall or OS.

## Setup

`setup.bat` and `setup.sh` look for Python (offering to install it with
winget, Homebrew or your Linux package manager when there is none) and then
run `tools/wisp_setup.py`, which:

- asks before every install and every change outside the Wisp folder. The
  offers are: Python, Node.js (for the desktop overlay), `agy install` to put
  the Antigravity CLI on your PATH, and, with `--connect` only, registering
  Wisp with Claude Code or Codex and copying the delegation skill. Installers
  such as winget may show their own permission prompt. Without a terminal,
  or with `--no-input`, it asks nothing and installs nothing;
- otherwise writes only inside the Wisp folder: `.venv/`,
  `tools/wisp_shell/node_modules/` for the desktop overlay, and its log,
  `.wisp-setup.log`;
- keeps a backup (`config.toml.wisp-backup`) before it edits a Codex config;
- installs the exact versions pinned in `requirements.txt` (or
  `requirements-dev.txt`) from PyPI, and the Electron shell with `npm ci`,
  which installs exactly what `package-lock.json` lists;
- passes arguments to each program directly rather than through a shell
  command line, and gives every step a time limit;
- never reads or changes your Antigravity sign-in;
- refuses to delete `.venv` with `--recreate-venv` unless it really is a
  virtual environment (it must contain `pyvenv.cfg`).

`--check` changes nothing at all.

## The widget's local server

- It listens on `127.0.0.1` without a password, because only you can reach
  it there. Binding it to any other address requires `--auth-token` or
  `--generate-token`. Every route then needs that token, `/health` reveals as
  little as possible, and `Host` headers are checked to block DNS-rebinding
  attacks.
- `/api/ask` only accepts image attachments from the workspace and the
  capture folder.
- `.antigravity-reports/` holds your full review requests. Treat it as
  private: don't share it, and add it to your project's `.gitignore` (Wisp's
  own repository already ignores it).

## Reporting a vulnerability

Please report vulnerabilities privately through a
[GitHub security advisory](https://github.com/harlixay7/Wisp/security/advisories/new),
not in a public issue. It helps to include:

- what an attacker could do, and what they need first (for example, a
  malicious file in the reviewed project);
- the steps to reproduce it, with your operating system and Wisp version
  (`python tools/antigravity_bridge.py --status` prints it);
- any logs or reports, with private details removed.

We can't promise a response time. Fixes go into `main` and the next release;
older releases don't get separate patches. For bugs that aren't security
problems, a normal [issue](https://github.com/harlixay7/Wisp/issues/new/choose)
is fine.
