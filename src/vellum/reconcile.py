"""``vellum tick`` — the stateless reconciler.

``spec/decisions/2026-08-28-reconciler.md``: "The forge and the repos are the
database; every orchestrator action is idempotent and lease-guarded; events
schedule reconciliation rather than carry state. Missed events cost latency,
never correctness." ``spec/features/orchestration.md`` says the same thing as a
loop: "Each tick computes the difference and takes idempotent, lease-guarded
convergent actions."

So a tick is one pass of *read desired state, read observed state, compute the
convergent next actions, act*. It is not a daemon, it holds nothing between
runs, and running it twice over an unchanged world writes nothing the second
time.

Desired state, observed state, and the line between them
--------------------------------------------------------
**Desired state is repository state**: the ledger records under
``<checkout>/ledger`` — approved versions, their work plans, each item's state,
lease and cost — plus the spec tree and ``releases.yaml``. This module reads all
of that off disk.

**Observed state is the forge**, and this command does not reach one. It is
supplied by the caller, as ``--observed <file>``, exactly the way
``backpressure --pending`` takes the count of unlanded spec PRs and ``certify
check --head`` takes the PR head: where a decision needs a number or a fact only
a forge can see, the caller that *can* see it passes it in, and the report says
plainly when it was not supplied. Do not reach for a forge API here — the
division is the point, and it is what makes the reconciler's behavior a PASS-able
property rather than a deployment one
(``spec/features/scenarios-and-harness.md``).

What a tick does, and what it only decides
------------------------------------------
Two kinds of convergent action fall out of that division, and every action this
computes is labelled with which kind it is:

``taken``
    A write to the ledger, which is repository state this command owns:
    committing a work plan, marking an item superseded, claiming an item under
    a lease, recording a new briefing.

``for the caller``
    A forge action, which this command can only *decide*: file a work-item
    issue, open a question issue, draft a ``spec:clarify`` PR, close a resolved
    question. These are emitted, never performed.

That split is why a scenario like ``@id:comment-becomes-clarify-pr`` is half
implementable here: the decision — *this* open question, with an owner comment
and no clarify PR yet, needs one drafted — is computed from state and asserted;
that a comment *arrives* and that a PR *appears* stay deployment properties the
harness requires a forge for.

Leases, and the one ordering that matters
-----------------------------------------
"Executors claim items under leases matched to role capability; a lapsed lease
returns the item to the queue" (``spec/features/orchestration.md``); "the
reconciler writes it at claim, clears it at report, and treats an expired lease
as no lease" (``spec/features/ledger.md``). ``active_lease()`` in
``vellum.ledger`` resolves expiry, so an item is claimed exactly when it returns
something.

**``active_lease`` is asked before ``take_lease``, always**, and ``_claim()``
below is the only place either is reached from, so the order cannot be got wrong
by a later caller. Re-dispatching a claimed item is the double-claim
``@id:fire-and-collect`` exists to forbid: two executors on one item, both
pushing to the same branch.

There is no mid-run channel
---------------------------
``spec/decisions/2026-08-28-fire-and-collect-executors.md`` makes the absence
normative. So when new direction arrives for an item whose lease is live, this
does **not** end the run and does not re-dispatch: it records the direction on
the item's ``briefing`` — a real ledger field, "what the agent knew" — and
leaves the lease to lapse. The next tick after expiry dispatches a fresh run
carrying that briefing. That is the spec's own remedy ("the current run is ended
or its lease is left to lapse ... a fresh run is spawned with a briefing
carrying the direction"), and the half this command can perform is the one it
performs.

Parking is observed, not stored
-------------------------------
``spec/features/question-protocol.md`` says a work item "parks" while its
question is open. ``vellum.ledger.ITEM_STATES`` has no ``parked`` state, and one
is not invented here: under the reconciler decision the forge *is* the database,
so a parked item is precisely one the observed state shows an open question
issue against, and it is recomputed every tick. Nothing is written and nothing
can go stale. See ``PR: spec clarification requests`` — whether the ledger
should carry a park is a spec question, not a guess for this module.

Exit codes follow the CLI's contract (``vellum.cli``): 0 reconciled, 1 a wave is
parked past the question timebox, 2 the tick could not be performed.
"""

from __future__ import annotations

import datetime
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vellum.backpressure import NOT_A_RECORD, ledger_dir_for
from vellum.config import ConfigError
from vellum.config import load as load_config
from vellum.gitver import GitUnavailable, is_ancestor, repo_root
from vellum.ledger import (
    SHA_RE,
    LedgerError,
    active_lease,
    dump,
    find_item,
    load,
    new_lease,
    parse_time,
    upsert_plan,
)
from vellum.text import one_line

#: Record states a version has left the reconciler's attention in. The same two
#: ``vellum.backpressure`` counts as settled, for the same reason: one shipped,
#: the other never will.
SETTLED_RECORD_STATES = ("shipped", "superseded")

#: Work-item states a tick may still dispatch from. ``merged`` is finished and
#: ``superseded`` will not be done at all; ``implementing`` is re-dispatchable
#: precisely when its lease has lapsed, which is what a lapsed lease *means*
#: (``spec/features/orchestration.md``: "a lapsed lease returns the item to the
#: queue and the next executor restarts from the last pushed commit").
DISPATCHABLE_ITEM_STATES = ("planned", "implementing")

#: Item state a coalesced item is marked with. Already in
#: ``vellum.ledger.ITEM_STATES``; nothing new is invented to hold this outcome.
SUPERSEDED = "superseded"

#: ``satisfies:`` entries naming a scenario. The same prefix ``vellum.chain``
#: reads, because overlap between two waves is decided on the criteria they
#: claim and those are written in one form.
SCENARIO_PREFIX = "scenario:"

#: The label a question's resolution PR carries
#: (``spec/features/question-protocol.md``; ``labels.spec`` in the
#: installation config lists it).
CLARIFY_LABEL = "spec:clarify"

#: ``spec/features/question-protocol.md``: "Past the timebox (default 24h) the
#: wave parks." The installation config carries the real value under
#: ``questions.timebox_hours``; this is the spec's default for a config that
#: does not.
DEFAULT_TIMEBOX_HOURS = 24

#: How long a claim this command takes is good for. **Not a spec value** — no
#: spec sentence and no key in ``.vellum/config.yaml`` gives a lease duration —
#: so it is stated here, overridable with ``--lease-minutes``, and named in the
#: report rather than left to look derived.
DEFAULT_LEASE_MINUTES = 60

#: Every kind of convergent action a tick can reach, and whether this command
#: performs it (``True``) or only decides it (``False``, for the caller that can
#: see a forge). The set is closed on purpose: an action nothing in the spec
#: asks for is a spec question, not a new string here.
ACTION_KINDS: dict[str, bool] = {
    # -- decided, performed by the caller ------------------------------------
    "plan": False,          # an approved version with no work plan: run the planner
    "file-issue": False,    # a planned item whose issue the forge does not show
    "open-question": False, # a raised question the corpus does not answer
    "answer-question": False,  # a raised question the corpus answers: reply, file nothing
    "draft-clarify": False, # an open question with an owner comment and no clarify PR
    "close-question": False,  # a question whose clarify PR merged
    "dispatch": False,      # spawn an executor for a claimed item
    # -- performed here, as a ledger write ------------------------------------
    "commit-plan": True,
    "supersede": True,
    "claim": True,
    "record-direction": True,
    # -- neither: a statement about why nothing was done -----------------------
    "hold": False,
}


class TickError(Exception):
    """The tick could not be performed."""


# ---------------------------------------------------------------- narrowing

#: Anything that is not a single-line, printable-ish run of characters. The
#: report below is printed, and a caller may well pipe it into a runner's step
#: summary the way ``spec-ci.yml`` pipes ``backpressure``'s — where a value
#: carrying a newline starts a line of its own and a line of its own is all
#: ``::add-mask`` needs. Every string this module prints that came from outside
#: it (an executor name, a question, a briefing, a path in an observed-state
#: file) goes through ``one_line`` first, which lives in ``vellum.text`` because
#: ``release.py`` needs the same rule and two spellings of it would drift.


# ------------------------------------------------------------------ actions


@dataclass(frozen=True)
class Action:
    """One convergent action a tick reached."""

    kind: str
    #: The spec version whose record this is about, or "" where none applies.
    version: str
    #: The work item's issue number, or None.
    item: int | None
    #: One line, already narrowed. Printed and emitted verbatim.
    detail: str

    @property
    def taken(self) -> bool:
        """True when this command performed it; False when it only decided it."""
        return ACTION_KINDS[self.kind]

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "version": self.version,
            "item": self.item,
            "detail": self.detail,
            "taken": self.taken,
        }

    def __str__(self) -> str:
        # Joined from the parts that are there rather than formatted with a gap
        # and squeezed afterwards: `detail` is narrowed text from outside this
        # module, and collapsing runs of spaces inside it would edit the very
        # value the report is quoting.
        parts = [f"[{self.kind}]", self.version[:12] if self.version else "-" * 12]
        if self.item is not None:
            parts.append(f"item {self.item}")
        parts.append(self.detail)
        return " ".join(parts)


@dataclass
class Tick:
    """One reconciliation pass."""

    ledger: Path
    now: datetime.datetime
    #: Records read, by sha, in ledger filename order.
    records: list[str]
    actions: list[Action] = field(default_factory=list)
    #: Ledger files rewritten by this tick.
    written: list[str] = field(default_factory=list)
    #: Ledger files that could not be read as records.
    unreadable: list[str] = field(default_factory=list)
    #: True when the caller supplied observed state at all.
    observed_supplied: bool = False
    #: Questions open past the timebox: ``(version, issue, hours)``.
    parked: list[tuple[str, int, float]] = field(default_factory=list)
    #: Plain statements about how the tick was configured or what it could not see.
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def blocked(self) -> bool:
        """A wave is parked past the question timebox."""
        return bool(self.parked)

    def of_kind(self, kind: str) -> list[Action]:
        return [a for a in self.actions if a.kind == kind]

    def to_dict(self) -> dict:
        return {
            "ledger": str(self.ledger),
            "now": self.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "records": list(self.records),
            "actions": [a.to_dict() for a in self.actions],
            "written": list(self.written),
            "unreadable": list(self.unreadable),
            "observed_supplied": self.observed_supplied,
            "parked": [
                {"version": v, "issue": i, "hours_open": round(h, 2)}
                for v, i, h in self.parked
            ],
            "notes": list(self.notes),
            "dry_run": self.dry_run,
            "parked_wave": self.blocked,
        }

    def report(self) -> str:
        lines = [
            f"Tick at {self.now.strftime('%Y-%m-%dT%H:%M:%SZ')} over {self.ledger}",
            f"  {len(self.records)} record(s), {len(self.actions)} action(s)",
            "",
        ]
        if self.actions:
            taken = sum(1 for a in self.actions if a.taken)
            lines.append(
                f"{taken} action(s) taken here, {len(self.actions) - taken} for the caller:"
            )
            for action in self.actions:
                mark = "written" if action.taken else "caller"
                lines.append(f"  {mark:<8} {action}")
        else:
            lines.append("Nothing to converge: desired and observed state agree.")
        lines.append("")
        if self.written:
            lines.append(
                f"Ledger files written: {', '.join(one_line(n, 80) for n in self.written)}"
            )
            lines.append("")
        elif self.dry_run and any(a.taken for a in self.actions):
            lines.append("--dry-run: nothing was written.")
            lines.append("")
        if self.unreadable:
            # `one_line` for the reason every other outside string in this
            # report gets it: a ledger filename is written by whoever can land a
            # merge on the intent repo, git permits a newline in one, and the
            # `*.yaml` glob matches across it — so an unnarrowed name puts a
            # line of its own into a caller's $GITHUB_STEP_SUMMARY.
            lines.append(
                f"{len(self.unreadable)} file(s) in the ledger could not be read as "
                f"records and were not reconciled: "
                f"{', '.join(one_line(n, 80) for n in self.unreadable)}"
            )
            lines.append("")
        for note in self.notes:
            lines.append(note)
        if self.notes:
            lines.append("")
        if not self.observed_supplied:
            lines.append(
                "Observed state was not supplied. A tick reads the forge half from "
                "--observed; without it nothing is known to be filed, claimed by "
                "anyone else, asked or answered, and the actions above are computed "
                "from repository state alone."
            )
            lines.append("")
        if self.blocked:
            first = self.parked[0]
            lines.append(
                f"PARKED: question issue {first[1]} on wave {first[0][:12]} has been "
                f"open {first[2]:.1f}h, past the timebox. Past the timebox the wave "
                f"parks (spec/features/question-protocol.md); "
                f"{len(self.parked)} question(s) in total."
            )
        else:
            lines.append("OK: the tick converged.")
        return "\n".join(lines)


# ------------------------------------------------------------ observed state


@dataclass
class Observed:
    """The forge half, as the caller supplied it.

    Every field defaults to "nothing seen", and ``supplied`` records whether a
    file was given at all — so "no issues are filed" and "I could not see the
    forge" stay different answers, which is the distinction ``--pending`` draws
    in ``vellum backpressure`` and ``--strict`` draws in ``vellum ledger verify``.
    """

    supplied: bool = False
    #: Work-item issue numbers that exist on the forge.
    issues: set[int] = field(default_factory=set)
    #: Question issues, as supplied. Each is a dict; see ``_parse_questions``.
    questions: list[dict] = field(default_factory=list)
    #: Questions an agent has raised this tick and that no issue exists for yet.
    raised: list[dict] = field(default_factory=list)
    #: New direction recorded for a work item: ``(version, issue, briefing)``.
    directions: list[tuple[str, int, str]] = field(default_factory=list)


def _int_or_none(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _sha_or_empty(value) -> str:
    sha = str(value or "").strip().lower()
    return sha if SHA_RE.match(sha) else ""


def _mappings(data: dict, key: str) -> list[dict]:
    """``data[key]`` as a list of mappings; anything else is refused, not skipped.

    Refused rather than skipped for the reason ``vellum verify deps`` refuses a
    TOML value it cannot read exactly: an observed-state file the caller
    mistyped, silently read as "nothing observed", produces a tick that converges
    on a world that is not there — it would file issues that exist and dispatch
    items somebody is already running.
    """
    value = data.get(key)
    if value in (None, []):
        return []
    if not isinstance(value, list) or any(not isinstance(e, dict) for e in value):
        raise TickError(
            f"observed state: {key!r} is {type(value).__name__}, expected a list of "
            f"mappings. A tick reads the forge only through this file, so a shape "
            f"it cannot read is refused rather than read as 'nothing there'."
        )
    return value


def read_observed(path: str | Path | None) -> Observed:
    """Parse ``--observed``. Absent is "nothing seen", not "nothing there"."""
    if path is None:
        return Observed()
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise TickError(f"{path}: cannot read the observed state: {exc}") from exc
    except yaml.YAMLError as exc:
        raise TickError(f"{path}: observed state is not valid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TickError(f"{path}: observed state is not a YAML mapping")

    observed = Observed(supplied=True)
    # The raw value, never ``or []``: a falsy scalar (``0``, ``false``, ``''``,
    # ``{}``) is a shape this cannot read, and coercing it to "no issues are
    # filed" is the mis-decision the whole refuse-not-misread rule exists to
    # stop — it tells the tick to file an issue for every planned item, all of
    # which already exist. Only absent and the empty list are empty, which is
    # exactly how ``_mappings`` reads its key.
    issues = data.get("issues")
    if issues in (None, []):
        issues = []
    elif not isinstance(issues, list):
        raise TickError(
            f"{path}: 'issues' is {type(issues).__name__}, expected a list of issue "
            f"numbers. A tick reads the forge only through this file, so a shape it "
            f"cannot read is refused rather than read as 'nothing there'."
        )
    for entry in issues:
        number = _int_or_none(entry)
        if number is None:
            raise TickError(f"{path}: 'issues' holds {entry!r}, which is not an issue number")
        observed.issues.add(number)

    observed.questions = _mappings(data, "questions")
    observed.raised = _mappings(data, "raised")
    for entry in _mappings(data, "directions"):
        issue = _int_or_none(entry.get("item"))
        if issue is None:
            raise TickError(f"{path}: a 'directions' entry names no work item")
        observed.directions.append(
            (_sha_or_empty(entry.get("version")), issue, str(entry.get("briefing") or ""))
        )
    return observed


# ------------------------------------------------------------- the corpus


#: Words carrying no discriminating power in a question. Deliberately short: a
#: long stoplist is a place for a term that mattered to disappear, and the cost
#: of keeping a term is a question that escalates to a human, which is the safe
#: direction (see ``corpus_answer``).
_STOPWORDS = frozenset(
    """
    about above after again against because been before being below between both
    could does doing down during each from further have having here into more
    most only other over same should some such than that their them then there
    these they this those through under until very were what when where which
    while will with would your ours
    """.split()
)

#: A word worth matching on: four characters or more, letters first. Shorter
#: tokens ("PR", "id", "v42") are too common to discriminate and too easy to
#: match by accident.
_TERM_RE = re.compile(r"[a-z][a-z0-9_-]{3,}")

#: Fewer significant terms than this and a mechanical answer is a coincidence
#: rather than a match, so the question escalates.
MIN_QUESTION_TERMS = 2

#: What fraction of a question's significant terms a document must carry to be
#: read as answering it. **The spec states the duty and not the rule**
#: (``spec/features/question-protocol.md``: "A question the corpus answers is
#: answered mechanically by the orchestrator"), so the default here is the
#: strictest one there is — every term — and ``--corpus-match`` is the knob for
#: an installation that wants to loosen it. Strict is the right default because
#: the two ways of being wrong are not symmetric: a question wrongly bounced
#: hands an agent an answer that is not the answer, and the mistake lands in
#: code; a question wrongly escalated costs a human one glance at an issue.
DEFAULT_CORPUS_MATCH = 1.0


def question_terms(question: str) -> list[str]:
    """The significant terms of *question*, lowercased and de-duplicated."""
    seen: list[str] = []
    for term in _TERM_RE.findall(str(question or "").lower()):
        if term not in _STOPWORDS and term not in seen:
            seen.append(term)
    return seen


def _corpus_files(checkout: Path) -> list[Path]:
    """The corpus, in the ladder's own order.

    ``spec/features/question-protocol.md`` names it: "the briefing, touched spec
    sections and cross-references, decisions, area notes, the index". The
    briefing is per-item and is searched by ``corpus_answer`` before this list;
    everything else is a file, and they are visited in the order the sentence
    gives them, so that among documents matching equally well the one the ladder
    reaches first is the one quoted back.
    """
    spec = checkout / "spec"
    decisions = spec / "decisions"
    sections = sorted(
        p for p in spec.rglob("*.md")
        # `not in p.parents` rather than `p.parent != decisions`, so a decision
        # filed in a subdirectory is still read at the decisions tier and not
        # ahead of it. The tiers are the ladder's order and a file in the wrong
        # one changes which reference a question is answered with.
        if decisions not in p.parents
    )
    return (
        sections
        + sorted(decisions.glob("*.md"))
        + sorted((checkout / ".vellum" / "memory" / "areas").glob("*.md"))
    )


@dataclass(frozen=True)
class CorpusAnswer:
    """A corpus document that answers a question."""

    #: The reference to reply with: a checkout-relative path, or a sentence
    #: naming the briefing.
    reference: str
    #: The question's significant terms this document carries.
    matched: list[str]
    #: Every significant term the question had.
    terms: list[str]


def corpus_answer(
    checkout: str | Path,
    question: str,
    briefing: str | None = None,
    match: float = DEFAULT_CORPUS_MATCH,
) -> CorpusAnswer | None:
    """The corpus document answering *question*, or None.

    ``@id:corpus-answer-bounces``. A document answers when it carries at least
    *match* of the question's significant terms; the best-scoring document wins,
    and the ladder's order breaks ties. A question with fewer than
    ``MIN_QUESTION_TERMS`` significant terms is never answered mechanically — at
    one term a match is a coincidence — and escalates instead.

    The threshold is the underspecified part and is deliberately a parameter
    rather than a constant buried in a comparison: see ``DEFAULT_CORPUS_MATCH``
    and the spec-clarification request in this wave's PR.
    """
    terms = question_terms(question)
    if len(terms) < MIN_QUESTION_TERMS:
        return None
    # ceil, so a threshold of 0.5 over three terms needs two of them and not
    # one; the epsilon keeps 1.0 * 3 from ceiling to 4 on a float representation.
    needed = min(len(terms), max(MIN_QUESTION_TERMS, math.ceil(match * len(terms) - 1e-9)))
    root = Path(checkout)

    candidates: list[tuple[str, str]] = []
    if briefing:
        candidates.append(("the work item's briefing", str(briefing)))
    for path in _corpus_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            reference = path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - corpus files are under root
            reference = path.name
        candidates.append((reference, text))

    best: CorpusAnswer | None = None
    for reference, text in candidates:
        haystack = text.lower()
        matched = [t for t in terms if t in haystack]
        if len(matched) < needed:
            continue
        if best is None or len(matched) > len(best.matched):
            best = CorpusAnswer(reference=reference, matched=matched, terms=terms)
            if len(matched) == len(terms):
                break
    return best


# ---------------------------------------------------------------- the tick


def _load_record(path: Path) -> dict | None:
    try:
        data = load(path)
    except LedgerError:
        return None
    return data if isinstance(data, dict) else None


def _read_records(ledger: Path) -> tuple[dict[str, tuple[Path, dict]], list[str]]:
    """``{sha: (path, record)}`` and the filenames that are not records."""
    found: dict[str, tuple[Path, dict]] = {}
    unreadable: list[str] = []
    for path in sorted(ledger.glob("*.yaml")):
        if path.name in NOT_A_RECORD:
            continue
        data = _load_record(path)
        if data is None:
            unreadable.append(path.name)
            continue
        sha = _sha_or_empty(data.get("spec_version"))
        if not sha:
            unreadable.append(path.name)
            continue
        found[sha] = (path, data)
    return found, unreadable


def _items(record: dict) -> list[dict]:
    items = record.get("work_items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _claims(item: dict) -> set[str]:
    """The scenario ids an item's ``satisfies:`` claims."""
    return {
        str(ref).strip()[len(SCENARIO_PREFIX):]
        for ref in (item.get("satisfies") or [])
        if str(ref).strip().startswith(SCENARIO_PREFIX)
    }


def _armed(ledger: Path, sha: str) -> set[str]:
    """Scenario ids ``ledger/suite-<sha>.json`` dates to *sha*.

    The criteria a version armed — the same reading ``vellum.chain`` gives the
    word, and the only account of "what this version changed" that is a fact
    about the repository rather than a plan somebody has yet to write.
    """
    path = ledger / f"suite-{sha}.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list):
        return set()
    return {
        s["id"]
        for s in scenarios
        if isinstance(s, dict) and isinstance(s.get("id"), str) and s.get("version") == sha
    }


def _newer(checkout: Path, older: str, newer: str, records: dict) -> tuple[bool, bool]:
    """Is *newer* a later spec version than *older*, and did ancestry say so?

    Ancestry first: "later means descendant" is the ordering the spec relies on
    (``spec/decisions/2026-08-28-versions-are-commits.md``), and shas do not
    compare. Where the checkout cannot answer — a shallow clone, a record for a
    commit this tree does not have, no git at all — the records' own ``approved``
    times are the fallback, which is the only other clock the ledger has. The
    fallback is reported rather than silent, because it is weaker: two versions
    approved in the same second do not order.

    Returns ``(later, by_approved_time)``. The second half is what makes
    "reported" true rather than merely intended: the caller decides a
    **supersede** on this answer — a ledger write that takes an item out of the
    queue — and a weaker clock can order two records wrongly, so a supersede
    resting on the fallback has to leave a trace in the report.
    """
    try:
        return is_ancestor(repo_root(checkout), older, newer), False
    except (GitUnavailable, OSError, ValueError):
        pass
    a = parse_time(records[older][1].get("approved"))
    b = parse_time(records[newer][1].get("approved"))
    return bool(a and b and b > a), True


def _conformed_baseline(ledger: Path, channel: str) -> tuple[str | None, str]:
    """``releases.yaml``'s ``channels.<channel>.spec_conformed``, and why not."""
    path = ledger / "releases.yaml"
    if not path.is_file():
        return None, f"no {path.name}"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return None, f"{path.name} could not be read: {one_line(exc)}"
    channels = (data or {}).get("channels") if isinstance(data, dict) else None
    entry = channels.get(channel) if isinstance(channels, dict) else None
    value = entry.get("spec_conformed") if isinstance(entry, dict) else None
    sha = _sha_or_empty(value)
    if sha:
        return sha, ""
    return None, (
        f"{path.name} records no conformed baseline for channel {channel!r} "
        f"(spec_conformed is {value!r})"
    )


class _Reconciler:
    """One pass. Holds the mutable half so ``reconcile`` reads as a sequence."""

    def __init__(
        self,
        checkout: Path,
        ledger: Path,
        records: dict[str, tuple[Path, dict]],
        observed: Observed,
        now: datetime.datetime,
        executor: str | None,
        lease_minutes: int,
        channel: str,
        corpus_match: float,
    ) -> None:
        self.checkout = checkout
        self.ledger = ledger
        self.records = records
        self.observed = observed
        self.now = now
        self.executor = executor
        self.lease_minutes = lease_minutes
        self.channel = channel
        self.corpus_match = corpus_match
        self.actions: list[Action] = []
        self.notes: list[str] = []
        self.parked: list[tuple[str, int, float]] = []
        #: (version, issue) of items an open question parks this tick.
        self.parked_items: set[tuple[str, int]] = set()
        #: Records this pass changed, by sha.
        self.dirty: set[str] = set()
        #: Version pairs a supersede was decided on without ancestry.
        self.time_ordered: set[tuple[str, str]] = set()
        #: Items leased with nothing confirming their forge issue is filed.
        self.leased_unconfirmed: list[int] = []

    # ------------------------------------------------------------- helpers

    def act(self, kind: str, version: str, item: int | None, detail: str) -> None:
        self.actions.append(Action(kind, version, item, one_line(detail)))

    def touched(self, sha: str) -> None:
        self.dirty.add(sha)

    def note_time_ordered(self, older: str, newer: str) -> None:
        """Say in the report that a supersede rests on the weaker clock.

        ``_newer``'s docstring promises the ancestry fallback "is reported
        rather than silent, because it is weaker", and a supersede is the one
        thing it decides that writes: the item leaves the queue. Two versions
        approved in the same second do not order at all, and a clock skewed
        between two approvals orders them wrongly — so a reader who sees a
        supersede needs to know which of the two answers ordered it. Once per
        pair, because the pair is what was ordered.
        """
        if (older, newer) in self.time_ordered:
            return
        self.time_ordered.add((older, newer))
        self.notes.append(
            f"Ancestry could not order {older[:12]} against {newer[:12]} — a shallow "
            f"clone, or a record for a commit this tree does not have — so their "
            f"'approved' times did, and a supersede rests on that. It is the weaker "
            f"ordering (two versions approved in the same second do not order); check "
            f"the superseded item(s) above if the two were approved close together."
        )

    def _claim(self, sha: str, item: dict, briefing: str | None) -> bool:
        """Claim *item* for this tick's executor, unless it is already claimed.

        The single place ``take_lease``'s effect is reached, and it asks
        ``active_lease`` first. That ordering is the whole of the lease mutex:
        "the reconciler ... treats an expired lease as no lease, returning the
        item to the queue" (``spec/features/ledger.md``) cuts both ways — an
        *unexpired* lease means the item is somebody's, and dispatching it again
        puts two executors on one branch, which is what
        ``@id:fire-and-collect`` forbids.
        """
        held = active_lease(item, now=self.now)
        if held is not None:
            self.act(
                "hold", sha, item.get("issue"),
                f"claimed by {one_line(held.get('executor'), 40)} until "
                f"{one_line(held.get('expires'), 40)}; not re-dispatched",
            )
            return False
        if self.executor is None:
            self.act(
                "dispatch", sha, item.get("issue"),
                "unclaimed and ready; no --executor was named, so no lease was taken",
            )
            return False
        taken = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        expires = (self.now + datetime.timedelta(minutes=self.lease_minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # `taken` is the tick's own moment, not the wall clock `new_lease`
        # defaults to. A tick reconciles *as of* `--now` — that is what resolves
        # every expiry it reads — so a claim it writes stamped from a different
        # clock would put `taken` after `expires` on any replay of an old moment,
        # and the lease's own window would then describe no interval at all.
        item["lease"] = new_lease(self.executor, expires, taken=taken)
        item["state"] = "implementing"
        self.touched(sha)
        self.act("claim", sha, item.get("issue"), f"leased to {self.executor} until {expires}")
        self.act(
            "dispatch", sha, item.get("issue"),
            "spawn a fresh run"
            + (f" with the briefing: {one_line(briefing, 60)}" if briefing else ""),
        )
        return True

    # --------------------------------------------------------------- passes

    def open_records(self) -> list[str]:
        """Records still in the reconciler's window, oldest name first."""
        return [
            sha
            for sha, (_, record) in sorted(self.records.items(), key=lambda kv: kv[1][0].name)
            if str(record.get("state") or "").strip() not in SETTLED_RECORD_STATES
        ]

    def commit_plan(self, sha: str, plan: list[dict]) -> None:
        """Commit a work plan into a record. ``spec/features/orchestration.md``:
        "The plan is committed to the ledger record, then filed as issues."
        """
        path, record = self.records[sha]
        before = dump(record)
        upsert_plan(record, plan)
        if str(record.get("state") or "").strip() == "approved":
            record["state"] = "planning"
        if dump(record) != before:
            self.touched(sha)
            self.act(
                "commit-plan", sha, None,
                f"{len(plan)} work item(s) committed to {path.name}",
            )

    def plan_and_file(self, sha: str) -> None:
        """Approved versions with no plan, and planned items with no issue."""
        path, record = self.records[sha]
        items = _items(record)
        if not items:
            baseline, why = _conformed_baseline(self.ledger, self.channel)
            self.act(
                "plan", sha, None,
                "approved with no work plan; plan against the conformed baseline "
                + (baseline[:12] if baseline else f"— none recorded: {why}"),
            )
            return
        for item in items:
            issue = _int_or_none(item.get("issue"))
            if issue is None:
                continue
            if str(item.get("state") or "").strip() == SUPERSEDED:
                continue
            if issue in self.observed.issues:
                continue
            self.act(
                "file-issue", sha, issue,
                f"{one_line(item.get('title') or 'untitled', 60)} "
                f"({one_line(item.get('repo') or 'no repo', 30)})"
                + ("" if self.observed.supplied else " — no observed issues were supplied"),
            )

    def coalesce(self, open_shas: list[str]) -> None:
        """Wave coalescing. ``spec/features/orchestration.md``: "a newer approved
        version re-plans unstarted overlapping work against the newest target;
        superseded items are marked in the ledger".

        Overlap is decided on criteria: an older item is superseded when what it
        claims intersects what the newer version touches — the newer version's
        own planned claims, plus the scenarios its suite arms. Unstarted is read
        as the spec writes it: state ``planned`` **and** no live lease. An item
        an executor is mid-run on is not unstarted, and a superseded in-flight
        item "stops" by the lease being left to lapse, which is the only stopping
        this side can perform (there is no mid-run channel).
        """
        for newer in open_shas:
            _, new_record = self.records[newer]
            touches = set().union(*(_claims(i) for i in _items(new_record))) if _items(
                new_record
            ) else set()
            touches |= _armed(self.ledger, newer)
            if not touches:
                continue
            for older in open_shas:
                if older == newer:
                    continue
                later, by_time = _newer(self.checkout, older, newer, self.records)
                if not later:
                    continue
                _, old_record = self.records[older]
                for item in _items(old_record):
                    if str(item.get("state") or "").strip() != "planned":
                        continue
                    if active_lease(item, now=self.now) is not None:
                        continue
                    overlap = sorted(_claims(item) & touches)
                    if not overlap:
                        continue
                    item["state"] = SUPERSEDED
                    self.touched(older)
                    self.act(
                        "supersede", older, _int_or_none(item.get("issue")),
                        f"unstarted and overlapping {newer[:12]} on "
                        f"{', '.join(SCENARIO_PREFIX + i for i in overlap)}"
                        + (" (ordered by approved time, not ancestry)" if by_time else ""),
                    )
                    if by_time:
                        self.note_time_ordered(older, newer)

    def directions(self) -> None:
        """New direction for an item: record it, never steer a running executor.

        ``spec/decisions/2026-08-28-fire-and-collect-executors.md``: no mid-run
        channel exists or may be assumed. So the direction is written onto the
        item's ``briefing`` — the ledger's own record of what the agent knew —
        and a live lease is left to lapse. The dispatch that carries the
        direction happens on the tick after expiry, from ``queue()``.
        """
        for version, issue, briefing in self.observed.directions:
            shas = [version] if version in self.records else list(self.records)
            for sha in shas:
                _, record = self.records[sha]
                item = find_item(record, issue)
                if item is None:
                    continue
                if item.get("briefing") != briefing:
                    item["briefing"] = briefing
                    self.touched(sha)
                    self.act(
                        "record-direction", sha, issue,
                        f"briefing updated: {one_line(briefing, 60)}",
                    )
                held = active_lease(item, now=self.now)
                if held is not None:
                    self.act(
                        "hold", sha, issue,
                        f"a run holds this item until {one_line(held.get('expires'), 40)}; "
                        f"there is no mid-run channel, so the lease is left to lapse and "
                        f"a fresh run will carry the new briefing",
                    )
                break

    def questions(self, timebox_hours: float) -> None:
        """Open question issues: clarify drafts, closures, parks and timeboxes."""
        for entry in self.observed.questions:
            issue = _int_or_none(entry.get("issue"))
            if issue is None:
                raise TickError("observed state: a 'questions' entry names no issue number")
            version = _sha_or_empty(entry.get("version"))
            item = _int_or_none(entry.get("item"))
            closed = bool(entry.get("closed"))
            clarify_pr = _int_or_none(entry.get("clarify_pr"))
            merged = bool(entry.get("clarify_merged"))
            # Refused, not downgraded to "no comments". The failure direction
            # here is the safe one — a missed draft-clarify heals next tick —
            # but a shape the tick cannot read is refused everywhere else in
            # this parser, and a rule with one quiet exception is not a rule a
            # caller can rely on.
            comments = entry.get("comments")
            if comments in (None, []):
                comments = []
            elif not isinstance(comments, list):
                raise TickError(
                    f"observed state: question issue {issue}'s 'comments' is "
                    f"{type(comments).__name__}, expected a list. A tick reads the "
                    f"forge only through this file, so a shape it cannot read is "
                    f"refused rather than read as 'nothing there'."
                )

            if merged and not closed:
                # "Merging the micro-PR closes the issue, re-arms the briefing,
                # and resumes the item" (spec/features/question-protocol.md).
                self.act(
                    "close-question", version, item,
                    f"question issue {issue}: its {CLARIFY_LABEL} PR #{clarify_pr} merged; "
                    f"close the issue and resume the item",
                )
                continue
            if closed:
                if not merged and clarify_pr is None:
                    self.notes.append(
                        f"Question issue {issue} was closed with no {CLARIFY_LABEL} PR "
                        f"landed, which counts as withdrawing the question "
                        f"(spec/features/question-protocol.md)."
                    )
                continue

            # Still open: the item it names is parked, and other items continue.
            if item is not None:
                self.parked_items.add((version, item))
            if clarify_pr is None and comments:
                self.act(
                    "draft-clarify", version, item,
                    f"question issue {issue} has {len(comments)} owner comment(s) and no "
                    f"{CLARIFY_LABEL} PR; draft one referencing the issue",
                )
            opened = parse_time(entry.get("opened"))
            if opened is not None:
                hours = (self.now - opened).total_seconds() / 3600.0
                if hours >= timebox_hours:
                    self.parked.append((version, issue, hours))

    def raised(self) -> None:
        """Questions raised this tick: answer from the corpus, or escalate.

        ``@id:corpus-answer-bounces``. The ambiguity ladder's mechanical half:
        answer it and file nothing, or open the question issue and park the item.
        """
        for entry in self.observed.raised:
            question = str(entry.get("asks") or entry.get("question") or "")
            version = _sha_or_empty(entry.get("version"))
            item = _int_or_none(entry.get("item"))
            briefing = None
            if version in self.records and item is not None:
                found = find_item(self.records[version][1], item)
                briefing = (found or {}).get("briefing")
            answer = corpus_answer(
                self.checkout, question, briefing=briefing, match=self.corpus_match
            )
            if answer is not None:
                self.act(
                    "answer-question", version, item,
                    f"answered from the corpus: {one_line(answer.reference, 80)} "
                    f"(carries {len(answer.matched)}/{len(answer.terms)} of the "
                    f"question's terms) — reply with the reference and open no issue",
                )
                continue
            self.act(
                "open-question", version, item,
                f"the corpus does not answer {one_line(question, 60)!r}; open a "
                f"question issue mentioning the owner and park the item",
            )
            if item is not None:
                self.parked_items.add((version, item))

    def queue(self, open_shas: list[str]) -> None:
        """The work-item queue: dispatch what is unclaimed, hold what is claimed."""
        for sha in open_shas:
            _, record = self.records[sha]
            for item in _items(record):
                issue = _int_or_none(item.get("issue"))
                state = str(item.get("state") or "").strip()
                if state not in DISPATCHABLE_ITEM_STATES:
                    continue
                if item.get("pr") not in (None, ""):
                    continue  # reported: its PR is the forge's to merge
                if issue is not None and (sha, issue) in self.parked_items:
                    self.act(
                        "hold", sha, issue,
                        "parked behind an open question; other items continue",
                    )
                    continue
                if issue is not None and self.observed.supplied and issue not in self.observed.issues:
                    self.act(
                        "hold", sha, issue,
                        "its issue is not filed yet; nothing is dispatched before the "
                        "work item exists on the forge",
                    )
                    continue
                # A claim is the one action here that *writes* on an unverified
                # fact: with no observed state nothing says this item's issue was
                # ever filed, and the lease goes on regardless. The claim is still
                # taken — "absent observed = repository state alone" is this
                # command's contract, and the lease mutex is a property of
                # repository state that must hold with no forge in reach — but it
                # is named, so the foot-gun cannot fire quietly.
                # Asked of the return value, not before the call: `_claim`
                # writes no lease for an item somebody already holds, and none
                # at all without an executor to name. Only a lease that was
                # really taken belongs in this note.
                leased = self._claim(sha, item, item.get("briefing"))
                if leased and issue is not None and not self.observed.supplied:
                    self.leased_unconfirmed.append(issue)
        if self.leased_unconfirmed:
            items = ", ".join(str(i) for i in self.leased_unconfirmed)
            self.notes.append(
                f"A claim wants observed state. Work item(s) {items} were leased and "
                f"dispatched with no --observed, so nothing confirmed their forge "
                f"issues exist — a lease is a write, and this is the one action a "
                f"tick takes on a fact it could not check. Pass --observed to have an "
                f"unfiled item held instead of claimed."
            )


def reconcile(
    checkout: str | Path,
    ledger_dir: str | Path | None = None,
    observed: str | Path | None = None,
    plan: list[dict] | None = None,
    version: str | None = None,
    executor: str | None = None,
    lease_minutes: int = DEFAULT_LEASE_MINUTES,
    timebox_hours: float | None = None,
    channel: str | None = None,
    corpus_match: float = DEFAULT_CORPUS_MATCH,
    now: datetime.datetime | None = None,
    dry_run: bool = False,
) -> Tick:
    """One reconciliation pass over the intent checkout at *checkout*.

    Idempotent by construction: every action is computed from state, and a
    record is written only when this pass actually changed its bytes. Running a
    tick twice over an unchanged world writes nothing the second time, which is
    the property decision D11 asks for and the reason a missed webhook costs
    latency and not correctness.
    """
    root = Path(checkout)
    ledger = Path(ledger_dir) if ledger_dir is not None else ledger_dir_for(root)
    if not ledger.is_dir():
        raise TickError(
            f"{ledger}: no ledger directory. A tick reconciles ledger records, so "
            f"there is nothing here to converge; is {checkout} an intent checkout?"
        )
    if not 0.0 < corpus_match <= 1.0:
        raise TickError(
            f"--corpus-match must be greater than 0 and at most 1 (got {corpus_match}). "
            f"At or below 0 every document 'answers' every question, which would "
            f"bounce every question back with a reference to whatever file sorted "
            f"first."
        )
    if lease_minutes <= 0:
        raise TickError(
            f"--lease-minutes must be positive (got {lease_minutes}). A lease that "
            f"expires when it is taken claims nothing, and `active_lease` would read "
            f"it as free on the very next tick."
        )
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)

    records, unreadable = _read_records(ledger)
    seen = read_observed(observed)

    if version is not None:
        wanted = str(version).strip().lower()
        matched = [s for s in records if s.startswith(wanted) or wanted.startswith(s)]
        if len(matched) != 1:
            raise TickError(
                f"--version {version!r} names {len(matched)} of the {len(records)} "
                f"record(s) in {ledger}; give a sha that names exactly one."
            )
        records = {matched[0]: records[matched[0]]}

    notes: list[str] = []
    if timebox_hours is None:
        try:
            questions = load_config(root).get("questions")
            configured = questions.get("timebox_hours") if isinstance(questions, dict) else None
        except ConfigError:
            configured = None
        if isinstance(configured, (int, float)) and not isinstance(configured, bool):
            timebox_hours = float(configured)
        else:
            timebox_hours = float(DEFAULT_TIMEBOX_HOURS)
            notes.append(
                f"No questions.timebox_hours in the installation config; using the "
                f"spec's default of {DEFAULT_TIMEBOX_HOURS}h "
                f"(spec/features/question-protocol.md)."
            )

    engine = _Reconciler(
        checkout=root,
        ledger=ledger,
        records=records,
        observed=seen,
        now=moment,
        executor=executor,
        lease_minutes=lease_minutes,
        channel=channel or "production",
        corpus_match=corpus_match,
    )
    engine.notes.extend(notes)
    if executor is not None:
        engine.notes.append(
            f"Claims are taken for {one_line(executor, 40)} and last "
            f"{lease_minutes} minute(s). No spec sentence or config key gives a lease "
            f"duration, so that number is this command's, not the installation's."
        )

    open_shas = engine.open_records()
    if plan is not None:
        if len(records) != 1:
            raise TickError(
                "--plan commits a work plan into one record; pass --version to say "
                "which. A plan is produced for a version, and committing one into "
                "every open record would file the same work several times."
            )
        engine.commit_plan(next(iter(records)), plan)
        open_shas = engine.open_records()

    engine.coalesce(open_shas)
    engine.directions()
    engine.questions(timebox_hours)
    engine.raised()
    for sha in open_shas:
        engine.plan_and_file(sha)
    engine.queue(open_shas)

    written: list[str] = []
    if not dry_run:
        for sha in sorted(engine.dirty):
            path, record = records[sha]
            path.write_text(dump(record), encoding="utf-8")
            written.append(path.name)

    return Tick(
        ledger=ledger,
        now=moment,
        records=sorted(records),
        actions=engine.actions,
        written=written,
        unreadable=unreadable,
        observed_supplied=seen.supplied,
        parked=engine.parked,
        notes=engine.notes,
        dry_run=dry_run,
    )


def run(
    checkout: str,
    ledger_dir: str | None = None,
    observed: str | None = None,
    plan: list[dict] | None = None,
    version: str | None = None,
    executor: str | None = None,
    lease_minutes: int = DEFAULT_LEASE_MINUTES,
    timebox_hours: float | None = None,
    channel: str | None = None,
    corpus_match: float = DEFAULT_CORPUS_MATCH,
    now: datetime.datetime | None = None,
    dry_run: bool = False,
    as_json: bool = False,
    out=None,
) -> int:
    """Reconcile, report, and exit 1 when a wave is parked past the timebox.

    ``--json`` puts the payload on stdout and every diagnostic on stderr, so a
    parked tick still parses — the property ``vellum budget --json`` and ``suite
    extract -o -`` already have.
    """
    stream = out if out is not None else sys.stdout
    tick = reconcile(
        checkout,
        ledger_dir=ledger_dir,
        observed=observed,
        plan=plan,
        version=version,
        executor=executor,
        lease_minutes=lease_minutes,
        timebox_hours=timebox_hours,
        channel=channel,
        corpus_match=corpus_match,
        now=now,
        dry_run=dry_run,
    )
    if as_json:
        print(json.dumps(tick.to_dict(), indent=1), file=stream)
    else:
        print(tick.report(), file=stream)
    if tick.blocked:
        version_sha, issue, hours = tick.parked[0]
        print(
            f"vellum: tick — wave {version_sha[:12] or '(unnamed)'} is parked: question "
            f"issue {issue} has been open {hours:.1f}h, past the timebox "
            f"({len(tick.parked)} question(s) past it in total)",
            file=sys.stderr,
        )
        return 1
    return 0
