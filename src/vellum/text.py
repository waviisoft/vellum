"""Narrowing outside strings before they reach a report.

Every command whose report a caller may pipe into a forge step summary has the
same problem with the same shape: a value carrying a newline starts a line of
its own, and a line of its own is all ``::error``, ``::notice`` or
``::add-mask`` needs. The values are executor names, question text, briefings,
ledger filenames, scenario ids, spec-tree paths — all of them written by
whoever can land a merge in the intent repo.

``reconcile.py`` grew this first and ``release.py`` needs the identical rule, so
it is stated once here rather than twice: two spellings of "how this project
flattens an untrusted string" is how the two come to disagree, and the one that
drifts is the one nobody is looking at.
"""

from __future__ import annotations

import re

_WHITESPACE_RUN = re.compile(r"\s+")


def one_line(value, limit: int = 120) -> str:
    """*value* as one short line: whitespace collapsed, then truncated."""
    text = _WHITESPACE_RUN.sub(" ", str(value if value is not None else "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
