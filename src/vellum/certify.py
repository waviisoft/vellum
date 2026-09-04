"""``vellum certify`` — the recorded proof that authorizes an auto-merge.

``spec/features/certification-and-releases.md``: "Auto-merge requires, once per
merge candidate: verifier approval, and a certification run on neutral
infrastructure ... Certification is recorded in the ledger; any subsequent
commit invalidates it. The examined party never executes its own proof."
``spec/features/ledger.md`` gives that sentence a shape to live in:
``certification: {sha, run, at, result}`` on the work item, and "only a recorded
green certification at the current head authorizes auto-merge".

Two scenarios state the whole contract, and this command is written to be the
thing that answers both of them:

* ``@id:no-self-certified-merge`` — an implementation PR whose *in-session*
  checks pass, with no certification recorded for its head commit, does not
  auto-merge. So the default is deny, and passing checks are not an input here
  at all: this command is never told whether anything passed in-session,
  because a party's report about itself is not the evidence being asked for.
* ``@id:new-commit-invalidates-cert`` — a certification recorded at one commit
  does not authorize a later one. So the head is an *argument*, compared
  exactly, and "any subsequent commit invalidates it" needs no notion of
  subsequent: a head that is not the certified sha is uncertified, whether it
  came before, after, or from another branch entirely.

Two subcommands, because they are asked by different parties at different
times: ``certify record`` is written by the certification runner when a run
finishes, and ``certify check`` is read by the gate that would merge.

    vellum certify record <checkout> --version <spec-sha> --item <issue> \\
        --sha <head-sha> --result green|red [--run <ref>] [--at <iso>]
    vellum certify check  <checkout> --version <spec-sha> --item <issue> \\
        --head <head-sha>

``check`` exits 0 when a green certification exists at exactly that head, and 1
when it does not — the two answers a merge gate branches on. It reaches no
forge and runs nothing: the head sha is supplied by the caller that can see the
PR, the way ``backpressure --pending`` and ``budget --projected`` take the
numbers only a forge can know. Nothing here decides *whether* the run was
honest; it records which commit was proved and refuses to let that evidence
travel to another commit.

Exit codes follow the CLI's split (``src/vellum/cli.py``). A denial is 1 — the
command answered, and the answer blocks a merge. 2 stays "could not answer":
no ledger, no record for that version, no such work item, a sha that is not a
sha. Both are non-zero and both block, so the safe direction is not at stake;
what is at stake is whether the caller can tell "this commit is not certified"
from "you pointed me at the wrong repository", and those must not share a code.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from vellum.backpressure import ledger_dir_for
from vellum.ledger import (
    LedgerError,
    certification_authorizes,
    certify as certify_item,
    clean_run_reference,
    credential_free_run,
    find_item,
    find_record,
    load,
    parse_certified_sha,
    parse_version,
)
from vellum.text import one_line


def _ledger(checkout: str | Path, ledger_dir: str | Path | None) -> Path:
    ledger = Path(ledger_dir) if ledger_dir is not None else ledger_dir_for(checkout)
    if not ledger.is_dir():
        # Named as its own failure rather than left to surface as "no record
        # for that version": those are different mistakes, and the second
        # sentence sends a caller looking through a ledger that is not there.
        raise LedgerError(
            f"{ledger}: no ledger directory. Certification is recorded in the "
            f"ledger, so there is nothing here to record or read; is {checkout} "
            f"an intent checkout?"
        )
    return ledger


@dataclass
class Authorization:
    """The answer ``certify check`` gives about one work item at one head."""

    record: Path
    version: str
    issue: int
    head: str
    authorized: bool
    reason: str
    #: The certification as recorded, whatever shape it is in, or None.
    certification: object = None

    def report(self) -> str:
        verdict = "AUTHORIZED" if self.authorized else "DENIED"
        lines = [
            f"Certification of work item {self.issue} at {self.head[:12]}: {verdict}",
            f"  record  {self.record.name}",
        ]
        if isinstance(self.certification, dict):
            # Every field here was read off disk and is echoed to a job log and,
            # piped onward, a step summary. `one_line` keeps a value carrying a
            # newline from starting a line of its own, and `credential_free_run`
            # strips a run reference on the way *out* as well as on the way in:
            # a record written before that rule, or edited by hand, still holds
            # whatever it holds, and printing it is the second publication.
            recorded = self.certification
            lines.append(
                f"  recorded  sha {one_line(recorded.get('sha') or '-')[:12]}"
                f"  result {one_line(recorded.get('result') or '-')}"
                f"  at {one_line(recorded.get('at') or '-')}"
                f"  run {one_line(credential_free_run(recorded.get('run')) or '-')}"
            )
        else:
            lines.append("  recorded  (none)")
        lines.append(f"  {self.reason}")
        return "\n".join(lines)


def check(
    checkout: str | Path,
    version: str,
    issue: int,
    head: str,
    ledger_dir: str | Path | None = None,
) -> Authorization:
    """Does a recorded green certification authorize a merge at *head*?"""
    ledger = _ledger(checkout, ledger_dir)
    sha = parse_version(version)
    head = parse_certified_sha(head, what="head commit")
    path = find_record(ledger, sha)
    if path is None:
        raise LedgerError(f"no ledger record for {sha} in {ledger}")
    item = find_item(load(path), issue)
    if item is None:
        raise LedgerError(
            f"work item {issue} is not in {path.name}, so there is no "
            f"certification to check. Is --item the issue number of the work "
            f"item this PR implements?"
        )
    authorized, reason = certification_authorizes(item, head)
    return Authorization(
        record=path,
        version=sha,
        issue=issue,
        head=head,
        authorized=authorized,
        reason=reason,
        certification=item.get("certification"),
    )


def run_check(
    checkout: str,
    version: str,
    issue: int,
    head: str,
    ledger_dir: str | None = None,
    out=None,
) -> int:
    """Report the authorization and exit 1 when it is denied."""
    stream = out if out is not None else sys.stdout
    result = check(checkout, version, issue, head, ledger_dir=ledger_dir)
    # Printed either way, for the reason the other guards print theirs: a gate
    # that speaks only when it blocks leaves a reviewer unable to see which
    # commit was actually certified, which is the half of the answer that
    # catches a runner certifying the wrong one.
    print(result.report(), file=stream)
    if not result.authorized:
        print(f"vellum: certification — {result.reason}", file=sys.stderr)
        return 1
    return 0


def run_record(
    checkout: str,
    version: str,
    issue: int,
    sha: str,
    result: str,
    run: str | None = None,
    at: str | None = None,
    ledger_dir: str | None = None,
    out=None,
) -> int:
    """Record a certification run. Always 0 — a red run is recorded, not refused.

    A red certification is evidence too, and the command that writes it has not
    failed at anything. What a red result does is deny the merge, and that
    denial is ``certify check``'s answer to give.
    """
    stream = out if out is not None else sys.stdout
    ledger = _ledger(checkout, ledger_dir)
    # Normalised once, here, and echoed as stored. Echoing the caller's own
    # casing back reads as confirmation that what they typed is what the record
    # now holds, and the two are not the same string.
    certified = parse_certified_sha(sha)
    path = certify_item(
        ledger, parse_version(version), issue, certified, result, run=run, at=at
    )
    print(
        f"{path}: work item {issue} certified {result} at {certified[:12]}",
        file=stream,
    )
    stored, removed = clean_run_reference(run)
    if removed:
        # Said out loud rather than done quietly: the record now holds
        # something other than what was typed, and a reader comparing the two
        # deserves to be told why rather than left to suspect a typo.
        note = (
            f"vellum: --run dropped its {' and '.join(removed)}; it is stored "
            f"and printed as {stored!r}. A run reference is committed to the "
            f"intent repo and echoed by every `certify check`, so it carries "
            f"no credential."
        )
        if "userinfo" in removed:
            # The half that is not housekeeping. Dropping it here undoes
            # nothing: the value has already been through a shell history and a
            # process table by the time this command sees it.
            note += " Rotate the credential you passed."
        print(note, file=stream)
    return 0
