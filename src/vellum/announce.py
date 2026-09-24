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
dispatched yet. It lives in a work item's own append-only log (``announcements:``),
because the announcement is a fact about that unit of work and the ledger is
where facts about a unit of work already live. The log is never rewritten or
pruned, only appended to, and an arrival is deduplicated by its own ``id`` —
the same event recorded twice (a replayed handoff, a re-run ``ledger advance
--pr``) lands once, and two different events addressed to two different roles
both survive rather than one overwriting the other.

**A handoff** is the one announcement an agent authors rather than the forge
emitting it for free: a blocked run's proposal, addressed to the role that holds
the tree the fix lies in, carrying the evidence that produced it. It is a file of
its own under ``ledger/handoffs/`` because it carries prose — what was tried,
what was observed, what was proven — that a receiver reads, and because "a
handoff is durable and lives in the forge".

**``announce handoff`` is the ledger holder's act, done on the sender's
behalf.** It writes into ``ledger/`` — a handoff record and an entry appended
to the announcing item's ``announcements:`` log — and nothing a sender does not already hold write
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

**It widens no boundary.** Recording a handoff writes the handoff record and
appends to the announcing item's ``announcements:`` log, and nothing in the tree the handoff is
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

import contextlib
import dataclasses
import datetime
import errno
import hashlib
import os
import re
import tempfile
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
    locked as ledger_locked,
    now as ledger_now,
    ordered,
    parse_time,
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

#: The three kinds of event that carry an addressee
#: (``spec/features/continuous-engineering.md``): a run finishes, a run blocks
#: and hands off, and the owner says something.
ANNOUNCEMENT_KINDS = ("finished", "handoff", "direction")

#: One entry of a work item's ``announcements:`` log, in the order it is
#: written. ``dispatched`` is a boolean rather than a timestamp deliberately:
#: what the idempotence rule needs to know is *whether* the receiver has been
#: started, and a clock in a ledger record is a byte that differs between two
#: runs of the same world. ``id`` is first because it is what the log
#: deduplicates by; ``settled`` is last and omitted entirely when absent — an
#: entry is only ever born or later marked "self" or "answered", never
#: anything else.
ANNOUNCEMENT_KEYS = ("id", "kind", "to", "asks", "handoff", "dispatched", "settled")

#: The frontmatter keys of a handoff record, in the order they are written.
#: ``to`` is first because the addressee is what makes the record deliverable at
#: all, and ``from`` is beside it because a handoff addressed back to its sender
#: is the stall this feature exists to end.
HANDOFF_KEYS = ("to", "from", "version", "item", "paths", "asks", "asks_sha256",
                "recorded", "answered", "answered_by")

#: ``ledger/handoffs/0001-a-slug.md``. Numbered so the tree reads in the order
#: the handoffs were raised, and slugged so a reader knows what one is about
#: before opening it.
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# `\d{4,}` (S3), not `\d{4}`: names are rendered `:04d`, so the 10000th
# handoff is five digits. A four-digit-only pattern does not match a longer
# number's *prefix* either — `\d{4}` is exactly four, so `re.match` fails
# outright on "10000-x.md" — which would make `_next_number` stop counting
# past 9999 and collide, and `valid_handoff_name` refuse every handoff
# already past it as if it did not exist.
_NAME_RE = re.compile(r"^(\d{4,})-")

#: A handoff record's own name shape (SB2). Accepted only in exactly this form
#: — never an absolute path, never one carrying a ``/`` of its own — because a
#: name reaches a filesystem join (``handoff_dir(ledger_dir) / name``) and a
#: string like ``../../spec/0001-fix-it.md`` is not a handoff's name, it is a
#: traversal wearing one.
HANDOFF_NAME_RE = re.compile(r"^\d{4,}-[a-z0-9-]+\.md$")

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

#: A handoff's ``--asks`` cap (S-1): shorter than an evidence field's own,
#: since an ask is a sentence a receiver is addressed by, not the proof
#: behind it — and capped before it is ever hashed or scrubbed, so neither of
#: those has to reckon with an unbounded string.
MAX_ASKS_BYTES = 4096

#: A direction's ``--briefing`` cap (S-1), for the same reason: scrubbed and
#: hashed for its announcement id, and both of those want a bound in place
#: before they run.
MAX_BRIEFING_BYTES = 16384

#: Control characters this module refuses in text that reaches a terminal or a
#: CI log (SN1, S6). ``\n`` and ``\t`` are carved out for the evidence bodies —
#: prose needs a line break — and everything else refused here is a character
#: that changes how a terminal or a reader displays what follows rather than
#: being displayed itself: ``\x1b[2J`` and the rest of the C0 range (``\r``
#: included — a bare carriage return repaints the current line) are cursor
#: moves and screen clears; ``\x7f``-``\x9f`` adds DEL and the C1 controls,
#: which are ordinary bytes in a terminal's own escape sequences; and the
#: bidi embedding/override/isolate characters (U+202A-202E, U+2066-2069) are
#: how "right-to-left override" attacks make a filename or a line of code
#: display in an order it is not stored in.
_BAD_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")

#: A URL-shaped substring inside a larger piece of prose (SN2) — as opposed to
#: ``ledger.clean_run_reference``, which reads a whole value as one URL. An
#: evidence field is prose that may *contain* a link a run followed, not a
#: link itself.
#:
#: **S-1: the scheme is bounded, not ``\w+``.** ``\w+://\S+`` backtracks
#: catastrophically on a long run of word characters that never reaches a
#: literal ``://`` — every prefix length of the run retries the same failed
#: match — so a crafted evidence field a few tens of kilobytes long (well
#: under ``MAX_EVIDENCE_BYTES``) could take this regex engine minutes rather
#: than milliseconds. A scheme is a handful of letters, digits, ``+``, ``-``
#: and ``.`` in real use (``https``, ``git+ssh``, ``x-custom-scheme``) and
#: never remotely evidence-field-length, so ``{0,31}`` bounds the backtracking
#: without narrowing what this actually needs to match.
_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{0,31}://\S+")


def refuse_controls(argname: str, text) -> None:
    value = str(text or "")
    found = _BAD_CONTROL_RE.search(value)
    if found:
        raise AnnounceError(
            f"--{argname} carries a control character ({found.group(0)!r}) other "
            f"than newline or tab: {one_line(value)!r}. A value like this reaches "
            f"a terminal or a CI log as itself, and an escape sequence there is a "
            f"command, not a description."
        )


def _cap_bytes(argname: str, text, limit: int, label: str) -> str:
    value = str(text or "")
    size = len(value.encode("utf-8"))
    if size > limit:
        raise AnnounceError(
            f"--{argname} is {size} bytes, over the {limit}-byte cap on {label}; "
            f"refused rather than truncated, because a truncated proof is not "
            f"the proof"
        )
    return value


def _cap_evidence(argname: str, text) -> str:
    return _cap_bytes(argname, text, MAX_EVIDENCE_BYTES, "one handoff evidence field")


#: A credential-shaped query parameter, wherever it sits — inside a URL this
#: module already found, or bare in prose (S7). ``clean_run_reference``'s own
#: "userinfo" signal says nothing about a token riding in the query string
#: instead, which is the ordinary shape a CI system hands one out in.
_QUERY_TOKEN_RE = re.compile(
    r"(?i)\b((?:access_)?token|api[_-]?key|secret)=([^&\s]+)"
)

#: An ``Authorization: Bearer <token>`` value, however it is introduced.
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/-]+=*)")

#: A shell-style ``SOMETHING_TOKEN=``/``_SECRET=``/``_KEY=`` assignment, the
#: shape a copy-pasted environment or CI log line carries a credential in.
_ASSIGNMENT_RE = re.compile(r"\b([A-Z][A-Z0-9_]*(?:_TOKEN|_SECRET|_KEY))=(\S+)")


def scrub_credentials(argname: str, text: str, notes: list[str]) -> str:
    """Strip a credential from *text* wherever one of these shapes finds it
    (SN2, S7): ``user:token@`` in a URL's userinfo, a token-shaped query
    parameter, a ``Bearer`` value, or a ``*_TOKEN``/``*_SECRET``/``*_KEY``
    assignment. Applied to every evidence field and to ``--asks``/
    ``--briefing`` alike — a run's own words are exactly where a credential
    it used along the way ends up quoted.
    """
    changed = False

    def url_repl(match: re.Match) -> str:
        nonlocal changed
        cleaned, removed = clean_run_reference(match.group(0))
        if "userinfo" in removed:
            changed = True
            return cleaned or match.group(0)
        return match.group(0)

    def redact(label: str, group: int = 1):
        def repl(match: re.Match) -> str:
            nonlocal changed
            changed = True
            return f"{match.group(group)}=[redacted]"
        return repl

    def bearer_repl(match: re.Match) -> str:
        nonlocal changed
        changed = True
        return "Bearer [redacted]"

    text = _URL_RE.sub(url_repl, text)
    text = _QUERY_TOKEN_RE.sub(redact("query"), text)
    text = _BEARER_RE.sub(bearer_repl, text)
    text = _ASSIGNMENT_RE.sub(redact("assignment"), text)
    if changed:
        notes.append(
            f"--{argname} carried what looks like a credential; stripped it "
            f"before recording — rotate it."
        )
    return text


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


def canonical_time(value: str | None, argname: str = "--now") -> str | None:
    """*value* as the canonical ISO instant this project writes (K1), or None
    when *value* is absent.

    Never the raw string: ``--now`` reaches a record's frontmatter, and a
    value like ``$'2026-01-17T01:00:00Z\\nanswered: 2026-01-01T00:00:00Z'``
    written verbatim forges a second frontmatter line. Parsed the same way
    ``vellum tick`` parses a moment (``ledger.parse_time``) and re-emitted in
    the one shape this project ever writes, so what lands in the file is
    never anything the caller typed.
    """
    if value is None:
        return None
    moment = parse_time(value)
    if moment is None:
        raise AnnounceError(
            f"{argname} {one_line(str(value))!r} is not a parseable ISO 8601 moment"
        )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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

def handoff_announcement_id(name: str) -> str:
    """The log id a handoff record's own announcement is filed under.

    Deterministic from the handoff's name alone, which is itself the record's
    identity once created — so an arrival that reuses an existing handoff
    (``record_handoff``'s B1 identity match) always computes the same id and
    the log's own dedup-by-id makes the "repair a missing announcement"
    replay a no-op rather than a special case.
    """
    return f"handoff:{name}"


def finished_announcement_id(pr: int | None) -> str:
    """The log id a "work item finished" event is filed under. Keyed on the PR
    number alone (not the item or version, which the log entry already lives
    inside) when one was reported: the same PR reported twice is the same
    piece of news. A finished run that reports no pull request at all has
    only one piece of news to give an item — "it finished" — so every such
    call collapses to the one ``finished:done`` id."""
    return f"finished:pr{pr}" if pr is not None else "finished:done"


def direction_announcement_id(briefing: str) -> str:
    """The log id a piece of direction is filed under: a short hash of the
    *scrubbed* briefing text (the credential-stripped form actually stored),
    so two different directions never collide and the same direction resent
    verbatim always resolves to the same id, whatever role it is redirected
    to on a later call."""
    digest = hashlib.sha256(str(briefing or "").encode("utf-8")).hexdigest()
    return f"direction:{digest[:12]}"


def new_announcement(
    id: str,
    kind: str,
    to: str,
    asks: str,
    handoff: str = "",
    *,
    dispatched: bool = False,
    settled: str | None = None,
) -> dict:
    """One entry of the ``announcements:`` log, in the emission order
    ``ANNOUNCEMENT_KEYS`` gives.

    *id* is required and non-empty: the log is deduplicated by it
    (``append_announcement``), so an entry with no identity could neither be
    matched on replay nor found again by ``answer_handoff``.
    """
    if kind not in ANNOUNCEMENT_KINDS:
        raise AnnounceError(
            f"{kind!r} is not an announcement kind ({', '.join(ANNOUNCEMENT_KINDS)})"
        )
    if not str(id or "").strip():
        raise AnnounceError(
            "an announcement needs an id: the append-only log is deduplicated by it"
        )
    entry = {
        "id": id,
        "kind": kind,
        "to": to,
        "asks": one_line(asks, 200),
        "handoff": handoff,
        "dispatched": bool(dispatched),
    }
    if settled:
        entry["settled"] = settled
    return ordered(entry, ANNOUNCEMENT_KEYS)


def announcements(item: dict) -> list[dict]:
    """The append-only log of every announcement ever raised against *item*,
    oldest first. Never rewritten, only appended to (``append_announcement``)
    — an entry's presence here is itself the durable record that the event
    happened, whether or not it has been dispatched yet."""
    found = item.get("announcements")
    return [e for e in found if isinstance(e, dict)] if isinstance(found, list) else []


def find_announcement(item: dict, id: str) -> dict | None:
    """The log entry with this *id*, or None."""
    for entry in announcements(item):
        if str(entry.get("id") or "") == str(id):
            return entry
    return None


def is_pending(item: dict) -> bool:
    """True when this item has at least one announcement nothing has
    dispatched yet."""
    return any(not e.get("dispatched") for e in announcements(item))


def append_announcement(item: dict, announcement: dict) -> bool:
    """Append *announcement* to the log, unless its ``id`` is already there.

    **Idempotent by id, and append-only.** The same event arriving twice — a
    replayed handoff, a re-run ``ledger advance --pr``, a resent direction —
    computes the same id and this is a no-op; the first arrival's entry,
    dispatched or not, is left exactly as it stood. Two *different* events,
    however similar, get different ids and both survive as their own entries
    — which is what closes the old standing-announcement-plus-pending-queue
    model's whole class of supersede/repair/reopen bugs by construction: there
    is no "newest wins" and nothing to lose track of superseding.

    Returns True when this actually changed the item, so a caller writes a
    record only when a byte of it moved — ``vellum tick``'s D11 idempotence.
    """
    id = str(announcement.get("id") or "")
    if not id:
        raise AnnounceError("an announcement needs an id to be appended to the log")
    log = item.setdefault("announcements", [])
    if any(str(e.get("id") or "") == id for e in log if isinstance(e, dict)):
        return False
    log.append(announcement)
    return True


def retry_unaddressed(checkout: str | Path, ledger_dir: str | Path, item: dict) -> bool:
    """Try to resolve an addressee for every log entry recorded without one
    (rule 4).

    An entry is born with ``to: ""`` in exactly one case: an implicit
    ``finished`` announcement (``ledger advance --pr``, K3's soft-fail path)
    raised when no declared role could be found yet — recorded rather than
    refused, because a run reporting its own pull request must never fail
    over an address it could not compute. That does not mean the news stays
    undeliverable forever: an installation that adds ``write_boundaries``
    after the fact should not have to replay every PR it already reported.
    Called on every reconciler pass (rule 5's caller) and again from
    ``ledger.advance`` itself, so either the next tick or the next explicit
    call notices as soon as an addressee becomes resolvable.

    Returns True when this resolved at least one entry, so a caller knows to
    write the record back.
    """
    pending = [e for e in announcements(item) if not str(e.get("to") or "").strip()]
    if not pending:
        return False
    try:
        to = addressee_for_ledger(checkout, ledger_dir)
    except AnnounceError:
        return False
    if not to:
        return False
    for entry in pending:
        entry["to"] = to
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
    #: sha256 of the full, untruncated ask (S2) — identity compares this, not
    #: `asks`, which `one_line(..., 200)` truncates and two different asks
    #: sharing that prefix would otherwise collide on.
    asks_sha256: str = ""
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


#: The shortest fence this ever renders (K2). Longer only when the content
#: itself contains a run of backticks that long or longer — never shorter,
#: so a fence is always at least a real markdown code fence.
_MIN_FENCE = "```"


def _fence_for(text: str) -> str:
    """A backtick fence *text* cannot contain, and so cannot close early.

    A run of backticks inside evidence — a person's own fenced snippet quoted
    back, or an attempt to forge a section boundary — is measured, and the
    fence is one backtick longer than the longest run found. Markdown fencing
    rules already give this property to nested fences; this is the same rule
    applied to a fence chosen at render time rather than by a human eye.
    """
    longest = 0
    for run in re.findall(r"`+", text):
        longest = max(longest, len(run))
    return "`" * max(len(_MIN_FENCE), longest + 1)


def _fenced_block(heading: str, text: str) -> list[str]:
    fence = _fence_for(text)
    return [f"## {heading}", "", fence, text, fence, ""]


def render_handoff(handoff: Handoff) -> str:
    """The record, as YAML frontmatter over the evidence in the run's own words.

    Frontmatter because the fields a receiver is *routed* by — the addressee
    above all — must be readable without reading prose, and markdown below it
    because the evidence is prose: "what was tried, what was observed, and what
    was proven, in the words of the run that found it". The forge renders it, a
    person reads it, and ``read_handoff`` reads it back.

    Each evidence field sits inside a fence chosen so its own content cannot
    close it (K2): parsing this back reads by fence, never by searching for
    the next ``## `` — evidence that itself contains a heading or a shorter
    fence must not be able to truncate the real content or forge a different
    section when the record is read.
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
    lines.append(f"asks_sha256: {handoff.asks_sha256}".rstrip())
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
    ]
    lines += _fenced_block("What was tried", handoff.tried)
    lines += _fenced_block("What was observed", handoff.observed)
    lines += _fenced_block("What was proved", handoff.proved)
    lines += ["## The change this asks for", ""]
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


#: A fence line on its own: three or more backticks and nothing else (K2).
_FENCE_LINE_RE = re.compile(r"^`{3,}$")

#: The evidence headings, in the fixed order ``render_handoff`` emits them.
_EVIDENCE_HEADINGS = ("What was tried", "What was observed", "What was proved")


def _evidence_sections(text: str) -> dict[str, str]:
    """``{heading: body}`` for every evidence section, read in one sequential
    pass (K2) rather than as three independent searches.

    Sequential on purpose: each section's real fence consumes its *entire*
    body before the scan resumes looking for the next heading. A ``## What
    was proved`` (or a fence-looking line) an attacker embedded inside
    ``tried``'s own content is skipped over as part of what ``tried``
    consumed — the scan for ``proved`` never begins until after ``tried``'s
    true closing fence, so it cannot be fooled by anything that appeared
    before that point. A search that instead looked for each heading
    independently, anywhere in the whole text, would find the *first* match
    for ``proved`` even when that match sits inside ``tried``'s body — which
    is exactly the forgery this guards against.
    """
    lines = text.split("\n")
    found: dict[str, str] = {}
    pos = 0
    for heading in _EVIDENCE_HEADINGS:
        heading_line = f"## {heading}"
        idx = next((i for i in range(pos, len(lines)) if lines[i] == heading_line), None)
        if idx is None:
            found[heading] = ""
            continue
        j = idx + 1
        while j < len(lines) and lines[j] == "":
            j += 1
        if j >= len(lines) or not _FENCE_LINE_RE.match(lines[j]):
            found[heading] = ""
            pos = idx + 1
            continue
        fence = lines[j]
        body = []
        k = j + 1
        while k < len(lines) and lines[k] != fence:
            body.append(lines[k])
            k += 1
        found[heading] = "\n".join(body)
        pos = k + 1
    return found


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

    Opened with ``O_NOFOLLOW`` (R1/S1): whatever validated this path is safe
    to read, this never follows a symlink at the last moment regardless — a
    file that changed under a caller between listing and reading is refused,
    not silently read through. Size is checked with ``fstat`` *before* any
    content is read, and the read itself is bounded to that size, so a
    symlink (or a legitimate file) driving this to read an arbitrarily large
    target cannot inflate memory past what the cap already refuses on paper —
    the check happens before the cost, not after. Capped (SS7): over
    ``MAX_HANDOFF_FILE_BYTES``, or not valid UTF-8, both raise
    ``AnnounceError`` — never a raw ``OSError`` or ``UnicodeDecodeError`` an
    uninvolved caller (``vellum tick``, reading every handoff a wave's items
    name) is not written to expect.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise AnnounceError(f"{path}: cannot read handoff record: {exc}") from exc
    try:
        size = os.fstat(fd).st_size
        if size > MAX_HANDOFF_FILE_BYTES:
            raise AnnounceError(
                f"{path}: {size} bytes, over the {MAX_HANDOFF_FILE_BYTES}-byte "
                f"cap on a handoff record; refused rather than read"
            )
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = os.read(fd, min(1 << 16, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        raise AnnounceError(f"{path}: cannot read handoff record: {exc}") from exc
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AnnounceError(f"{path}: not valid UTF-8: {exc}") from exc
    front = _frontmatter(text)
    raw_item = str(front.get("item") or "").strip()
    sections = _evidence_sections(text)
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
        asks_sha256=str(front.get("asks_sha256") or ""),
        recorded=str(front.get("recorded") or ""),
        answered=str(front.get("answered") or ""),
        answered_by=str(front.get("answered_by") or ""),
        tried=sections["What was tried"],
        observed=sections["What was observed"],
        proved=sections["What was proved"],
    )


def _handoff_paths(ledger_dir: str | Path) -> list[Path]:
    """Every legitimate handoff path in ``handoffs/``, in name order (R1/S1).

    ``lstat``-checked (``Path.is_symlink()``) rather than the ``is_file()``
    a naive ``iterdir()`` filter would use — ``is_file()`` follows a symlink
    and would happily admit ``0001-x.md -> /etc/shadow``, which is exactly
    the read this exists to refuse before ``read_handoff`` ever opens
    anything. Also filtered to the name shape ``valid_handoff_name`` accepts,
    for the same reason ``find_handoff`` filters it: a name this did not
    write is not a record this reads.
    """
    tree = handoff_dir(ledger_dir)
    if not tree.is_dir():
        return []
    found = []
    for entry in tree.iterdir():
        if not valid_handoff_name(entry.name):
            continue
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
        except OSError:
            continue
        found.append(entry)
    return sorted(found, key=lambda p: p.name)


def handoffs(ledger_dir: str | Path) -> list[Handoff]:
    """Every handoff this checkout records, in name order.

    A record that fails to read — corrupt, oversized, or reached only
    through something ``_handoff_paths`` already excluded — is skipped
    rather than raised (R1/S1): one bad file in ``handoffs/`` must not make
    every command that lists or scans them fail.
    """
    found = []
    for path in _handoff_paths(ledger_dir):
        try:
            found.append(read_handoff(path))
        except AnnounceError:
            continue
    return found


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

    **S-6: written whole to a temp file first, published with ``os.link``.**
    The previous approach opened ``target`` itself with
    ``O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW`` and then wrote the content —
    which means the name existed, empty, for the whole span between the
    ``open`` and the ``write`` completing. A reader racing that window
    (``_next_number``'s own ``iterdir``, a concurrent ``record_handoff``
    scanning for a match, ``handoffs()`` listing the directory) could open and
    read a file this had created but not yet filled. Writing the full content
    to a private temp file in the same directory first, and only then linking
    it into place, means the name never becomes visible under ``target`` until
    the content behind it is already complete — there is no window to race.
    ``os.link`` keeps exactly the exclusivity ``O_EXCL`` gave: it fails with
    ``FileExistsError`` when ``target`` is already occupied by anything at
    all, symlink included (SB1), and this still just tries the next number —
    which is what also closes the ordinary concurrent-name race, where two
    callers compute the same ``_next_number()`` before either has published.
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
        fd, tmp_name = tempfile.mkstemp(dir=tree, prefix=".handoff-", suffix=".tmp")
        try:
            try:
                os.write(fd, content)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp_name, 0o644)
            try:
                os.link(tmp_name, target)
            except FileExistsError:
                continue  # the name is already occupied; try the next number
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    continue  # a symlink already occupies this name
                raise
            return handoff
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    raise AnnounceError(f"{tree}: no free handoff number found after 10000 tries")


#: The two frontmatter lines ``answer`` may ever change (K2).
_ANSWER_LINE_RE = re.compile(r"^(answered|answered_by):")


def _patch_answer(text: str, answered: str, answered_by: str) -> str:
    """*text* with only its ``answered:``/``answered_by:`` frontmatter lines
    replaced (K2) — never a re-render from parsed evidence, which would trust
    ``_section``'s read of a body a hand edit or a crafted ``--tried`` could
    have made say something the file's own bytes do not.
    """
    lines = text.split("\n")
    out = []
    in_frontmatter = False
    seen_open = False
    for line in lines:
        if line == "---" and not seen_open:
            seen_open = True
            in_frontmatter = True
            out.append(line)
            continue
        if line == "---" and in_frontmatter:
            in_frontmatter = False
            out.append(line)
            continue
        if in_frontmatter and line.startswith("answered:"):
            out.append(f"answered: {answered}".rstrip())
            continue
        if in_frontmatter and line.startswith("answered_by:"):
            out.append(f"answered_by: {answered_by}".rstrip())
            continue
        out.append(line)
    return "\n".join(out)


def write_handoff(ledger_dir: str | Path, handoff: Handoff, checkout: str | Path) -> Path:
    """Patch an *existing* handoff record's answer in place — ``answer``'s
    write, and the only write this ever performs on a record it did not just
    create.

    Refuses a symlinked record outright rather than writing through it
    (SB1). Patches the raw text (K2) rather than re-rendering from
    ``handoff``'s parsed fields: a full re-render trusts the parser's read of
    the evidence bodies, and evidence a run supplied is not something this
    module re-derives a file from a second time. Only ``answered:`` and
    ``answered_by:`` ever change; every other byte, including the fences and
    bodies of the evidence sections, is copied through untouched.

    **Atomic (round-6 S-6, the ``answer`` half).** The previous version opened
    ``path`` itself with ``O_WRONLY``, truncated it to zero, and then wrote —
    which means the file sat empty on disk for the whole span between the
    truncate and the write completing. A concurrent ``record_handoff`` replay
    racing that window (its identity check reads every handoff in
    ``handoffs/``) would read an empty file, fail the identity match against
    it, and mint a second handoff record — dispatching the receiver again for
    news it already answered. A crash in the same window left an empty file
    behind permanently: unparseable, so `is_answered` reads false forever and
    the item holds on a handoff that can never be marked answered again.
    Written whole to a temp file in the same directory first (the same
    pattern ``ledger.write`` and ``announce._create_handoff`` already use) and
    published with ``os.replace``, so the name never shows anything but the
    complete old content or the complete new content — never a truncated
    file in between.
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
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AnnounceError(f"{path}: cannot read handoff record: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AnnounceError(f"{path}: not valid UTF-8: {exc}") from exc
    patched = _patch_answer(text, handoff.answered, handoff.answered_by)
    content = patched.encode("utf-8")
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(tree), prefix=f".{path.name}.", suffix=".tmp")
    except OSError as exc:
        raise AnnounceError(f"{path}: cannot patch handoff record: {exc}") from exc
    try:
        umask = os.umask(0)
        os.umask(umask)
        os.fchmod(fd, 0o666 & ~umask)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
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

    **Locked, whole (round-6 S-6, the ``answer`` half).** The find, the
    identity/role checks, the patch and the settle all hold the shared ledger
    lock — the same lock ``record_handoff`` holds across its own identity
    check and create. Without it, a ``record_handoff`` replay's identity scan
    could read this handoff mid-patch (see ``write_handoff``) and, seeing
    something that fails to match, mint a second record for news already
    answered. The lock is reentrant (``ledger.locked``), so
    ``_settle_handoff_announcement`` taking it again below is a no-op, not a
    deadlock.
    """
    with _locked(ledger_dir):
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
            handoff.answered = canonical_time(at) or ledger_now()
            handoff.answered_by = recorded_by
            write_handoff(ledger_dir, handoff, checkout)
            _settle_handoff_announcement(ledger_dir, handoff)
        return handoff_dir(ledger_dir) / name


def _settle_handoff_announcement(ledger_dir: str | Path, handoff: Handoff) -> None:
    """Mark the owning ledger record's log entry for *handoff* ``settled:
    "answered"`` (rule 3), when it is still undelivered.

    Best-effort and never raises: the handoff file itself, just patched by
    the caller above, is what ``_withheld_reason`` actually reads to decide
    whether a delivery dispatches it, so a ledger record this cannot find —
    moved, renamed, or simply absent because *handoff.item* was never set —
    leaves the one fact that matters, "an answered handoff dispatches
    nobody", intact either way. This is annotation on top of that fact, not a
    second copy of it.
    """
    if handoff.item is None:
        return
    try:
        with _locked(ledger_dir):
            path, record = _record_for(ledger_dir, handoff.version)
            item = find_item(record, handoff.item)
            if item is None:
                return
            entry = find_announcement(item, handoff_announcement_id(handoff.name))
            if entry is None or entry.get("dispatched") or entry.get("settled"):
                return
            entry["settled"] = "answered"
            write(path, record)
    except AnnounceError:
        return


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
    """Put *announcement* on a work item, and write the record if it moved.

    The whole read-modify-write holds the shared ledger lock (K4): two
    concurrent announcements against different items in the same record must
    serialize, or the second writer's read (taken before the first's write
    lands) silently drops the first's change.
    """
    with _locked(ledger_dir):
        path, record = _record_for(ledger_dir, version)
        found = find_item(record, item)
        if found is None:
            raise AnnounceError(
                f"{path.name} has no work item {item}; an announcement is about a unit "
                f"of work, and this record's items are "
                f"{', '.join(str(i.get('issue')) for i in record.get('work_items') or []) or '(none)'}"
            )
        if append_announcement(found, announcement):
            write(path, record)
        return path, found


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
    orphan record behind. The whole call — validation, the identity check,
    and the create-or-reuse it decides between — holds the shared ledger lock
    (K4): two concurrent handoffs must not both see "no match" and both
    create a record, and a replay that finds a match still repairs the
    announcement below rather than trusting it is already there.
    """
    with _locked(ledger_dir):
        path, record = _record_for(ledger_dir, version)
        found = find_item(record, item)
        if found is None:
            raise AnnounceError(
                f"{path.name} has no work item {item}; an announcement is about a unit "
                f"of work, and this record's items are "
                f"{', '.join(str(i.get('issue')) for i in record.get('work_items') or []) or '(none)'}"
            )
        # S2: identity is pinned to the record's own full spec version, never
        # an abbreviation — a short sha typed twice must not mint two records.
        full_version = str(record.get("spec_version") or version)

        declared = declared_boundaries(checkout)
        sender = require_role(checkout, sender, "--from")

        refuse_controls("asks", asks)
        refuse_controls("tried", tried)
        refuse_controls("observed", observed)
        refuse_controls("proved", proved)
        if not str(tried or "").strip() or not str(proved or "").strip():
            raise AnnounceError(
                "a handoff carries what was tried and what was proved — the "
                "evidence a receiver verifies rather than rediscovers. An ask with "
                "none of that is a question, and goes by the question protocol "
                "instead (spec/features/question-protocol.md)"
            )
        asks = _cap_bytes("asks", asks, MAX_ASKS_BYTES, "a handoff's ask")
        tried = _cap_evidence("tried", tried)
        observed = _cap_evidence("observed", observed)
        proved = _cap_evidence("proved", proved)
        local_notes: list[str] = []
        tried = scrub_credentials("tried", tried, local_notes)
        observed = scrub_credentials("observed", observed, local_notes)
        proved = scrub_credentials("proved", proved, local_notes)
        asks = scrub_credentials("asks", asks, local_notes)
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
        # S2: the FULL asks text, hashed — `asks_norm` truncates at 200
        # characters, so two asks sharing that prefix would otherwise collide
        # and the second one's evidence would silently vanish into the first
        # one's record.
        asks_hash = hashlib.sha256(str(asks or "").encode("utf-8")).hexdigest()
        identity_paths = tuple(sorted(proposed))
        existing = _matching_handoff(ledger_dir, full_version, item, to, asks_hash, identity_paths)
        if existing is not None:
            # K4: still idempotent — the log is deduplicated by id
            # (`handoff:<name>`), so a replay that finds a matching handoff
            # appends nothing new; it also repairs an announcement a prior
            # crash or lost update left missing, rather than trusting the
            # record is already right.
            record_announcement(
                ledger_dir, full_version, item,
                new_announcement(handoff_announcement_id(existing.name), "handoff",
                                 to, existing.asks, handoff=existing.name),
            )
            return handoff_dir(ledger_dir) / existing.name, existing

        template = Handoff(
            name="", to=to, sender=sender, version=full_version, item=item, paths=proposed,
            asks=asks_norm, asks_sha256=asks_hash,
            recorded=canonical_time(at) or ledger_now(), answered="", answered_by="",
            tried=tried, observed=observed, proved=proved,
        )
        handoff = _create_handoff(ledger_dir, checkout, template)
        record_announcement(
            ledger_dir, full_version, item,
            new_announcement(handoff_announcement_id(handoff.name), "handoff",
                             to, handoff.asks, handoff=handoff.name),
        )
        return handoff_dir(ledger_dir) / handoff.name, handoff


def _matching_handoff(
    ledger_dir: str | Path,
    version: str,
    item: int,
    to: str,
    asks_hash: str,
    paths_norm: tuple[str, ...],
) -> Handoff | None:
    """An existing handoff with this exact identity, or None (B1).

    Each record is read independently, and one that fails to read (SS7's cap,
    a decode error, a corrupt frontmatter) is skipped with the failure
    swallowed rather than raised: one bad file in ``handoffs/`` must not
    break every future ``announce handoff`` call that happens to scan past
    it looking for a match.
    """
    wanted = (str(version), item, to, asks_hash, paths_norm)
    for path in _handoff_paths(ledger_dir):
        try:
            existing = read_handoff(path)
        except AnnounceError:
            continue
        found = (
            str(existing.version), existing.item, existing.to,
            existing.asks_sha256 or hashlib.sha256(existing.asks.encode("utf-8")).hexdigest(),
            tuple(sorted(existing.paths)),
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
    notes: list[str] | None = None,
) -> tuple[Path, dict, str]:
    """Record the owner's direction against a work item, and announce it.

    Mirrors ``reconcile.directions()``'s own write — the item's ``briefing``
    field, updated only when it actually changed — so a webhook can record and
    deliver direction in one act (S2) without waiting for a tick, and the tick
    path stays exactly what it was for the installations that still poll it as
    a fallback. The whole read-modify-write holds the shared ledger lock (K4).
    """
    with _locked(ledger_dir):
        path, record = _record_for(ledger_dir, version)
        found = find_item(record, item)
        if found is None:
            raise AnnounceError(
                f"{path.name} has no work item {item}; an announcement is about a unit "
                f"of work, and this record's items are "
                f"{', '.join(str(i.get('issue')) for i in record.get('work_items') or []) or '(none)'}"
            )
        role = require_role(checkout, to, "--to") if to else addressee_for_ledger(checkout, ledger_dir)
        # S6: the owner's own words reach a briefing an agent reads, and from
        # there a terminal or a log the same way any other evidence would.
        refuse_controls("briefing", briefing)
        briefing = _cap_bytes("briefing", briefing, MAX_BRIEFING_BYTES, "a direction's briefing")
        local_notes: list[str] = []
        briefing = scrub_credentials("briefing", briefing, local_notes)
        if notes is not None:
            notes.extend(local_notes)
        changed_briefing = found.get("briefing") != briefing
        if changed_briefing:
            found["briefing"] = briefing
        announcement = new_announcement(
            direction_announcement_id(briefing), "direction", role, briefing,
        )
        changed_announcement = append_announcement(found, announcement)
        if changed_briefing or changed_announcement:
            write(path, record)
        return path, found, role


# ------------------------------------------------------------------ delivery

@dataclass(frozen=True)
class Delivery:
    """One dispatch action: everything undelivered addressed to one role, for
    one work item, resolved into the single command that role receives.

    **One dispatch per (version, item, to), covering every entry it groups
    (rule 2).** A blocked run's handoff and, moments later, that same item
    finishing both address the ledger holder; delivering them as two separate
    dispatches would run the receiver's collection twice for one commission.
    Grouped here into one ``Delivery`` instead — ``entries`` carries every log
    entry it speaks for, and ``deliver`` marks all of them dispatched together.
    """

    version: str
    item: int | None
    role: str
    detail: str
    #: Why it was not delivered, or "" when it was. An answered handoff is the
    #: one case: "a handoff already acted on dispatches nobody".
    withheld: str = ""
    #: The live ``(path, record)`` this came from, for ``deliver`` to write
    #: back through directly (N2/SS8) — never by looking the item back up by
    #: ``issue``, which an item with ``issue: null`` (or a duplicate) could
    #: not be found by a second time.
    record: tuple[Path, dict] | None = field(default=None, repr=False, compare=False)
    #: The live announcement-log entries this dispatch speaks for — one for an
    #: ordinary delivery or a withheld one, more than one when rule 2 grouped
    #: several undelivered entries addressed to the same role together.
    entries: tuple[dict, ...] = field(default=(), repr=False, compare=False)


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


def _withheld_reason(ledger_dir: str | Path, entry: dict) -> str:
    name = str(entry.get("handoff") or "").strip()
    if not name:
        return ""
    try:
        recorded = find_handoff(ledger_dir, name)
    except AnnounceError as exc:
        # A handoff this cannot read says nothing about whether it was
        # answered — the safe reading is to hold it, not to dispatch on a
        # guess (nit: this used to return "" here, which `deliver` reads as
        # "not withheld" and dispatches anyway; `reconcile._dispatch_one`
        # already holds in the equivalent case, and this now matches it).
        return f"handoff {name} could not be read, so it is held rather than dispatched: {exc}"
    if recorded is not None and recorded.is_answered:
        return (
            f"handoff {name} was answered on {recorded.answered}; a handoff "
            f"already acted on dispatches nobody"
        )
    return ""


def _resolve_version_path(ledger_dir: str | Path, version: str) -> Path | None:
    """*version* resolved to its record's own path (S-2), the same way every
    other announce command resolves it (``_record_for``), rather than a bare
    string comparison against ``spec_version`` — a comparison an abbreviated
    sha, valid everywhere else this project takes ``--version``, would simply
    never match."""
    return find_record(ledger_dir, version)


def deliveries(
    ledger_dir: str | Path,
    item: int | None = None,
    handoff: str | None = None,
    version: str | None = None,
) -> list[Delivery]:
    """Every undispatched announcement, resolved into the dispatch(es) it
    would cause.

    Read-only: what is *deliverable* is a question about the record, and asking
    it must not be the thing that answers it. ``--version`` (N2/SS8, S-2)
    narrows to one record the way every other announce command already does —
    resolved through ``find_record`` so an abbreviated sha matches exactly as
    it would anywhere else this project takes ``--version`` — so a transport
    that knows which wave it is delivering for is not made to scan every open
    one.
    """
    resolved_path = None
    if version is not None:
        resolved_path = _resolve_version_path(ledger_dir, version)
        if resolved_path is None:
            return []
    found: list[Delivery] = []
    for path, record in _records(ledger_dir):
        if resolved_path is not None and path != resolved_path:
            continue
        rec_version = str(record.get("spec_version") or "")
        for entry in record.get("work_items") or []:
            if not isinstance(entry, dict):
                continue
            issue = entry.get("issue")
            if item is not None and issue != item:
                continue
            issue_int = issue if isinstance(issue, int) and not isinstance(issue, bool) else None
            pending = [a for a in announcements(entry) if not a.get("dispatched")]
            if handoff is not None:
                pending = [a for a in pending if str(a.get("handoff") or "").strip() == handoff]
            # Rule 2: group every deliverable entry addressed to the same role
            # into one dispatch. A withheld entry is reported on its own
            # instead — grouping it with a role's other, deliverable entries
            # would either withhold news that is not withheld, or silently
            # drop the one that is.
            grouped: dict[str, list[dict]] = {}
            for entry_ann in pending:
                role = str(entry_ann.get("to") or "").strip()
                if not role:
                    # Rule 4: unaddressed — recorded, but nothing to deliver
                    # to yet. A reconciler pass retries resolving it.
                    continue
                withheld = _withheld_reason(ledger_dir, entry_ann)
                if withheld:
                    found.append(Delivery(rec_version, issue_int, role,
                                          dispatch_detail(entry_ann), withheld,
                                          record=(path, record), entries=(entry_ann,)))
                    continue
                grouped.setdefault(role, []).append(entry_ann)
            for role, group in grouped.items():
                detail = "; ".join(dispatch_detail(a) for a in group)
                found.append(Delivery(rec_version, issue_int, role, detail, "",
                                      record=(path, record), entries=tuple(group)))
    return found


#: Kept as an alias: ``ledger.locked`` is the one lock every read-modify-write
#: of a ledger record holds now (K4) — ``record_handoff``, ``record_announcement``,
#: ``record_direction``, ``deliver``, and ``ledger.advance``/``open_record`` all
#: share it, relocated out of the tracked ledger tree (R2).
_locked = ledger_locked


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
            for entry in delivery.entries:
                entry["dispatched"] = True
            if delivery.record is not None:
                path, record = delivery.record
                touched[path] = record
            sent.append(delivery)
        # No pruning: the log is append-only (unlike the old
        # standing-announcement-plus-pending-queue shape, S4's "prune every
        # dispatched pending entry before writing" no longer applies — a
        # dispatched entry stays in `announcements:` as the durable record
        # that it happened, and the log's own size is bounded by how many
        # events a work item actually raises, not by anything this prunes).
        for path, record in touched.items():
            write(path, record)
    return sent, held


def utc(value: str | None) -> datetime.datetime | None:
    """Parse an ISO moment the way the ledger does, for a caller's ``--now``."""
    from vellum.ledger import parse_time

    return parse_time(value) if value else None


__all__ = [
    "ANNOUNCEMENT_KEYS", "ANNOUNCEMENT_KINDS", "AnnounceError", "HANDOFF_DIRNAME",
    "HANDOFF_NAME_RE", "Handoff", "MAX_ASKS_BYTES", "MAX_BRIEFING_BYTES",
    "MAX_EVIDENCE_BYTES", "MAX_HANDOFF_FILE_BYTES",
    "addressee", "addressee_for_ledger", "announcements", "answer_handoff",
    "append_announcement", "declared_boundaries", "direction_announcement_id",
    "dispatch_detail", "find_announcement", "find_handoff",
    "finished_announcement_id", "handoff_announcement_id", "handoff_dir",
    "handoffs", "holders", "is_pending", "new_announcement",
    "read_handoff", "record_announcement",
    "record_direction", "record_handoff", "refuse_controls", "render_handoff",
    "require_role", "retry_unaddressed", "scrub_credentials",
    "valid_handoff_name", "write_handoff",
    "Delivery", "deliver", "deliveries",
]
