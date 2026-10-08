"""Governance and hygiene of the shipped Skills/ registry."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from tests.helpers import ROOT
from tests.helpers.hygiene import USER_PROFILE_PATH_PATTERNS
from tools.skill_loader import SkillLoader

SKILLS_DIR = ROOT / "Skills"

REQUIRED_FRONTMATTER = (
    "name",
    "version",
    "description",
    "activation_triggers",
    "input_contract",
    "output_contract",
)
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _banned_patterns() -> tuple[str, ...]:
    """Hygiene patterns for the skill-file scan (machine-independent)."""
    return (
        r"(?i)\b[C-Z]:\\+Users\\[A-Za-z0-9._~-]+",
        r"[A-Za-z]:\\\\?AgentReview",
        r"gmail\.com",
        r"OneDrive",
        r"\[cite:",
        r"&#x20;",
    )


# Sections every skill body carries (see CONTRIBUTING.md, "Adding a review playbook").
REQUIRED_BODY_SECTIONS = (
    "## Mission",
    "## Inputs to establish first",
    "## Method",
    "## Checklist",
    "## Evidence standard",
    "## Severity guide",
    "## Skill-specific output",
    "## Anti-patterns",
    "## Done when",
)
VERDICTS = ("PASS", "PASS_WITH_FIXES", "BLOCK")
BRIEF_CHARS = (600, 1800)
EXPECTED_SKILL_COUNT = 17


def _documents() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    for path in sorted(SKILLS_DIR.iterdir()):
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".yaml":
            docs[path.name] = yaml.safe_load(text)
            continue
        match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
        assert match, f"{path.name}: markdown skill missing YAML frontmatter"
        docs[path.name] = yaml.safe_load(match.group(1))
    return docs


def _activation(doc: dict, filename: str) -> dict:
    """Returns the activation mapping (uniform ``activation_triggers`` schema)."""
    del filename
    return doc.get("activation_triggers") or {}


class TestRegistryGovernance:
    def test_expected_skill_count(self) -> None:
        files = [path for path in SKILLS_DIR.iterdir() if path.suffix in (".md", ".yaml", ".yml")]
        assert len(files) == EXPECTED_SKILL_COUNT

    def test_frontmatter_complete(self) -> None:
        for name, doc in _documents().items():
            missing = [key for key in REQUIRED_FRONTMATTER if key not in doc or not doc[key]]
            assert not missing, f"{name}: missing {missing}"

    def test_names_unique_and_canonical(self) -> None:
        names = [doc["name"] for doc in _documents().values()]
        assert len(names) == len(set(names))
        for name in names:
            assert re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name), name

    def test_versions_are_semver(self) -> None:
        for name, doc in _documents().items():
            assert SEMVER.match(str(doc["version"])), f"{name}: version {doc['version']}"

    def test_descriptions_route_and_disambiguate(self) -> None:
        for name, doc in _documents().items():
            description = str(doc["description"])
            assert description.startswith("Use when"), (
                f"{name}: description must open 'Use when ...'"
            )
            assert "Not for" in description or "not for" in description, (
                f"{name}: description should state the negative boundary"
            )

    def test_no_cross_skill_keyword_collisions(self) -> None:
        keyword_owner: dict[str, str] = {}
        collisions: list[str] = []
        for name, doc in _documents().items():
            for keyword in _activation(doc, name).get("keywords", []):
                key = str(keyword).strip().lower()
                if key in keyword_owner and keyword_owner[key] != name:
                    collisions.append(f"{key!r}: {keyword_owner[key]} vs {name}")
                keyword_owner[key] = name
        assert not collisions, collisions

    def test_do_not_use_when_present(self) -> None:
        for name, doc in _documents().items():
            assert _activation(doc, name).get("do_not_use_when"), name

    def test_file_name_matches_skill_name(self) -> None:
        for filename, doc in _documents().items():
            assert re.match(r"^\d{2}_", filename), filename
            assert Path(filename).stem.split("_", 1)[1] == doc["name"], filename

    def test_body_has_required_sections(self) -> None:
        for path in sorted(SKILLS_DIR.glob("*.md")):
            body = path.read_text(encoding="utf-8")
            missing = [section for section in REQUIRED_BODY_SECTIONS if section not in body]
            assert not missing, f"{path.name}: missing {missing}"

    def test_brief_is_standalone_and_bounded(self) -> None:
        low, high = BRIEF_CHARS
        for name, doc in _documents().items():
            brief = str(doc.get("brief") or "")
            assert low <= len(brief) <= high, f"{name}: brief is {len(brief)} chars"
            for verdict in VERDICTS:
                assert verdict in brief, f"{name}: brief does not define {verdict}"

    def test_only_the_implementation_skill_requests_write_access(self) -> None:
        writers = sorted(
            doc["name"]
            for doc in _documents().values()
            if (doc.get("input_contract") or {}).get("write_access") == "required"
        )
        assert writers == ["safe-implementation"]

    def test_skills_defer_to_the_shared_protocol(self) -> None:
        """The bridge sends the review protocol once; skills must not restate it."""
        for path in sorted(SKILLS_DIR.glob("*.md")):
            body = path.read_text(encoding="utf-8")
            assert "<<<WISP_VERDICT" not in body, f"{path.name}: restates the verdict block"

    def test_loader_loads_the_whole_registry_without_warnings(self) -> None:
        loader = SkillLoader(SKILLS_DIR)
        assert len(loader.skills) == EXPECTED_SKILL_COUNT
        assert loader.warnings == []

    def test_every_shipped_skill_has_payload_and_triggers(self) -> None:
        registry = ROOT / "Skills"
        if not registry.is_dir():
            pytest.skip("workspace Skills registry is not present")
        loader = SkillLoader(registry)

        skills = loader.skills

        assert skills
        for skill in skills:
            assert skill.instructions_payload.strip()
            assert skill.activation_triggers


class TestRegistryHygiene:
    def test_no_machine_paths_or_export_artifacts(self) -> None:
        for path in SKILLS_DIR.iterdir():
            if path.suffix not in (".md", ".yaml", ".yml"):
                continue
            text = path.read_text(encoding="utf-8")
            patterns = list(_banned_patterns()) + list(USER_PROFILE_PATH_PATTERNS)
            if not os.environ.get("CI"):
                # Personal usernames exist on developer machines only. Match them
                # as a path segment so a common word ("root") is not a false hit.
                patterns.append(rf"[\\/]{re.escape(Path.home().name)}[\\/]")
            for pattern in patterns:
                assert not re.search(pattern, text), (
                    f"{path.name}: matches banned pattern {pattern}"
                )

    def test_skill_files_reasonable_size(self) -> None:
        for path in SKILLS_DIR.iterdir():
            if path.suffix not in (".md", ".yaml", ".yml"):
                continue
            size_kb = path.stat().st_size / 1024
            assert 4 <= size_kb <= 20, f"{path.name}: {size_kb:.1f} KB outside guidance band"
