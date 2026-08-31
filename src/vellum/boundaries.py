"""``vellum verify boundaries`` — the write-boundary guard.

``spec/behaviors/write-boundaries.md``: "Implementation PRs may not touch
``harness/``, scenario blocks in ``spec/``, or the ledger; ``.vellum/memory/`` is
the sole Vellum tree implementers write, in their own product repo." The same
behavior adds why a *guard* exists at all when the boundaries are meant to be
credential facts: "CI enforces the same boundaries as a backstop in colocated
development contexts."

This is that backstop, and its scope is exactly one repository: a checkout, the
file in it that declares boundaries, and a diff inside it. The boundaries it
reads are the ``write_boundaries.<role>`` *data* in that file. The prose in
``spec/behaviors/write-boundaries.md`` is the intent-side statement of the same
rule and is not machine-readable; nothing here tries to derive one from the
other, because a guard that inferred its allowlist from prose would be enforcing
its own reading of the spec rather than the installation's declared policy.

Two kinds of repository declare boundaries and they keep them in different
files. A product repo has ``.vellum/product.yaml``; the *intent* repo has no
product file, and its own roles — the harness engineer writing ``harness/``, the
librarian writing the memory, the orchestrator writing the ledger — are governed
by a ``write_boundaries`` block in ``.vellum/config.yaml`` beside the
installation's other policy. The block has one shape and one reader
(``product.role_trees``) either way; only the file it is read out of differs.

**The source is chosen by which file exists, and there is no cascade.** With
``--boundaries-from auto`` a product file wins if there is one, the config is
read if there is not, and a checkout with neither is refused. What deliberately
does *not* happen is falling through from one file to the other when the first
one has no ``write_boundaries`` key: a product repo whose boundaries were
deleted would then quietly start being judged against the installation's
policy — a different repo's allowlist, silently applied to this one's diff —
which is the failure this whole module is shaped to avoid. A caller that wants
no ambiguity at all names the source (``--boundaries-from product|config``), and
then a missing file is an error rather than the other file.

Direction of failure
--------------------
An allowlist guard has one dangerous direction — passing a diff it should have
faulted — and every judgment call here leans the other way:

* a role the declaring file does not name is refused (exit 2), not defaulted;
* a checkout declaring no boundaries at all is refused, not treated as
  unrestricted, and a named ``--boundaries-from`` never silently falls back
  to the other file;
* a boundary entry that would admit everything is refused, not normalised away;
* with no merge base to compare against, the *wider* diff is read (see
  ``gitver.changed_paths``);
* renames are not detected, so a file moved out of a protected tree still shows
  as a write to it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from vellum.config import ConfigError, config_path
from vellum.config import write_boundaries as config_boundaries
from vellum.gitver import GitUnavailable, changed_paths, resolve
from vellum.product import ProductFileError, product_path, under, write_boundaries


#: Where boundaries may be declared, and the order ``auto`` looks in. ``product``
#: is first because a repo carrying a product file is a product repo, whatever
#: else it also carries.
SOURCES = ("product", "config")

#: ``--boundaries-from`` accepts these; ``auto`` is the default.
SOURCE_CHOICES = ("auto", *SOURCES)

_READERS = {"product": (product_path, write_boundaries),
            "config": (config_path, config_boundaries)}


class BoundaryError(Exception):
    """The boundaries could not be checked."""


def resolve_source(checkout: Path, requested: str = "auto") -> tuple[str, Path]:
    """Which file declares this checkout's boundaries: ``("product"|"config", path)``.

    A named source must be present — a caller that said ``config`` and got the
    product file would be told its diff was judged against boundaries it did not
    name. ``auto`` takes the first that exists and refuses a checkout with
    neither, naming both, because "no boundaries here" is not a boundary check
    that passed.
    """
    if requested not in SOURCE_CHOICES:
        raise BoundaryError(
            f"unknown boundary source {requested!r}; expected one of "
            f"{', '.join(SOURCE_CHOICES)}"
        )
    for kind in SOURCES if requested == "auto" else (requested,):
        path = _READERS[kind][0](checkout)
        if path.is_file():
            return kind, path
        if requested != "auto":
            raise BoundaryError(
                f"{path}: no such file, and --boundaries-from {kind} named it. "
                f"A boundary check with nothing to check against is not a pass."
            )
    raise BoundaryError(
        f"{checkout}: declares no write boundaries — neither "
        f"{product_path(checkout)} nor {config_path(checkout)} exists. A product "
        f"repo declares them in its product file and the intent repo in its "
        f"installation config (spec/behaviors/write-boundaries.md)."
    )


@dataclass
class Boundaries:
    """One boundary check: what the branch changed, and what it may change."""

    checkout: Path
    #: ``product`` or ``config`` — which file ``source`` is.
    source_kind: str
    #: The file the boundaries were read out of.
    source: Path
    role: str
    base: str
    head: str
    #: ``merge-base`` or ``two-dot`` — see ``gitver.changed_paths``.
    basis: str
    allowed: list[str]
    changed: list[str]
    offending: list[str]

    @property
    def crossed(self) -> bool:
        return bool(self.offending)

    def report(self) -> str:
        lines = [
            f"Write boundaries for role {self.role!r} in {self.checkout}",
            f"  declared in: {self.source} ({self.source_kind})",
            f"  base {self.base[:12]} .. head {self.head[:12]}  ({self.basis})",
            f"  may write: {', '.join(self.allowed)}",
            "",
            f"{len(self.changed)} path(s) changed:",
        ]
        for path in self.changed:
            lines.append(f"  {'CROSSES' if path in self.offending else '  ok   '}  {path}")
        if not self.changed:
            lines.append("  (nothing changed)")
        lines.append("")
        if self.crossed:
            lines.append(
                f"BLOCKED: {len(self.offending)} path(s) lie outside every tree "
                f"{self.role!r} may write — {', '.join(self.offending)}. A PR "
                f"does not reach outside the trees its role owns "
                f"(spec/behaviors/write-boundaries.md)."
            )
        else:
            lines.append(
                f"OK: every changed path lies inside a tree {self.role!r} may write."
            )
        if self.basis == "two-dot":
            lines.append(
                "Compared base to head directly: the two commits share no merge "
                "base in this checkout (a shallow clone?). That reads a superset "
                "of what the branch changed, so a path may be reported that the "
                "branch did not touch — never the other way round."
            )
        return "\n".join(lines)


def check(
    checkout: str | Path,
    base: str,
    head: str,
    role: str,
    source: str = "auto",
) -> Boundaries:
    """Which of the paths a branch changed lie outside *role*'s declared trees."""
    path = Path(checkout)
    kind, declared_in = resolve_source(path, source)
    try:
        allowed = _READERS[kind][1](path, role)
    except (ProductFileError, ConfigError) as exc:
        raise BoundaryError(str(exc)) from exc
    if not path.is_dir():
        raise BoundaryError(f"{path}: not a directory; is this a product checkout?")
    try:
        base_sha, head_sha = resolve(path, base), resolve(path, head)
        changed, basis = changed_paths(path, base_sha, head_sha)
    except GitUnavailable as exc:
        raise BoundaryError(
            f"{path}: cannot read the diff between {base!r} and {head!r}: {exc}"
        ) from exc
    offending = [p for p in changed if not any(under(p, tree) for tree in allowed)]
    return Boundaries(
        checkout=path,
        source_kind=kind,
        source=declared_in,
        role=role,
        base=base_sha,
        head=head_sha,
        basis=basis,
        allowed=allowed,
        changed=changed,
        offending=offending,
    )


def run(checkout: str, base: str, head: str, role: str, source: str = "auto",
        out=None) -> int:
    """Report the boundary check and exit 1 when the diff crosses one.

    The report is printed either way, for the reason ``backpressure`` prints
    its window on a green run: a check that speaks only when it fails leaves a
    reviewer unable to see *which* trees were considered in bounds, which is the
    half of the answer that catches a mis-declared boundary.
    """
    stream = out if out is not None else sys.stdout
    result = check(checkout, base, head, role, source)
    print(result.report(), file=stream)
    if result.crossed:
        print(
            f"vellum: write boundary — {result.role} may not write "
            f"{', '.join(result.offending)}; see {result.source}",
            file=sys.stderr,
        )
        return 1
    return 0
