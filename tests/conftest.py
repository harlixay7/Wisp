"""Shared pytest fixtures: live-registry isolation and a small skill registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.bridge import make_skill_yaml


@pytest.fixture(autouse=True)
def _isolated_live_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps every test (and any child process it spawns) off the real registry."""
    monkeypatch.setenv("ANTIGRAVITY_LIVE_REGISTRY", str(tmp_path / "live-registry.json"))


@pytest.fixture()
def registry(tmp_path: Path) -> Path:
    """Two loadable skills plus a template that ``--skills all`` must skip."""
    skill_dir = tmp_path / ".skills" / "antigravity"
    skill_dir.mkdir(parents=True)
    (skill_dir / "01_crash_ops.yaml").write_text(
        make_skill_yaml("crash_ops", payload="CRASH_OPS_INSTRUCTIONS"), encoding="utf-8"
    )
    (skill_dir / "02_ast_audit.yaml").write_text(
        make_skill_yaml("ast_audit", payload="AST_AUDIT_INSTRUCTIONS"), encoding="utf-8"
    )
    (skill_dir / "template_custom_skill.yaml").write_text(
        make_skill_yaml(
            "template_custom_skill", kind="template", payload="TEMPLATE_ONLY_INSTRUCTIONS"
        ),
        encoding="utf-8",
    )
    return skill_dir
