"""Skill loader: YAML/Markdown parsing, validation, selection, and directory resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.bridge import make_skill_yaml
from tools.skill_loader import (
    ALL_SELECTOR,
    INDEX_PURPOSE_MAX_CHARS,
    SHIPPED_SKILL_DIR,
    SKILL_ROLE_MANDATORY,
    SKILL_ROLE_RECOMMENDED,
    SkillLoader,
    SkillNotFoundError,
    SkillValidationError,
    clip_line,
    first_sentence,
    render_registry_index,
    render_skill_block,
    render_skill_pointer,
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
    def test_valid_registry_loads_and_selects(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        payloads = " ".join(
            skill.instructions_payload for skill in loader.select(ALL_SELECTOR)
        )

        assert "CRASH_OPS_INSTRUCTIONS" in payloads
        assert "AST_AUDIT_INSTRUCTIONS" in payloads
        assert "TEMPLATE_ONLY_INSTRUCTIONS" not in payloads
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


class TestSkillBriefs:
    def test_brief_is_parsed_and_stripped(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        (skill_dir / "01_briefed.md").write_text(
            "---\n"
            "name: briefed\n"
            "version: 4.0.0\n"
            "description: Use when testing briefs. Not for anything else.\n"
            "brief: |\n"
            "  Mission line.\n"
            "  - rule one\n"
            "activation_triggers: [brief]\n"
            "input_contract: {requires_worktree: true}\n"
            "output_contract: {findings: shared format}\n"
            "---\n"
            "# Body\n",
            encoding="utf-8",
        )

        skill = SkillLoader(skill_dir).load("briefed")

        assert skill.brief == "Mission line.\n- rule one"
        assert skill.effective_brief == skill.brief

    def test_missing_brief_falls_back_to_first_sentence(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(
            make_skill_yaml(
                "no_brief",
                description="Use when auditing e.g. parsers. Not for docs (use other).",
            ),
            encoding="utf-8",
        )

        skill = SkillLoader(tmp_path).load("no_brief")

        assert skill.brief == ""
        assert skill.effective_brief == "Use when auditing e.g. parsers."
        assert skill.purpose == "Use when auditing e.g. parsers."

    def test_non_text_brief_fails_validation(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(
            make_skill_yaml("bad_brief") + "brief:\n  - not text\n", encoding="utf-8"
        )

        with pytest.raises(SkillValidationError) as excinfo:
            _ = SkillLoader(tmp_path).skills

        assert "brief" in str(excinfo.value)

    def test_purpose_is_one_short_line(self) -> None:
        text = "Use when " + "auditing very long things " * 20 + "now. Second sentence."

        sentence = first_sentence(text)
        clipped = clip_line(sentence, INDEX_PURPOSE_MAX_CHARS)

        assert sentence.endswith("now.")
        assert len(clipped) <= INDEX_PURPOSE_MAX_CHARS
        assert "\n" not in clipped
        assert clipped.endswith("…")

    def test_write_access_flag_reads_input_contract(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(
            make_skill_yaml("writer", input_contract={"write_access": "required"}),
            encoding="utf-8",
        )
        (tmp_path / "b.yaml").write_text(make_skill_yaml("reader"), encoding="utf-8")

        loader = SkillLoader(tmp_path)

        assert loader.load("writer").requires_write_access is True
        assert loader.load("reader").requires_write_access is False


class TestCompactRendering:
    def test_registry_index_lists_every_skill_with_absolute_path(self, registry: Path) -> None:
        loader = SkillLoader(registry)

        index = render_registry_index(loader.skills)

        lines = index.splitlines()
        assert len(lines) == len(loader.skills)
        for line, skill in zip(lines, loader.skills, strict=True):
            assert line.startswith(f"- **{skill.name}** — ")
            assert line.endswith(f" · {skill.absolute_path}")
            assert skill.instructions_payload not in line

    def test_skill_block_points_at_the_full_file(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(
            make_skill_yaml("briefed", version="4.0.0", brief="BRIEF TEXT", payload="FULL BODY"),
            encoding="utf-8",
        )
        skill = SkillLoader(tmp_path).load("briefed")

        block = render_skill_block(skill, SKILL_ROLE_MANDATORY)

        assert block.splitlines()[0] == "### briefed (v4.0.0) — mandatory"
        assert block.splitlines()[1] == (
            f"Full procedure: {skill.absolute_path} — read it in full before "
            "starting; it is binding."
        )
        assert block.endswith("BRIEF TEXT")
        assert "FULL BODY" not in block
        pointer = render_skill_pointer(skill, SKILL_ROLE_RECOMMENDED)
        assert "recommended" in pointer
        assert str(skill.absolute_path) in pointer
        assert "BRIEF TEXT" not in pointer

    def test_unknown_role_is_rejected(self, registry: Path) -> None:
        skill = SkillLoader(registry).load("crash_ops")

        with pytest.raises(ValueError):
            render_skill_block(skill, "optional")


class TestSkillAliases:
    """Renamed skills keep resolving under their earlier names."""

    @staticmethod
    def _registry(tmp_path: Path, *files: tuple[str, str]) -> Path:
        skill_dir = tmp_path / "Skills"
        skill_dir.mkdir()
        for file_name, text in files:
            (skill_dir / file_name).write_text(text, encoding="utf-8")
        return skill_dir

    def test_alias_resolves_to_the_renamed_skill(self, tmp_path: Path) -> None:
        skill_dir = self._registry(
            tmp_path,
            ("01_plan-review.yaml", make_skill_yaml("plan-review", aliases=["old-plan-engine"])),
        )
        loader = SkillLoader(skill_dir)

        assert loader.load("OLD-PLAN-ENGINE").name == "plan-review"
        selected = loader.select("old-plan-engine, plan-review")
        assert [skill.name for skill in selected] == ["plan-review"]
        assert loader.available() == ["plan-review"]

    def test_alias_colliding_with_another_skill_name_fails(self, tmp_path: Path) -> None:
        skill_dir = self._registry(
            tmp_path,
            ("01_a.yaml", make_skill_yaml("alpha", aliases=["beta"])),
            ("02_b.yaml", make_skill_yaml("beta")),
        )

        with pytest.raises(SkillValidationError, match="Duplicate skill name or alias 'beta'"):
            _ = SkillLoader(skill_dir).skills

    def test_invalid_alias_is_rejected(self, tmp_path: Path) -> None:
        skill_dir = self._registry(
            tmp_path, ("01_a.yaml", make_skill_yaml("alpha", aliases=["has spaces"]))
        )

        with pytest.raises(SkillValidationError, match="invalid alias"):
            _ = SkillLoader(skill_dir).skills

    def test_every_shipped_skill_keeps_its_previous_name(self) -> None:
        skills = SkillLoader(SHIPPED_SKILL_DIR).skills

        assert all(skill.aliases for skill in skills)
        for skill in skills:
            for alias in skill.aliases:
                assert SkillLoader(SHIPPED_SKILL_DIR).load(alias).name == skill.name
