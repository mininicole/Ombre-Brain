"""Process-wide runtime safety modes for Ombre services."""

from __future__ import annotations

import os


def memory_read_only() -> bool:
    """Return whether persistent Gale Memory mutations must be rejected."""

    return os.environ.get("OMBRE_READ_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

