"""Reading the spec repo's git history to date each scenario.

A spec version *is* a main commit whose diff touches the spec tree
(``spec/decisions/2026-08-28-versions-are-commits.md``): its identity is the
sha and its order is ancestry. ``suite.py`` walks those commits oldest-first
and compares scenario fingerprints between them, so a moved or reformatted
scenario keeps its version and a changed one advances.

``spec-v<N>`` tags survive as decoration. Nothing here reads them to decide
anything — ``names()`` exists only so a sha can be *reported* with a friendly
label — so a missing, late or wrong tag changes no version. That is the whole
point of the change: under tags, the dating data was a registry maintained
beside git and its absence was silent; under ancestry it is the history
itself, which a full clone cannot lack.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: Decorative version names. Read for display only; never for dating.
TAG_RE = re.compile(r"^spec-v(\d+)$")


class GitUnavailable(Exception):
    """The spec tree is not inside a readable git repository."""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitUnavailable(" ".join(proc.stderr.split()) or f"git {' '.join(args)} failed")
    return proc.stdout


def repo_root(path: Path) -> Path:
    """The work-tree root containing *path*."""
    return Path(_git(path, "rev-parse", "--show-toplevel").strip())


def prefix_of(root: Path, spec_root: Path) -> str:
    """The spec tree's path within its repo, e.g. ``spec``. Empty when it is the root."""
    rel = spec_root.resolve().relative_to(root.resolve()).as_posix()
    return "" if rel == "." else rel


def spec_commits(repo: Path, ref: str, prefix: str) -> list[str]:
    """Every spec version in *ref*'s ancestry, oldest first.

    A version is a commit whose diff against its first parent touches the spec
    tree, which is what ``--first-parent`` plus the pathspec asks git for. Main
    is squash-linear and never rewritten, so first-parent history is linear and
    this ordering is total — the property the spec relies on for "later means
    descendant".

    Walking *ref*'s ancestry rather than every ref in the repo is what makes
    extraction a property of the checkout: a commit that is not an ancestor of
    what is checked out is not a version this tree has, however new it is.
    """
    args = ["rev-list", "--first-parent", "--reverse", ref]
    if prefix:
        args += ["--", prefix]
    return [line.strip() for line in _git(repo, *args).split("\n") if line.strip()]


def names(repo: Path) -> dict[str, str]:
    """Decorative ``spec-v<N>`` names by commit sha, for reporting only.

    A commit carrying several such tags keeps the highest-numbered one, so the
    map is a function; a commit carrying none is simply absent. Nothing decides
    behavior on what this returns.
    """
    try:
        out = _git(
            repo,
            "for-each-ref",
            "refs/tags/spec-v*",
            # `*objectname` dereferences an annotated tag to its commit and is
            # empty for a lightweight one, which points at the commit already.
            "--format=%(refname:short) %(objectname) %(*objectname)",
        )
    except GitUnavailable:
        return {}
    found: dict[str, tuple[int, str]] = {}
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) < 2:
            continue
        name, direct, deref = parts[0], parts[1], (parts[2] if len(parts) > 2 else "")
        m = TAG_RE.match(name)
        if not m:
            continue
        sha = deref or direct
        number = int(m.group(1))
        if number >= found.get(sha, (-1, ""))[0]:
            found[sha] = (number, name)
    return {sha: name for sha, (_, name) in found.items()}


def tags(repo: Path, pattern: str) -> list[str]:
    """Tag names in *repo* matching a refname glob, unordered.

    Deliberately returns names and nothing else: the caller decides what a name
    means and how names order. `names()` above reads `spec-v<N>` for display;
    `install.releases` reads `v<N.N.N>` to report an installation's currency.
    Neither ordering belongs here — a shared "sort the tags" would have to pick
    one, and lexical order is exactly the hazard both callers exist to avoid
    (`v0.10.0` sorts below `v0.9.0`).
    """
    return [
        line.strip()
        for line in _git(
            repo, "for-each-ref", f"refs/tags/{pattern}", "--format=%(refname:short)"
        ).split("\n")
        if line.strip()
    ]


def markdown_at(repo: Path, ref: str, prefix: str) -> list[str]:
    """Repo-relative paths of every ``.md`` file under *prefix* at *ref*."""
    args = ["ls-tree", "-r", "--name-only", ref]
    if prefix:
        args += ["--", prefix]
    return [p for p in _git(repo, *args).split("\n") if p.endswith(".md")]


def show(repo: Path, ref: str, path: str) -> str | None:
    """File contents at *ref*, or None when the path does not exist there."""
    try:
        return _git(repo, "show", f"{ref}:{path}")
    except GitUnavailable:
        return None


def head_commit(repo: Path) -> str | None:
    try:
        return _git(repo, "rev-parse", "HEAD").strip()
    except GitUnavailable:
        return None


def is_shallow(repo: Path) -> bool:
    """True when the clone's history is truncated.

    Truncation is the one way ancestry dating can still be wrong, and it is
    wrong in the dangerous direction: the commits below the graft are invisible,
    so every scenario they introduced re-dates *forward* to the oldest commit
    that is visible, arming scenarios the product already satisfies. It cannot
    be inferred from the walk — a short history and a truncated one look
    identical — so it is asked directly and reported.
    """
    try:
        return _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    except GitUnavailable:
        return False


def resolve(repo: Path, ref: str) -> str:
    """*ref* as a full 40-character commit sha.

    ``rev-parse`` alone would happily resolve a tree or a tag object, and the
    pipeline commands key ledger records on what this returns — so the ``^{commit}``
    peel is not decoration. A ref that names no commit raises ``GitUnavailable``.
    """
    return _git(repo, "rev-parse", f"{ref}^{{commit}}").strip()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """True when *ancestor* is reachable from *descendant*.

    ``merge-base --is-ancestor`` exits 1 for "no" and >1 for "I could not
    tell" — an unknown sha, a corrupt object — and collapsing the two would
    turn "I cannot see that commit" into a confident "not a version". So the
    return code is read directly rather than through ``_git``, and anything
    above 1 is raised.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode > 1:
        raise GitUnavailable(
            " ".join(proc.stderr.split()) or f"merge-base --is-ancestor {ancestor} {descendant} failed"
        )
    return proc.returncode == 0


def merge_base(repo: Path, a: str, b: str) -> str | None:
    """The best common ancestor of *a* and *b*, or None when there is none.

    None is a real answer here, not a failure: a shallow clone can hold both
    commits and none of their shared history. ``git merge-base`` exits 1 for
    "there is no merge base" and >1 for "I could not tell", and the two are not
    collapsed for the reason `is_ancestor` gives about the same distinction.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", a, b],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise GitUnavailable(
            " ".join(proc.stderr.split()) or f"merge-base {a} {b} failed"
        )
    return proc.stdout.strip() or None


def changed_paths(repo: Path, base: str, head: str) -> tuple[list[str], str]:
    """Repo-relative paths a branch changed, and how the comparison was made.

    Returns ``(paths, basis)`` where *basis* is ``merge-base`` or ``two-dot``.

    The merge base is what a forge means by "what this PR changed": diffing the
    two refs directly also reports, inverted, everything that landed on *base*
    since the branch left it. For a guard that is not merely imprecise — it
    faults an implementer for a harness commit somebody else pushed to main.

    When there is no merge base to be had (a shallow CI clone; two unrelated
    histories) the comparison falls back to the direct diff and says so. That
    direction is deliberate: the direct diff is a *superset* of the branch's own
    changes, so a guard reading it can report a path the branch did not touch,
    but can never miss one it did. A guard may be wrong loudly; it may not be
    wrong quietly.

    ``--no-renames`` is load-bearing. With rename detection on, moving a file
    out of a protected tree is reported only under its new name, and the write
    to the tree it left — a deletion — disappears from the diff a boundary check
    reads.
    """
    basis = "merge-base"
    start = merge_base(repo, base, head)
    if start is None:
        basis = "two-dot"
        start = base
    out = _git(
        repo, "diff", "--name-only", "--no-renames", "-z", start, head
    )
    # -z: a path with a newline or a quote in it is otherwise emitted quoted and
    # escaped, and a guard that unquotes it wrongly reads a path that is not the
    # one in the tree. NUL cannot appear in a path, so this parse cannot be
    # confused by one.
    return sorted({p for p in out.split("\0") if p}), basis
