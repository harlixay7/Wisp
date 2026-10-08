"""Machine-path hygiene patterns shared by the registry and documentation checks."""

from __future__ import annotations

import os
from pathlib import Path

# Structural user-profile path patterns: the actual leak vector, and
# machine-independent by construction (a dynamic "local username must not
# appear" check is NOT machine-independent: on GitHub runners
# Path.home().name is literally "runner", which false-positives on ordinary
# words like "workflow runner").
USER_PROFILE_PATH_PATTERNS = (
    r"(?i)\b[C-Z]:\\+Users\\[A-Za-z0-9._~-]+",
    r"(?i)/(?:home|Users)/[A-Za-z0-9._~-]{2,}",
)


def local_username_leaks(text: str) -> bool:
    """True when the LOCAL username appears in ``text``.

    Runs only outside CI: personal usernames exist on developer machines, not
    on CI runners (where ``Path.home().name`` is a generic account such as
    "runner"; treating that as a leak would false-positive on ordinary
    English like "workflow runner"). Short usernames are skipped as too
    generic to match reliably.
    """
    if os.environ.get("CI"):
        return False
    username = Path.home().name
    if len(username) < 5:
        return False
    return username.lower() in text.lower()
