"""Shared test helpers: fixture paths, the intent checkout, throwaway repos."""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Names the intent-repo checkout the pinned-tree tests read. The pin of record
#: is a file now (spec/decisions/2026-08-28-pin-file.md), so nothing mounts the
#: intent repo for us and the checkout has to be supplied. CI's conformance job
#: sets this; ``./spec`` is honoured too, since the decision keeps a manual
#: mount as a developer convenience.
#:
#: Re-exported from the CLI rather than spelled again here. ``vellum pin
#: advance`` reads the same variable to find an intent checkout, and two
#: definitions of one environment variable is how the tests and the command
#: come to disagree about where the intent repo is.
from vellum.config import INTENT_ENV  # noqa: E402  (re-exported, see above)

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


def run_cli_streams(argv):
    """Run the CLI, returning ``(exit_code, stdout, stderr)`` kept apart.

    ``run_cli`` joins the two, which is what most assertions want. This is for
    the ones whose subject is *which* stream a message went to — ``suite
    extract … -o -`` writes the suite to stdout, so a diagnostic printed there
    would corrupt a pipe into ``jq``, and a joined buffer cannot tell.
    """
    from vellum.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def run_cli(argv):
    """Run the CLI, swallowing its output so the test log stays quiet.

    Returns ``(exit_code, output)`` with stdout and stderr joined into the one
    buffer: a caller asserting on a failure message should not have to know
    which stream the command chose.
    """
    code, out, err = run_cli_streams(argv)
    return code, out + err


class WrongPin(Exception):
    """A checkout was supplied, but it is not at the pinned commit."""


def _pin() -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / ".vellum" / "product.yaml").read_text())["pin"]


def pinned_commit() -> str:
    """The spec version .vellum/product.yaml pins: a commit sha.

    Tests read the pin rather than hard-coding it, for the reason they always
    have — a hard-coded pin fails on every advance, which is noise, not a
    defect. What changed is only that the pin is a commit rather than an
    integer (spec/decisions/2026-08-28-versions-are-commits.md).
    """
    return str(_pin()["commit"])


#: An ``@id:`` tag alone on its line, which is how a scenario carries its id.
#: Prose mentioning ``@id:`` inline (the identity decision does) is not a tag.
ID_TAG_LINE_RE = re.compile(r"^\s*@id:[a-z0-9]+(?:-[a-z0-9]+)*\s*$", re.MULTILINE)


def _candidate() -> Path | None:
    named = os.environ.get(INTENT_ENV)
    if named:
        return Path(named)
    mounted = REPO_ROOT / "spec"
    return mounted if mounted.is_dir() else None


def intent_checkout() -> Path | None:
    """The intent-repo checkout to read the pinned tree from, or None.

    Returns None when no checkout is available at all — the ordinary case for
    the `test` CI job and for a fresh clone, and the pinned-tree tests skip.
    Raises when a checkout *is* supplied but sits at some other commit: an
    absent tree is a fact about the environment, a wrong one is a mistake, and
    skipping quietly past it would report conformance against a tree that is
    not the pinned one.
    """
    path = _candidate()
    if path is None or not path.is_dir():
        return None
    try:
        top = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    # A directory materialized without git metadata answers for its *parent*
    # repo, silently — one of the failures that cost the submodule its job
    # (spec/decisions/2026-08-28-pin-file.md). Such a path is not a checkout.
    if Path(top).resolve() != path.resolve():
        return None
    pinned = pinned_commit()
    if head != pinned:
        raise WrongPin(
            f"{path} is at {head[:12]}, but .vellum/product.yaml pins "
            f"{pinned[:12]}. Move the checkout to the pin, or unset {INTENT_ENV}."
        )
    return path


def intent_spec_tree() -> Path | None:
    """The spec tree inside the intent checkout.

    Goes through ``resolve_spec_root`` rather than appending ``spec``: the
    checkout may be the intent repo (tree at ``<it>/spec``) or the tree itself,
    and that ambiguity has exactly one resolver.
    """
    from vellum.specfile import SpecTreeError, resolve_spec_root

    checkout = intent_checkout()
    if checkout is None:
        return None
    try:
        return resolve_spec_root(checkout)
    except SpecTreeError:
        return None


def _tree_files(tree: Path) -> list[Path]:
    return sorted(tree.rglob("*.md"))


def pinned_scenario_count(tree: Path) -> int:
    """Scenarios in the pinned tree, counted from its ``@id:`` tags.

    An oracle for extraction that is independent of the extractor and updates
    itself: a count hard-coded here would fail on every wave whose spec adds a
    scenario, which is the same noise as hard-coding the pin.
    """
    return sum(
        len(ID_TAG_LINE_RE.findall(f.read_text(encoding="utf-8")))
        for f in _tree_files(tree)
    )


def pinned_gherkin_file_count(tree: Path) -> int:
    """Files in the pinned tree carrying at least one gherkin fence."""
    return sum(
        1
        for f in _tree_files(tree)
        if "```gherkin" in f.read_text(encoding="utf-8")
    )


FEATURE_TEMPLATE = """---
id: {name}
title: {name}
since: spec-v1
---

# {name}

## Acceptance

```gherkin
{block}
```
"""


def make_tree(root: Path, blocks: dict[str, str]) -> Path:
    """A spec tree at ``<root>/spec`` holding one ``features/<name>.md`` per entry.

    No git, no commits: for tests whose subject is which files the scan visits
    and in what order, so the tree's shape is written in the test rather than
    read out of a fixture. ``iter_spec_files`` sorts by path, so the keys'
    alphabetical order is the order they are scanned in.
    """
    tree = root / "spec"
    (tree / "features").mkdir(parents=True)
    # The index lists the files actually written, so a tree built here is
    # link-clean and a test can assert its findings exactly rather than
    # filtering an LN001 the helper put there.
    (tree / "index.md").write_text(
        INDEX.replace(
            "features/auth.md",
            "\n".join(f"- features/{name}.md" for name in blocks),
        ),
        encoding="utf-8",
    )
    for name, block in blocks.items():
        (tree / "features" / f"{name}.md").write_text(
            FEATURE_TEMPLATE.format(name=name, block=block), encoding="utf-8"
        )
    return tree


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
    git(root, "init", "-q", "-b", "main", ".")
    return root


def commit_area(repo: Path, block: str, tag: str | None = None) -> str:
    """Write ``spec/features/auth.md`` with *block*, commit, return the sha.

    The commit is the version. *tag* is decoration: tests pass one only where
    what they are checking is the decorative name itself.
    """
    (repo / "spec" / "features" / "auth.md").write_text(
        AREA_TEMPLATE.format(block=block), encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", tag or "spec change", "--allow-empty")
    if tag:
        git(repo, "tag", tag)
    return git(repo, "rev-parse", "HEAD").strip()


def commit_elsewhere(repo: Path, message: str = "not a spec change") -> str:
    """Commit outside the spec tree — a commit that is not a version."""
    (repo / "notes.md").write_text(message + "\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").strip()


def write_area(repo: Path, block: str) -> None:
    """Write the area without committing — the working tree of a spec PR."""
    (repo / "spec" / "features" / "auth.md").write_text(
        AREA_TEMPLATE.format(block=block), encoding="utf-8"
    )


#: A minimal installation config. Only `budgets.divergence_cap` is read by any
#: command today, but the surrounding keys are kept so a sandbox config has the
#: shape of a real one and a reader that starts consulting a second key does
#: not silently find nothing.
CONFIG = """version_prefix: spec-v
budgets:
  per_item_usd: 10
  divergence_cap: {cap}
"""

PRODUCT = """# A product repo's backref and pin of record.
intent:
  repo: waviisoft/vellum-intent
  url: https://github.com/waviisoft/vellum-intent

# The pin of record. This comment exists to be preserved.
pin:
  commit: {commit}
  name: {name}

product:
  name: core
  trees: [src]

write_boundaries:
  implementer: [src, tests]
"""


#: The product map a real intent repo carries. `core` matches waviisoft/vellum's
#: own `product.name`, so a fixture cut pins the product this repo *is*.
WORKSPACE = """intent: waviisoft/vellum-intent
products:
  core: {repo: waviisoft/vellum, trees: [src, .vellum/memory]}
"""


def make_intent_repo(root: Path, cap: int = 3) -> Path:
    """A sandbox intent repo: git, a ``spec/`` tree, a config, a ledger.

    The shape ``vellum mint`` and ``vellum backpressure`` expect of a real
    intent checkout, built small enough that a test can state its whole history
    in three lines. ``make_spec_repo`` supplies the spec half; this adds the
    two directories the pipeline commands read.
    """
    make_spec_repo(root)
    (root / ".vellum").mkdir(parents=True, exist_ok=True)
    (root / ".vellum" / "config.yaml").write_text(CONFIG.format(cap=cap), encoding="utf-8")
    # The products a release cut may pin. Real intent repos carry this
    # (spec/features/repo-topology.md maps the products here), and
    # `vellum release cut` reads it as an allowlist rather than trusting the
    # product name it was handed, so a sandbox without one is not one.
    (root / ".vellum" / "workspace.yaml").write_text(WORKSPACE, encoding="utf-8")
    (root / "ledger").mkdir(exist_ok=True)
    return root


def make_product_repo(root: Path, commit: str = "0" * 40, name: str = "null") -> Path:
    """A sandbox product repo: just ``.vellum/product.yaml``, the pin of record.

    No git. ``vellum pin advance`` reads and rewrites one file and asks the
    *intent* repo the only question that needs history, so a product checkout
    needs nothing else to be one.
    """
    (root / ".vellum").mkdir(parents=True, exist_ok=True)
    (root / ".vellum" / "product.yaml").write_text(
        PRODUCT.format(commit=commit, name=name), encoding="utf-8"
    )
    return root


def write_record(ledger: Path, sha: str, state: str = "approved", name: str | None = None) -> Path:
    """A ledger record in the real emission shape, for tests about counting.

    Goes through ``ledger.dump`` rather than a hand-written string so a change
    to the record's key order or quoting cannot leave these fixtures behind.
    """
    from vellum.ledger import dump, new_record, record_path

    ledger.mkdir(parents=True, exist_ok=True)
    record = new_record(sha, name=name)
    record["state"] = state
    path = record_path(ledger, sha)
    path.write_text(dump(record), encoding="utf-8")
    return path


#: A product file carrying the three things the guards read: the pin, the trees
#: the repo owns, and the trees each role may write. Written as a template
#: rather than dumped from a dict because `.vellum/product.yaml` is a
#: comment-bearing file in every real repo and a fixture that round-trips
#: through `safe_dump` stops resembling one.
GUARDED_PRODUCT = """# A product repo's backref, pin of record and boundaries.
intent:
  repo: waviisoft/vellum-intent

pin:
  commit: {commit}
  name: null

product:
  name: core
  trees: [src, .vellum/memory]

{boundaries}"""

#: What `waviisoft/vellum` itself declares, minus the trees a fixture has no
#: use for. Tests that are about a *particular* boundary pass their own.
DEFAULT_BOUNDARIES = {"implementer": ["src", "tests", ".vellum/memory"]}

#: "the caller said nothing", which is not the same argument as `None`. `None`
#: means "write no write_boundaries key at all" — a product file from before the
#: guard existed — and a fixture has to be able to ask for that.
UNSET = object()


def boundaries_block(boundaries: dict | None) -> str:
    """`write_boundaries:` as YAML text, or nothing at all when None.

    None writes no key, which is a different fixture from an empty mapping: one
    is a product file from before the guard existed, the other declares that no
    role may write anything. Both are refusals and the tests assert they are
    reached by different messages.
    """
    if boundaries is None:
        return ""
    lines = ["write_boundaries:"]
    for role, trees in boundaries.items():
        rendered = ", ".join(str(t) for t in trees)
        lines.append(f"  {role}: [{rendered}]")
    return "\n".join(lines) + "\n"


def write_product(root: Path, commit: str = "0" * 40, boundaries=UNSET) -> Path:
    """Write `.vellum/product.yaml` into *root*."""
    (root / ".vellum").mkdir(parents=True, exist_ok=True)
    path = root / ".vellum" / "product.yaml"
    path.write_text(
        GUARDED_PRODUCT.format(
            commit=commit,
            boundaries=boundaries_block(
                DEFAULT_BOUNDARIES if boundaries is UNSET else boundaries
            ),
        ),
        encoding="utf-8",
    )
    return path


#: What the intent repo's own `write_boundaries` block is expected to declare.
#: The harness engineer owns `harness/` and nothing else — the recurring real
#: breach these fixtures are about is a harness session editing
#: `.vellum/memory/`, which belongs to the librarian.
DEFAULT_INTENT_BOUNDARIES = {
    "harness-engineer": ["harness"],
    "librarian": [".vellum/memory"],
}


def make_git_intent_repo(root: Path, boundaries=UNSET, files: dict | None = None) -> Path:
    """An intent repo with git history and a `write_boundaries` block in its config.

    The intent side of `make_git_product_repo`: the guard reads a diff, so the
    fixture has to be a real repository, and the file it reads boundaries out of
    is `.vellum/config.yaml` rather than `.vellum/product.yaml` — the intent repo
    has no product file, which is the whole reason the config is a source at all.
    """
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main", ".")
    write_intent_config(
        root, boundaries=DEFAULT_INTENT_BOUNDARIES if boundaries is UNSET else boundaries
    )
    seeded = {
        "harness/run.py": "# the harness engineer's own tree\n",
        "spec/index.md": INDEX,
        ".vellum/memory/areas/pipeline.md": "# Pipeline\n\nThe librarian's note.\n",
        "ledger/notes.md": "# the orchestrator's tree\n",
    }
    seeded.update(files or {})
    commit_files(root, seeded, "the installation begins")
    return root


def write_intent_config(root: Path, cap: int = 3, boundaries=UNSET) -> Path:
    """Write `.vellum/config.yaml`, optionally carrying a `write_boundaries` block."""
    (root / ".vellum").mkdir(parents=True, exist_ok=True)
    path = root / ".vellum" / "config.yaml"
    block = boundaries_block(
        DEFAULT_INTENT_BOUNDARIES if boundaries is UNSET else boundaries
    )
    path.write_text(CONFIG.format(cap=cap) + block, encoding="utf-8")
    return path


def commit_files(repo: Path, files: dict, message: str) -> str:
    """Write *files* (path -> text, or None to delete) and commit. Returns the sha."""
    for relative, text in files.items():
        path = repo / relative
        if text is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message, "--allow-empty")
    return git(repo, "rev-parse", "HEAD").strip()


def make_git_product_repo(root: Path, boundaries=UNSET, files: dict | None = None) -> Path:
    """A product repo with git history: a product file, a src tree, an area note.

    The guards over a product checkout all read a *diff*, so unlike
    `make_product_repo` — which needs no history, because `pin advance` reads
    one file — these fixtures have to be real repositories.
    """
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main", ".")
    write_product(root, boundaries=boundaries)
    seeded = {
        "src/app.py": "def main():\n    return 0\n",
        ".vellum/memory/areas/app.md": "# App\n\nThe note that exists before the change.\n",
        "harness/steps.py": "# the harness the implementer may not write\n",
        "README.md": "# product\n",
    }
    seeded.update(files or {})
    commit_files(root, seeded, "the product begins")
    return root


def branch(repo: Path, name: str) -> None:
    git(repo, "checkout", "-q", "-b", name)


def write_suite(ledger: Path, sha: str, scenarios) -> Path:
    """A ``ledger/suite-<sha>.json`` holding *scenarios*.

    *scenarios* is an iterable of ``id`` or ``(id, version)``; a bare id is
    dated to *sha*, which makes it a criterion that version armed. Written in
    the shape ``vellum suite extract`` emits (schema 2) rather than a minimal
    one, so a reader that starts consulting a second field does not find a
    fixture that never had it.
    """
    import json

    entries = []
    for scenario in scenarios:
        ident, version = scenario if isinstance(scenario, tuple) else (scenario, sha)
        entries.append(
            {
                "id": ident,
                "ref": f"scenario:{ident}",
                "file": f"features/{ident}.md",
                "line": 1,
                "version": version,
                "version_name": None,
                "pending": False,
                "feature": "Sandbox",
                "name": ident,
                "keyword": "Scenario",
                "tags": [f"@id:{ident}"],
                "background_steps": [],
                "steps": [{"keyword": "Given", "text": "a sandbox"}],
                "examples": [],
            }
        )
    ledger.mkdir(parents=True, exist_ok=True)
    path = ledger / f"suite-{sha}.json"
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "generator": "vellum tests",
                "spec_version": sha,
                "spec_version_name": None,
                "spec_head": sha,
                "spec_head_name": None,
                "shallow": False,
                "scenario_count": len(entries),
                "scenarios": entries,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return path


def write_releases(
    ledger: Path,
    cuts=(),
    spec_head: str | None = None,
    spec_conformed: str | None = None,
    channels: dict | None = None,
) -> Path:
    """A ``ledger/releases.yaml`` whose ``cuts`` name *cuts*.

    Each entry may be a sha (written as ``{wave: <sha>}``, the shape the intent
    repo's harness builds) or a mapping written through as-is, for the tests
    whose subject is a malformed cut.

    *spec_conformed* sets the production channel's pointer — the input the
    armed/enforced partition reads. *channels* replaces the whole mapping, for
    the tests whose subject is a channel that is not declared. The real file has
    ``spec_conformed: null``, which is why None is the default: a fixture that
    quietly conformed to something would make every partition test agree with a
    world the installation is not in.
    """
    import yaml

    entries = [c if isinstance(c, dict) else {"wave": c} for c in cuts]
    ledger.mkdir(parents=True, exist_ok=True)
    path = ledger / "releases.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "spec_head": spec_head,
                "channels": (
                    channels
                    if channels is not None
                    else {"production": {"spec_conformed": spec_conformed}}
                ),
                "cuts": entries,
                "stamps": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path
