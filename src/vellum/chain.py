"""``vellum ledger verify`` — the ledger guard.

``spec/features/ledger.md``: "The ledger guard fails a release whose chain does
not resolve — a work item with no PR, a criterion no work item claims, a cut
naming an uncertified wave." ``@id:chain-resolution-fails-release`` states the
failing shape: a cut including a wave whose work item lacks a merged PR fails
before promotion.

Where each check applies, and why they are not all the same scope
-----------------------------------------------------------------
Two of the spec's conditions are properties of a record on its own, and one is a
property of a *release*. Applying all three everywhere would make the guard
useless on a live ledger, so scope follows the sentence:

**Every record** is checked for links that are broken however the ledger is
read: a work item with no PR, and a ``satisfies:`` entry naming a scenario id
that does not exist in the suite at that version. Those are wrong the moment
they are written.

**Only waves a cut names** are checked for coverage — "a criterion no work item
claims" — and for certification. An open wave legitimately has criteria nothing
claims yet; that is what an unplanned wave *is*. The question "is every criterion
claimed" only becomes answerable, and only becomes a defect, at the cut, which is
also the only place the spec asks it ("fails a **release**").

The suite at a version
----------------------
``ledger/suite-<sha>.json`` is what ``on-spec-merge.yml`` writes beside the
record, and it is the suite at that version. When one is absent the id checks
cannot be made, and this reports them as **unchecked** rather than passing them:
a link nobody looked at is not a link that resolved. ``--strict`` turns an
unchecked record into a refusal, and belongs wherever the guard actually blocks
— the same split ``vellum backpressure`` draws for the same reason.

Certification has no field to read
----------------------------------
"A cut naming an uncertified wave" has no representation in the record schema:
``vellum.ledger.RECORD_KEYS`` has no certification key, and the intent repo's own
harness says so in as many words (``harness/support/adapter.py``,
``certification-runner``: "the ledger record schema in vellum.ledger has no
certification field at all"). Rather than invent one, this guard checks the
nearest thing the schema does record — that a cut wave has reached ``verified``
or ``shipped`` — and labels the finding as the proxy it is, in the report and in
``Finding.kind``. Closing that gap needs a spec slice, not a guess here.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vellum.backpressure import NOT_A_RECORD, ledger_dir_for
from vellum.ledger import SHA_RE

#: Record states in which a wave's work is finished as far as the ledger knows.
#: Used only as the certification proxy described in the module docstring.
CERTIFIABLE_STATES = ("verified", "shipped")

#: A work item's ``satisfies:`` entry naming a scenario. Prose slices are
#: referenced by file path and heading anchor instead (``spec/features/ledger.md``)
#: and are not scenario ids, so they are counted and not resolved here.
SCENARIO_PREFIX = "scenario:"


class ChainError(Exception):
    """The chain could not be verified."""


@dataclass
class Finding:
    """One broken link."""

    #: ``no-pr`` | ``unknown-scenario`` | ``unknown-wave`` | ``uncertified-wave``
    #: | ``unclaimed-criterion``
    kind: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.detail}"


@dataclass
class Chain:
    """One run of the ledger guard."""

    ledger: Path
    records: int
    cuts: list[str]
    findings: list[Finding] = field(default_factory=list)
    #: ``(record filename, why)`` for records whose ids could not be resolved.
    unchecked: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    @property
    def broken(self) -> bool:
        return bool(self.findings)

    def report(self) -> str:
        lines = [
            f"Ledger chain in {self.ledger}",
            f"  {self.records} record(s), {len(self.cuts)} cut(s)",
            "",
        ]
        if self.findings:
            lines.append(f"{len(self.findings)} broken link(s):")
            for finding in self.findings:
                lines.append(f"  [{finding.kind}] {finding}")
        else:
            lines.append("No broken links.")
        lines.append("")
        if self.unchecked:
            lines.append(
                f"{len(self.unchecked)} record(s) could not have their scenario "
                f"references resolved:"
            )
            for name, why in self.unchecked:
                lines.append(f"  {name}: {why}")
            lines.append(
                "Unchecked is not passed. Pass --strict wherever this guard blocks."
            )
            lines.append("")
        if self.unreadable:
            lines.append(
                f"{len(self.unreadable)} file(s) in the ledger could not be read as "
                f"records: {', '.join(self.unreadable)}"
            )
            lines.append("")
        if self.broken:
            first = self.findings[0]
            lines.append(
                f"BLOCKED: the chain does not resolve. First broken link — "
                f"[{first.kind}] {first}. A release whose chain does not resolve "
                f"fails before promotion (spec/features/ledger.md)."
            )
        else:
            lines.append("OK: every link in the ledger chain resolves.")
        if any(f.kind == "uncertified-wave" for f in self.findings):
            lines.append(
                "Note: 'uncertified-wave' is a proxy. The record schema has no "
                "certification field, so this reads the record's state instead and "
                "faults a cut wave that has not reached "
                f"{' or '.join(CERTIFIABLE_STATES)}."
            )
        return "\n".join(lines)


def _load_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


def _suite_ids(ledger: Path, sha: str) -> tuple[set[str], dict[str, str], str | None]:
    """``(ids, version by id, why not)`` from ``ledger/suite-<sha>.json``."""
    path = ledger / f"suite-{sha}.json"
    if not path.is_file():
        return set(), {}, f"no {path.name} beside the record"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return set(), {}, f"{path.name} could not be read: {exc}"
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list):
        return set(), {}, f"{path.name} declares no scenarios list"
    ids, versions = set(), {}
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        ident = scenario.get("id")
        if not isinstance(ident, str) or not ident:
            continue
        ids.add(ident)
        version = scenario.get("version")
        if isinstance(version, str):
            versions[ident] = version
    return ids, versions, None


def _cut_waves(ledger: Path) -> tuple[list[str], list[Finding]]:
    """The waves ``releases.yaml`` cuts name, and any that are unreadable."""
    path = ledger / "releases.yaml"
    if not path.is_file():
        return [], []
    data = _load_yaml(path)
    if not isinstance(data, dict):
        return [], [Finding("unknown-wave", path.name, "not a YAML mapping")]
    cuts = data.get("cuts")
    if cuts in (None, []):
        return [], []
    if not isinstance(cuts, list):
        return [], [Finding("unknown-wave", path.name, f"cuts is {cuts!r}, not a list")]
    waves, findings = [], []
    for index, cut in enumerate(cuts):
        where = f"{path.name} cuts[{index}]"
        entries = []
        if isinstance(cut, dict):
            wave = cut.get("wave")
            entries = wave if isinstance(wave, list) else [wave]
            entries += cut.get("waves") if isinstance(cut.get("waves"), list) else []
        elif isinstance(cut, str):
            entries = [cut]
        entries = [e for e in entries if e is not None]
        if not entries:
            findings.append(Finding("unknown-wave", where, "names no wave"))
            continue
        for entry in entries:
            sha = str(entry).strip().lower()
            if not SHA_RE.match(sha):
                findings.append(
                    Finding("unknown-wave", where,
                            f"names {entry!r}, which is not a spec version (a commit sha)")
                )
                continue
            waves.append(sha)
    return waves, findings


def verify(checkout: str | Path, ledger_dir: str | Path | None = None,
           strict: bool = False) -> Chain:
    """Every link in the ledger chain, and the ones that do not resolve."""
    ledger = Path(ledger_dir) if ledger_dir is not None else ledger_dir_for(checkout)
    if not ledger.is_dir():
        raise ChainError(
            f"{ledger}: no ledger directory. The chain is made of ledger records, "
            f"so there is nothing here to resolve; is {checkout} an intent checkout?"
        )

    cuts, findings = _cut_waves(ledger)
    unchecked: list[tuple[str, str]] = []
    unreadable: list[str] = []
    by_sha: dict[str, tuple[str, dict]] = {}

    for path in sorted(ledger.glob("*.yaml")):
        if path.name in NOT_A_RECORD:
            continue
        data = _load_yaml(path)
        if not isinstance(data, dict):
            unreadable.append(path.name)
            continue
        sha = str(data.get("spec_version") or "").strip().lower()
        if not SHA_RE.match(sha):
            unreadable.append(path.name)
            continue
        by_sha[sha] = (path.name, data)

    records = 0
    for sha, (name, data) in sorted(by_sha.items(), key=lambda kv: kv[1][0]):
        records += 1
        items = data.get("work_items")
        items = items if isinstance(items, list) else []
        ids, _, why = _suite_ids(ledger, sha)
        if why is not None:
            unchecked.append((name, why))
        for item in items:
            if not isinstance(item, dict):
                findings.append(Finding("no-pr", name, f"work item {item!r} is not a mapping"))
                continue
            issue = item.get("issue")
            if item.get("pr") in (None, ""):
                findings.append(
                    Finding(
                        "no-pr", name,
                        f"work item {issue} ({item.get('title') or 'untitled'}) "
                        f"names no PR",
                    )
                )
            if why is not None:
                continue
            for ref in item.get("satisfies") or []:
                text = str(ref).strip()
                if not text.startswith(SCENARIO_PREFIX):
                    continue  # a prose slice: a path and an anchor, not an id
                ident = text[len(SCENARIO_PREFIX):]
                if ident not in ids:
                    findings.append(
                        Finding(
                            "unknown-scenario", name,
                            f"work item {issue} claims {text}, which is not in the "
                            f"suite at {sha[:12]}",
                        )
                    )

    # --------------------------------------------------------------- cuts
    for sha in cuts:
        if sha not in by_sha:
            findings.append(
                Finding("unknown-wave", "releases.yaml",
                        f"a cut names wave {sha[:12]}, which has no ledger record")
            )
            continue
        name, data = by_sha[sha]
        state = str(data.get("state") or "").strip()
        if state not in CERTIFIABLE_STATES:
            findings.append(
                Finding(
                    "uncertified-wave", "releases.yaml",
                    f"a cut names wave {sha[:12]} ({name}), whose state is "
                    f"{state or '(none)'} — it has not reached "
                    f"{' or '.join(CERTIFIABLE_STATES)}",
                )
            )
        ids, versions, why = _suite_ids(ledger, sha)
        if why is not None:
            continue  # already reported as unchecked above
        claimed = {
            str(ref).strip()[len(SCENARIO_PREFIX):]
            for item in (data.get("work_items") or [])
            if isinstance(item, dict)
            for ref in (item.get("satisfies") or [])
            if str(ref).strip().startswith(SCENARIO_PREFIX)
        }
        # Criteria this version *armed*: scenarios the suite dates to this very
        # commit. The rest of the suite belongs to earlier waves and was claimed
        # — or not — there; re-faulting it at every cut would make the guard
        # noisier the longer an installation runs.
        armed = sorted(i for i in ids if versions.get(i) == sha)
        for ident in armed:
            if ident not in claimed:
                findings.append(
                    Finding(
                        "unclaimed-criterion", "releases.yaml",
                        f"a cut names wave {sha[:12]}, which arms scenario:{ident} "
                        f"and no work item in {name} claims it",
                    )
                )

    if unchecked and strict:
        raise ChainError(
            f"{ledger}: {len(unchecked)} record(s) could not have their scenario "
            f"references resolved ({', '.join(n for n, _ in unchecked)}). Under "
            f"--strict the chain is not pronounced sound at all rather than "
            f"pronounced sound on links nothing looked at."
        )
    return Chain(
        ledger=ledger,
        records=records,
        cuts=cuts,
        findings=findings,
        unchecked=unchecked,
        unreadable=unreadable,
    )


def run(checkout: str, ledger_dir: str | None = None, strict: bool = False, out=None) -> int:
    """Report the chain and exit 1 on a broken link."""
    stream = out if out is not None else sys.stdout
    chain = verify(checkout, ledger_dir=ledger_dir, strict=strict)
    print(chain.report(), file=stream)
    if chain.broken:
        first = chain.findings[0]
        print(
            f"vellum: ledger chain — [{first.kind}] {first} "
            f"({len(chain.findings)} broken link(s) in total)",
            file=sys.stderr,
        )
        return 1
    return 0
