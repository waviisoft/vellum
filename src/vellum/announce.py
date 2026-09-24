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

**``announce handoff`` is the ledger holder's act, done on the sender's
behalf.** It writes into ``ledger/`` — a handoff record and the announcing
item's ``announced:`` field — and nothing a sender does not already hold write
access to under fire-and-collect is touched by it. ``--from`` is attribution
only: under fire-and-collect the orchestrator is the one that records the
handoff when it collects a blocked run, and the sender named is who raised it,
never who ran this command.

**Delivery** is what turns a pending announcement into an addressed ``dispatch``
action. It happens twice over, and the difference between the two is the whole
point of the feature:

* ``vellum announce handoff``, ``vellum announce finished`` and ``vellum
  announce direction`` deliver it *themselves*, in the same act that records
  it. That is the push — the worker announces, the dispatch is the
  announcement's own consequence, and **no reconciler pass is involved at
  all**. Which transport carries the command's output onward is an
  installation's (a webhook, a claim daemon, a workflow):
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
plan is the ledger, so they go to the ledger's holder — which is why that
holder, the orchestrator, is who this module says commissions a run: it is the
only writer of the ledger, and the ledger is what a claim comes from. ``--to``
overrides it wherever an installation knows better.
"""

from __future__ import annotations

import dataclasses
import datetime
import errno
import fcntl
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from vellum import paths as _paths
from vellum.config import CONFIG_RELPATH, ConfigError
from vellum.config import load as load_config
from vellum.ledger import (
    LedgerError,
    clean_run_reference,
    find_item,
    find_record,
    load,
    now as ledger_now,
    ordered,
    write,
)
from vellum.product import ProductFileError, role_trees, under
from vellum.text import one_line


class AnnounceError(Exception):
    """An announcement could not be recorded, read or delivered."""


#: The directory a handoff record lands in, relative to the ledger directory.
#: One file per handoff: a handoff "asks for the one thing that unblocks one
#: unit of work", so one record is one ask, and a receiver reads exactly the one
#: it was dispatched about.
HANDOFF_DIRNAME = "handoffs"

#: The lock file a delivery holds for its whole read-modify-write cycle
#: (SS6). ``dispatched`` is authoritative only once it is *committed* — a
#: reader between two concurrent deliveries' read and write sees a record that
#: has not been written yet, and without this lock both would see "pending"
#: and both would dispatch.
LOCK_NAME = ".announce.lock"

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
                "answered", "answered_by")

#: ``ledger/handoffs/0001-a-slug.md``. Numbered so the tree reads in the order
#: the handoffs were raised, and slugged so a reader knows what one is about
#: before opening it.
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NAME_RE = re.compile(r"^(\d{4})-")

#: A handoff record's own name shape (SB2). Accepted only in exactly this form
#: — never an absolute path, never one carrying a ``/`` of its own — because a
#: name reaches a filesystem join (``handoff_dir(ledger_dir) / name``) and a
#: string like ``../../spec/0001-fix-it.md`` is not a handoff's name, it is a
#: traversal wearing one.
HANDOFF_NAME_RE = re.compile(r"^\d{4}-[a-z0-9-]+\.md$")

#: A handoff record, read whole, capped well above three evidence fields at
#: their own cap plus the markdown this module wraps them in (SS7). A cap
#: rather than no limit at all: an unreadable-in-good-faith file must not be
#: read as a promise to consume the disk trying.
MAX_HANDOFF_FILE_BYTES = 1 << 20  # 1 MiB

#: One evidence field's own cap (S5): verbatim, not one-lined, and capped
#: rather than silently truncated — a truncated proof is not the proof, and a
#: cap the caller cannot see get applied is worse than a refusal they can act
#: on.
MAX_EVIDENCE_BYTES = 64 * 1024

#: Control characters this module refuses in text that reaches a terminal or a
#: CI log (SN1). ``\n`` and ``\t`` are carved out for the evidence bodies —
#: prose needs a line break — and everything else in this range is the family
#: ``\x1b[2J`` belongs to: cursor moves, screen clears, OSC sequences a
#: terminal or a log viewer executes rather than displays.
_BAD_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: A URL-shaped substring inside a larger piece of prose (SN2) — as opposed to
#: ``ledger.clean_run_reference``, which reads a whole value as one URL. An
#: evidence field is prose that may *contain* a link a run followed, not a
#: link itself.
_URL_RE = re.compile(r"\w+://\S+")


def _sans_dispatched(announcement: dict) -> dict:
    return {k: v for k, v in announcement.items() if k != "dispatched"}


def _refuse_controls(argname: str, text) -> None:
    value = str(text or "")
    found = _BAD_CONTROL_RE.search(value)
    if found:
        raise AnnounceError(
            f"--{argname} carries a control character ({found.group(0)!r}) other "
            f"than newline or tab: {one_line(value)!r}. A value like this reaches "
            f"a terminal or a CI log as itself, and an escape sequence there is a "
            f"command, not a description."
        )


def _cap_evidence(argname: str, text) -> str:
    value = str(text or "")
    size = len(value.encode("utf-8"))
    if size > MAX_EVIDENCE_BYTES:
        raise AnnounceError(
            f"--{argname} is {size} bytes, over the {MAX_EVIDENCE_BYTES}-byte cap "
            f"on one handoff evidence field; refused rather than truncated, "
            f"because a truncated proof is not the proof"
        )
    return value


def _scrub_credentials(argname: str, text: str, notes: list[str]) -> str:
    """Strip ``user:token@`` from any URL inside *text* (SN2)."""

    def repl(match: re.Match) -> str:
        cleaned, removed = clean_run_reference(match.group(0))
        if "userinfo" in removed:
            notes.append(
                f"--{argname} carried a URL with a credential in its userinfo; "
                f"stripped it before recording — rotate that credential."
            )
            return cleaned or match.group(0)
        return match.group(0)

    return _URL_RE.sub(repl, text)


def _check_evidence_path(value: str) -> str:
    """*value* as a repo-relative path a handoff may propose a change to."""
    text = str(value)
    if not text.strip():
        raise AnnounceError(
            "--path names no file: a handoff proposes a change to a path in "
            "this repository"
        )
    if not text.isprintable() or text != text.strip():
        raise AnnounceError(
            f"--path {one_line(text)!r} is not a printable path with no "
            f"surrounding whitespace"
        )
    pure = PurePosixPath(text)
    if pure.is_absolute():
        raise AnnounceError(
            f"--path {text!r} is absolute; a handoff proposes a change inside "
            f"this checkout"
        )
    parts = pure.parts
    if ".." in parts:
        raise AnnounceError(f"--path {text!r} climbs out of the checkout with '..'")
    if parts and parts[0] == ".git":
        raise AnnounceError(
            f"--path {text!r} is under .git/, which is not this repository's "
            f"content"
        )
    return text


def _slug(text: str, limit: int = 48) -> str:
    made = _SLUG_RE.sub("-", str(text or "").lower()).strip("-")
    return (made[:limit].rstrip("-") or "handoff")


# --------------------------------------------------------------- the address

def declared_boundaries(checkout: str | Path) -> dict[str, list[str]]:
    """``{role: [tree, ...]}`` as this installation's config declares it.

    Read through ``product.role_trees`` — the same reader
    ``vellum.config.write_boundaries`` calls for one role — so there is one
    reader of the ``write_boundaries`` shape rather than a second spelling of
    its per-entry validation that could drift from the guard's (N5).
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
    for role in declared:
        try:
            found[str(role)] = role_trees(declared, str(role), path=path)
        except ProductFileError as exc:
            raise AnnounceError(str(exc)) from exc
    return found


def holders(checkout: str | Path, tree: str) -> list[str]:
    """Every declared role whose trees cover *tree*, in declaration order.

    Matched with ``product.under``, which is the component-wise rule the guard
    states for its own entries — so this answers the question the guard answers
    rather than a looser one of its own, and ``src`` never admits ``srcs/``.
    *tree* is taken whole (S6/SS4): a full path like ``spec/features/auth.md``
    is matched against every declared tree, so a role declaring the narrower
    ``spec/features`` is found for it — truncating to the path's first
    component would test ``spec`` instead, which no role need declare for its
    subtree to be held.
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


def require_role(checkout: str | Path, role: str, argname: str) -> str:
    """*role* as a declared role, printable and unpadded (SB3, SS1).

    The one check every role-shaped argument goes through — ``--to`` and
    ``--from`` on a handoff, ``--to`` on a finished announcement — so a value
    like ``$'owner\\n::add-mask::x'`` is refused here rather than reaching a
    terminal or a CI log after being accepted as an addressee nobody declared.
    """
    text = str(role)
    if not text or not text.isprintable() or text != text.strip():
        raise AnnounceError(
            f"{argname} {one_line(text)!r} is not a printable role name with no "
            f"surrounding whitespace; a role is data an installation declares "
            f"(spec/features/roles.md)"
        )
    declared = declared_boundaries(checkout)
    if text not in declared:
        raise AnnounceError(
            f"this installation declares no role {text!r}; it declares "
            f"{', '.join(sorted(declared)) or '(nothing)'}. {argname} names a "
            f"role, and a role is data an installation declares "
            f"(spec/features/roles.md)"
        )
    return text


def addressee_for_ledger(checkout: str | Path, ledger_dir: str | Path) -> str:
    """Who a finished run, or new direction, is addressed to.

    **The commissioner of a run is the role that holds the ledger.** The
    orchestrator is the only writer of the ledger — it claims every item under
    a lease — so the ledger's declared holder is the one party a finished run
    or new direction can be said to have been commissioned by, on the same
    rule a handoff uses: an announcement goes to the role that holds the tree
    the act it asks for must be written in, and the act here is the wave's
    plan moving, which is a write to the ledger.

    *checkout* must be named explicitly by the caller (S4) — this never
    derives it from ``ledger_dir``'s parent, because a ``--ledger-dir`` is not
    obliged to sit inside the checkout its config lives in, and guessing that
    it does would read the wrong installation's ``write_boundaries`` (or none)
    without saying so.
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


def pending_announcements(item: dict) -> list[dict]:
    """Announcements a superseding one displaced before they were delivered.

    **S1: superseding is per addressee.** ``set_announcement`` replaces
    ``announced:`` with the newest event, and that is right when both are
    addressed to the same role — the newest is what that role should act on.
    It is wrong when they differ: a blocked run that also opened a pull
    request has raised news for two roles, and overwriting the first with the
    second would dispatch nobody for it. So a standing, undelivered
    announcement addressed to a *different* role than the one superseding it
    is kept here instead of dropped, and delivered on the next pass exactly
    as ``announced:`` itself would be.
    """
    found = item.get("announced_pending")
    return [e for e in found if isinstance(e, dict)] if isinstance(found, list) else []


def is_pending(item: dict) -> bool:
    """True when this item has an announcement nothing has dispatched yet."""
    found = announced(item)
    return found is not None and not found.get("dispatched")


def set_announcement(item: dict, announcement: dict) -> bool:
    """Put *announcement* on *item*, superseding whatever stood there.

    **One pending announcement per addressee, and the newest wins for that
    addressee (B2, S1).** Compared with ``dispatched`` excluded: an
    announcement already delivered and one just like it in every other way are
    the same event, and rewriting the record to say ``dispatched: false``
    again would redeliver it — ``announce finished --pr 7`` run twice must
    dispatch once, not twice.

    A standing announcement addressed to a role *other* than the new one, and
    not yet dispatched, is not overwritten in place: it is kept in
    ``announced_pending`` so its own delivery still happens (S1). One
    addressed to the *same* role is superseded outright — the newest is what
    that role should act on — and one already dispatched is superseded too,
    since nothing is lost by that.

    Returns True when this actually changed the item, so a caller writes a
    record only when a byte of it moved — ``vellum tick``'s D11 idempotence.
    """
    standing = announced(item)
    if standing is not None and _sans_dispatched(standing) == _sans_dispatched(announcement):
        return False
    if (standing is not None and not standing.get("dispatched")
            and str(standing.get("to") or "") != str(announcement.get("to") or "")):
        pending = item.setdefault("announced_pending", [])
        pending.append(standing)
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
    answered_by: str = ""
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
            "answered_by": self.answered_by,
        }


def handoff_dir(ledger_dir: str | Path) -> Path:
    return Path(ledger_dir) / HANDOFF_DIRNAME


def _next_number(tree: Path) -> int:
    """The next handoff number: every directory entry counts (SB1).

    Including a dangling symlink — an attacker-planted
    ``0001-fix-it.md -> ~/.ssh/authorized_keys`` still names ``0001``, and a
    count that skipped it (the way ``is_file()`` would, since it follows a
    link and a dangling one is never a file) would hand a fresh handoff the
    very name that link occupies.
    """
    if not tree.is_dir():
        return 1
    seen = [int(m.group(1)) for m in
            (_NAME_RE.match(p.name) for p in tree.iterdir()) if m]
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
    lines.append(f"answered_by: {handoff.answered_by}".rstrip())
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
    about the ledger: this reads scalars and one list of strings out of a
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


def valid_handoff_name(name: object) -> bool:
    """True when *name* is a handoff record's own name and nothing else (SB2).

    ``name == Path(name).name`` refuses anything carrying a ``/`` of its own —
    an absolute path, or a relative one climbing out with ``..`` — before the
    shape check even runs, and the shape check refuses everything that is not
    literally ``NNNN-slug.md``.
    """
    return (isinstance(name, str) and name == Path(name).name
            and bool(HANDOFF_NAME_RE.fullmatch(name)))


def read_handoff(path: Path) -> Handoff:
    """Read one handoff record.

    Capped (SS7): a handoff over ``MAX_HANDOFF_FILE_BYTES`` and one that is
    not valid UTF-8 both refuse rather than read, as ``AnnounceError`` — never
    a raw ``OSError`` or ``UnicodeDecodeError`` an uninvolved caller (``vellum
    tick``, reading every handoff a wave's items name) is not written to
    expect.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AnnounceError(f"{path}: cannot read handoff record: {exc}") from exc
    if len(raw) > MAX_HANDOFF_FILE_BYTES:
        raise AnnounceError(
            f"{path}: {len(raw)} bytes, over the {MAX_HANDOFF_FILE_BYTES}-byte "
            f"cap on a handoff record; refused rather than read"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AnnounceError(f"{path}: not valid UTF-8: {exc}") from exc
    front = _frontmatter(text)
    raw_item = str(front.get("item") or "").strip()
    return Handoff(
        name=path.name,
        to=str(front.get("to") or ""),
        sender=str(front.get("from") or ""),
        version=str(front.get("version") or ""),
        # `isdecimal()`, not `isdigit()` (SN3): "²".isdigit() is True and
        # int("²") raises, so a handoff record carrying that character in
        # `item:` would crash the read rather than read back `item: None`.
        item=int(raw_item) if raw_item.isdecimal() else None,
        paths=list(front.get("paths") or []),
        asks=str(front.get("asks") or ""),
        recorded=str(front.get("recorded") or ""),
        answered=str(front.get("answered") or ""),
        answered_by=str(front.get("answered_by") or ""),
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
    """The handoff record named *name*, or None.

    Never through a symlink, never outside ``handoffs/``, and never for a
    name that is not one of these records' own shape (SB1, SB2): a caller
    handing this an ``--handoff`` value straight from the command line, or a
    ``handoff:`` field straight out of a ledger record, gets None for
    anything that is not a legitimate name — the same answer as "no such
    record" — rather than a path this reads or writes through.
    """
    if not valid_handoff_name(name):
        return None
    refusal = _paths.unsafe_read(Path(ledger_dir), f"{HANDOFF_DIRNAME}/{name}")
    if refusal is not None:
        return None
    return read_handoff(handoff_dir(ledger_dir) / name)


def _handoff_dir_refusal(checkout: str | Path, ledger_dir: str | Path) -> str | None:
    """Why the handoffs directory is not one this may write into, or None.

    Bounded by *checkout* when ``ledger_dir`` sits lexically inside it (the
    ordinary shape), and by ``ledger_dir`` itself otherwise — either way, a
    ``ledger/handoffs`` that is itself a symlink (to ``../.git/hooks``, say)
    is refused before ``mkdir(parents=True)`` can silently create nothing and
    write through it (SB1).
    """
    checkout = Path(checkout)
    ledger_dir = Path(ledger_dir)
    try:
        relative = (ledger_dir.relative_to(checkout) / HANDOFF_DIRNAME).as_posix()
        root = checkout
    except ValueError:
        root, relative = ledger_dir, HANDOFF_DIRNAME
    refusal = _paths.write_refusal(root, relative)
    if refusal is None:
        return None
    code, subject = refusal
    return f"{subject} ({code})"


def _create_handoff(ledger_dir: str | Path, checkout: str | Path, base: Handoff) -> Handoff:
    """Create a new handoff record, retrying the next number as needed.

    ``os.O_EXCL | os.O_NOFOLLOW`` (SB1): the file must not already exist, and
    if the name is occupied by a symlink — dangling or not — this never opens
    through it. Either way the fix is the same: try the next number. That also
    closes the ordinary concurrent-name race, where two callers compute the
    same ``_next_number()`` before either has written: the second one's
    ``O_EXCL`` open fails with ``EEXIST`` and it moves on rather than
    clobbering the first.
    """
    refusal = _handoff_dir_refusal(checkout, ledger_dir)
    if refusal is not None:
        raise AnnounceError(
            f"{handoff_dir(ledger_dir)}: refused — {refusal}. A handoff record "
            f"is written inside the ledger and nowhere a symlink can redirect "
            f"it."
        )
    tree = handoff_dir(ledger_dir)
    tree.mkdir(parents=True, exist_ok=True)
    for _ in range(10_000):
        name = f"{_next_number(tree):04d}-{_slug(base.asks)}.md"
        handoff = dataclasses.replace(base, name=name)
        target = tree / name
        content = render_handoff(handoff).encode("utf-8")
        try:
            fd = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
            )
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                continue  # a symlink already occupies this name; skip past it
            raise
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return handoff
    raise AnnounceError(f"{tree}: no free handoff number found after 10000 tries")


def write_handoff(ledger_dir: str | Path, handoff: Handoff, checkout: str | Path) -> Path:
    """Rewrite an *existing* handoff record in place — ``answer``'s write.

    Refuses a symlinked record outright rather than writing through it
    (SB1): an existing record this did not just create is only ever rewritten
    to add an answer, and a name that resolves to something outside
    ``handoffs/`` is not this record any more, whatever wrote it there.
    """
    tree = handoff_dir(ledger_dir)
    path = tree / handoff.name
    if path.is_symlink():
        raise AnnounceError(
            f"{path} is a symlink; an existing handoff record is never "
            f"rewritten through one"
        )
    refusal = _handoff_dir_refusal(checkout, ledger_dir)
    if refusal is not None:
        raise AnnounceError(f"{path}: refused — {refusal}")
    content = render_handoff(handoff).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, content)
    finally:
        os.close(fd)
    return path


def answer_handoff(
    ledger_dir: str | Path,
    checkout: str | Path,
    name: str,
    by: str | None = None,
    at: str | None = None,
) -> Path:
    """Mark a handoff answered: the addressed role has acted on it.

    What makes "an answered handoff dispatches nobody" a fact about a record
    rather than about somebody's memory. Idempotent — answering twice leaves the
    first answer's time in place, because the question is *whether* it was acted
    on and a second stamp would rewrite a record nothing changed.

    *by* is optional and defaults to the handoff's own addressee — a caller
    that already knows who it dispatched (a transport collecting the
    addressed role's own run) need not repeat it. Given explicitly, it must
    be the addressee or the role that holds the ledger (N3): anyone else
    recording an answer is not evidence that the addressed role acted, which
    is the one fact this record exists to carry — and never the handoff's own
    sender, ledger holder or not, since a sender answering its own handoff is
    the self-clearance a handoff must not become.
    """
    handoff = find_handoff(ledger_dir, name)
    if handoff is None:
        raise AnnounceError(
            f"{handoff_dir(ledger_dir) / str(name)}: no handoff by that name. This "
            f"checkout records {', '.join(h.name for h in handoffs(ledger_dir)) or '(none)'}"
        )
    if by is None:
        recorded_by = handoff.to
    else:
        if by == handoff.sender:
            raise AnnounceError(
                f"{by!r} raised this handoff ({handoff.name}) and may not also "
                f"answer it — that is the self-clearance a handoff must not become"
            )
        holder = None
        try:
            holder = addressee_for_ledger(checkout, ledger_dir)
        except AnnounceError:
            holder = None
        if by != handoff.to and by != holder:
            raise AnnounceError(
                f"{by!r} may not answer a handoff addressed to {handoff.to!r}: only "
                f"the addressee, or the role that holds the ledger "
                f"({holder or '(none declared)'}), may record that it was acted on"
            )
        recorded_by = by
    if not handoff.is_answered:
        handoff.answered = at or ledger_now()
        handoff.answered_by = recorded_by
        write_handoff(ledger_dir, handoff, checkout)
    return handoff_dir(ledger_dir) / name


# ------------------------------------------------------- recording an event

def _record_for(ledger_dir: str | Path, version: str) -> tuple[Path, dict]:
    if not str(version).isprintable():
        raise AnnounceError(
            f"--version {one_line(str(version))!r} is not printable; a spec "
            f"version is a commit sha"
        )
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
    """Record that the addressed dispatch for this item's main announcement has
    been emitted. Kept for a direct caller; ``deliver`` below writes back
    through the live record it already holds instead of calling this, which is
    what lets it mark an ``announced_pending`` entry too and an item whose
    ``issue`` is ``null`` (N2/SS8, where a lookup by issue could never find it
    again).
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
    notes: list[str] | None = None,
) -> tuple[Path, Handoff]:
    """Record a blocked run's handoff, and announce it against its work item.

    The addressee is computed from the proposed change when ``to`` is absent: the
    role this installation declares as the holder of the tree the change lies in,
    which is the only reading of "a role that may make the change" that is a fact
    about the installation rather than a choice made here.

    **Idempotent by identity (B1).** A handoff's identity is
    ``(version, item, to, asks, sorted paths)``; an identical arrival reuses the
    record it already wrote rather than creating a second one and a second
    dispatch — the arrival of the *same* handoff twice runs its receiver once,
    same as the arrival of an answered one runs it zero times.

    Validated before anything is written (SS2): version and item are resolved
    against the ledger first, so a refusal after that point never leaves an
    orphan record behind.
    """
    path, record = _record_for(ledger_dir, version)
    found = find_item(record, item)
    if found is None:
        raise AnnounceError(
            f"{path.name} has no work item {item}; an announcement is about a unit "
            f"of work, and this record's items are "
            f"{', '.join(str(i.get('issue')) for i in record.get('work_items') or []) or '(none)'}"
        )

    declared = declared_boundaries(checkout)
    sender = require_role(checkout, sender, "--from")

    _refuse_controls("asks", asks)
    _refuse_controls("tried", tried)
    _refuse_controls("observed", observed)
    _refuse_controls("proved", proved)
    if not str(tried or "").strip() or not str(proved or "").strip():
        raise AnnounceError(
            "a handoff carries what was tried and what was proved — the "
            "evidence a receiver verifies rather than rediscovers. An ask with "
            "none of that is a question, and goes by the question protocol "
            "instead (spec/features/question-protocol.md)"
        )
    tried = _cap_evidence("tried", tried)
    observed = _cap_evidence("observed", observed)
    proved = _cap_evidence("proved", proved)
    local_notes: list[str] = []
    tried = _scrub_credentials("tried", tried, local_notes)
    observed = _scrub_credentials("observed", observed, local_notes)
    proved = _scrub_credentials("proved", proved, local_notes)
    if notes is not None:
        notes.extend(local_notes)

    proposed = [_check_evidence_path(p) for p in (paths or []) if str(p).strip()]

    if to is None:
        if not proposed:
            raise AnnounceError(
                "a handoff proposes a change, so it needs --path (the change it is "
                "about) to read a holder off, or --to (the role it is for). "
                "Without either there is nothing to address it to"
            )
        wanted = {addressee(checkout, p) for p in proposed}
        if len(wanted) > 1:
            raise AnnounceError(
                f"the proposed change reaches trees held by {', '.join(sorted(wanted))}. "
                f"A handoff asks for the one thing that unblocks one unit of work, so "
                f"it is addressed to one role — split it, or name the addressee with --to"
            )
        to = wanted.pop()
    else:
        to = require_role(checkout, to, "--to")

    if to == sender:
        raise AnnounceError(
            f"a handoff addressed back to {sender}, the role that raised it, asks "
            f"the blocked run to unblock itself — which is the stall this record "
            f"exists to end. Address it to the role that holds the tree"
        )
    if not declared.get(to):
        raise AnnounceError(
            f"write_boundaries.{to} declares no tree; an addressee must hold at "
            f"least one tree to receive a handoff about a change "
            f"(spec/behaviors/write-boundaries.md)"
        )
    for p in proposed:
        if to not in holders(checkout, p):
            raise AnnounceError(
                f"{to!r} does not hold {p}; a handoff addressed to {to} must be "
                f"the declared holder of every path it proposes "
                f"(spec/behaviors/write-boundaries.md)"
            )

    asks_norm = one_line(asks, 200)
    identity_paths = tuple(sorted(proposed))
    existing = _matching_handoff(ledger_dir, version, item, to, asks_norm, identity_paths)
    if existing is not None:
        return handoff_dir(ledger_dir) / existing.name, existing

    template = Handoff(
        name="", to=to, sender=sender, version=version, item=item, paths=proposed,
        asks=asks_norm, recorded=at or ledger_now(), answered="", answered_by="",
        tried=tried, observed=observed, proved=proved,
    )
    handoff = _create_handoff(ledger_dir, checkout, template)
    record_announcement(
        ledger_dir, version, item,
        new_announcement("handoff", to, handoff.asks, handoff=handoff.name),
    )
    return handoff_dir(ledger_dir) / handoff.name, handoff


def _matching_handoff(
    ledger_dir: str | Path,
    version: str,
    item: int,
    to: str,
    asks_norm: str,
    paths_norm: tuple[str, ...],
) -> Handoff | None:
    """An existing handoff with this exact identity, or None (B1)."""
    wanted = (str(version), item, to, asks_norm, paths_norm)
    for existing in handoffs(ledger_dir):
        found = (
            str(existing.version), existing.item, existing.to,
            one_line(existing.asks, 200), tuple(sorted(existing.paths)),
        )
        if found == wanted:
            return existing
    return None


def record_direction(
    ledger_dir: str | Path,
    checkout: str | Path,
    version: str,
    item: int,
    briefing: str,
    to: str | None = None,
    at: str | None = None,
) -> tuple[Path, dict]:
    """Record the owner's direction against a work item, and announce it.

    Mirrors ``reconcile.directions()``'s own write — the item's ``briefing``
    field, updated only when it actually changed — so a webhook can record and
    deliver direction in one act (S2) without waiting for a tick, and the tick
    path stays exactly what it was for the installations that still poll it as
    a fallback.
    """
    path, record = _record_for(ledger_dir, version)
    found = find_item(record, item)
    if found is None:
        raise AnnounceError(
            f"{path.name} has no work item {item}; an announcement is about a unit "
            f"of work, and this record's items are "
            f"{', '.join(str(i.get('issue')) for i in record.get('work_items') or []) or '(none)'}"
        )
    role = require_role(checkout, to, "--to") if to else addressee_for_ledger(checkout, ledger_dir)
    changed_briefing = found.get("briefing") != briefing
    if changed_briefing:
        found["briefing"] = briefing
    changed_announcement = set_announcement(found, new_announcement("direction", role, briefing))
    if changed_briefing or changed_announcement:
        write(path, record)
    return path, found


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
    #: The live ``(path, record)`` and announcement dict this came from, for
    #: ``deliver`` to write back through directly (N2/SS8) — never by looking
    #: the item back up by ``issue``, which an item with ``issue: null`` (or a
    #: duplicate) could not be found by a second time.
    record: tuple[Path, dict] | None = field(default=None, repr=False, compare=False)
    slot: dict | None = field(default=None, repr=False, compare=False)


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


def _withheld_reason(ledger_dir: str | Path, slot: dict) -> str:
    name = str(slot.get("handoff") or "").strip()
    if not name:
        return ""
    try:
        recorded = find_handoff(ledger_dir, name)
    except AnnounceError:
        return ""
    if recorded is not None and recorded.is_answered:
        return (
            f"handoff {name} was answered on {recorded.answered}; a handoff "
            f"already acted on dispatches nobody"
        )
    return ""


def deliveries(
    ledger_dir: str | Path,
    item: int | None = None,
    handoff: str | None = None,
    version: str | None = None,
) -> list[Delivery]:
    """Every announcement standing undispatched, as the dispatch it would cause.

    Read-only: what is *deliverable* is a question about the record, and asking
    it must not be the thing that answers it. ``--version`` (N2/SS8) narrows to
    one record the way every other announce command already does, so a
    transport that knows which wave it is delivering for is not made to scan
    every open one.
    """
    found: list[Delivery] = []
    for path, record in _records(ledger_dir):
        rec_version = str(record.get("spec_version") or "")
        if version is not None and rec_version != version:
            continue
        for entry in record.get("work_items") or []:
            if not isinstance(entry, dict):
                continue
            issue = entry.get("issue")
            if item is not None and issue != item:
                continue
            slots: list[dict] = []
            standing = announced(entry)
            if standing is not None:
                slots.append(standing)
            slots.extend(pending_announcements(entry))
            for slot in slots:
                if slot.get("dispatched"):
                    continue
                name = str(slot.get("handoff") or "").strip()
                if handoff is not None and name != handoff:
                    continue
                role = str(slot.get("to") or "").strip()
                if not role:
                    continue
                withheld = _withheld_reason(ledger_dir, slot)
                issue_int = issue if isinstance(issue, int) and not isinstance(issue, bool) else None
                found.append(Delivery(rec_version, issue_int, role,
                                      dispatch_detail(slot), withheld,
                                      record=(path, record), slot=slot))
    return found


@contextmanager
def _locked(ledger_dir: str | Path):
    """Hold an exclusive lock over one delivery's whole read-modify-write cycle.

    SS6: two concurrent ``announce deliver`` runs both read "pending" before
    either writes "dispatched", and both then dispatch. ``dispatched`` is
    authoritative only once it is *committed* to the record — this is what
    makes that true in the presence of a second process doing the same read.
    """
    tree = Path(ledger_dir)
    tree.mkdir(parents=True, exist_ok=True)
    lock_path = tree / LOCK_NAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def deliver(
    ledger_dir: str | Path,
    item: int | None = None,
    handoff: str | None = None,
    version: str | None = None,
) -> tuple[list[Delivery], list[Delivery]]:
    """Dispatch what is pending, once. Returns ``(dispatched, withheld)``.

    The half that writes: every announcement this actually delivered is marked
    ``dispatched`` in the record, so the next reader of it — this function again,
    a reconciler pass, the same transport redelivering a webhook — emits nothing.
    That is "dispatch is idempotent and terminates", made a property of the
    record rather than of anybody's memory.

    Marks the live announcement dict ``deliveries`` already holds a reference
    to, rather than looking the item back up by ``issue`` (N2/SS8): an item
    whose ``issue`` is ``null`` could never be found that way twice, which
    would redispatch it on every call forever. The whole read-modify-write
    cycle runs under one lock (SS6).
    """
    with _locked(ledger_dir):
        sent: list[Delivery] = []
        held: list[Delivery] = []
        touched: dict[Path, dict] = {}
        for delivery in deliveries(ledger_dir, item=item, handoff=handoff, version=version):
            if delivery.withheld:
                held.append(delivery)
                continue
            if delivery.slot is not None:
                delivery.slot["dispatched"] = True
            if delivery.record is not None:
                path, record = delivery.record
                touched[path] = record
            sent.append(delivery)
        for path, record in touched.items():
            write(path, record)
    return sent, held


def utc(value: str | None) -> datetime.datetime | None:
    """Parse an ISO moment the way the ledger does, for a caller's ``--now``."""
    from vellum.ledger import parse_time

    return parse_time(value) if value else None


__all__ = [
    "ANNOUNCED_KEYS", "ANNOUNCEMENT_KINDS", "AnnounceError", "HANDOFF_DIRNAME",
    "HANDOFF_NAME_RE", "Handoff", "MAX_EVIDENCE_BYTES", "MAX_HANDOFF_FILE_BYTES",
    "addressee", "addressee_for_ledger", "announced", "answer_handoff",
    "declared_boundaries", "dispatch_detail", "find_handoff", "handoff_dir",
    "handoffs", "holders", "is_pending", "mark_dispatched", "new_announcement",
    "pending_announcements", "read_handoff", "record_announcement",
    "record_direction", "record_handoff", "render_handoff", "require_role",
    "set_announcement", "valid_handoff_name", "write_handoff", "Delivery",
    "deliver", "deliveries",
]
