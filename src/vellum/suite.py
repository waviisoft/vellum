"""``vellum suite extract`` — the acceptance suite for a spec tree.

Walks the tree, collects every fenced Gherkin scenario, and dates each one with
the spec version that introduced or last changed it. A version is a commit
(``spec/decisions/2026-08-28-versions-are-commits.md``), so a scenario's version
is a sha and "newer" is ancestry. The suite is the contract: "product X conforms
to spec version C" is defined as suite@C passing against a deployment of X
(``spec/features/scenarios-and-harness.md``).

Extraction refuses a tree whose blocks would cost the suite scenarios. Two
constructs do that: a ``gherkin`` fence that does not parse, and a ``Rule:``,
whose nested scenarios are deliberately not admitted. Either way the scenarios
are missing from the suite with nothing recording their absence, and every
consumer downstream reads the suite as the whole intent — so both raise
``DroppedScenarios`` here rather than being skipped past (waviisoft/vellum#7).

The refusal is scoped to that harm and no wider. A tree can fail ``lint`` for a
dozen reasons that leave every scenario in the suite — an unresolved link, a
missing ``@id:``, a fence declaring no scenarios — and ``extract`` goes on
emitting those trees. It is not a second lint; it declines to answer only where
the answer would be quietly short. Walking *history* stays tolerant even of the
two: see ``scenarios_in``.
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
    GitUnavailable,
    head_commit,
    is_shallow,
    markdown_at,
    names,
    prefix_of,
    repo_root,
    show,
    spec_commits,
)
from vellum.specfile import (
    SpecFile,
    iter_spec_files,
    parse_spec_text,
    resolve_spec_root,
)

#: 2 since versions became commits: ``version`` and ``spec_version`` are shas
#: rather than integers, ``version_name``/``spec_version_name`` carry the
#: decorative names, ``tagged`` became ``shallow``, and ``source_commit`` is
#: gone — it was always the commit extracted at, which is now ``spec_version``.
SUITE_SCHEMA = 2


@dataclass
class SuiteEntry:
    scenario: Scenario
    relpath: str
    #: The commit that introduced or last changed this scenario, or None when
    #: no commit in the checkout's ancestry carries it.
    version: str | None
    #: True when this scenario's content exists only in the working tree — an
    #: uncommitted spec edit, or a tree with no readable history at all. It has
    #: no version because the version it will belong to has no sha yet.
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
    """One scenario as it stood at a version, while the history is being walked."""

    id: str | None
    fingerprint: str
    version: str
    consumed: bool = False


@dataclass
class History:
    """What the spec-touching commits say about when each scenario last changed."""

    by_id: dict[str, str] = field(default_factory=dict)
    #: Fingerprints as of the newest version. The fallback for a scenario whose
    #: id the history does not carry — which is how the 19 scenarios written
    #: before ids existed keep their version across the change that introduced
    #: them.
    by_fingerprint: dict[str, str] = field(default_factory=dict)
    #: The newest spec version in the walk, or None when the walk was empty.
    latest: str | None = None
    #: Ancestry rank per version sha, oldest = 0. Shas do not compare, so this
    #: is what "earliest" means where the tag-era code could say ``min()``.
    order: dict[str, int] = field(default_factory=dict)


@dataclass
class Suite:
    #: The version being extracted at: a checkout's pinned commit, or the head
    #: of the working tree. None when the tree has no readable git history.
    #: This is the commit of the checkout, which need not itself touch the spec
    #: tree — a pin may name any commit, and CI compares this against it.
    spec_version: str | None
    #: The newest actual spec version in this checkout's ancestry — the commit
    #: the ledger's ``spec_head`` pointer names when the checkout is main.
    #: None when the walk found no spec-touching commit.
    spec_head: str | None = None
    entries: list[SuiteEntry] = field(default_factory=list)
    #: Decorative names by commit sha, for reporting. Never read for dating.
    names: dict[str, str] = field(default_factory=dict)
    #: True when the clone's history is truncated, so dating may be wrong.
    shallow: bool = False


@dataclass
class BlockError:
    """A ``gherkin`` fence that would cost the suite scenarios, located in its file.

    Two kinds, one type, because extraction refuses for one reason and it is
    not "the block is malformed": scenarios a stock Cucumber runner executes
    that ``suite.json`` would not describe, with nothing saying so. A fence
    that does not parse drops all of its; a ``Rule:`` drops the ones nested
    under it, which the parser reads perfectly well and ``parse_block``
    deliberately does not admit. ``code`` is the lint finding naming the same
    block, so the refusal can point at it rather than restate it.
    """

    relpath: str
    #: 1-based line of the fence's opening ``` — which block failed.
    block_line: int
    #: 1-based line of the fault inside that block: where the parser gave up,
    #: or where the ``Rule:`` is written.
    line: int
    #: The rest of the sentence after "gherkin block at line <n>".
    message: str
    #: ``GH001`` for a parse failure, ``GH010`` for a ``Rule:``.
    code: str

    def format(self) -> str:
        return (
            f"{self.relpath}:{self.line}: gherkin block at line "
            f"{self.block_line} {self.message}"
        )


class DroppedScenarios(Exception):
    """Extraction refused: some block in the tree would leave scenarios out.

    Raised rather than skipped past, because the scenarios a block drops are
    exactly the ones a consumer of the suite cannot see are missing
    (waviisoft/vellum#7). ``errors`` carries every offending block, not just
    the first, so one run names the whole repair list the way ``lint_tree``
    does.
    """

    def __init__(self, errors: list[BlockError]):
        self.errors = list(errors)
        super().__init__(f"{len(self.errors)} gherkin block(s) would drop scenarios")

    @property
    def codes(self) -> list[str]:
        """The lint codes covering these blocks, for pointing the reader at them."""
        return sorted({e.code for e in self.errors})


def scan_file(sf: SpecFile) -> tuple[list[Scenario], list[BlockError]]:
    """One spec file's scenarios, and every fence of its that would drop some.

    Both halves, because the two callers want different things from the same
    walk: ``extract`` refuses a tree with any dropping block, while the history
    walk reads old trees it cannot ask anyone to fix and takes the scenarios
    alone.

    Takes the parsed ``SpecFile`` rather than its text so extraction does not
    re-split a file it has already read, and — the part worth keeping — so the
    fences lint judges and the fences extraction judges are the same objects by
    construction rather than by two calls agreeing.
    """
    found: list[Scenario] = []
    failed: list[BlockError] = []
    for fence in sf.fences:
        if fence.info != "gherkin":
            continue
        try:
            block = parse_block(fence.body, fence.body_line)
        except GherkinParseError as exc:
            failed.append(
                BlockError(
                    relpath=sf.relpath,
                    block_line=fence.start_line,
                    line=exc.line,
                    message=f"does not parse: {exc.message}",
                    code="GH001",
                )
            )
            continue
        found.extend(block.scenarios)
        # A Rule is banned and its children are not admitted, so this block
        # parses cleanly and still hands back fewer scenarios than a stock
        # runner would execute — the same silent absence as a parse failure,
        # reached the other way (spec/decisions/2026-08-28-no-rules.md,
        # waviisoft/vellum-intent#16). The count is what makes it a refusal
        # rather than a style rule, and it is GH010's count: a Rule holding
        # nothing costs the suite nothing, fails lint on its own account, and
        # is not extraction's business.
        for rule in block.rules:
            if not rule.scenarios:
                continue
            failed.append(
                BlockError(
                    relpath=sf.relpath,
                    block_line=fence.start_line,
                    line=rule.line,
                    message=(
                        f"nests {rule.scenarios} scenario(s) under a banned "
                        f"{rule.keyword} ('{rule.name}'); they would be "
                        f"omitted from the suite"
                    ),
                    code="GH010",
                )
            )
    return found, failed


def scenarios_in(relpath: str, text: str) -> list[Scenario]:
    """Every scenario in one spec file's ``gherkin`` fences, dropping blocks skipped.

    This is the tolerant half of ``scan_file``, and what walking *history*
    needs: a commit that is already in the past cannot be repaired, so a block
    that failed to parse there — or nested scenarios under a ``Rule:`` there —
    is a fact about an old tree rather than a defect in the one being
    extracted. The tree the caller actually named goes through ``extract``,
    which refuses it — see ``DroppedScenarios``.

    Takes text rather than a ``SpecFile`` because its caller has text: the
    history walk reads blobs out of git, not files off disk.
    """
    return scan_file(parse_spec_text(relpath, text))[0]


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
    # background_steps is always empty in a conforming tree — Backgrounds are
    # banned (spec/decisions/2026-08-28-no-backgrounds.md) and lint rejects them
    # (GH008). The term stays because that decision also records what must
    # happen if the ban is ever lifted: a Background's steps are part of every
    # affected scenario's fingerprint, so a Background edit bumps every scenario
    # in the feature. The opposite reading is "rejected outright as a violation
    # of invariant 4", so this is the recorded semantic, kept implemented rather
    # than deleted and re-derived.
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


def version_history(repo: Path, prefix: str, ref: str = "HEAD") -> History:
    """Walk *ref*'s spec versions oldest-first, dating each scenario.

    A scenario is matched to its predecessor by id. When the id does not match
    anything at the previous version — because the scenario has just been given
    one — it falls back to matching an unclaimed scenario with the same
    fingerprint, which is what lets adding an ``@id:`` tag be the presentation
    change the spec says it is rather than a rewrite of the whole suite.

    The walk is over commits rather than ``spec-v*`` tags. The comparison it
    performs is unchanged; what changed is where the sequence comes from, and
    so what can go missing from it. A tag registry could lose an entry silently
    and re-date everything introduced at it; ancestry is the history itself.
    """
    commits = spec_commits(repo, ref, prefix)
    if not commits:
        return History()

    history = History(
        latest=commits[-1], order={sha: i for i, sha in enumerate(commits)}
    )
    previous: list[_Seen] = []
    for commit in commits:
        by_id = {seen.id: seen for seen in previous if seen.id}
        by_fingerprint: dict[str, list[_Seen]] = {}
        for seen in previous:
            by_fingerprint.setdefault(seen.fingerprint, []).append(seen)

        current: list[_Seen] = []
        for scenario_id, fp in _scenarios_at(repo, commit, prefix):
            match = by_id.get(scenario_id) if scenario_id else None
            if match is not None and not match.consumed:
                match.consumed = True
                version = match.version if match.fingerprint == fp else commit
            else:
                match = next(
                    (s for s in by_fingerprint.get(fp, []) if not s.consumed), None
                )
                if match is not None:
                    match.consumed = True
                    version = match.version
                else:
                    version = commit
            current.append(_Seen(id=scenario_id, fingerprint=fp, version=version))

        previous = current

    for seen in previous:
        if seen.id:
            history.by_id[seen.id] = seen.version
        # Among scenarios sharing content, take the earliest version: that is
        # when the behavior was first specified, and dating a fallback match
        # too early only leaves it enforced, never wrongly armed. "Earliest" is
        # ancestry rank, since two shas do not compare.
        held = history.by_fingerprint.get(seen.fingerprint)
        if held is None or history.order[seen.version] < history.order[held]:
            history.by_fingerprint[seen.fingerprint] = seen.version
    return history


def extract(spec_dir: str | Path) -> Suite:
    """The suite for the tree at *spec_dir*.

    Raises ``DroppedScenarios`` when any ``gherkin`` fence in the tree would
    leave scenarios out of the suite — a fence that does not parse, or one
    nesting scenarios under a banned ``Rule:``. Those scenarios would otherwise
    be absent from ``suite.json`` with nothing saying so, and every consumer —
    the harness, a briefing assembler, the ledger's armed-scenario accounting —
    would read the smaller suite as the whole intent (waviisoft/vellum#7).

    Lint findings that cost the suite nothing do not reach here: a tree with an
    unresolved link, a missing ``@id:`` or a fence declaring no scenarios still
    extracts, because its suite is complete. ``extract`` is not a second lint.
    """
    root = resolve_spec_root(spec_dir)

    # The tree is read and judged before any git work: a refusal costs no
    # history walk, and every failing block in the tree is named at once
    # rather than one per run.
    scanned: list[tuple[str, list[Scenario]]] = []
    failures: list[BlockError] = []
    for sf in iter_spec_files(root):
        found, failed = scan_file(sf)
        scanned.append((sf.relpath, found))
        failures.extend(failed)
    if failures:
        raise DroppedScenarios(failures)

    history = History()
    head = None
    decorations: dict[str, str] = {}
    shallow = False
    try:
        repo = repo_root(root)
        head = head_commit(repo)
        history = version_history(repo, prefix_of(repo, root), ref=head or "HEAD")
        decorations = names(repo)
        shallow = is_shallow(repo)
    except (GitUnavailable, ValueError):
        # No readable git history: nothing can be dated, and saying so beats
        # inventing a version for a tree whose history we cannot see.
        pass

    entries: list[SuiteEntry] = []
    for relpath, found in scanned:
        for sc in found:
            version = history.by_id.get(sc.id) if sc.id else None
            if version is None:
                version = history.by_fingerprint.get(fingerprint(sc))
            entries.append(
                SuiteEntry(
                    scenario=sc,
                    relpath=relpath,
                    version=version,
                    pending=version is None,
                )
            )
    entries.sort(key=lambda e: (e.relpath, e.scenario.line))
    return Suite(
        spec_version=head,
        spec_head=history.latest,
        entries=entries,
        names={
            sha: decorations[sha]
            for sha in _cited(head, history.latest, entries)
            if sha in decorations
        },
        shallow=shallow,
    )


def _cited(head: str | None, spec_head: str | None, entries: list[SuiteEntry]) -> list[str]:
    """Every sha the suite reports, so only those need a decorative name."""
    seen = [sha for sha in (head, spec_head) if sha]
    seen += [e.version for e in entries if e.version]
    return sorted(set(seen))


def to_dict(suite: Suite) -> dict:
    return {
        "schema": SUITE_SCHEMA,
        "generator": f"vellum {__version__}",
        "spec_version": suite.spec_version,
        # Decoration, emitted so a human reading suite.json sees "spec-v11"
        # next to the sha. Nothing consumes it.
        "spec_version_name": suite.names.get(suite.spec_version or ""),
        "spec_head": suite.spec_head,
        "spec_head_name": suite.names.get(suite.spec_head or ""),
        "shallow": suite.shallow,
        "scenario_count": len(suite.entries),
        "scenarios": [
            {
                "id": e.id,
                "ref": e.ref,
                "file": e.relpath,
                "line": e.scenario.line,
                "version": e.version,
                "version_name": suite.names.get(e.version or ""),
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
    """Write ``suite.json`` (or stdout when *out_path* is ``-``).

    Exits 1 without writing anything when a block in the tree would drop
    scenarios, naming each on stderr. 1 rather than 2 for the same reason
    ``lint`` uses it: the path was a spec tree, the tree is the problem.

    Every word of a refusal goes to stderr, including when *out_path* is ``-``:
    stdout carries the suite and nothing else, so ``extract … -o - | jq`` sees
    an empty stream rather than diagnostics parsed as JSON.
    """
    import sys

    try:
        suite = extract(spec_dir)
    except DroppedScenarios as exc:
        for err in exc.errors:
            print(f"vellum: {err.format()}", file=sys.stderr)
        print(
            f"vellum: {len(exc.errors)} gherkin block(s) in {spec_dir} would "
            f"leave scenarios out of the suite; no suite written. They would "
            f"be absent from it with nothing saying so. `vellum lint "
            f"{spec_dir}` reports the same blocks as "
            f"{', '.join(exc.codes)}.",
            file=sys.stderr,
        )
        return 1

    payload = json.dumps(to_dict(suite), indent=2) + "\n"
    if out_path == "-":
        (out or sys.stdout).write(payload)
    else:
        Path(out_path).write_text(payload, encoding="utf-8")
    return 0
