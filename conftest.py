"""Pytest bootstrap: keeps the repository root importable as a package root."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_live_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keeps every test (and any child process it spawns) off the real registry."""
    monkeypatch.setenv(
        "ANTIGRAVITY_LIVE_REGISTRY", str(tmp_path / "live-registry.json")
    )
