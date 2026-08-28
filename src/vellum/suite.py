"""``vellum suite extract`` — the acceptance suite for a spec tree.

Walks the tree, collects every fenced Gherkin scenario, and dates each one with
the spec version that introduced or last changed it. The suite is the contract:
"product X conforms to spec-vN" is defined as suite@spec-vN passing against a
deployment of X (``spec/features/scenarios-and-harness.md``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from vellum import __version__
from vellum.gherkin_blocks import GherkinParseError, Scenario, parse_block
from vellum.gitver import (
    BASE_VERSION,
    GitUnavailable,
    head_commit,
    markdown_at,
    prefix_of,
    repo_root,
    show,
    spec_tags,
)
from vellum.specfile import iter_spec_files, parse_spec_text, resolve_spec_root

SUITE_SCHEMA = 1


@dataclass
class SuiteEntry:
    scenario: Scenario
    relpath: str
    version: int
    #: True when no ``spec-v*`` tag yet contains this scenario — it belongs to
    #: the version the spec PR under review will mint.
    pending: bool

    @property
    def id(self) -> str:
        return f"{self.relpath}#{self.scenario.anchor}"


@dataclass
class Suite:
    spec_version: int
    entries: list[SuiteEntry] = field(default_factory=list)
    source_commit: str | None = None
    tagged: bool = False


def scenarios_in(relpath: str, text: str) -> list[Scenario]:
    """Every scenario in one spec file's ``gherkin`` fences.

    Blocks that fail to parse are skipped, not raised: ``vellum lint`` is where
    an unparseable block fails a run, and extraction must still describe the
    rest of the tree.
    """
    sf = parse_spec_text(relpath, text)
    found: list[Scenario] = []
    for fence in sf.fences:
        if fence.info != "gherkin":
            continue
        try:
            found.extend(parse_block(fence.body, fence.body_line))
        except GherkinParseError:
            continue
    return found


def fingerprint(sc: Scenario) -> str:
    """Content hash of a scenario: name, tags, steps, examples — never position.

    Line numbers and surrounding prose are excluded so that moving or
    reformatting a scenario does not read as a behavioral change.
    """
    payload = {
        "keyword": sc.keyword,
        "name": sc.name,
        "tags": sorted(sc.tags),
        "background": [[s.keyword, s.text] for s in sc.background_steps],
        "steps": [[s.keyword, s.text] for s in sc.steps],
        "examples": sc.examples,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _fingerprints_at(repo: Path, ref: str, prefix: str) -> dict[tuple[str, str], str]:
    """``(relpath, anchor) -> fingerprint`` for the whole tree at one ref."""
    out: dict[tuple[str, str], str] = {}
    for path in markdown_at(repo, ref, prefix):
        text = show(repo, ref, path)
        if text is None:
            continue
        relpath = path[len(prefix) + 1 :] if prefix else path
        for sc in scenarios_in(relpath, text):
            out[(relpath, sc.anchor)] = fingerprint(sc)
    return out


def version_history(repo: Path, prefix: str) -> tuple[dict[tuple[str, str], int], int]:
    """Map each scenario to the version that introduced or last changed it.

    Returns the map and the highest tagged version (0 when the repo has no
    ``spec-v*`` tags yet).
    """
    tags = spec_tags(repo)
    if not tags:
        return {}, 0
    versions: dict[tuple[str, str], int] = {}
    previous: dict[tuple[str, str], str] = {}
    for number, tag in tags:
        current = _fingerprints_at(repo, tag, prefix)
        for key, fp in current.items():
            if previous.get(key) != fp:
                versions[key] = number
        previous = current
    return versions, tags[-1][0]


def extract(spec_dir: str | Path) -> Suite:
    root = resolve_spec_root(spec_dir)

    versions: dict[tuple[str, str], int] = {}
    latest = 0
    commit = None
    try:
        repo = repo_root(root)
        versions, latest = version_history(repo, prefix_of(repo, root))
        commit = head_commit(repo)
    except (GitUnavailable, ValueError):
        # No readable git history: every scenario belongs to the base version.
        pass

    # A scenario the tags do not yet carry belongs to the version this spec
    # change will mint — which is what spec CI needs when it runs on a PR.
    next_version = latest + 1 if latest else BASE_VERSION

    entries: list[SuiteEntry] = []
    for sf in iter_spec_files(root):
        for sc in scenarios_in(sf.relpath, sf.text):
            key = (sf.relpath, sc.anchor)
            tagged = key in versions
            entries.append(
                SuiteEntry(
                    scenario=sc,
                    relpath=sf.relpath,
                    version=versions[key] if tagged else next_version,
                    pending=not tagged,
                )
            )
    entries.sort(key=lambda e: (e.relpath, e.scenario.line))
    return Suite(
        spec_version=max(latest, next_version if entries else latest) or BASE_VERSION,
        entries=entries,
        source_commit=commit,
        tagged=bool(latest),
    )


def to_dict(suite: Suite) -> dict:
    return {
        "schema": SUITE_SCHEMA,
        "generator": f"vellum {__version__}",
        "spec_version": suite.spec_version,
        "source_commit": suite.source_commit,
        "tagged": suite.tagged,
        "scenario_count": len(suite.entries),
        "scenarios": [
            {
                "id": e.id,
                "file": e.relpath,
                "anchor": e.scenario.anchor,
                "line": e.scenario.line,
                "version": e.version,
                "pending": e.pending,
                "feature": e.scenario.feature,
                "name": e.scenario.name,
                "keyword": e.scenario.keyword,
                "tags": e.scenario.tags,
                "background_steps": [
                    {"keyword": s.keyword, "text": s.text} for s in e.scenario.background_steps
                ],
                "steps": [
                    {"keyword": s.keyword, "text": s.text} for s in e.scenario.steps
                ],
                "examples": e.scenario.examples,
            }
            for e in suite.entries
        ],
    }


def run(spec_dir: str, out_path: str = "suite.json", out=None) -> int:
    """Write ``suite.json`` (or stdout when *out_path* is ``-``)."""
    import sys

    payload = json.dumps(to_dict(extract(spec_dir)), indent=2) + "\n"
    if out_path == "-":
        (out or sys.stdout).write(payload)
    else:
        Path(out_path).write_text(payload, encoding="utf-8")
    return 0
