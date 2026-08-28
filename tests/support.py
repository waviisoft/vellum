"""Shared test helpers: fixture paths and throwaway spec repositories."""

from __future__ import annotations

import re
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


def pinned_version() -> int:
    """The spec version .vellum/product.yaml pins, as an integer.

    Tests read the pin rather than hard-coding it: a test that hard-codes the
    version fails every time the pin advances, which is noise, not a defect.
    """
    import yaml

    pin = yaml.safe_load((REPO_ROOT / ".vellum" / "product.yaml").read_text())["pin"]
    return int(str(pin["version"]).removeprefix("spec-v"))


#: An ``@id:`` tag alone on its line, which is how a scenario carries its id.
#: Prose mentioning ``@id:`` inline (the identity decision does) is not a tag.
ID_TAG_LINE_RE = re.compile(r"^\s*@id:[a-z0-9]+(?:-[a-z0-9]+)*\s*$", re.MULTILINE)

PINNED_SPEC = REPO_ROOT / "spec"


def pinned_spec_is_checked_out() -> bool:
    return (PINNED_SPEC / "spec" / "index.md").is_file()


def _pinned_spec_files() -> list[Path]:
    return sorted((PINNED_SPEC / "spec").rglob("*.md"))


def pinned_scenario_count() -> int:
    """Scenarios in the pinned tree, counted from its ``@id:`` tags.

    An oracle for extraction that is independent of the extractor and updates
    itself: a count hard-coded here would fail on every wave whose spec adds a
    scenario, which is the same noise as hard-coding the pinned version.
    """
    return sum(
        len(ID_TAG_LINE_RE.findall(f.read_text(encoding="utf-8")))
        for f in _pinned_spec_files()
    )


def pinned_gherkin_file_count() -> int:
    """Files in the pinned tree carrying at least one gherkin fence."""
    return sum(
        1
        for f in _pinned_spec_files()
        if "```gherkin" in f.read_text(encoding="utf-8")
    )


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
