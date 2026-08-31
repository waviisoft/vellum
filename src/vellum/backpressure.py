"""``vellum backpressure`` — the divergence gate.

``spec/features/spec-pipeline.md``: "past the configured cap of unshipped spec
versions, further spec merges are blocked until implementation catches up."
This is the body of the ``backpressure`` job in
``adapters/github/spec-ci.yml``, which until now echoed a stub and exited 0.

What is counted
---------------
Ledger records whose state is neither ``shipped`` nor ``superseded``. Those two
are the only ways a version leaves the window: one shipped, the other will
never ship. Everything between ``approved`` and ``verified`` is intent the
product has not delivered, which is exactly what the cap measures.

Counting states rather than differencing ``spec_head`` against a release
pointer is deliberate. The pointer half (``ledger/releases.yaml``) is not
maintained yet — its ``spec_conformed`` is ``null`` — so a difference computed
from it would be a number with no input. The records are real and the state
field is written by the same automation the cap governs.

Why ``>=`` and not ``>``
------------------------
The question this answers is "may another version land", not "how many are
there". ``@id:backpressure-blocks-merge`` states it exactly: a cap of 3 with 3
approved-and-unshipped versions blocks the next merge. Landing one more would
put the window past the cap, so the gate closes at the cap, not after it.

What this cannot see
--------------------
``spec/features/spec-pipeline.md`` also says the cap "counts approved-but-
unlanded spec PRs together with landed-but-unshipped versions". A command
reading a checkout can only see the landed half — an open PR is forge state,
not repository state, and reaching for it would need credentials this command
does not take and an API it should not know about. ``--pending`` exists for
that half: a caller that *can* see the forge passes the count, and the gate
measures the sum the spec describes. Left unset it is zero, and the report says
so rather than implying the whole window was measured.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from vellum.config import ConfigError, config_path, divergence_cap
from vellum.gitver import TAG_RE
from vellum.ledger import SHA_RE

#: States a version has left the divergence window in. Everything else counts.
SETTLED_STATES = ("shipped", "superseded")

#: Not a version record, and the one file in ``ledger/`` that is a mapping with
#: no ``spec_version``. Named rather than inferred, so that a genuinely
#: malformed record is still reported instead of quietly skipped as "probably
#: releases.yaml".
NOT_A_RECORD = ("releases.yaml",)


class BackpressureError(Exception):
    """The window could not be measured."""


@dataclass
class Window:
    """The divergence window at one moment."""

    cap: int
    #: ``(sha, state, name)`` per unshipped record, in ledger filename order.
    unshipped: list[tuple[str, str, str | None]]
    #: Approved-but-unlanded spec PRs the caller told us about.
    pending: int
    #: Records skipped because they could not be read as records.
    unreadable: list[str]

    @property
    def count(self) -> int:
        return len(self.unshipped) + self.pending

    @property
    def blocked(self) -> bool:
        return self.count >= self.cap

    def report(self) -> str:
        lines = [
            f"Divergence window: {self.count} of {self.cap}",
            "",
        ]
        for sha, state, name in self.unshipped:
            lines.append(f"  {sha[:12]}  {state:<12} {name or ''}".rstrip())
        if self.pending:
            lines.append(f"  {'':12}  {'(approved, unlanded spec PRs)':<12} +{self.pending}")
        if not self.unshipped and not self.pending:
            lines.append("  (nothing unshipped)")
        lines.append("")
        if self.unreadable:
            lines.append(
                f"{len(self.unreadable)} file(s) in the ledger could not be read as "
                f"records and were not counted: {', '.join(self.unreadable)}"
            )
            lines.append("")
        if self.blocked:
            lines.append(
                f"BLOCKED: {self.count} spec version(s) are approved and unshipped, "
                f"at or past the divergence cap of {self.cap}. Further spec merges "
                f"wait until implementation catches up "
                f"(spec/features/spec-pipeline.md)."
            )
        else:
            lines.append(
                f"OK: room for {self.cap - self.count} more version(s) before "
                f"backpressure applies."
            )
        if not self.pending:
            lines.append(
                "Counted from the ledger only: landed-but-unshipped versions. "
                "Approved-but-unlanded spec PRs are forge state — pass --pending "
                "to include them."
            )
        return "\n".join(lines)


# =====================================================================
# `report()` is printed, and `spec-ci.yml` pipes it straight into
# $GITHUB_STEP_SUMMARY. Both fields below arrive from the intent repo's
# `ledger/`, which anyone who can land a merge there writes, so both are
# narrowed here rather than at the point of printing: a newline in either
# forges a `::error` / `::notice` / `::add-mask` workflow command on its own
# line, and `::add-mask` in particular is how a forged line reaches beyond
# cosmetics. `sha` needs no such treatment — `SHA_RE` above already accepts
# nothing but hex.
# =====================================================================


def _first_word(state: str) -> str:
    """A record's ``state``, narrowed to the one token a state can be."""
    words = state.split()
    return words[0] if words else "(no state)"


def _decorative_name(value) -> str | None:
    """A record's ``name``, if it is one worth printing.

    `spec-v<N>` or nothing, the same rule `pin.py` applies to the same field
    for the same reason. A name is decoration — nothing here reads it to
    decide anything — so a malformed one is dropped and the row prints without
    it, rather than raising and taking the whole measurement down.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text if TAG_RE.match(text) else None


def ledger_dir_for(checkout: str | Path) -> Path:
    return Path(checkout) / "ledger"


def measure(
    checkout: str | Path,
    ledger_dir: str | Path | None = None,
    cap: int | None = None,
    pending: int = 0,
    strict: bool = False,
) -> Window:
    """The divergence window for the intent checkout at *checkout*.

    A record this cannot read is reported and not counted, which makes the
    window *narrower* than the truth — a gate failing open on corruption. That
    is the tolerant default, because a name-keyed leftover from before versions
    were commits is a migration to do rather than a merge to block. *strict*
    refuses to answer instead, and belongs on wherever the gate actually
    blocks: there, "I could not read three records" must not read as "there is
    room for three more versions".
    """
    if pending < 0:
        raise BackpressureError(f"--pending must not be negative (got {pending})")
    if cap is None:
        try:
            cap = divergence_cap(checkout)
        except ConfigError as exc:
            raise BackpressureError(str(exc)) from exc
    elif cap < 0:
        raise BackpressureError(f"--cap must not be negative (got {cap})")

    ledger = Path(ledger_dir) if ledger_dir is not None else ledger_dir_for(checkout)
    if not ledger.is_dir():
        raise BackpressureError(
            f"{ledger}: no ledger directory. Backpressure counts ledger records, so "
            f"there is nothing here to measure; is {checkout} an intent checkout?"
        )

    unshipped: list[tuple[str, str, str | None]] = []
    unreadable: list[str] = []
    for path in sorted(ledger.glob("*.yaml")):
        if path.name in NOT_A_RECORD:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            unreadable.append(path.name)
            continue
        if not isinstance(data, dict):
            unreadable.append(path.name)
            continue
        sha = str(data.get("spec_version") or "").strip().lower()
        # A record keyed by anything but a sha is not a version this CLI
        # recognises (`spec/decisions/2026-08-28-versions-are-commits.md`), and
        # counting one would let a name-keyed leftover hold the gate closed.
        if not SHA_RE.match(sha):
            unreadable.append(path.name)
            continue
        state = str(data.get("state") or "").strip()
        if state in SETTLED_STATES:
            continue
        # Settled-ness is decided on the WHOLE value, and only then is the
        # value narrowed for display. A record whose state is `shipped` with
        # anything after it has not shipped, and collapsing before the test
        # would drop it from the window — a gate opening on a crafted record,
        # which is the one direction this must never fail in.
        unshipped.append((sha, _first_word(state), _decorative_name(data.get("name"))))

    if unreadable and strict:
        raise BackpressureError(
            f"{ledger}: {len(unreadable)} file(s) could not be read as ledger records "
            f"({', '.join(unreadable)}). Under --strict the window is not measured at "
            f"all rather than measured short: an unreadable record is one this cannot "
            f"prove has shipped."
        )
    return Window(cap=cap, unshipped=unshipped, pending=pending, unreadable=unreadable)


def run(
    checkout: str,
    ledger_dir: str | None = None,
    cap: int | None = None,
    pending: int = 0,
    strict: bool = False,
    out=None,
) -> int:
    """Report the window and exit non-zero past the cap.

    The report goes to stdout whether the gate is open or closed: a check that
    only speaks when it fails tells a passing run nothing about how much room
    is left, and the margin is the useful half of a backpressure signal.
    """
    stream = out if out is not None else sys.stdout
    window = measure(
        checkout, ledger_dir=ledger_dir, cap=cap, pending=pending, strict=strict
    )
    print(window.report(), file=stream)
    if window.blocked:
        print(
            f"vellum: backpressure — {window.count} unshipped spec version(s) "
            f"at a cap of {window.cap}; see {config_path(checkout)}",
            file=sys.stderr,
        )
        return 1
    return 0
