"""Shared test helpers: fixture paths and throwaway spec repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]

INDEX = """---
id: index
title: Spec index
since: spec-v1
---

# Index

features/auth.md
"""

AREA_TEMPLATE = """---
id: auth
title: Auth
since: spec-v1
---

# Auth

## Acceptance

```gherkin
{block}
```
"""


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=tests@vellum.invalid",
            "-c",
            "user.name=vellum tests",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def make_spec_repo(root: Path) -> Path:
    """An empty git repo with a ``spec/`` tree inside it. Returns the repo root."""
    (root / "spec" / "features").mkdir(parents=True)
    (root / "spec" / "index.md").write_text(INDEX, encoding="utf-8")
    git(root, "init", "-q", ".")
    return root


def commit_area(repo: Path, block: str, tag: str | None = None) -> None:
    """Write ``spec/features/auth.md`` with *block*, commit, and optionally tag."""
    (repo / "spec" / "features" / "auth.md").write_text(
        AREA_TEMPLATE.format(block=block), encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", tag or "wip", "--allow-empty")
    if tag:
        git(repo, "tag", tag)


def write_area(repo: Path, block: str) -> None:
    """Write the area without committing — the working tree of a spec PR."""
    (repo / "spec" / "features" / "auth.md").write_text(
        AREA_TEMPLATE.format(block=block), encoding="utf-8"
    )
