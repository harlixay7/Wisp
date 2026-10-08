"""Skill loader: YAML/Markdown parsing, validation, selection, directory resolution, and manifests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.bridge import make_skill_yaml
from tools.skill_loader import (
    ALL_SELECTOR,
    SHIPPED_SKILL_DIR,
    SkillLoader,
    SkillNotFoundError,
    SkillValidationError,
    resolve_skill_dir,
)

ESCAPED_MARKDOWN_SKILL = (
    "\\---\n"
    "\n"
    "name: escaped-skill\n"
    "\n"
    "version: 2.0.0\n"
    "\n"
    "description: A rich-text exported skill.\n"
    "\n"
    "activation\\_triggers:\n"
    "\n"
    "&#x20; task\\_modes:\n"
    "\n"
    "&#x20;   - MODE\\_ONE\n"
    "\n"
    "&#x20; keywords:\n"
    "\n"
    "&#x20;   - keyword\n"
    "\n"
    "input\\_contract:\n"
    "\n"
    "&#x20; requires\\_worktree: true\n"
    "\n"
    "output\\_contract:\n"
    "\n"
    "&#x20; requires\\_verdict: true\n"
    "\n"
    "\\---\n"
    "\n"
    "\\# MANDATE\n"
    "\n"
    "Operate \\*\\*strictly\\*\\* in read-only mode.\n"
)


class TestSkillLoader:
    def test_valid_registry_loads_and_renders(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        rendered = loader.render_selected(ALL_SELECTOR)

        assert "CRASH_OPS_INSTRUCTIONS" in rendered
        assert "AST_AUDIT_INSTRUCTIONS" in rendered
        assert "TEMPLATE_ONLY_INSTRUCTIONS" not in rendered
        assert loader.available() == [
            "crash_ops",
            "ast_audit",
            "template_custom_skill",
        ]

    def test_load_by_filename_stem(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        skill = loader.load("01_crash_ops")

        assert skill.name == "crash_ops"

    def test_select_deduplicates_and_preserves_order(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        selected = loader.select("ast_audit,ast_audit,crash_ops")

        assert [skill.name for skill in selected] == ["ast_audit", "crash_ops"]

    def test_unknown_identifier_reports_available(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        with pytest.raises(SkillNotFoundError) as excinfo:
            loader.load("does_not_exist")

        assert "ast_audit" in str(excinfo.value)

    def test_missing_required_field_fails_validation(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "broken.yaml").write_text(
            make_skill_yaml("broken", omit="instructions_payload"), encoding="utf-8"
        )

        with pytest.raises(SkillValidationError) as excinfo:
            _ = SkillLoader(skill_dir).skills

        assert "instructions_payload" in str(excinfo.value)

    def test_invalid_yaml_fails_validation(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "bad.yaml").write_text("name: [unclosed", encoding="utf-8")

        with pytest.raises(SkillValidationError):
            _ = SkillLoader(skill_dir).skills

    def test_missing_registry_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SkillNotFoundError):
            _ = SkillLoader(tmp_path / "nope").skills


class TestSkillFileFormats:
    def test_escaped_rich_text_markdown_skill_loads(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "02_escaped.md").write_text(ESCAPED_MARKDOWN_SKILL, encoding="utf-8")

        skill = SkillLoader(skill_dir).load("escaped-skill")

        assert skill.name == "escaped-skill"
        assert "MODE_ONE" in skill.activation_triggers
        assert "keyword" in skill.activation_triggers
        assert "**strictly**" in skill.instructions_payload
        assert skill.input_contract["requires_worktree"] is True

    def test_markdown_without_frontmatter_is_rejected(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "plain.md").write_text("# No front matter\n\nBody only.", encoding="utf-8")

        with pytest.raises(SkillValidationError):
            _ = SkillLoader(skill_dir).skills

    def test_yaml_mapping_triggers_are_flattened(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "mapping.yaml").write_text(
            "\n".join(
                [
                    "name: mapping_triggers",
                    "version: 1.0.0",
                    "description: Trigger mapping test.",
                    "activation_triggers:",
                    "  task_modes:",
                    "    - A_MODE",
                    "  keywords:",
                    "    - kw",
                    "input_contract:",
                    "  required: [paths]",
                    "output_contract:",
                    "  format: markdown",
                    'instructions_payload: "DO"',
                ]
            ),
            encoding="utf-8",
        )

        skill = SkillLoader(skill_dir).load("mapping_triggers")

        assert skill.activation_triggers == ("A_MODE", "kw")

    def test_empty_placeholder_is_skipped_with_warning(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "01_valid.yaml").write_text(
            make_skill_yaml("valid_one", payload="VALID"), encoding="utf-8"
        )
        (skill_dir / "04_pending.md").write_bytes(b"")

        loader = SkillLoader(skill_dir)
        selected = loader.select(ALL_SELECTOR)

        assert [skill.name for skill in selected] == ["valid_one"]
        assert any("04_pending.md" in warning for warning in loader.warnings)

    def test_skill_yaml_with_utf8_bom_is_accepted(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "bom_skill.yaml").write_bytes(
            b"\xef\xbb\xbf"
            + make_skill_yaml("bom_skill", payload="BOM_INSTRUCTIONS").encode("utf-8")
        )

        skill = SkillLoader(skill_dir).load("bom_skill")

        assert skill.instructions_payload == "BOM_INSTRUCTIONS"


class TestSkillDirResolution:
    def test_workspace_registry_wins(self, registry: Path, tmp_path: Path) -> None:
        resolved, fell_back = resolve_skill_dir(tmp_path, registry)

        assert resolved == registry.resolve()
        assert fell_back is False

    def test_workspace_registry_detected(self, tmp_path: Path) -> None:
        local = tmp_path / "Skills"
        local.mkdir()
        (local / "a.yaml").write_text(make_skill_yaml(), encoding="utf-8")

        resolved, fell_back = resolve_skill_dir(tmp_path)

        assert resolved == local.resolve()
        assert fell_back is False

    def test_falls_back_to_shipped_registry(self, tmp_path: Path) -> None:
        resolved, fell_back = resolve_skill_dir(tmp_path)

        assert fell_back is True
        assert resolved == SHIPPED_SKILL_DIR

    def test_missing_override_is_strict(self, tmp_path: Path) -> None:
        with pytest.raises(SkillNotFoundError):
            resolve_skill_dir(tmp_path, tmp_path / "missing")


class TestSkillManifest:
    def test_manifest_is_metadata_only(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        manifest = loader.render_manifest()

        assert "## ADVERSARIAL SKILL REGISTRY MANIFEST" in manifest
        for skill in loader.skills:
            assert f"**{skill.name}**" in manifest
            assert str(skill.source_path) in manifest
            marker = skill.instructions_payload.strip()[:60]
            assert marker not in manifest

    def test_prompt_blocks_are_labeled(self, registry: Path) -> None:
        loader = SkillLoader(registry)
        skills = loader.select(["crash_ops"])

        active = loader.render_prompt(skills, heading="## ACTIVE ADVERSARIAL SKILLS (MANDATORY)")
        recommended = loader.render_prompt(
            skills, heading="## RECOMMENDED ADVERSARIAL SKILLS (TASK-DEPENDENT)"
        )

        assert "## ACTIVE ADVERSARIAL SKILLS (MANDATORY)" in active
        assert "## RECOMMENDED ADVERSARIAL SKILLS (TASK-DEPENDENT)" in recommended
