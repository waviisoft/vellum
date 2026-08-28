"""Cross-reference checking for a spec tree.

The spec tree references sibling documents two ways: as markdown links, and as
bare paths in prose and index tables (``features/spec-pipeline.md``). Both are
cross-references and both are checked, under one resolution rule — try the
referring file's own directory, then the spec root, then the spec root's parent
(the intent-repo root, where ``docs/`` lives).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from vellum.slug import heading_anchor
from vellum.specfile import SpecFile

_MD_LINK_RE = re.compile(r"\[[^\]\n]*\]\(\s*([^)\s]+?)\s*(?:\"[^\"]*\")?\s*\)")
_BARE_PATH_RE = re.compile(r"(?:[\w.\-]+/)*[\w.\-]+\.md(?:#[\w\-./]+)?")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*$")
_EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", re.IGNORECASE)


@dataclass
class Reference:
    target: str
    fragment: str
    #: 1-based line within the referring spec file.
    line: int


def _masked_lines(sf: SpecFile) -> list[str]:
    """File lines with fenced blocks and inline code blanked out.

    Fenced blocks hold Gherkin prose that names paths which need not exist
    (``features/auth.md`` in ``features/orchestration.md`` is an illustration,
    not a cross-reference), and inline code quotes globs like ``spec/**``.
    """
    lines = list(sf.lines)
    for fence in sf.fences:
        for i in range(fence.start_line - 1, min(fence.end_line, len(lines))):
            lines[i] = ""
    for i, line in enumerate(lines):
        if line:
            lines[i] = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line)
    return lines


def find_references(sf: SpecFile) -> list[Reference]:
    """Every markdown link and bare ``.md`` path outside code, in file order."""
    refs: list[Reference] = []
    for idx, line in enumerate(_masked_lines(sf), start=1):
        spans: list[tuple[int, int]] = []
        for m in _MD_LINK_RE.finditer(line):
            spans.append(m.span())
            raw = m.group(1)
            if _EXTERNAL_RE.match(raw):
                continue
            target, _, fragment = raw.partition("#")
            refs.append(Reference(target, fragment, idx))
        for m in _BARE_PATH_RE.finditer(line):
            if any(s <= m.start() and m.end() <= e for s, e in spans):
                continue  # already captured as a markdown link
            target, _, fragment = m.group(0).partition("#")
            # A bare path ends where the sentence does: "...see auth.md#acceptance."
            refs.append(Reference(target, fragment.rstrip(".,;:!?"), idx))
    return refs


def resolve(ref: Reference, sf: SpecFile, root: Path) -> Path | None:
    """First existing file among: referrer's dir, spec root, spec root's parent."""
    if not ref.target:
        return sf.path if sf.path.is_file() else None
    for base in (sf.path.parent, root, root.parent):
        candidate = (base / ref.target).resolve()
        if candidate.is_file():
            return candidate
    return None


def heading_anchors(path: Path) -> set[str]:
    """GitHub-style anchors for every heading, with ``-2`` suffixes on repeats."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return anchors
    for line in text.split("\n"):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        base = heading_anchor(m.group(1))
        counts[base] = counts.get(base, 0) + 1
        anchors.add(base if counts[base] == 1 else f"{base}-{counts[base] - 1}")
    return anchors
