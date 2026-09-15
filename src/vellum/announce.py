"""``vellum announce`` — the addressed event, and the dispatch its arrival causes.

``spec/decisions/2026-09-12-collection-is-caused-not-scheduled.md`` finds that
the defect behind every stall it measured "is not an absent channel. It is an
absent **cause**." Direction already landed in the forge and a fresh run already
picked it up in its briefing; what was never said is *who starts that fresh run,
or when*, and the answer today is a person who notices or a timer that fires.

So this module supplies the cause, and nothing else. Three things live here:

**An announcement** is a durable, addressed record that a run writes at its own
boundary — it has finished, or it is blocked and has stopped. It names the role
it is addressed to and what it asks for, and it records whether it has been
dispatched yet. It is a work item's own field (``announced:``), because the
announcement is a fact about that unit of work and the ledger is where facts
about a unit of work already live.

**A handoff** is the one announcement an agent authors rather than the forge
emitting it for free: a blocked run's proposal, addressed to the role that holds
the tree the fix lies in, carrying the evidence that produced it. It is a file of
its own under ``ledger/handoffs/`` because it carries prose — what was tried,
what was observed, what was proven — that a receiver reads, and because "a
handoff is durable and lives in the forge".

**Delivery** is what turns a pending announcement into an addressed ``dispatch``
action. It happens twice over, and the difference between the two is the whole
point of the feature:

* ``vellum announce handoff`` and ``vellum announce finished`` deliver it
  *themselves*, in the same act that records it. That is the push — the worker
  announces, the dispatch is the announcement's own consequence, and **no
  reconciler pass is involved at all**. Which transport carries the command's
  output onward is an installation's (a webhook, a claim daemon, a workflow):
  ``spec/features/continuous-engineering.md`` puts the wire out of scope and
  keeps the push in it.
* ``vellum tick`` drains whatever is still pending, as the **fallback**. That is
  the reconciler's own rule — "missed events cost latency, never correctness"
  (``spec/decisions/2026-08-28-reconciler.md``) — and it is not the mechanism: a
  pass over a world that announced nothing addresses nobody, which is what makes
  the dispatch a function of the announcement rather than of the scan.

What this module does not do, and will not
------------------------------------------
**It opens no channel into a running agent.** An announcement is written at a
run's boundary and read out of the repository afterwards;
``spec/decisions/2026-08-28-fire-and-collect-executors.md`` survives clause for
clause, and nothing here addresses a run that is still going.

**It widens no boundary.** Recording a handoff writes the handoff record and the
announcing item's ``announced:`` field, and nothing in the tree the handoff is
*about*. The sender proposes; the role that holds the tree writes. The guard
that says so is ``vellum verify boundaries``, and it is unchanged — a handoff
grants nobody reach they did not declare.

**It does not decide which role must act on an owner's review.** That is
undeclared: a work item names a repo, a title and the slices it satisfies, and
nothing maps any of that to a party (see ``addressee_for_ledger`` below, and the
pull request that landed this). What this module does instead is read an
addressee out of data the installation already declares — the ``write_boundaries``
block — under one rule that covers all three kinds of event:

    **An announcement is addressed to the role that holds the tree in which the
    act it asks for must be written.**

A handoff asks for a change in a tree, so it goes to that tree's holder. A
finished run and new direction both ask for the wave's plan to move, and the
plan is the ledger, so they go to the ledger's holder. ``--to`` overrides it
wherever an installation knows better. That is a placeholder for "the role that
commissioned it" and it is deliberately a narrow one: it decides nothing the
spec has not, and it is computed from the installation's own declaration rather
than invented here.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path

from vellum.config import CONFIG_RELPATH, ConfigError
from vellum.config import load as load_config
from vellum.ledger import (
    LedgerError,
    find_item,
    find_record,
    load,
    now as ledger_now,
    ordered,
    write,
)
from vellum.product import ProductFileError, normalise_tree, under
from vellum.text import one_line


class AnnounceError(Exception):
    """An announcement could not be recorded, read or delivered."""


#: The directory a handoff record lands in, relative to the ledger directory.
#: One file per handoff: a handoff "asks for the one thing that unblocks one unit
#: of work", so one record is one ask, and a receiver reads exactly the one it
#: was dispatched about.
HANDOFF_DIRNAME = "handoffs"

#: The three kinds of event that carry an addressee
#: (``spec/features/continuous-engineering.md``): a run finishes, a run blocks
#: and hands off, and the owner says something.
ANNOUNCEMENT_KINDS = ("finished", "handoff", "direction")

#: ``announced:`` on a work item, in the order it is written. ``dispatched`` is a
#: boolean rather than a timestamp deliberately: what the idempotence rule needs
#: to know is *whether* the receiver has been started, and a clock in a ledger
#: record is a byte that differs between two runs of the same world.
ANNOUNCED_KEYS = ("kind", "to", "asks", "handoff", "dispatched")

#: The frontmatter keys of a handoff record, in the order they are written.
#: ``to`` is first because the addressee is what makes the record deliverable at
#: all, and ``from`` is beside it because a handoff addressed back to its sender
#: is the stall this feature exists to end.
HANDOFF_KEYS = ("to", "from", "version", "item", "paths", "asks", "recorded",
                "answered")

#: ``ledger/handoffs/0001-a-slug.md``. Numbered so the tree reads in the order
#: the handoffs were raised, and slugged so a reader knows what one is about
#: before opening it.
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NAME_RE = re.compile(r"^(\d{4})-")


def _slug(text: str, limit: int = 48) -> str:
    made = _SLUG_RE.sub("-", str(text or "").lower()).strip("-")
    return (made[:limit].rstrip("-") or "handoff")


# --------------------------------------------------------------- the address

def declared_boundaries(checkout: str | Path) -> dict[str, list[str]]:
    """``{role: [tree, ...]}`` as this installation's config declares it.

    Read through ``product.normalise_tree`` so a tree-widening entry is refused
    here exactly as ``vellum verify boundaries`` refuses it — one reader for one
    block, rather than a second spelling of the same rule that could drift from
    the guard's.
    """
    path = Path(checkout) / CONFIG_RELPATH
    try:
        declared = load_config(checkout).get("write_boundaries")
    except ConfigError as exc:
        raise AnnounceError(str(exc)) from exc
    if not isinstance(declared, dict):
        raise AnnounceError(
            f"{path}: no write_boundaries mapping. An announcement is addressed to "
            f"a role, and a role is data an installation declares — this checkout "
            f"declares none, so there is no address space to send one into "
            f"(spec/features/roles.md)."
        )
    found: dict[str, list[str]] = {}
    for role, trees in declared.items():
        if not isinstance(trees, list):
            raise AnnounceError(
                f"{path}: write_boundaries.{role} is {trees!r}; expected a list of trees"
            )
        try:
            found[str(role)] = [
                normalise_tree(entry, path=path, where=f"write_boundaries.{role}")
                for entry in trees
            ]
        except ProductFileError as exc:
            raise AnnounceError(str(exc)) from exc
    return found


def holders(checkout: str | Path, tree: str) -> list[str]:
    """Every declared role whose trees cover *tree*, in declaration order.

    Matched with ``product.under``, which is the component-wise rule the guard
    states for its own entries — so this answers the question the guard answers
    rather than a looser one of its own, and ``src`` never admits ``srcs/``.
    """
    wanted = "/".join(p for p in str(tree).strip("/").split("/") if p)
    return [role for role, trees in declared_boundaries(checkout).items()
            if any(under(wanted, declared) for declared in trees)]


def addressee(checkout: str | Path, tree: str) -> str:
    """The one declared role that may write *tree*.

    **Asserted to be exactly one rather than chosen**, and that is a finding
    rather than a nicety. ``write_boundaries`` is a map from role to trees and
    nothing anywhere refuses two roles declaring overlapping ones, so "the role
    that may make the change" is a sentence that presumes something no guard
    holds. Where an installation declares two, this refuses and names both,
    because a product that picked one would be deciding an installation's
    question on its behalf — and ``--to`` is how the installation answers it.
    """
    covering = holders(checkout, tree)
    if not covering:
        raise AnnounceError(
            f"no declared role may write {tree}/ in this installation, so there is "
            f"nobody to address this to. write_boundaries declares "
            f"{_declared(checkout)}. Name the addressee with --to, or declare the "
            f"tree's holder (spec/behaviors/write-boundaries.md)."
        )
    if len(covering) > 1:
        raise AnnounceError(
            f"write_boundaries declares {', '.join(covering)} as roles that may "
            f"write {tree}/, and an announcement is addressed to one role. Nothing "
            f"in the product makes a tree's holder unique, so this is not a choice "
            f"to make here — name the addressee with --to."
        )
    return covering[0]


def _declared(checkout: str | Path) -> str:
    try:
        return ", ".join(sorted(declared_boundaries(checkout))) or "(nothing)"
    except AnnounceError:
        return "(nothing readable)"


def addressee_for_ledger(checkout: str | Path, ledger_dir: str | Path) -> str:
    """Who a finished run, or new direction, is addressed to.

    **The one place the undeclared question is answered, and it answers a
    narrower one.** ``spec/features/continuous-engineering.md`` says a finished
    run dispatches "the role that commissioned it" and a review dispatches "the
    role that must act on it"; nothing in the product records either. A work
    item names a repo, a title and the slices it satisfies, and the only party
    anywhere in its block is the lease's executor — the role doing the work,
    never the role that asked for it.

    So this does not invent that map. It applies the same rule a handoff uses —
    the announcement goes to the role that holds the tree the act it asks for
    must be written in — and for both of these the act is the wave's plan
    moving, which is a write to the ledger. ``--to`` overrides it. The broader
    rule is a question for the architect and is raised as one.
    """
    root = Path(checkout).resolve()
    ledger = Path(ledger_dir).resolve()
    try:
        tree = ledger.relative_to(root).as_posix()
    except ValueError:
        # A ledger directory outside the checkout it is reconciled with: there is
        # no tree in this repository for it, so there is nothing to read a holder
        # off. Named rather than defaulted, for `role_trees`'s reason.
        raise AnnounceError(
            f"{ledger} is not inside {root}, so no declared tree holds it and an "
            f"announcement about it cannot be addressed. Name the addressee with --to."
        ) from None
    return addressee(root, tree)


# ------------------------------------------------------- the announcement

def new_announcement(kind: str, to: str, asks: str, handoff: str = "") -> dict:
    """One ``announced:`` block, in the emission order ``ANNOUNCED_KEYS`` gives."""
    if kind not in ANNOUNCEMENT_KINDS:
        raise AnnounceError(
            f"{kind!r} is not an announcement kind ({', '.join(ANNOUNCEMENT_KINDS)})"
        )
    return ordered({
        "kind": kind,
        "to": to,
        "asks": one_line(asks, 200),
        "handoff": handoff,
        "dispatched": False,
    }, ANNOUNCED_KEYS)


def announced(item: dict) -> dict | None:
    """The announcement standing against *item*, or None."""
    found = item.get("announced")
    return found if isinstance(found, dict) else None


def is_pending(item: dict) -> bool:
    """True when this item has an announcement nothing has dispatched yet."""
    found = announced(item)
    return found is not None and not found.get("dispatched")


def set_announcement(item: dict, announcement: dict) -> bool:
    """Put *announcement* on *item*, superseding whatever stood there.

    **One pending announcement per unit of work, and the newest wins.** An
    announcement says what changed about this item and who must act on it; two
    standing at once would dispatch the receiving role twice for one unit, which
    is the spawn loop "dispatch is idempotent and terminates" is about. It would
    also be the worse half of that: an owner's review that arrives after a run
    finished is strictly newer direction, and dispatching for the finish as well
    would send the role to act on state the review has already overtaken.

    Returns True when this actually changed the item, so a caller writes a record
    only when a byte of it moved — ``vellum tick``'s D11 idempotence.
    """
    if announced(item) == announcement:
        return False
    item["announced"] = announcement
    return True


def dispatch_detail(announcement: dict) -> str:
    """What the addressed dispatch says, which is the announcement's own ask.

    Carried rather than pointed at: a dispatch that said direction existed and
    left the receiver to go and find it is "a pass that went looking" wearing the
    sender's name, and the receiver's job is to verify rather than to rediscover.
    """
    return one_line(announcement.get("asks") or "", 200)


# ---------------------------------------------------------- handoff records

@dataclass
class Handoff:
    """One handoff record, as it is written and as it is read back."""

    name: str
    to: str
    sender: str
    version: str
    item: int | None
    paths: list[str] = field(default_factory=list)
    asks: str = ""
    recorded: str = ""
    answered: str = ""
    tried: str = ""
    observed: str = ""
    proved: str = ""

    @property
    def is_answered(self) -> bool:
        return bool(str(self.answered or "").strip())

    def to_dict(self) -> dict:
        return {
            "name": self.name, "to": self.to, "from": self.sender,
            "version": self.version, "item": self.item, "paths": list(self.paths),
            "asks": self.asks, "recorded": self.recorded, "answered": self.answered,
        }


def handoff_dir(ledger_dir: str | Path) -> Path:
    return Path(ledger_dir) / HANDOFF_DIRNAME


def _next_number(tree: Path) -> int:
    if not tree.is_dir():
        return 1
    seen = [int(m.group(1)) for m in
            (_NAME_RE.match(p.name) for p in tree.iterdir() if p.is_file()) if m]
    return (max(seen) + 1) if seen else 1


def render_handoff(handoff: Handoff) -> str:
    """The record, as YAML frontmatter over the evidence in the run's own words.

    Frontmatter because the fields a receiver is *routed* by — the addressee
    above all — must be readable without reading prose, and markdown below it
    because the evidence is prose: "what was tried, what was observed, and what
    was proven, in the words of the run that found it". The forge renders it, a
    person reads it, and ``read_handoff`` reads it back.
    """
    lines = ["---", f"to: {handoff.to}", f"from: {handoff.sender}"]
    if handoff.version:
        lines.append(f"version: {handoff.version}")
    if handoff.item is not None:
        lines.append(f"item: {handoff.item}")
    lines.append("paths:")
    for path in handoff.paths:
        lines.append(f"  - {path}")
    lines.append(f"asks: {handoff.asks}")
    lines.append(f"recorded: {handoff.recorded}")
    # `answered:` with nothing after it rather than `answered: `, so an
    # unanswered handoff carries no trailing whitespace — a byte a reviewer's
    # editor strips, which would make the record differ from itself.
    lines.append(f"answered: {handoff.answered}".rstrip())
    lines += ["---", "", f"# Handoff: {handoff.asks}", ""]
    if handoff.item is not None:
        lines += [
            f"Work item {handoff.item} could not proceed by itself. The change it "
            f"needs lies in a tree `{handoff.sender}` may not write, so this asks "
            f"`{handoff.to}` — the role this installation declares as that tree's "
            f"holder — to make it.",
            "",
        ]
    lines += [
        "The sender proposes and the holder writes: nothing in this record grants "
        "its sender reach it did not declare "
        "(spec/behaviors/write-boundaries.md).",
        "",
        "## What was tried", "", handoff.tried, "",
        "## What was observed", "", handoff.observed, "",
        "## What was proved", "", handoff.proved, "",
        "## The change this asks for", "",
    ]
    lines += [f"- `{path}`" for path in handoff.paths] or ["- (no path named)"]
    lines.append("")
    return "\n".join(lines)


def _frontmatter(text: str) -> dict[str, object]:
    """The record's frontmatter, read flat.

    Flat rather than through a YAML parser for ``vellum.reconcile``'s reason
    about the ledger: this reads five scalars and one list of strings out of a
    file this module wrote, and a parser would make the reader's answer depend
    on YAML's rules for prose it is not reading.
    """
    found: dict[str, object] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return found
    key = ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("  - ") and key == "paths":
            found.setdefault("paths", []).append(line[4:].strip())
            continue
        name, sep, value = line.partition(":")
        if not sep or name != name.strip():
            continue
        key = name.strip()
        if key == "paths":
            found["paths"] = []
            continue
        found[key] = value.strip()
    return found


def _section(text: str, heading: str) -> str:
    """One ``## heading`` section's body, as one paragraph."""
    found = re.search(rf"(?m)^## {re.escape(heading)}\n(.*?)(?=\n## |\Z)", text,
                      flags=re.DOTALL)
    return found.group(1).strip() if found else ""


def read_handoff(path: Path) -> Handoff:
    text = path.read_text(encoding="utf-8")
    front = _frontmatter(text)
    raw_item = str(front.get("item") or "").strip()
    return Handoff(
        name=path.name,
        to=str(front.get("to") or ""),
        sender=str(front.get("from") or ""),
        version=str(front.get("version") or ""),
        item=int(raw_item) if raw_item.isdigit() else None,
        paths=list(front.get("paths") or []),
        asks=str(front.get("asks") or ""),
        recorded=str(front.get("recorded") or ""),
        answered=str(front.get("answered") or ""),
        tried=_section(text, "What was tried"),
        observed=_section(text, "What was observed"),
        proved=_section(text, "What was proved"),
    )


def handoffs(ledger_dir: str | Path) -> list[Handoff]:
    """Every handoff this checkout records, in name order."""
    tree = handoff_dir(ledger_dir)
    if not tree.is_dir():
        return []
    return [read_handoff(path) for path in sorted(tree.iterdir()) if path.is_file()]


def find_handoff(ledger_dir: str | Path, name: str) -> Handoff | None:
    path = handoff_dir(ledger_dir) / name
    return read_handoff(path) if path.is_file() else None


def write_handoff(ledger_dir: str | Path, handoff: Handoff) -> Path:
    tree = handoff_dir(ledger_dir)
    tree.mkdir(parents=True, exist_ok=True)
    path = tree / handoff.name
    path.write_text(render_handoff(handoff), encoding="utf-8")
    return path


def answer_handoff(ledger_dir: str | Path, name: str, at: str | None = None) -> Path:
    """Mark a handoff answered: the addressed role has acted on it.

    What makes "an answered handoff dispatches nobody" a fact about a record
    rather than about somebody's memory. Idempotent — answering twice leaves the
    first answer's time in place, because the question is *whether* it was acted
    on and a second stamp would rewrite a record nothing changed.
    """
    handoff = find_handoff(ledger_dir, name)
    if handoff is None:
        raise AnnounceError(
            f"{handoff_dir(ledger_dir) / name}: no handoff by that name. This "
            f"checkout records {', '.join(h.name for h in handoffs(ledger_dir)) or '(none)'}"
        )
    if not handoff.is_answered:
        handoff.answered = at or ledger_now()
        write_handoff(ledger_dir, handoff)
    return handoff_dir(ledger_dir) / name


# ------------------------------------------------------- recording an event

def _record_for(ledger_dir: str | Path, version: str) -> tuple[Path, dict]:
    path = find_record(ledger_dir, version)
    if path is None:
        raise AnnounceError(
            f"no ledger record for {version} in {ledger_dir}; a run announces "
            f"against the wave its work item belongs to, so the record has to exist"
        )
    return path, load(path)


def record_announcement(
    ledger_dir: str | Path,
    version: str,
    item: int,
    announcement: dict,
) -> tuple[Path, dict]:
    """Put *announcement* on a work item, and write the record if it moved."""
    path, record = _record_for(ledger_dir, version)
    found = find_item(record, item)
    if found is None:
        raise AnnounceError(
            f"{path.name} has no work item {item}; an announcement is about a unit "
            f"of work, and this record's items are "
            f"{', '.join(str(i.get('issue')) for i in record.get('work_items') or []) or '(none)'}"
        )
    if set_announcement(found, announcement):
        write(path, record)
    return path, found


def mark_dispatched(ledger_dir: str | Path, version: str, item: int) -> bool:
    """Record that the addressed dispatch for this item has been emitted.

    The durable half of "dispatch is idempotent and terminates": the next reader
    of this record — a tick, another transport, the same command run twice — sees
    an announcement that has already started its receiver and emits nothing.
    """
    path, record = _record_for(ledger_dir, version)
    found = find_item(record, item)
    standing = announced(found) if found is not None else None
    if standing is None or standing.get("dispatched"):
        return False
    standing["dispatched"] = True
    write(path, record)
    return True


def record_handoff(
    ledger_dir: str | Path,
    checkout: str | Path,
    version: str,
    item: int,
    sender: str,
    asks: str,
    tried: str,
    observed: str,
    proved: str,
    paths: list[str] | None = None,
    to: str | None = None,
    at: str | None = None,
) -> tuple[Path, Handoff]:
    """Record a blocked run's handoff, and announce it against its work item.

    The addressee is computed from the proposed change when ``to`` is absent: the
    role this installation declares as the holder of the tree the change lies in,
    which is the only reading of "a role that may make the change" that is a fact
    about the installation rather than a choice made here.
    """
    proposed = [p for p in (paths or []) if str(p).strip()]
    if to is None:
        if not proposed:
            raise AnnounceError(
                "a handoff proposes a change, so it needs --path (the change it is "
                "about) to read a holder off, or --to (the role it is for). Without "
                "either there is nothing to address it to"
            )
        wanted = {addressee(checkout, Path(p).parts[0]) for p in proposed}
        if len(wanted) > 1:
            raise AnnounceError(
                f"the proposed change reaches trees held by {', '.join(sorted(wanted))}. "
                f"A handoff asks for the one thing that unblocks one unit of work, so "
                f"it is addressed to one role — split it, or name the addressee with --to"
            )
        to = wanted.pop()
    else:
        declared = declared_boundaries(checkout)
        if to not in declared:
            raise AnnounceError(
                f"this installation declares no role {to!r}; it declares "
                f"{', '.join(sorted(declared)) or '(nothing)'}. A handoff is "
                f"addressed to a role, and a role is data an installation declares "
                f"(spec/features/roles.md)"
            )
    if to == sender:
        raise AnnounceError(
            f"a handoff addressed back to {sender}, the role that raised it, asks "
            f"the blocked run to unblock itself — which is the stall this record "
            f"exists to end. Address it to the role that holds the tree"
        )

    tree = handoff_dir(ledger_dir)
    handoff = Handoff(
        name=f"{_next_number(tree):04d}-{_slug(asks)}.md",
        to=to,
        sender=sender,
        version=version,
        item=item,
        paths=proposed,
        asks=one_line(asks, 200),
        recorded=at or ledger_now(),
        answered="",
        tried=one_line(tried, 400),
        observed=one_line(observed, 400),
        proved=one_line(proved, 400),
    )
    path = write_handoff(ledger_dir, handoff)
    record_announcement(
        ledger_dir, version, item,
        new_announcement("handoff", to, handoff.asks, handoff=handoff.name),
    )
    return path, handoff


# ------------------------------------------------------------------ delivery

@dataclass(frozen=True)
class Delivery:
    """One pending announcement, resolved into the dispatch it would cause."""

    version: str
    item: int | None
    role: str
    detail: str
    #: Why it was not delivered, or "" when it was. An answered handoff is the
    #: one case: "a handoff already acted on dispatches nobody".
    withheld: str = ""


def _records(ledger_dir: str | Path) -> list[tuple[Path, dict]]:
    """Every ledger record, oldest filename first.

    The same glob and the same exclusion ``vellum tick`` reads records with, so a
    transport delivering an announcement and a pass delivering the same one are
    looking at the same set of files.
    """
    from vellum.backpressure import NOT_A_RECORD

    found = []
    for path in sorted(Path(ledger_dir).glob("*.yaml")):
        if path.name in NOT_A_RECORD:
            continue
        try:
            record = load(path)
        except LedgerError:
            continue
        if isinstance(record, dict) and record.get("spec_version"):
            found.append((path, record))
    return found


def deliveries(
    ledger_dir: str | Path,
    item: int | None = None,
    handoff: str | None = None,
) -> list[Delivery]:
    """Every announcement standing undispatched, as the dispatch it would cause.

    Read-only: what is *deliverable* is a question about the record, and asking
    it must not be the thing that answers it.
    """
    found: list[Delivery] = []
    for _, record in _records(ledger_dir):
        version = str(record.get("spec_version") or "")
        for entry in record.get("work_items") or []:
            if not isinstance(entry, dict):
                continue
            standing = announced(entry)
            if standing is None or standing.get("dispatched"):
                continue
            issue = entry.get("issue")
            name = str(standing.get("handoff") or "").strip()
            if item is not None and issue != item:
                continue
            if handoff is not None and name != handoff:
                continue
            role = str(standing.get("to") or "").strip()
            if not role:
                continue
            withheld = ""
            if name:
                recorded = find_handoff(ledger_dir, name)
                if recorded is not None and recorded.is_answered:
                    withheld = (
                        f"handoff {name} was answered on {recorded.answered}; a "
                        f"handoff already acted on dispatches nobody"
                    )
            found.append(Delivery(version, issue, role,
                                  dispatch_detail(standing), withheld))
    return found


def deliver(
    ledger_dir: str | Path,
    item: int | None = None,
    handoff: str | None = None,
) -> tuple[list[Delivery], list[Delivery]]:
    """Dispatch what is pending, once. Returns ``(dispatched, withheld)``.

    The half that writes: every announcement this actually delivered is marked
    ``dispatched`` in the record, so the next reader of it — this function again,
    a reconciler pass, the same transport redelivering a webhook — emits nothing.
    That is "dispatch is idempotent and terminates", made a property of the
    record rather than of anybody's memory.
    """
    sent: list[Delivery] = []
    held: list[Delivery] = []
    for delivery in deliveries(ledger_dir, item=item, handoff=handoff):
        if delivery.withheld:
            held.append(delivery)
            continue
        if delivery.item is not None:
            mark_dispatched(ledger_dir, delivery.version, delivery.item)
        sent.append(delivery)
    return sent, held


def utc(value: str | None) -> datetime.datetime | None:
    """Parse an ISO moment the way the ledger does, for a caller's ``--now``."""
    from vellum.ledger import parse_time

    return parse_time(value) if value else None


__all__ = [
    "ANNOUNCED_KEYS", "ANNOUNCEMENT_KINDS", "AnnounceError", "HANDOFF_DIRNAME",
    "Handoff", "addressee", "addressee_for_ledger", "announced", "answer_handoff",
    "declared_boundaries", "dispatch_detail", "find_handoff", "handoff_dir",
    "handoffs", "holders", "is_pending", "mark_dispatched", "new_announcement",
    "read_handoff", "record_announcement", "record_handoff", "render_handoff",
    "set_announcement", "write_handoff", "Delivery", "deliver", "deliveries",
]
