# Skill Family Repair Report

The canonical repair and incident report lives at [`docs/skills-repair-report.md`](docs/skills-repair-report.md).

Context: the 12-skill registry was repaired and hardened to v3.0.0 (2026-10-07). One operational rule came out of it and is now enforced in code and tests: **never place non-skill files in `Skills/`** — every `.md` there must carry YAML frontmatter, and one malformed file empties the entire skill registry for every delegation. `tests/test_skill_registry_governance.py` keeps those properties true.
