"""Repository consistency: docs, versions, lockfiles, and the README test count."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from tests.helpers import ROOT
from tests.helpers.hygiene import USER_PROFILE_PATH_PATTERNS, local_username_leaks
from tools import antigravity_viewer
from tools.antigravity_bridge import WISP_VERSION

CANONICAL_SKILL = ROOT / ".opencode" / "skills" / "antigravity-delegation" / "SKILL.md"
MASTER_DOC = ROOT / "AgentSkill.md"


class TestDocCanonicalization:
    def test_master_doc_embeds_canonical_skill_verbatim(self) -> None:
        canonical = CANONICAL_SKILL.read_text(encoding="utf-8")
        master = MASTER_DOC.read_text(encoding="utf-8")
        marker = "````markdown\n"
        start = master.index(marker) + len(marker)
        end = master.index("\n````", start)
        assert master[start:end].strip() == canonical.strip(), (
            "AgentSkill.md §2 is stale: regenerate it from "
            ".opencode/skills/antigravity-delegation/SKILL.md"
        )

    def test_master_doc_labels_the_embed_as_generated(self) -> None:
        master = MASTER_DOC.read_text(encoding="utf-8")
        assert "generated" in master.lower()
        assert ".opencode/skills/antigravity-delegation/SKILL.md" in master

    def test_no_machine_paths_in_repo_docs(self) -> None:
        # Structural user-profile patterns are the machine-independent guard;
        # the local-username check runs on developer machines only (on CI
        # runners Path.home().name is a generic account like "runner").
        checked = [
            "AgentSkill.md",
            "AGENTS.md",
            "README.md",
            "CHANGELOG.md",
            "SECURITY.md",
            "docs/delegation-playbook.md",
        ]
        for name in checked:
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


class TestCharterParity:
    def test_agents_blocklist_enumeration_matches_bridge(self) -> None:
        """The AGENTS.md charter enumerates the variable families the bridge strips."""
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for marker in (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_API_KEY",
            "GIT_ASKPASS",
            "NODE_OPTIONS",
            "PYTHONPATH",
            "BLOCKED_ENV_PREFIXES",
        ):
            assert marker in agents, f"AGENTS.md missing {marker}"


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
