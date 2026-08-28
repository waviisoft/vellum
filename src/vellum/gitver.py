"""Reading the spec repo's git history to date each scenario.

Versions are bare monotonic integers carried by ``spec-v<N>`` tags (decision
D6). Rather than blame lines, ``suite.py`` walks these tags in order and
compares scenario fingerprints between them, so a moved or reformatted scenario
keeps its version and a changed one advances.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

TAG_RE = re.compile(r"^spec-v(\d+)$")

#: The version a spec tree carries before any tag exists.
BASE_VERSION = 1


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
    """The work-tree root containing *path* (the submodule's own root)."""
    return Path(_git(path, "rev-parse", "--show-toplevel").strip())


def prefix_of(root: Path, spec_root: Path) -> str:
    """The spec tree's path within its repo, e.g. ``spec``. Empty when it is the root."""
    rel = spec_root.resolve().relative_to(root.resolve()).as_posix()
    return "" if rel == "." else rel


def spec_tags(repo: Path) -> list[tuple[int, str]]:
    """``spec-v<N>`` tags as ``(N, tag)``, ascending. Malformed tags are ignored."""
    out = _git(repo, "tag", "--list", "spec-v*")
    tags = []
    for line in out.split("\n"):
        m = TAG_RE.match(line.strip())
        if m:
            tags.append((int(m.group(1)), line.strip()))
    return sorted(tags)


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
