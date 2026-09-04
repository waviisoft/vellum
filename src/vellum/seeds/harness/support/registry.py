"""The step registry: which sentence in the spec runs which function.

A step definition is a function registered against a keyword type
(``Given``/``When``/``Then``) and a regular expression matched against the
step's text. Matching is exact — the pattern is anchored at both ends — and an
ambiguous match is a defect, not a coin toss: two definitions claiming one step
raise rather than silently picking the first.

``And``/``But``/``*`` carry the type of the step above them, as Gherkin
requires; `normalize_keywords` does that once when a scenario is loaded, so
step definitions never see a continuation keyword.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

GIVEN, WHEN, THEN = "Given", "When", "Then"
_CONTINUATIONS = {"And", "But", "*"}


@dataclass(frozen=True)
class Definition:
    keyword: str
    pattern: re.Pattern
    fn: Callable
    where: str


class AmbiguousStep(Exception):
    """More than one definition matches one step."""


_DEFINITIONS: list[Definition] = []


def _register(keyword: str, pattern: str):
    def decorate(fn):
        _DEFINITIONS.append(
            Definition(
                keyword=keyword,
                pattern=re.compile(f"^{pattern}$"),
                fn=fn,
                where=f"{fn.__module__}.{fn.__name__}",
            )
        )
        return fn

    return decorate


def given(pattern: str):
    return _register(GIVEN, pattern)


def when(pattern: str):
    return _register(WHEN, pattern)


def then(pattern: str):
    return _register(THEN, pattern)


def normalize_keywords(steps: list[dict]) -> list[tuple[str, str]]:
    """``[(type, text)]`` with continuations resolved to the step above them."""
    out: list[tuple[str, str]] = []
    current = GIVEN
    for step in steps:
        word = step["keyword"].strip()
        if word not in _CONTINUATIONS:
            current = word
        out.append((current, " ".join(step["text"].split())))
    return out


def find(keyword: str, text: str) -> tuple[Definition, re.Match] | None:
    """The one definition matching this step, or None when none does."""
    matches = [
        (d, m)
        for d in _DEFINITIONS
        if d.keyword == keyword and (m := d.pattern.match(text))
    ]
    if len(matches) > 1:
        where = ", ".join(d.where for d, _ in matches)
        raise AmbiguousStep(f"{keyword} {text!r} matches {len(matches)} definitions: {where}")
    return matches[0] if matches else None


def defined() -> list[Definition]:
    return list(_DEFINITIONS)
