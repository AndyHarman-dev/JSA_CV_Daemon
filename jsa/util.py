"""Shared utility functions used across multiple JSA modules."""

from __future__ import annotations

import re


def slugify(s: str) -> str:
    """Convert a string to a filesystem-safe slug (lowercase, hyphens/spaces → underscores)."""
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
