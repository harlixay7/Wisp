"""Repository consistency: docs, versions, lockfiles, and the README test count."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from tests.helpers import ROOT
from tests.helpers.hygiene import USER_PROFILE_PATH_PATTERNS, local_username_leaks
from tools import antigravity_viewer
from tools.antigravity_bridge import BLOCKED_ENV_EXACT, BLOCKED_ENV_PREFIXES, WISP_VERSION
from tools.skill_loader import SHIPPED_SKILL_DIR, Skill, SkillLoader

DELEGATION_SKILL = ROOT / "integrations" / "antigravity-delegation" / "SKILL.md"

# Documentation the project maintains. CHANGELOG.md is checked for machine
# paths but not for current names or removed files: released entries are
# history and keep the names they shipped with.
MAINTAINED_DOCS = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "docs/integrations.md",
    "docs/delegation-playbook.md",
    "integrations/antigravity-delegation/SKILL.md",
)
REMOVED_FILES = ("AgentSkill.md", "opencode.json", "tools/antigravity_mcp.cmd", ".opencode")
_SKIPPED_DIRS = {".git", ".venv", "node_modules", ".claude", ".antigravity-reports", "__pycache__"}
_TEXT_SUFFIXES = {".md", ".py", ".json", ".yml", ".yaml", ".toml", ".sh", ".bat", ".cmd"}
_LINK_PATTERN = re.compile(r"\]\(([^)\s]+)\)|(?:href|src|srcset)=\"([^\"]+)\"")
_TABLE_SKILL_CELL = re.compile(r"`([a-z0-9]+(?:-[a-z0-9]+)*)`")


def _shipped_skills() -> list[Skill]:
    return SkillLoader(SHIPPED_SKILL_DIR).skills


def _section(text: str, heading: str) -> str:
    """The body of the Markdown section that starts with ``heading``."""
    start = text.index(heading)
    following = re.search(r"^## ", text[start + len(heading) :], re.MULTILINE)
    return text[start : start + len(heading) + following.start()] if following else text[start:]


def _table_skill_names(section: str, column: int) -> set[str]:
    """Backticked names in one column of every table row in ``section``."""
    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        match = _TABLE_SKILL_CELL.fullmatch(cells[column]) if len(cells) > abs(column) else None
        if match:
            names.add(match.group(1))
    return names


def _github_anchors(markdown: str) -> set[str]:
    """Heading anchors as GitHub generates them, including duplicate suffixes."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        match = None if in_fence else re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        slug = re.sub(r"[^\w\- ]", "", match.group(1).strip().lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


class TestDelegationSkill:
    def test_front_matter_follows_the_agent_skills_format(self) -> None:
        text = DELEGATION_SKILL.read_text(encoding="utf-8")
        assert text.startswith("---\n"), "SKILL.md must open with YAML front matter"
        front = yaml.safe_load(text.split("---\n", 2)[1])

        assert front["name"] == DELEGATION_SKILL.parent.name == "antigravity-delegation"
        description = front["description"]
        assert isinstance(description, str) and description.strip()
        assert len(description) <= 1024, "Agent Skills cap descriptions at 1024 characters"
        assert re.fullmatch(r"\d+\.\d+\.\d+", str(front["metadata"]["version"]))

    def test_routing_table_names_every_shipped_skill(self) -> None:
        text = DELEGATION_SKILL.read_text(encoding="utf-8")
        routed = _table_skill_names(_section(text, "## 3. Choosing skills"), column=-1)

        assert routed == {skill.name for skill in _shipped_skills()}

    def test_skill_carries_no_setup_material(self) -> None:
        """Registration belongs in docs/integrations.md; the skill is for agents."""
        text = DELEGATION_SKILL.read_text(encoding="utf-8")
        for marker in ("claude mcp add", "mcp_servers.antigravity", "mcpServers", "cmdkey"):
            assert marker not in text, f"SKILL.md contains setup material: {marker}"


class TestDocCanonicalization:
    def test_readme_playbook_table_names_every_shipped_skill(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        listed = _table_skill_names(_section(readme, "## Review playbooks"), column=0)

        assert listed == {skill.name for skill in _shipped_skills()}

    def test_docs_use_current_skill_names_not_aliases(self) -> None:
        aliases = {alias for skill in _shipped_skills() for alias in skill.aliases}
        assert aliases, "the shipped playbooks are expected to keep their old names as aliases"
        for name in MAINTAINED_DOCS:
            text = (ROOT / name).read_text(encoding="utf-8")
            stale = sorted(alias for alias in aliases if alias in text)
            assert not stale, f"{name} uses retired skill names: {stale}"

    def test_removed_files_stay_removed_and_unreferenced(self) -> None:
        for removed in REMOVED_FILES:
            assert not (ROOT / removed).exists(), f"{removed} was removed; do not restore it"
        banned = (
            "AgentSkill.md",
            "antigravity_mcp.cmd",
            ".opencode/skills/antigravity-delegation",
        )
        this_file = Path(__file__).resolve()
        for path in ROOT.rglob("*"):
            if (
                not path.is_file()
                or _SKIPPED_DIRS.intersection(path.relative_to(ROOT).parts)
                or path.suffix not in _TEXT_SUFFIXES
                or path.name == "CHANGELOG.md"
                or path.resolve() == this_file
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for marker in banned:
                assert marker not in text, f"{path.relative_to(ROOT)} references {marker}"

    def test_relative_links_in_docs_resolve(self) -> None:
        for name in (*MAINTAINED_DOCS, "CHANGELOG.md"):
            doc = ROOT / name
            text = doc.read_text(encoding="utf-8")
            for match in _LINK_PATTERN.finditer(text):
                target = match.group(1) or match.group(2)
                if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("<"):
                    continue
                path_part, _, anchor = target.partition("#")
                resolved = (doc.parent / path_part).resolve() if path_part else doc
                assert resolved.exists(), f"{name}: broken link to {target}"
                if anchor and resolved.suffix == ".md":
                    anchors = _github_anchors(resolved.read_text(encoding="utf-8"))
                    where = path_part or name
                    assert anchor in anchors, f"{name}: no heading for #{anchor} in {where}"

    def test_no_machine_paths_in_repo_docs(self) -> None:
        # Structural user-profile patterns are the machine-independent guard;
        # the local-username check runs on developer machines only (on CI
        # runners Path.home().name is a generic account like "runner").
        for name in (*MAINTAINED_DOCS, "CHANGELOG.md"):
            path = ROOT / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in USER_PROFILE_PATH_PATTERNS:
                assert not re.search(pattern, text), f"{name}: user-profile path ({pattern})"
            assert "gmail.com" not in text, name
            assert not re.search(r"[A-Za-z]:\\+AgentReview", text), f"{name}: hardcoded bridge path"
            assert "OneDrive" not in text, name
            if not os.environ.get("CI"):
                assert not local_username_leaks(text), f"{name}: local username leaked"
            assert not re.search(r"[A-Za-z]:\\\\?Users\\\\", text), f"{name}: user-profile path"

    def test_example_payload_is_brand_neutral_and_bounded(self) -> None:
        example = ROOT / "examples" / "delegation-case-study.json"
        assert example.is_file()
        payload = json.loads(example.read_text(encoding="utf-8"))
        blob = json.dumps(payload)
        assert not re.search(r"(?i)viducia|shortstudio", blob), (
            "the example must not reference private projects"
        )
        assert payload.get("skills"), "example must demonstrate explicit skill selection"
        assert not (ROOT / "EXAMPLE_delegation_payload.json").exists()


class TestVersionParity:
    def test_viewer_version_tracks_product_version(self) -> None:
        """The status API, Server header, and UI footer all report WISP_VERSION."""
        assert antigravity_viewer.SERVER_VERSION == WISP_VERSION

    def test_shell_lockfile_version_parity(self) -> None:
        """package-lock.json must not lag package.json after a version bump."""
        shell = ROOT / "tools" / "wisp_shell"
        package = json.loads((shell / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((shell / "package-lock.json").read_text(encoding="utf-8"))
        assert lock["version"] == package["version"]
        assert lock["packages"][""]["version"] == package["version"]


class TestSecurityDocParity:
    def test_security_doc_lists_every_stripped_variable(self) -> None:
        """SECURITY.md enumerates exactly what the bridge strips from agy's environment."""
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        for prefix in BLOCKED_ENV_PREFIXES:
            assert prefix.rstrip("_") in security, f"SECURITY.md missing {prefix}"
        for exact in BLOCKED_ENV_EXACT:
            assert exact in security, f"SECURITY.md missing {exact}"
        assert "BLOCKED_ENV_PREFIXES" in security and "BLOCKED_ENV_EXACT" in security


class TestReadmeTestCount:
    def test_readme_test_count_matches_collected(self) -> None:
        """The test count quoted in the README must match what pytest collects."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r"#\s*(\d+) test cases", readme)
        assert match, "README test-case count not found"
        claimed = int(match.group(1))
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=180,
        )
        match_collected = re.search(r"(\d+) tests collected", result.stdout)
        assert match_collected, result.stdout[-400:]
        actual = int(match_collected.group(1))
        assert claimed == actual, (
            f"README claims {claimed} test cases but {actual} are collected - "
            "update the README to the real count"
        )
