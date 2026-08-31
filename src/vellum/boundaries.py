"""``vellum verify boundaries`` — the write-boundary guard.

``spec/behaviors/write-boundaries.md``: "Implementation PRs may not touch
``harness/``, scenario blocks in ``spec/``, or the ledger; ``.vellum/memory/`` is
the sole Vellum tree implementers write, in their own product repo." The same
behavior adds why a *guard* exists at all when the boundaries are meant to be
credential facts: "CI enforces the same boundaries as a backstop in colocated
development contexts."

This is that backstop, and its scope is exactly one repository: a product
checkout, its own ``.vellum/product.yaml``, and a diff inside it. The boundaries
it reads are the ``write_boundaries.<role>`` *data* in that file. The prose in
``spec/behaviors/write-boundaries.md`` is the intent-side statement of the same
rule and is not machine-readable; nothing here tries to derive one from the
other, because a guard that inferred its allowlist from prose would be enforcing
its own reading of the spec rather than the installation's declared policy.

Direction of failure
--------------------
An allowlist guard has one dangerous direction — passing a diff it should have
faulted — and every judgment call here leans the other way:

* a role the product file does not declare is refused (exit 2), not defaulted;
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

from vellum.gitver import GitUnavailable, changed_paths, resolve
from vellum.product import ProductFileError, product_path, under, write_boundaries


class BoundaryError(Exception):
    """The boundaries could not be checked."""


@dataclass
class Boundaries:
    """One boundary check: what the branch changed, and what it may change."""

    checkout: Path
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
                f"{self.role!r} may write — {', '.join(self.offending)}. An "
                f"implementation PR does not reach outside its own trees "
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
) -> Boundaries:
    """Which of the paths a branch changed lie outside *role*'s declared trees."""
    path = Path(checkout)
    try:
        allowed = write_boundaries(path, role)
    except ProductFileError as exc:
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
        role=role,
        base=base_sha,
        head=head_sha,
        basis=basis,
        allowed=allowed,
        changed=changed,
        offending=offending,
    )


def run(checkout: str, base: str, head: str, role: str, out=None) -> int:
    """Report the boundary check and exit 1 when the diff crosses one.

    The report is printed either way, for the reason ``backpressure`` prints
    its window on a green run: a check that speaks only when it fails leaves a
    reviewer unable to see *which* trees were considered in bounds, which is the
    half of the answer that catches a mis-declared boundary.
    """
    stream = out if out is not None else sys.stdout
    result = check(checkout, base, head, role)
    print(result.report(), file=stream)
    if result.crossed:
        print(
            f"vellum: write boundary — {result.role} may not write "
            f"{', '.join(result.offending)}; see {product_path(checkout)}",
            file=sys.stderr,
        )
        return 1
    return 0
