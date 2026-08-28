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
from vellum.gherkin_blocks import (
    GherkinParseError,
    Scenario,
    Step,
    parse_block,
    scenario_ref,
)
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
    def id(self) -> str | None:
        return self.scenario.id

    @property
    def ref(self) -> str | None:
        """How the ledger refers to this scenario: ``scenario:<id>``."""
        return scenario_ref(self.scenario.id) if self.scenario.id else None


@dataclass
class _Seen:
    """One scenario as it stood at a tag, while versions are being walked."""

    id: str | None
    fingerprint: str
    version: int
    consumed: bool = False


@dataclass
class History:
    """What the ``spec-v*`` tags say about when each scenario last changed."""

    by_id: dict[str, int] = field(default_factory=dict)
    #: Fingerprints as of the newest tag. The fallback for a scenario whose id
    #: the tags do not carry — which is how the 19 scenarios written before
    #: ids existed keep their version across the change that introduced them.
    by_fingerprint: dict[str, int] = field(default_factory=dict)
    latest: int = 0


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


def _normalized(step: Step) -> list[str]:
    return [step.keyword_type, " ".join(step.text.split())]


def fingerprint(sc: Scenario) -> str:
    """Content hash of a scenario: normalized steps and example tables only.

    "Changed" means the fingerprint changed
    (``spec/decisions/2026-08-28-scenario-identity.md``). Titles and tags are
    presentation and are excluded, as are line numbers and surrounding prose —
    so renaming a scenario, re-tagging it, moving it, or re-indenting it does
    not read as a behavioral change. Background steps are included because they
    execute as part of the scenario.
    """
    payload = {
        "steps": [_normalized(s) for s in (*sc.background_steps, *sc.steps)],
        "examples": [
            {
                "header": [" ".join(c.split()) for c in ex["header"]],
                "rows": [[" ".join(c.split()) for c in row] for row in ex["rows"]],
            }
            for ex in sc.examples
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _scenarios_at(repo: Path, ref: str, prefix: str) -> list[tuple[str | None, str]]:
    """``(id, fingerprint)`` for every scenario in the tree at one ref."""
    out: list[tuple[str | None, str]] = []
    for path in sorted(markdown_at(repo, ref, prefix)):
        text = show(repo, ref, path)
        if text is None:
            continue
        relpath = path[len(prefix) + 1 :] if prefix else path
        for sc in scenarios_in(relpath, text):
            out.append((sc.id, fingerprint(sc)))
    return out


def version_history(repo: Path, prefix: str) -> History:
    """Walk the ``spec-v*`` tags, dating each scenario by when it last changed.

    A scenario is matched to its predecessor by id. When the id does not match
    anything at the previous tag — because the scenario has just been given
    one — it falls back to matching an unclaimed scenario with the same
    fingerprint, which is what lets adding an ``@id:`` tag be the presentation
    change the spec says it is rather than a rewrite of the whole suite.
    """
    tags = spec_tags(repo)
    if not tags:
        return History()

    history = History(latest=tags[-1][0])
    previous: list[_Seen] = []
    for number, tag in tags:
        by_id = {seen.id: seen for seen in previous if seen.id}
        by_fingerprint: dict[str, list[_Seen]] = {}
        for seen in previous:
            by_fingerprint.setdefault(seen.fingerprint, []).append(seen)

        current: list[_Seen] = []
        for scenario_id, fp in _scenarios_at(repo, tag, prefix):
            match = by_id.get(scenario_id) if scenario_id else None
            if match is not None and not match.consumed:
                match.consumed = True
                version = match.version if match.fingerprint == fp else number
            else:
                match = next(
                    (s for s in by_fingerprint.get(fp, []) if not s.consumed), None
                )
                if match is not None:
                    match.consumed = True
                    version = match.version
                else:
                    version = number
            current.append(_Seen(id=scenario_id, fingerprint=fp, version=version))

        previous = current

    for seen in previous:
        if seen.id:
            history.by_id[seen.id] = seen.version
        # Among scenarios sharing content, take the earliest version: that is
        # when the behavior was first specified, and dating a fallback match
        # too early only leaves it enforced, never wrongly armed.
        history.by_fingerprint[seen.fingerprint] = min(
            history.by_fingerprint.get(seen.fingerprint, seen.version), seen.version
        )
    return history


def extract(spec_dir: str | Path) -> Suite:
    root = resolve_spec_root(spec_dir)

    history = History()
    commit = None
    try:
        repo = repo_root(root)
        history = version_history(repo, prefix_of(repo, root))
        commit = head_commit(repo)
    except (GitUnavailable, ValueError):
        # No readable git history: every scenario belongs to the base version.
        pass

    # A scenario the tags do not yet carry belongs to the version this spec
    # change will mint — which is what spec CI needs when it runs on a PR.
    next_version = history.latest + 1 if history.latest else BASE_VERSION

    entries: list[SuiteEntry] = []
    for sf in iter_spec_files(root):
        for sc in scenarios_in(sf.relpath, sf.text):
            version = history.by_id.get(sc.id) if sc.id else None
            if version is None:
                version = history.by_fingerprint.get(fingerprint(sc))
            entries.append(
                SuiteEntry(
                    scenario=sc,
                    relpath=sf.relpath,
                    version=version if version is not None else next_version,
                    pending=version is None,
                )
            )
    entries.sort(key=lambda e: (e.relpath, e.scenario.line))
    return Suite(
        spec_version=(
            next_version if any(e.pending for e in entries) else history.latest
        )
        or BASE_VERSION,
        entries=entries,
        source_commit=commit,
        tagged=bool(history.latest),
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
                "ref": e.ref,
                "file": e.relpath,
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
