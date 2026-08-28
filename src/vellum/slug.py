"""Slugs used to anchor scenarios.

The anchor scheme (``<feature-slug>/<scenario-slug>``) is a documented default,
not a settled decision: see the open question filed against the intent repo,
"Question: scenario identity and anchor scheme", and memory/waves/spec-v1.md.
"""

import re

_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, collapse every run of non-alphanumerics to a single hyphen."""
    return _NON_WORD.sub("-", text.strip().lower()).strip("-")


def heading_anchor(text: str) -> str:
    """GitHub's heading-anchor slug: drop punctuation, spaces become hyphens."""
    kept = "".join(c for c in text.strip().lower() if c.isalnum() or c in " -_")
    return kept.replace(" ", "-")
