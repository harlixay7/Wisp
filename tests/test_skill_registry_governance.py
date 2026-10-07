"""Executable governance for the skill registry (audit #45).

The repair report claims machine-verifiable properties for the twelve
adversarial skills; this module turns those claims into permanent regression
tests: frontmatter completeness, unique names, semver versions, discriminating
routing (no cross-skill keyword collisions, ``do_not_use_when`` present),
mandatory deliverable machinery (scratchpad + EARS + verdict enums), registry
loadability via the production loader, hygiene (no machine paths or banned
artifacts), and size sanity.

It also pins the documentation-canonicalization contract (audit #46):
``AgentSkill.md`` §2 embeds the deployable skill verbatim, byte-for-byte.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "Skills"
CANONICAL_SKILL = ROOT / ".opencode" / "skills" / "antigravity-delegation" / "SKILL.md"
MASTER_DOC = ROOT / "AgentSkill.md"

REQUIRED_FRONTMATTER = (
    "name",
    "version",
    "description",
    "activation_triggers",
    "input_contract",
    "output_contract",
)
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# Structural user-profile path patterns: the actual leak vector, and
# machine-independent by construction (a dynamic "local username must not
# appear" check is NOT machine-independent — on GitHub runners
# Path.home().name is literally "runner", which false-positives on ordinary
# words like "workflow runner").
USER_PROFILE_PATH_PATTERNS = (
    r"(?i)\b[C-Z]:\\+Users\\[A-Za-z0-9._~-]+",
    r"(?i)/(?:home|Users)/[A-Za-z0-9._~-]{2,}",
)


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


def _local_username_leaks(text: str) -> bool:
    """True when the LOCAL username appears in ``text``.

    Runs only outside CI: personal usernames exist on developer machines, not
    on CI runners (where ``Path.home().name`` is a generic account such as
    "runner" — treating that as a leak would false-positive on ordinary
    English like "workflow runner"). Short usernames are skipped as too
    generic to match reliably.
    """
    if os.environ.get("CI"):
        return False
    username = Path.home().name
    if len(username) < 5:
        return False
    return username.lower() in text.lower()
REQUIRED_MACHINERY = (
    ("scratchpad", "every skill mandates a reasoning scratchpad block"),
    ("EARS", "every skill expresses remediations in EARS syntax"),
    ("do_not_use_when", "every skill carries routing disclaimers"),
)


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
    def test_exactly_twelve_skills(self) -> None:
        files = [path for path in SKILLS_DIR.iterdir() if path.suffix in (".md", ".yaml", ".yml")]
        assert len(files) == 12

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
            assert description.startswith("Use when"), f"{name}: description must open 'Use when ...'"
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

    def test_machinery_present_in_instructions(self) -> None:
        for name, doc in _documents().items():
            body = doc.get("instructions_payload") or ""
            if not body:
                # Markdown skills keep the mandate outside the frontmatter.
                path = SKILLS_DIR / name
                if path.suffix != ".yaml":
                    body = path.read_text(encoding="utf-8")
            lowered = body.lower()
            for needle, why in REQUIRED_MACHINERY:
                if needle == "do_not_use_when":
                    continue  # frontmatter-level, checked above
                assert needle.lower() in lowered, f"{name}: {why}"

    def test_verdict_enums_declared(self) -> None:
        for name, doc in _documents().items():
            path = SKILLS_DIR / name
            body = str(doc.get("instructions_payload") or "") or path.read_text(encoding="utf-8")
            assert "VERDICT" in body.upper(), f"{name}: no verdict contract"
            assert "REGISTRY" in body.upper() or "MATRIX" in body.upper(), (
                f"{name}: no registry/matrix deliverable"
            )

    def test_loader_loads_the_whole_registry_without_warnings(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT))
        from tools.skill_loader import SkillLoader

        loader = SkillLoader(SKILLS_DIR)
        assert len(loader.skills) == 12
        assert loader.warnings == []


class TestRegistryHygiene:
    def test_no_machine_paths_or_export_artifacts(self) -> None:
        for path in SKILLS_DIR.iterdir():
            if path.suffix not in (".md", ".yaml", ".yml"):
                continue
            text = path.read_text(encoding="utf-8")
            patterns = list(_banned_patterns()) + list(USER_PROFILE_PATH_PATTERNS)
            if not os.environ.get("CI"):
                # Personal usernames exist on developer machines only.
                patterns.append(re.escape(Path.home().name))
            for pattern in patterns:
                assert not re.search(pattern, text), f"{path.name}: matches banned pattern {pattern}"

    def test_skill_files_reasonable_size(self) -> None:
        for path in SKILLS_DIR.iterdir():
            if path.suffix not in (".md", ".yaml", ".yml"):
                continue
            size_kb = path.stat().st_size / 1024
            assert 4 <= size_kb <= 20, f"{path.name}: {size_kb:.1f} KB outside guidance band"


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
            "REPAIR_REPORT.md",
            "DELEGATION_PLAYBOOK.md",
            "AgentSkill.md",
            "AGENTS.md",
            "README.md",
            "CHANGELOG.md",
            "SECURITY.md",
            "docs/skills-repair-report.md",
            "docs/benchmark-protocol.md",
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
                assert not _local_username_leaks(text), f"{name}: local username leaked"
            assert not re.search(r"[A-Za-z]:\\\\?Users\\\\", text), f"{name}: user-profile path"

    def test_example_payload_is_brand_neutral_and_bounded(self) -> None:
        example = ROOT / "examples" / "delegation-case-study.json"
        assert example.is_file()
        payload = json.loads(example.read_text(encoding="utf-8"))
        blob = json.dumps(payload)
        for brand in ("Viducia", "ShortStudio"):
            assert brand not in blob
        assert payload.get("skills"), "example must demonstrate explicit skill selection"
        assert not (ROOT / "EXAMPLE_delegation_payload.json").exists()
