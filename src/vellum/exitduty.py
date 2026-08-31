"""``vellum verify exit-duty`` — the memory-update guard.

``spec/features/memory-and-briefings.md``: "Exit duty: a work item is done only
when the implementer updated the area notes it touched; the memory diff rides in
the PR and is verifier-reviewed." ``@id:exit-duty-required`` states the failing
shape: a PR that touched ``src/billing/`` with no diff under
``.vellum/memory/areas/`` is incomplete.

What this can and cannot check
------------------------------
The scenario's second Given is "**no** diff under ``.vellum/memory/areas/`` for
the affected area", and the guard implemented here answers the first half of
that sentence: source changed, and nothing under ``.vellum/memory/areas/``
changed with it.

It deliberately does not try to answer "for the affected area", because the
correspondence between a source path and an area note is not mechanical and this
repository is its own counter-example: ``src/vellum/`` is documented by
``.vellum/memory/areas/cli.md``. There is no derivation from ``src/vellum`` to
``cli`` — areas are editorial groupings, named by the librarian, and a guard
that guessed at the mapping would fault correct PRs (the note it wanted exists
under another name) and pass incorrect ones (a one-word touch to an unrelated
note). Deciding *which* note a change belongs in is exactly the judgment the
spec assigns to the verifier reviewing the memory diff.

So the mechanical half is enforced mechanically and the editorial half is left
where the spec puts it. The report says which of the two it checked, so nobody
reads a green run as "the right note was updated".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from vellum.gitver import GitUnavailable, changed_paths, resolve
from vellum.product import ProductFileError, normalise_tree, under

#: Where a product repo's area notes live (``spec/features/memory-and-briefings.md``).
AREAS_TREE = ".vellum/memory/areas"

#: The tree exit duty is owed for, when the caller names none. ``src`` is what
#: the scenario names and what ``product.trees`` lists in every product repo the
#: installation has; ``--src`` exists for a repo laid out differently. Entries
#: are normalised by ``product.normalise_tree`` before anything is compared
#: against them, so a non-default layout cannot turn the guard off by naming a
#: tree that matches nothing.
DEFAULT_SOURCE_TREES = ("src",)


class ExitDutyError(Exception):
    """Exit duty could not be checked."""


@dataclass
class ExitDuty:
    """One exit-duty check."""

    checkout: Path
    base: str
    head: str
    basis: str
    source_trees: list[str]
    areas_tree: str
    changed: list[str]
    source_changed: list[str]
    memory_changed: list[str]

    @property
    def owed(self) -> bool:
        """True when source moved and no area note moved with it."""
        return bool(self.source_changed) and not self.memory_changed

    def report(self) -> str:
        lines = [
            f"Exit duty in {self.checkout}",
            f"  base {self.base[:12]} .. head {self.head[:12]}  ({self.basis})",
            f"  source: {', '.join(self.source_trees)}   notes: {self.areas_tree}",
            "",
            f"{len(self.source_changed)} source path(s) changed:",
        ]
        lines += [f"    {p}" for p in self.source_changed] or ["    (none)"]
        lines.append(f"{len(self.memory_changed)} area note(s) changed:")
        lines += [f"    {p}" for p in self.memory_changed] or ["    (none)"]
        lines.append("")
        if self.owed:
            lines.append(
                f"BLOCKED: this diff changes source and nothing under "
                f"{self.areas_tree}. A work item is done only when the implementer "
                f"updated the area notes it touched, and the memory diff rides in "
                f"the PR (spec/features/memory-and-briefings.md)."
            )
        elif not self.source_changed:
            lines.append(
                "OK: no source changed, so no exit duty is owed for this diff."
            )
        else:
            lines.append(
                f"OK: source changed and {len(self.memory_changed)} area note(s) "
                f"changed with it."
            )
        lines.append(
            f"Checked that *some* note under {self.areas_tree} changed, not that it "
            f"is the right one: an area is an editorial grouping and its name is not "
            f"derivable from a source path (this product's own src/vellum/ is "
            f"documented by areas/cli.md). Whether the note matches the change is the "
            f"verifier's reading of the memory diff."
        )
        if self.basis == "two-dot":
            lines.append(
                "Compared base to head directly: no merge base was available. That "
                "reads a superset of the branch's changes, so exit duty may be "
                "reported satisfied by a note somebody else changed on the base."
            )
        return "\n".join(lines)


def check(
    checkout: str | Path,
    base: str,
    head: str,
    source_trees: tuple[str, ...] | list[str] | None = None,
) -> ExitDuty:
    """Whether a branch that changed source also changed an area note."""
    path = Path(checkout)
    # Normalised the way a write boundary is, and by the same function. These
    # are an allowlist read the other way round — a path is source when it lies
    # under one of them — so a malformed entry turns the guard *off* instead of
    # widening it, which is quieter and therefore worse: `--src .` and
    # `--src src/` both leave `under(p, tree)` false for every path in the diff,
    # so exit duty is never owed and the run exits 0 looking configured.
    # `normalise_tree` refuses the ones that name no tree and trims the rest.
    try:
        trees = [
            normalise_tree(entry, path=path, where="--src")
            for entry in (source_trees or DEFAULT_SOURCE_TREES)
        ]
    except ProductFileError as exc:
        raise ExitDutyError(str(exc)) from exc
    if not path.is_dir():
        raise ExitDutyError(f"{path}: not a directory; is this a product checkout?")
    try:
        base_sha, head_sha = resolve(path, base), resolve(path, head)
        changed, basis = changed_paths(path, base_sha, head_sha)
    except GitUnavailable as exc:
        raise ExitDutyError(
            f"{path}: cannot read the diff between {base!r} and {head!r}: {exc}"
        ) from exc
    # A note is memory first: a path inside the areas tree is never also counted
    # as source, so an installation whose source tree is `.` (or that lists
    # `.vellum/memory` in product.trees, as this repo does) cannot make the
    # memory diff satisfy itself.
    memory_changed = [p for p in changed if under(p, AREAS_TREE)]
    source_changed = [
        p
        for p in changed
        if p not in memory_changed and any(under(p, tree) for tree in trees)
    ]
    return ExitDuty(
        checkout=path,
        base=base_sha,
        head=head_sha,
        basis=basis,
        source_trees=trees,
        areas_tree=AREAS_TREE,
        changed=changed,
        source_changed=source_changed,
        memory_changed=memory_changed,
    )


def run(
    checkout: str,
    base: str,
    head: str,
    source_trees: tuple[str, ...] | list[str] | None = None,
    out=None,
) -> int:
    """Report the exit-duty check and exit 1 when the duty is unmet."""
    stream = out if out is not None else sys.stdout
    result = check(checkout, base, head, source_trees)
    print(result.report(), file=stream)
    if result.owed:
        print(
            f"vellum: exit duty — {len(result.source_changed)} source path(s) "
            f"changed and nothing under {result.areas_tree} did; the memory diff "
            f"rides in the PR (spec/features/memory-and-briefings.md)",
            file=sys.stderr,
        )
        return 1
    return 0
