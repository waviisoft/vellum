"""``vellum mint`` — the bookkeeping a spec merge leaves behind.

This is the body of what ``adapters/github/on-spec-merge.yml`` used to run as
four shell steps, moved into the CLI because pipeline logic belongs in the
product and forge workflow bodies are single-command shims over it
(``spec/features/spec-pipeline.md``). Nothing here is new behavior: every guard
below was already in that workflow, and the notes say which step it came from.

**There is no minting arithmetic, and there must not be again.** The merge
commit IS the version (``spec/decisions/2026-08-28-versions-are-commits.md``),
so there is nothing to allocate and no registry to write before the version
exists. The name this command computes is decoration, and what it *does* is
open a ledger record for a version that already exists.

Three questions, one ``rev-list``
---------------------------------
``spec_commits()`` is ``rev-list --first-parent --reverse <ref> -- <prefix>``,
and the whole command turns on its last two entries and its length:

* **is this a version?** — only if it is the newest spec-touching commit in its
  own first-parent history, i.e. the list's last entry.
* **what is its baseline?** — the entry before that.
* **what is it called?** — ``spec-v<len(list)>``.

The workflow ran the same walk twice and reasoned about the two in prose ("the
baseline step below reads ``sed -n 2p`` of this same rev-list, which is the
previous spec version ONLY when line 1 is this commit"). Reading all three off
one list makes the guard and the baseline agree by construction rather than by
comment: there is no second walk that could answer differently.

Exit codes and the two benign no-ops
------------------------------------
Both no-ops exit **0**, and that is the workflow's behavior preserved exactly,
not a softening of it:

* **Not a spec version.** ``workflow_dispatch`` reaches any commit and the
  likeliest hand-run is the one just after a ledger commit lands — a commit
  touching ``ledger/`` and not ``spec/``. Recording it would write a baseline
  one version too old, silently, into the ledger's only trusted writer. A
  racing merge lands the same way and is equally benign.
* **Replay.** The record either exists for this commit or it does not
  (decision D11). ``open_record()`` is idempotent besides, so the guard exists
  to skip the steps that are *not* — tagging, filing issues, pushing a commit —
  rather than to fail the run. Reddening a re-run of a deliberately idempotent
  job trains people to ignore red.

A caller that needs to tell the two apart reads ``minted``/``reason`` from
``--emit`` rather than the exit code.

A **shallow clone** is the one refusal, and it exits 1. It is not a race and
not a replay; it is a misconfigured checkout, and every one of the three
answers above is wrong under it — the walk stops at the graft, so a commit that
is not a version looks like the newest one, the baseline names the truncation
point, and the count is short, which is how a version gets named after one that
already exists. See ``fetch-depth: 0`` in ``.vellum/memory/areas/cli.md``.

Tagging and pushing stay with the caller
----------------------------------------
This command writes files. It does not tag, and it does not push. The
decorative ``spec-vN`` tag is annotated with the head commit *message* — which
is attacker-supplied text, written by anyone who can land a commit on main — so
it stays in the workflow where it is already passed through ``env`` rather than
interpolated, and where ``continue-on-error`` keeps a failed tag push from
failing a run that has already recorded the version. What this command emits is
the computed *name*; what the caller does with it is the caller's business.

``--commit`` is the one exception, and only because the decision it encodes is
this command's to make: whether a record was written at all. It stages and
commits with a fixed message and stops there. Pushing needs a credential and a
branch, which are deployment facts.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from vellum.gitver import (
    GitUnavailable,
    is_shallow,
    prefix_of,
    repo_root,
    resolve,
    spec_commits,
)
from vellum.ledger import LedgerError, open_record, record_path
from vellum.specfile import resolve_spec_root

#: The identity ledger commits are made under, matching what
#: ``on-spec-merge.yml`` configured before it called git.
COMMITTER_NAME = "vellum-orchestrator"
COMMITTER_EMAIL = "orchestrator@vellum.invalid"


class MintError(Exception):
    """Minting cannot proceed: the checkout is not one this can read."""


@dataclass
class Mint:
    """What one ``vellum mint`` run decided."""

    sha: str
    #: True when a record was written by this run.
    minted: bool
    #: Empty when minted; otherwise which no-op was taken.
    reason: str = ""
    #: Decorative ``spec-vN``. Computed even on a replay, so a caller that has
    #: to re-tag a version whose record already exists still gets the name.
    name: str | None = None
    baseline: str | None = None
    record: Path | None = None
    committed: bool = False
    #: Everything the run has to say, in the order it decided it.
    notes: list[str] = field(default_factory=list)

    def emit(self) -> dict[str, str]:
        """The run as ``key=value`` pairs, for a caller's step outputs."""
        return {
            "sha": self.sha,
            "minted": "yes" if self.minted else "no",
            "reason": self.reason,
            "name": self.name or "",
            "baseline": self.baseline or "",
            "record": str(self.record) if self.record else "",
            "committed": "yes" if self.committed else "no",
        }

    def report(self) -> str:
        return "\n".join(self.notes)


def _git(repo: Path, *args: str) -> None:
    """Run a writing git command, raising ``MintError`` on failure.

    Arguments go as a list, never through a shell: the commit message this
    builds is derived from a computed name, but the habit is what keeps the
    next message that is *not* from becoming an injection site.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout).split())
        raise MintError(f"git {' '.join(args)}: {detail or 'failed'}")


def mint(
    checkout: str | Path,
    ref: str = "HEAD",
    ledger_dir: str | Path | None = None,
    spec_pr: int | None = None,
    labels: list[str] | None = None,
    commit: bool = False,
) -> Mint:
    """Open the ledger record for the spec version at *ref*, if there is one.

    *ledger_dir* defaults to ``<repo root>/ledger``, which is where the intent
    repo keeps it and what the workflow passed.
    """
    # `SpecTreeError` deliberately propagates: "that path is not a spec tree"
    # is what exit 2 means everywhere else in this CLI, and a caller telling a
    # bad checkout from a bad repository should not have to read the message.
    spec_root = resolve_spec_root(checkout)
    try:
        repo = repo_root(spec_root)
        prefix = prefix_of(repo, spec_root)
        # Asked before anything is decided, because every answer below is
        # derived from a history a shallow clone does not have.
        if is_shallow(repo):
            raise MintError(
                f"{repo} is a shallow clone. A version is a commit and minting reads "
                f"the first-parent history to decide whether this commit is one, what "
                f"it descends from and what to call it — all three are wrong below the "
                f"graft. Fetch the full history (fetch-depth: 0)."
            )
        sha = resolve(repo, ref)
        versions = spec_commits(repo, sha, prefix)
    except (GitUnavailable, ValueError) as exc:
        raise MintError(f"{checkout}: cannot read the spec history: {exc}") from exc

    ledger = Path(ledger_dir) if ledger_dir is not None else repo / "ledger"

    # The version half of the workflow's guard: this sha is a version iff it is
    # the newest spec-touching commit in its own first-parent history.
    if not versions or versions[-1] != sha:
        newest = versions[-1] if versions else None
        return Mint(
            sha=sha,
            minted=False,
            reason="not-a-spec-version",
            notes=[
                f"{sha} does not change {prefix or 'the spec tree'}, so there is no "
                f"version here to record. This run is a no-op.",
                f"The newest spec version in its history is {newest}."
                if newest
                else "No spec version exists in its history at all.",
            ],
        )

    name = f"spec-v{len(versions)}"
    baseline = versions[-2] if len(versions) > 1 else None

    # The replay half. `find_record` matches on the record's `spec_version`
    # field rather than only the filename, so a record written under the full
    # forty is still found when a caller passes an abbreviation.
    try:
        existing = _existing(ledger, sha)
    except LedgerError as exc:
        raise MintError(str(exc)) from exc
    if existing is not None:
        return Mint(
            sha=sha,
            minted=False,
            reason="replay",
            name=name,
            baseline=baseline,
            record=existing,
            notes=[
                f"{existing} already records {sha}; this run is a replay.",
                f"Its decorative name is {name}.",
            ],
        )

    try:
        path, created = open_record(
            ledger,
            sha,
            spec_pr=spec_pr,
            baseline=baseline,
            labels=list(labels or []),
            name=name,
        )
    except LedgerError as exc:
        raise MintError(str(exc)) from exc

    result = Mint(
        sha=sha,
        minted=created,
        name=name,
        baseline=baseline,
        record=path,
        notes=[
            f"Minted {sha}",
            f"  name     {name} (decoration; nothing reads it to decide anything)",
            f"  baseline {baseline or '(none — this is the first spec version)'}",
            f"  record   {path} (state approved)",
        ],
    )

    if commit:
        _commit_record(repo, ledger, result)
    else:
        result.notes.append(f"Nothing was committed. To commit: git add {ledger}")
    return result


def _existing(ledger: Path, sha: str) -> Path | None:
    """The record already holding *sha*, or None.

    ``find_record`` is imported here rather than at module scope only to keep
    the import list above naming what the flow reads at a glance; it is the
    same function ``ledger advance`` resolves records with, deliberately, so
    "does a record exist" and "which record would I advance" cannot disagree.
    """
    from vellum.ledger import find_record

    found = find_record(ledger, sha)
    if found is not None:
        return found
    # A record whose filename is the sha but which `find_record` skipped —
    # unreadable YAML, say — must not read as "no record": that would mint a
    # second one over it.
    direct = record_path(ledger, sha)
    return direct if direct.exists() else None


def _commit_record(repo: Path, ledger: Path, result: Mint) -> None:
    """Stage the ledger directory and commit it under a fixed message.

    Fixed, and derived only from what this command computed: the head commit's
    own message is attacker-supplied and has no business in a message this
    process builds. Pushing is not done here — it needs a credential and a
    branch, which are the caller's.
    """
    _git(repo, "config", "user.name", COMMITTER_NAME)
    _git(repo, "config", "user.email", COMMITTER_EMAIL)
    try:
        rel = ledger.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError as exc:
        raise MintError(
            f"{ledger} is outside {repo}; --commit only stages a ledger inside the checkout"
        ) from exc
    _git(repo, "add", "--", rel)
    staged = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", rel],
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode == 0:
        result.notes.append("Nothing to commit: the record is already in the tree.")
        return
    message = f"ledger: open {result.name or result.sha}"
    _git(repo, "commit", "-q", "-m", message)
    result.committed = True
    result.notes.append(f"Committed: {message} (not pushed — that is the caller's)")
