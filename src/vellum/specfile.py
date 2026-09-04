"""Locating, reading and structurally parsing files in a spec tree.

A *spec tree* is the directory holding ``index.md``, ``product.md``,
``features/``, ``behaviors/`` and ``decisions/``.  ``resolve_spec_root``
accepts either that directory or the intent-repo root that contains it, so
``vellum lint <path>`` works both inside the intent repo (where ``spec/`` is
the tree) and against a checkout of the whole intent repo (where the tree is
one level down). Callers hand it whichever they have — CI points it at the
checkout it fetched at the pin, a developer at either — and nothing else in
the codebase may guess which of the two a path is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: Frontmatter keys, by spec-file kind.  ``decisions/`` files are dated; every
#: other spec file carries the version it first appeared in — as a decorative
#: ``spec-v<N>`` name, which is what the whole tree writes and what
#: ``spec/index.md`` states. Versions are commits
#: (spec/decisions/2026-08-28-versions-are-commits.md), but nothing resolves
#: this field, so a name is the right thing to find here.
DECISION_KEYS = {"required": ("id", "title", "date"), "optional": ()}
AREA_KEYS = {"required": ("id", "title", "since"), "optional": ("status",)}

SINCE_RE = re.compile(r"^spec-v(\d+)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: A fence line: indent, marker, then the info string as free text.
#: CommonMark (§4.5) puts no constraint on what an info string may contain
#: beyond "no backtick in a backtick fence's", and matching only a single bare
#: word here is what desynced opening from closing (waviisoft/vellum#10): a
#: two-word info string made the opening line invisible, so its *closing* line
#: opened a phantom fence that swallowed the next block. Meaning is read from
#: the first word alone -- see ``Fence.language``.
# The trailing `\s*$` (not `[ \t]*$`) matters: a CRLF line ends in `\r`, and a
# closer whose captured info string is "\r" is not a closer, so a CRLF file
# would collapse into one fence — the phantom-fence class this rule exists to end.
_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})[ \t]*(.*?)\s*$")


class SpecTreeError(Exception):
    """The path given on the command line is not a spec tree."""


def resolve_spec_root(path: str | Path) -> Path:
    """Return the spec tree for *path*, accepting the intent-repo root too."""
    p = Path(path).resolve()
    if not p.is_dir():
        raise SpecTreeError(f"{path}: not a directory")
    if (p / "spec" / "index.md").is_file():
        return p / "spec"
    if (p / "index.md").is_file():
        return p
    raise SpecTreeError(
        f"{path}: no spec tree here (expected index.md, or spec/index.md)"
    )


def spec_kind(relpath: str) -> str:
    """``decision`` for dated decision records, ``area`` for everything else."""
    return "decision" if relpath.startswith("decisions/") else "area"


def schema_for(relpath: str) -> dict:
    return DECISION_KEYS if spec_kind(relpath) == "decision" else AREA_KEYS


@dataclass
class Fence:
    """A fenced code block. ``start_line``/``end_line`` are 1-based, inclusive."""

    #: The info string exactly as written, stripped of surrounding whitespace.
    #: Free text (CommonMark §4.5); ``language`` is the part with meaning.
    info: str
    start_line: int
    end_line: int
    body: str
    #: 1-based line of the first line of ``body``.
    body_line: int

    @property
    def language(self) -> str:
        """The info string's first word, lowercased — the block's language.

        Everything after that first word is an **attribute list, and this
        project ignores it entirely**: ```` ```gherkin title=demo ```` is a
        gherkin block, extracts like any other, and nothing anywhere reads
        ``title``. Attributes are carried on ``info`` for a reader, never
        interpreted — inventing a meaning for one here would make a spec tree
        depend on a markdown convention no renderer agrees on.

        Read the language through this property rather than off ``info``: the
        two were the same string only while the info string could not hold a
        second word, which is the condition waviisoft/vellum#10 removed.
        """
        words = self.info.split()
        return words[0].lower() if words else ""


@dataclass
class SpecFile:
    path: Path
    relpath: str
    text: str
    lines: list[str]
    frontmatter: dict | None = None
    #: Set when the frontmatter is absent or unreadable; lint reports it.
    frontmatter_error: str | None = None
    #: 1-based line where the body (after the closing ``---``) starts.
    body_line: int = 1
    fences: list[Fence] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return spec_kind(self.relpath)


def _opens(m: re.Match) -> bool:
    """Whether a fence line can *open* a block.

    Only one thing disqualifies one: a backtick fence's info string may not
    contain a backtick (CommonMark §4.5), or an inline code span in a
    paragraph would open a block. A tilde fence's info string may hold
    anything at all.
    """
    return not (m.group(2)[0] == "`" and "`" in m.group(3))


def find_fences(lines: list[str]) -> list[Fence]:
    """Fenced code blocks, matched on fence length and character like CommonMark.

    Opening and closing are asymmetric, and that asymmetry is what keeps the
    two in step: an opening fence may carry an info string, a closing fence
    carries nothing (CommonMark §4.5). So a line that is *not* recognised as
    an opening must not be recognised as a closing either — the bug this
    replaced (waviisoft/vellum#10) came from a rule that rejected
    ``gherkin title=demo`` as an opening while still accepting the block's own
    bare closing line, which then opened a phantom fence over everything up to
    the next block's opener and hid both blocks from lint and extraction at
    once.
    """
    fences: list[Fence] = []
    i = 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i])
        if not m or not _opens(m):
            i += 1
            continue
        indent, marker, info = m.group(1), m.group(2), m.group(3)
        start = i
        i += 1
        while i < len(lines):
            close = _FENCE_RE.match(lines[i])
            if (
                close
                and not close.group(3)
                and close.group(2)[0] == marker[0]
                and len(close.group(2)) >= len(marker)
            ):
                break
            i += 1
        end = min(i, len(lines) - 1)
        body_lines = lines[start + 1 : i]
        if indent:
            body_lines = [
                ln[len(indent) :] if ln.startswith(indent) else ln for ln in body_lines
            ]
        fences.append(
            Fence(
                info=info,
                start_line=start + 1,
                end_line=end + 1,
                body="\n".join(body_lines),
                body_line=start + 2,
            )
        )
        i += 1
    return fences


def parse_spec_text(relpath: str, text: str, path: Path | None = None) -> SpecFile:
    lines = text.split("\n")
    sf = SpecFile(path=path or Path(relpath), relpath=relpath, text=text, lines=lines)
    sf.fences = find_fences(lines)

    if not lines or lines[0].strip() != "---":
        sf.frontmatter_error = "file does not open with a '---' frontmatter block"
        return sf
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        sf.frontmatter_error = "frontmatter block is never closed with '---'"
        return sf
    sf.body_line = close + 2
    try:
        loaded = yaml.safe_load("\n".join(lines[1:close]))
    except yaml.YAMLError as exc:
        sf.frontmatter_error = f"frontmatter is not valid YAML: {_one_line(exc)}"
        return sf
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        sf.frontmatter_error = "frontmatter is not a YAML mapping"
        return sf
    sf.frontmatter = loaded
    return sf


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())


def read_spec_file(path: Path, root: Path) -> SpecFile:
    relpath = path.relative_to(root).as_posix()
    return parse_spec_text(relpath, path.read_text(encoding="utf-8"), path)


def iter_spec_files(root: Path) -> list[SpecFile]:
    """Every ``.md`` file in the tree, ordered by path for stable output."""
    return [read_spec_file(p, root) for p in sorted(root.rglob("*.md"))]
