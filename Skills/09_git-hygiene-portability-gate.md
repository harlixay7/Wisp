---
name: git-hygiene-portability-gate
version: 4.0.0
description: >-
  Use when checking that a repository goes from a fresh clone to a passing
  build and test run on every supported platform, or before cutting a release:
  documented setup run literally, lockfile and manifest consistency,
  cross-platform portability, machine-specific paths, secrets and large blobs in
  the tree and history, ignore-file gaps, CI matrix coverage, reproducible
  artifacts, licensing and tag hygiene. Produces a clone-to-green log, a
  portability matrix and findings. Not for exploitability of leaked secrets or
  vulnerable dependencies (use runtime-security-vault-engine) or accuracy of
  documentation beyond setup steps (use documentation-retraction-ledger-engine).
brief: |
  Mission: prove or disprove that a stranger can clone this repository and reach green tests on each supported platform using only what is documented, and that a release built from it is reproducible and clean.
  - Work from a fresh clone (or `git archive HEAD`) with a fresh environment, never the developer's working tree; run the documented setup literally and log every command, exit code and undocumented fix.
  - Check lockfiles against manifests with the ecosystem's frozen or check mode; applications pin through a lockfile, libraries declare ranges.
  - Scan for portability breakers: hardcoded user paths, case-only filename collisions, CRLF shebang scripts without .gitattributes, lost executable bits, bash-isms, Windows reserved names and long paths, locale-dependent encoding.
  - Inspect the tree and full history for secrets and large blobs; check tracked files that ignore rules should exclude and outputs a test run leaves untracked.
  - Compare the CI matrix with claimed platforms and runtime versions; CI steps the README omits are documentation gaps.
  - Release: tags match versions, CI builds artifacts from the tag, package contents and license files are right.
  Output before findings: clone-to-green log (step, command, result, evidence, undocumented fix) and portability matrix (concern by OS with OK, BREAKS, RISK, NOT RUN). PASS = documented setup reaches green and no P0/P1; PASS_WITH_FIXES = green only after small documented or config fixes; BLOCK = setup cannot reach green on a supported platform, checkout fails on one, or a secret or large blob is in history.
activation_triggers:
  task_modes:
    - CLONE_TO_GREEN_CHECK
    - RELEASE_HYGIENE_AUDIT
    - PORTABILITY_AUDIT
  keywords:
    - clone-to-green
    - fresh clone
    - gitattributes
    - line endings
    - case sensitivity
    - lockfile drift
    - hardcoded path
    - gitignore
    - large files in history
    - ci matrix
    - reproducible build
    - release tag
  do_not_use_when:
    - The question is whether a leaked secret or vulnerable dependency is exploitable (use runtime-security-vault-engine).
    - The README's claims about features or performance are under review rather than its setup steps (use documentation-retraction-ledger-engine).
    - A failing test needs a code fix rather than an environment or packaging fix (use zero-regression-surgical-implementation).
input_contract:
  requires_worktree: true
  required_inputs:
    - The repository at the revision under review
  optional_inputs:
    - Supported platforms and runtime versions
    - Release version or tag being prepared
    - Known setup complaints from contributors
output_contract:
  sections:
    - Clone-to-green log
    - Portability matrix
    - Largest objects (when history is in scope)
  findings: shared format
  verdict: shared verdict block
---

# Git hygiene and portability gate

## Mission
An excellent result tells the calling agent exactly where a newcomer's first hour breaks: the failing command, the platform, why, and the one-line fix to the repository or its docs; and whether a release built from this tree is reproducible and free of blobs and secrets. The most common failure is testing in the reviewer's already-working environment, where installed packages, ignored local files and exported variables hide every gap a clean machine would hit.

## Inputs to establish first
- Supported platforms and runtime versions: from README, classifiers, `requires-python`, `engines`, `.python-version`, `.nvmrc` and the CI matrix. Disagreements between these sources are findings.
- The documented setup path: README, CONTRIBUTING, Makefile or task runner, devcontainer, setup scripts.
- What the reviewer can execute: available OS, network access, package registries. Anything that cannot be run is marked NOT RUN and assessed statically with medium confidence at best.
- Whether history is in scope (release audits and first publication: yes; routine diffs: only the new commits).

## Method
1. Clean checkout. Use `git clone --no-local <path> <scratch>` or `git archive HEAD | tar -x -C <scratch>` so untracked and ignored local files are absent. Create a fresh virtual environment or empty dependency cache and unset project variables the docs do not mention. Done when the checkout contains only tracked files.
2. Literal setup. Execute each documented command verbatim in order, recording the exact command, exit code and the decisive output lines. When a step fails, find the minimal fix, apply it locally, note it as undocumented, and continue so later breakage is still found. Done when the test suite has run or an unfixable blocker is recorded.
3. Hidden-state check. In the original working tree run `git status --ignored --porcelain` and look for ignored files the code reads at runtime (local configs, generated stubs, downloaded models); each is a setup step the docs owe the reader. After the test run in the clean checkout, `git status --porcelain` lists outputs that should be ignored. Done when both lists are explained.
4. Dependency consistency. Run the ecosystem's check: `uv lock --check`, `poetry check --lock`, `pip-compile` with a diff against the committed file, `npm ci`, `pnpm install --frozen-lockfile`, `yarn install --immutable`, `cargo metadata --locked`, `go mod tidy` followed by `git diff --exit-code go.mod go.sum`. Done when lock and manifest agree or the drift is itemized.
5. Portability scan (checklist) across all tracked text files, scripts and configs. Done when each matrix cell is filled with evidence.
6. Tree and history scan (checklist). Done when the largest objects and any secret-pattern hits are listed with commit and path.
7. CI and release review. Done when every claimed platform and version is mapped to a CI job or marked uncovered, and release steps are traced from tag to artifact.

## Checklist

### Platform portability
- Case: `git ls-files | sort -f | uniq -di` finds paths differing only in case (checkout clobbers one file on Windows and default macOS). Imports whose case differs from the filename pass on case-insensitive systems and fail on Linux.
- Line endings: `git ls-files --eol` shows files committed with CRLF. Shebang scripts with CRLF fail with `env: 'python\r'` or `bash\r`. A sound `.gitattributes` has `* text=auto`, `eol=lf` for `*.sh` and other shebang scripts, `eol=crlf` for `*.bat` and `*.cmd`, and `binary` for images and archives. Without it, `core.autocrlf` on a contributor's machine decides.
- Executable bits: `git ls-files -s` shows mode 100755 for scripts invoked directly; commits from Windows often drop it (`git update-index --chmod=+x <file>` fixes it).
- Shell assumptions: bash syntax (`[[`, arrays, `source`) under `#!/bin/sh`; GNU-only flags (`sed -i` without a suffix argument, `readlink -f`, `grep -P`, `date -d`); Makefiles and npm scripts using `rm -rf`, `export`, `&&` chains or single quotes that cmd.exe does not understand; `python` vs `python3` vs `py`.
- Windows names and lengths: files named `CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9` with any extension; trailing dots or spaces; `:`, `*`, `?`, `"`, `<`, `>`, `|` in names; paths beyond 260 characters without `core.longpaths`; symlinks, which check out as small text files unless `core.symlinks` is enabled.
- Encoding: `open()` without `encoding=` uses the locale code page on Windows and raises `UnicodeDecodeError` on UTF-8 files; non-ASCII output to a legacy console.
- Process model: multiprocessing start methods differ by platform and version, so code relying on fork-inherited globals breaks under spawn; POSIX-only signals and `os.fork`; Windows cannot delete or rename open files, breaking tests that clean up while a handle is open.
- Paths: string concatenation or `split("/")` on filesystem paths, `/tmp` hardcoded instead of the platform temp directory, `~` passed unexpanded to subprocesses, `HOME` assumed on Windows (use the language's home-directory API).

### Machine-specific state
- Grep tracked files for absolute user paths (`/home/<name>`, `/Users/<name>`, drive-letter paths), usernames, hostnames and fixed ports.
- Committed environment artifacts carrying absolute paths: `.vscode/settings.json` interpreter paths, `.idea/`, `pyvenv.cfg`, `*.egg-link`, `.pth` files, coverage XML, notebooks with outputs, lockfile entries resolving to `file:` paths on one machine.
- Version derivation from git (setuptools-scm, `git describe`) breaks on shallow CI clones and source tarballs unless configured with a fallback or full fetch depth.

### Dependencies
- Applications and services commit a lockfile; libraries declare compatible ranges and test against both the lowest and the latest allowed versions where feasible. Demanding exact pins in a library's install requirements is wrong advice.
- Hashes where the toolchain supports them (`--require-hashes`, lockfile integrity fields); private indexes declared in config, not only on one machine.
- Platform markers and wheels: every dependency installs on every supported OS and runtime version without a compiler, or the docs say which toolchain is needed.
- Tooling pinned: pre-commit `rev` values and linter versions, so CI does not change on an upstream release.

### Tree and history
- Largest current files: `git ls-tree -r -l HEAD | sort -k4 -n | tail -20`. Largest objects in all history: `git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' | sort -k3 -n | tail -20`. Pack size: `git count-objects -vH`; use `git-sizer` when installed.
- Secret patterns in history: `git log -p --all -G '<regex>'` for key prefixes and `BEGIN .* PRIVATE KEY`; deleted env and credential files via `git log --all --diff-filter=D --name-only`; gitleaks with `--log-opts=--all` when available. Record existence and location; exploitability and rotation belong to runtime-security-vault-engine.
- `git ls-files -ci --exclude-standard` lists tracked files that ignore rules now match (committed before the rule existed).
- `.gitignore` coverage: virtual environments, dependency directories, build and dist outputs, caches, coverage, local env files (while keeping `.env.example` tracked), OS and editor files, report and log directories the tools write.
- Git LFS: patterns in `.gitattributes` vs `git lfs ls-files`; a clone without LFS installed receives pointer files, so the docs must say so. Submodules: pinned commits reachable, URLs usable without SSH keys in CI.

### CI and release
- The CI matrix covers every claimed OS and the minimum and maximum supported runtime versions; a platform claimed but untested is a RISK cell for every platform-sensitive construct found.
- CI steps the README lacks (system packages, environment variables, service containers) are documentation gaps; caches restoring dependency directories can hide lock drift.
- Reproducibility: build twice and compare hashes (`python -m build`, `npm pack`, `cargo package`); sources of drift are embedded timestamps (`SOURCE_DATE_EPOCH` unset), file ordering in archives, and absolute paths baked into artifacts.
- Package contents: list the sdist or tarball (`tar tzf`, `npm pack --dry-run`, `unzip -l` on wheels) to confirm required data files ship and test fixtures, local configs and reports do not.
- Tags and versions: annotated tag matches the manifest version and changelog; artifacts are built by CI from the tag, not uploaded from a laptop; protected default branch.
- Licensing: a LICENSE file whose identifier matches the manifest's SPDX field; vendored code keeps its license headers; NOTICE obligations for redistributed Apache-2.0 components; fonts and images have redistribution rights.

## Evidence standard
Proof is a command run in a clean checkout with its exit code and the decisive output, or a tracked path with the offending bytes (for line endings, `git ls-files --eol` output; for blobs, object id, size and introducing commit from `git log --all --find-object=<id>`). Reading the README is not running it. A breakage on a platform you could not run must cite the exact construct and the platform rule it violates, at medium confidence.

## Severity guide
- P0: documented setup cannot reach green on a supported platform with no reasonable workaround; checkout itself fails on a supported OS (case collision, reserved name, invalid character); a live-looking secret anywhere in reachable history; a missing lockfile letting a release resolve an incompatible major version.
- P1: green only after an undocumented step or variable; lock and manifest out of sync; CRLF shebang scripts with no `.gitattributes` in a project claiming Windows contributors; a user path in a shipped config; a supported platform absent from CI while platform-specific code exists; a large binary added in the change under review (permanent clone cost).
- P2: ignore gaps producing noise after a test run; unpinned dev tooling; long-path risk in deep fixture trees; missing NOTICE entries; non-reproducible artifact timestamps.
- P3: tag naming, changelog formatting, minor ordering in ignore files.

## Skill-specific output
1. Clone-to-green log: Step | Command (exact) | Result (PASS, FAIL, NOT RUN) | Evidence (exit code and decisive output) | Undocumented fix applied. Name the platform and runtime version in the table caption.
2. Portability matrix: rows are concerns (case, line endings, executable bits, shell, reserved names and length, encoding, process model, paths, dependency install, CI coverage); columns Linux | macOS | Windows | Evidence. Cells are OK, BREAKS, RISK or NOT RUN.
3. Largest objects, when history is in scope: Object id | Size | Path | Introducing commit | Still in HEAD.

## Anti-patterns
- Reviewing in the existing environment. Corrective: clean checkout and fresh environment, stated in the log caption.
- Stopping at the first failing step. Corrective: apply a local workaround, record it, and continue so all breakages come back in one pass.
- Flagging every forward slash as a Windows bug. Corrective: most Windows APIs accept `/`; flag only paths that are split, compared, or handed to cmd.exe or tools that require backslashes.
- Pinning dogma. Corrective: lockfiles for applications, ranges for libraries; judge by whether installs are reproducible, not by the presence of `==`.
- Prescribing history rewrites in passing. Corrective: rewriting history breaks every clone and fork and does not revoke a leaked credential; recommend rotation first, and history rewriting only as an owner decision with a coordination plan.
- Extension-based blob rules. Corrective: judge by size and churn; a 20 KB icon is fine, a regenerated 40 MB fixture on every commit is not.
- Claiming portability from one green CI job. Corrective: portability is per platform and per supported runtime version; uncovered cells stay RISK or NOT RUN.

## Done when
- [ ] The clone-to-green log comes from a clean checkout and fresh environment, with platform and versions stated.
- [ ] Every undocumented step is recorded with its fix to the docs or repository.
- [ ] Lock and manifest consistency was checked with the ecosystem's own command.
- [ ] Each portability matrix cell has evidence or is marked NOT RUN.
- [ ] Largest objects and secret-pattern hits are listed when history is in scope.
- [ ] Claimed platforms and versions are mapped to CI jobs.
- [ ] Release artifacts, tags and license files were checked when a release is in scope, and the verdict block reflects the severity counts.
