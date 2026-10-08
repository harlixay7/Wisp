"""Skill registry loader for the harness-agnostic Antigravity delegation bridge.

The registry lives in ``Skills`` (path configurable). Skill definitions may be
YAML files (``.yaml``/``.yml``) or Markdown files (``.md``/``.markdown``) with a
YAML front-matter block; Markdown bodies become the ``instructions_payload`` when
the front matter does not declare one. Rich-text export artifacts (escaped
markdown, HTML non-breaking spaces) are normalized automatically. This module
discovers the registry, enforces the metadata contract, resolves skill
identifiers, and renders skills for the Antigravity payload.

The bridge passes its whole payload on the ``agy`` command line, which Windows
caps near 32K characters, so it never inlines full skill bodies. It sends each
selected skill's short ``brief`` plus the absolute path of the full file (see
:func:`render_skill_block`) and a one-line-per-skill registry index
(:func:`render_registry_index`); the reviewer reads the full files from disk.

Run ``python -m tools.skill_loader --validate`` to validate a registry.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REQUIRED_FIELDS: tuple[str, ...] = (
    "name",
    "version",
    "description",
    "activation_triggers",
    "input_contract",
    "output_contract",
    "instructions_payload",
)
DEFAULT_SKILL_DIR = Path("Skills")
SHIPPED_SKILL_DIR = Path(__file__).resolve().parent.parent / DEFAULT_SKILL_DIR
YAML_SKILL_SUFFIXES = (".yaml", ".yml")
MARKDOWN_SKILL_SUFFIXES = (".md", ".markdown")
SKILL_SUFFIXES = YAML_SKILL_SUFFIXES + MARKDOWN_SKILL_SUFFIXES
ALL_SELECTOR = "all"
_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
# Index lines stay one short line per skill so the index of a large registry
# costs a few thousand characters of the command-line budget, not tens of
# thousands.
INDEX_PURPOSE_MAX_CHARS = 160
SKILL_ROLE_MANDATORY = "mandatory"
SKILL_ROLE_RECOMMENDED = "recommended"
_SKILL_ROLES = (SKILL_ROLE_MANDATORY, SKILL_ROLE_RECOMMENDED)
_SENTENCE_END_PATTERN = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`(\"'])")


class SkillError(Exception):
    """Base class for skill registry failures."""


class SkillValidationError(SkillError):
    """Raised when a skill definition violates the metadata contract."""


class SkillNotFoundError(SkillError):
    """Raised when a requested skill identifier is absent from the registry."""


def resolve_skill_dir(
    workspace: Path | str, override: Path | str | None = None
) -> tuple[Path, bool]:
    """Resolves the skill registry directory and whether a fallback was used.

    Priority: explicit override (strict, no fallback) > ``<workspace>/Skills`` >
    the registry shipped with the bridge. Raises :class:`SkillNotFoundError`
    when no candidate directory exists.
    """
    if override is not None and str(override).strip():
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_dir():
            raise SkillNotFoundError(f"Skill registry directory not found: {candidate}")
        return candidate, False
    workspace_candidate = Path(workspace).expanduser() / DEFAULT_SKILL_DIR
    if workspace_candidate.is_dir():
        return workspace_candidate.resolve(), False
    if SHIPPED_SKILL_DIR.is_dir():
        return SHIPPED_SKILL_DIR, True
    raise SkillNotFoundError(
        f"Skill registry directory not found: {workspace_candidate} "
        f"(no shipped registry at {SHIPPED_SKILL_DIR})"
    )


_HTML_SPACE_PATTERN = re.compile(r"&#x20;|&nbsp;")
_ESCAPED_MARKDOWN_PATTERN = re.compile(r"\\([\\`*_{}\[\]()#+\-.!&])")
_FRONTMATTER_DELIMITER_PATTERN = re.compile(r"^\\?-{3,}\s*$")


def _normalize_authoring_text(text: str) -> str:
    """Repairs rich-text/export artifacts (escaped markdown, HTML spaces)."""
    return _ESCAPED_MARKDOWN_PATTERN.sub(r"\1", _HTML_SPACE_PATTERN.sub(" ", text))


def _split_markdown_frontmatter(text: str) -> tuple[str | None, str]:
    """Splits Markdown front matter (``---`` or escaped ``\\---``) from the body."""
    lines = text.splitlines()
    opening: int | None = None
    for index, line in enumerate(lines):
        candidate = line.strip().lstrip("\ufeff")
        if not candidate:
            continue
        if _FRONTMATTER_DELIMITER_PATTERN.match(candidate):
            opening = index
        break
    if opening is None:
        return None, text
    for closing in range(opening + 1, len(lines)):
        if _FRONTMATTER_DELIMITER_PATTERN.match(lines[closing].strip()):
            frontmatter = "\n".join(lines[opening + 1 : closing])
            body = "\n".join(lines[closing + 1 :])
            return frontmatter, body
    return None, text


def _collect_trigger_strings(value: Any) -> list[str]:
    """Flattens trigger declarations (string, list, or mapping of lists) to strings."""
    collected: list[str] = []

    def _walk(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            text = item.strip()
            if text:
                collected.append(text)
        elif isinstance(item, Mapping):
            for sub_item in item.values():
                _walk(sub_item)
        elif isinstance(item, (list, tuple)):
            for sub_item in item:
                _walk(sub_item)

    _walk(value)
    seen: set[str] = set()
    unique: list[str] = []
    for trigger in collected:
        if trigger not in seen:
            seen.add(trigger)
            unique.append(trigger)
    return unique


def first_sentence(text: str) -> str:
    """Returns the first sentence of ``text`` collapsed onto one line.

    A sentence ends at ``.``/``!``/``?`` followed by whitespace and an
    upper-case letter, digit, quote or bracket, so abbreviations such as
    "e.g. foo" do not cut it short.
    """
    flat = " ".join(str(text or "").split())
    if not flat:
        return ""
    return _SENTENCE_END_PATTERN.split(flat, maxsplit=1)[0].strip()


def clip_line(text: str, limit: int) -> str:
    """Shortens one line to at most ``limit`` characters at a word boundary.

    Used only for skill index summaries the bridge composes itself; reviewer
    output is never shortened.
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[: max(limit - 1, 1)]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "\u2026"


def _mapping_requires_write(value: Any) -> bool:
    """True when a contract mapping declares ``write_access: required`` at any depth."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).strip().lower() == "write_access" and isinstance(item, str):
                if item.strip().lower() == "required":
                    return True
            if _mapping_requires_write(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_mapping_requires_write(item) for item in value)
    return False


def render_payload(value: Any, level: int = 0) -> str:
    """Renders an arbitrary YAML payload into deterministic Markdown text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip("\n")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        blocks: list[str] = []
        for key, item in value.items():
            heading = "#" * min(3 + level, 6)
            label = str(key).replace("_", " ").strip()
            rendered = render_payload(item, level + 1)
            blocks.append(f"{heading} {label}\n{rendered}".rstrip())
        return "\n\n".join(blocks)
    if isinstance(value, (list, tuple)):
        return "\n".join(f"- {render_payload(item, level + 1)}" for item in value)
    return str(value)


@dataclass(frozen=True)
class Skill:
    """A validated skill definition loaded from the registry."""

    name: str
    version: str
    description: str
    activation_triggers: tuple[str, ...]
    input_contract: Mapping[str, Any] = field(repr=False)
    output_contract: Mapping[str, Any] = field(repr=False)
    instructions_payload: str = field(repr=False)
    source_path: Path = field(repr=False)
    kind: str = "skill"
    # Optional: third-party registries predate the field, so it is not part of
    # REQUIRED_FIELDS and an absent value falls back to the description.
    brief: str = field(default="", repr=False)
    # Optional earlier names that still resolve to this skill, so renaming a
    # skill does not break envelopes and harness configs that use the old name.
    aliases: tuple[str, ...] = ()

    @property
    def stem(self) -> str:
        return self.source_path.stem

    @property
    def is_template(self) -> bool:
        return self.kind.lower() == "template"

    @property
    def absolute_path(self) -> Path:
        """The skill file as an absolute path the reviewer can open.

        ``os.path.abspath`` rather than ``resolve()``: a registry symlinked
        into the workspace must keep its in-workspace path, which is the one
        mounted for the reviewer.
        """
        return Path(os.path.abspath(self.source_path))

    @property
    def effective_brief(self) -> str:
        """The inline operating summary: ``brief``, else the description's first sentence."""
        return self.brief or first_sentence(self.description)

    @property
    def purpose(self) -> str:
        """One-line purpose for the registry index (first sentence, clipped)."""
        return clip_line(first_sentence(self.description), INDEX_PURPOSE_MAX_CHARS)

    @property
    def requires_write_access(self) -> bool:
        """True when ``input_contract`` declares ``write_access: required``."""
        return _mapping_requires_write(self.input_contract)


def parse_skill_document(document: object, source_path: Path) -> Skill:
    """Validates a parsed YAML document and returns a :class:`Skill`.

    Accepts any parsed value so that non-mapping roots (a bare list or scalar)
    are reported as validation errors rather than crashing.
    """
    if not isinstance(document, Mapping):
        raise SkillValidationError(f"{source_path}: root document must be a YAML mapping")

    missing = [
        name
        for name in REQUIRED_FIELDS
        if name not in document
        or document[name] is None
        or document[name] == ""
        or document[name] == []
        or document[name] == {}
    ]
    if missing:
        raise SkillValidationError(
            f"{source_path}: missing required metadata fields: {', '.join(missing)}"
        )

    name = str(document["name"]).strip()
    if not _NAME_PATTERN.match(name):
        raise SkillValidationError(
            f"{source_path}: invalid skill name {name!r}; expected {_NAME_PATTERN.pattern}"
        )
    version = str(document["version"]).strip()
    description = str(document["description"]).strip()

    triggers = tuple(_collect_trigger_strings(document["activation_triggers"]))
    if not triggers:
        raise SkillValidationError(
            f"{source_path}: 'activation_triggers' must contain at least one string "
            "(string, list, or mapping of lists)"
        )

    for contract_field in ("input_contract", "output_contract"):
        if not isinstance(document[contract_field], Mapping):
            raise SkillValidationError(
                f"{source_path}: '{contract_field}' must be a mapping"
            )

    payload = document["instructions_payload"]
    if not isinstance(payload, (str, Mapping)):
        raise SkillValidationError(
            f"{source_path}: 'instructions_payload' must be text or a mapping"
        )
    rendered_payload = render_payload(payload).strip()
    if not rendered_payload:
        raise SkillValidationError(f"{source_path}: 'instructions_payload' rendered empty")

    kind = str(document.get("kind", "skill")).strip() or "skill"
    raw_brief = document.get("brief")
    if raw_brief is not None and not isinstance(raw_brief, str):
        raise SkillValidationError(f"{source_path}: 'brief' must be text")
    brief = (raw_brief or "").strip()
    aliases = _parse_aliases(document.get("aliases"), source_path)

    return Skill(
        name=name,
        version=version,
        description=description,
        activation_triggers=triggers,
        input_contract=document["input_contract"],
        output_contract=document["output_contract"],
        instructions_payload=rendered_payload,
        source_path=source_path,
        kind=kind,
        brief=brief,
        aliases=aliases,
    )


def _parse_aliases(value: Any, source_path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise SkillValidationError(f"{source_path}: 'aliases' must be a name or a list of names")
    aliases: list[str] = []
    for item in items:
        alias = str(item).strip()
        if not _NAME_PATTERN.match(alias):
            raise SkillValidationError(
                f"{source_path}: invalid alias {alias!r}; expected {_NAME_PATTERN.pattern}"
            )
        if alias not in aliases:
            aliases.append(alias)
    return tuple(aliases)


def _load_markdown_skill(raw_text: str, path: Path) -> Skill:
    frontmatter, body = _split_markdown_frontmatter(raw_text)
    if frontmatter is None:
        raise SkillValidationError(
            f"{path}: Markdown skill files require YAML front matter delimited by '---'"
        )
    try:
        document = yaml.safe_load(_normalize_authoring_text(frontmatter))
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"{path}: invalid YAML front matter: {exc}") from exc
    if not isinstance(document, Mapping):
        raise SkillValidationError(f"{path}: front matter must be a YAML mapping")
    document = dict(document)
    payload = document.get("instructions_payload")
    if not payload:
        rendered_body = _normalize_authoring_text(body).strip()
        if not rendered_body:
            raise SkillValidationError(
                f"{path}: front matter has no 'instructions_payload' and the body is empty"
            )
        document["instructions_payload"] = rendered_body
    return parse_skill_document(document, path)


def _read_skill_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SkillValidationError(f"{path}: cannot read skill file: {exc}") from exc


def _parse_skill_text(text: str, path: Path) -> Skill:
    """Parses already-read skill text; the suffix of ``path`` picks the format."""
    if path.suffix.lower() in MARKDOWN_SKILL_SUFFIXES:
        return _load_markdown_skill(text, path)
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"{path}: invalid YAML: {exc}") from exc
    return parse_skill_document(document, path)


def load_skill_file(path: Path) -> Skill:
    """Reads, parses, and validates a single YAML or Markdown skill file."""
    raw_text = _read_skill_text(path)
    if not raw_text.strip():
        raise SkillValidationError(f"{path}: skill file is empty")
    return _parse_skill_text(raw_text, path)


class SkillLoader:
    """Discovers, validates and resolves skills from a registry directory."""

    def __init__(self, skill_dir: Path | str | None = None) -> None:
        self.skill_dir = Path(skill_dir) if skill_dir is not None else DEFAULT_SKILL_DIR
        self._skills: list[Skill] | None = None
        self._warnings: list[str] = []

    @property
    def skills(self) -> list[Skill]:
        """Every valid skill in the registry; raises ``SkillError`` on a broken file."""
        if self._skills is None:
            self._skills = self._discover()
        return list(self._skills)

    @property
    def warnings(self) -> list[str]:
        """Non-fatal registry diagnostics (e.g. placeholder files skipped)."""
        _ = self.skills
        return list(self._warnings)

    def _discover(self) -> list[Skill]:
        if not self.skill_dir.is_dir():
            raise SkillNotFoundError(
                f"Skill registry directory not found: {self.skill_dir}"
            )
        self._warnings = []
        skill_files = sorted(
            path
            for path in self.skill_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SKILL_SUFFIXES
        )
        skills: list[Skill] = []
        seen: dict[str, Path] = {}
        for path in skill_files:
            raw_text = _read_skill_text(path)
            # Empty placeholders are skipped here, while load_skill_file treats
            # them as errors: a registry may hold not-yet-authored stubs, but a
            # file requested explicitly must actually define a skill.
            if not raw_text.strip():
                self._warnings.append(
                    f"Skipped empty skill file (not authored yet): {path.name}"
                )
                continue
            skill = _parse_skill_text(raw_text, path)
            for identifier in (skill.name, *skill.aliases):
                prior = seen.get(identifier.lower())
                if prior is not None:
                    raise SkillValidationError(
                        f"Duplicate skill name or alias {identifier!r} in {path} "
                        f"(already defined in {prior})"
                    )
                seen[identifier.lower()] = path
            skills.append(skill)
        return skills

    def available(self) -> list[str]:
        """Names of every registered skill, in filename order."""
        return [skill.name for skill in self.skills]

    def load(self, identifier: str) -> Skill:
        """Resolves one skill by name, alias or filename stem (case-insensitive)."""
        key = str(identifier).strip()
        if not key:
            raise SkillNotFoundError("Skill identifier must not be empty")
        lowered = key.lower()
        for skill in self.skills:
            if skill.name.lower() == lowered or skill.stem.lower() == lowered:
                return skill
            if any(alias.lower() == lowered for alias in skill.aliases):
                return skill
        available = ", ".join(self.available()) or "(none)"
        raise SkillNotFoundError(
            f"Skill {key!r} not found in {self.skill_dir}. Available: {available}"
        )

    def select(self, selector: str | Sequence[str] | None) -> list[Skill]:
        """Resolves a selector (``all``, a comma list, or a sequence) into skills.

        ``all`` selects every non-template skill sorted by filename. Explicit
        identifiers resolve by skill name, alias or filename stem and are de-duplicated
        while preserving request order. Templates are never auto-selected.
        """
        entries = self._normalize_selector(selector)
        if not entries:
            return []
        if any(entry.lower() == ALL_SELECTOR for entry in entries):
            return [skill for skill in self.skills if not skill.is_template]
        selected: list[Skill] = []
        seen: set[str] = set()
        for entry in entries:
            skill = self.load(entry)
            if skill.name not in seen:
                seen.add(skill.name)
                selected.append(skill)
        return selected

    @staticmethod
    def _normalize_selector(selector: str | Sequence[str] | None) -> list[str]:
        if selector is None:
            return []
        if isinstance(selector, str):
            return [part.strip() for part in selector.split(",") if part.strip()]
        return [str(part).strip() for part in selector if str(part).strip()]


def _check_role(role: str) -> str:
    if role not in _SKILL_ROLES:
        raise ValueError(f"Skill role must be one of {', '.join(_SKILL_ROLES)}; got {role!r}")
    return role


def render_skill_block(skill: Skill, role: str) -> str:
    """Inline active-skill block: heading, binding pointer to the full file, brief.

    The brief must let the reviewer work well on its own; the pointer makes
    the full procedure binding without spending command-line budget on it.
    """
    _check_role(role)
    return (
        f"### {skill.name} (v{skill.version}) \u2014 {role}\n"
        f"Full procedure: {skill.absolute_path} \u2014 read it in full before starting; "
        "it is binding.\n\n"
        f"{skill.effective_brief}"
    ).rstrip()


def render_skill_pointer(skill: Skill, role: str) -> str:
    """Path-only line for an active skill whose brief did not fit the inline budget."""
    _check_role(role)
    return (
        f"- **{skill.name}** (v{skill.version}) \u2014 {role} \u00b7 read this file in full "
        f"before starting; it is binding: {skill.absolute_path}"
    )


def render_registry_index(skills: Sequence[Skill]) -> str:
    """Compact registry index: one line per skill with its purpose and absolute path."""
    return "\n".join(
        f"- **{skill.name}** \u2014 {skill.purpose} \u00b7 {skill.absolute_path}"
        for skill in skills
    )


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skill_loader",
        description="Validate and inspect the Antigravity adversarial skill registry.",
    )
    parser.add_argument(
        "--skill-dir",
        type=Path,
        default=DEFAULT_SKILL_DIR,
        help="Registry directory (default: Skills).",
    )
    parser.add_argument("--list", action="store_true", help="List discovered skills.")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate every skill definition in the registry.",
    )
    args = parser.parse_args(argv)

    loader = SkillLoader(args.skill_dir)
    try:
        skills = loader.skills
    except SkillError as exc:
        print(f"error: skill registry: {exc}", file=sys.stderr)
        return 2

    for warning in loader.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.validate:
        print(f"OK: {len(skills)} skill definition(s) validated in {args.skill_dir}")
    if args.list or not args.validate:
        for skill in skills:
            print(f"{skill.name} (v{skill.version}) [{skill.kind}] -> {skill.source_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
