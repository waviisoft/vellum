"""``vellum release cut`` and ``vellum suite partition`` — cuts and conformance.

Two commands, one subject: ``ledger/releases.yaml``, the file that until now
held a shape and nothing else. ``spec/features/ledger.md`` says what is in it —
"``releases.yaml`` records cuts (pinned waves, per-repo versions, channel,
optional friendly stamp) and the live pointers as commit shas: ``spec_head``,
and ``spec_conformed`` per channel" — and
``spec/features/certification-and-releases.md`` says what a cut *is*.

Which half is the product's and which is the deployment's
-----------------------------------------------------------
The spec sentence is one sentence and it names two different kinds of work:

    A cut pins the merged waves and per-repo versions, **runs the FULL enforced
    suite against the composed candidate**, and promotes to its channel.

Pinning and promoting are repository state — a file in the intent repo — and
are performed here. *Running the suite against a composed candidate* is not:
it needs a deployment to compose, a runner to execute against it, and neither
exists (``harness/support/adapter.py`` names the gap ``release-machinery``
beside ``certification-runner``). So the result of that run is **supplied by the
caller**, through ``--suite-result``, exactly as ``backpressure --pending``,
``budget --projected`` and ``certify check --head`` take the numbers only a
forge or a not-yet-built runner can know. Do not reach for a runner here.

That division is why the promotion half is opt-in rather than implied:

* with no ``--suite-result`` the cut is **recorded and not promoted** — the
  bookkeeping half, done, and the report says the suite result was not
  supplied rather than implying the candidate was proved;
* ``--suite-result green`` promotes: the channel's ``spec_conformed`` advances
  to the newest wave in the cut and every wave the cut names goes to
  ``shipped``, which is how a version leaves the divergence window
  (``vellum backpressure``);
* ``--suite-result red`` records the cut and refuses to promote, because
  "promotion occurs only if it passes" (``@id:full-suite-at-cut``). The
  rollback and the regression issue the spec asks for next are the
  deployment's — this command writes no forge state and files nothing.

Ancestry, never names
---------------------
``spec_conformed`` is a commit and the partition is an ancestry question
(``spec/decisions/2026-08-28-versions-are-commits.md``). Every ordering
decision in this module goes through ``vellum.gitver.is_ancestor``; nothing
reads a ``spec-v*`` name to decide anything, and there is no dating code here
at all — ``vellum suite extract`` dates the scenarios and this module reads the
versions it computed.

Two consequences worth stating, because both are the dangerous direction:

* **A shallow clone refuses.** Below the graft, ``merge-base --is-ancestor``
  answers "no" for commits that really are ancestors, so a truncated history
  makes an *enforced* scenario look armed — and an armed scenario is one
  conformance monitoring does not run. That is a regression check quietly
  switched off, so both commands refuse rather than answer from a history they
  can see is incomplete. ``vellum mint`` refuses a shallow clone for the
  sibling reason and this follows its exit code.
* **The pointer never moves backwards.** Advancing ``spec_conformed`` to a
  commit the current pointer is not an ancestor of would re-arm scenarios that
  are already enforced, which un-enforces regressions that have been caught for
  as long as the pointer has been where it is. Refused, not warned about.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vellum.backpressure import ledger_dir_for
from vellum.chain import CERTIFIABLE_STATES
from vellum.gitver import GitUnavailable, is_ancestor, is_shallow, repo_root
from vellum.ledger import (
    FULL_SHA_RE,
    SHA_RE,
    LedgerError,
    dump,
    find_record,
    load,
    ordered as _ordered,
    parse_time,
    parse_version,
)
from vellum.ledger import now as ledger_now
from vellum.suite import DroppedScenarios, extract, to_dict
from vellum.text import one_line

#: The file every command here reads and ``release cut`` writes.
RELEASES_RELPATH = "releases.yaml"

#: Where the products a cut may pin are declared: "``.vellum/workspace.yaml``
#: maps the products" (``spec/features/repo-topology.md``). Read here rather
#: than modelled in a module of its own, because this is its only reader —
#: a schema written ahead of one is a second place for the shape to drift.
WORKSPACE_RELPATH = Path(".vellum") / "workspace.yaml"

#: Fixed emission order for ``releases.yaml``, and for one cut inside it. The
#: reason ``vellum.ledger`` fixes its own: a state change is then a one-line
#: diff, and a read/write round-trip is byte-stable. Unrecognised keys are kept
#: and written after these, never dropped — this file is the intent repo's and
#: an installation may carry keys this version does not model.
RELEASES_KEYS = ("spec_head", "channels", "cuts", "stamps")
CUT_KEYS = ("id", "at", "channel", "waves", "versions", "suite_result", "promoted")

#: The two results the enforced suite at a cut can have. ``green`` promotes;
#: ``red`` does not. Absent is a third thing and not a default for either — see
#: the module docstring.
SUITE_RESULTS = ("green", "red")
GREEN = "green"

#: A product name in ``.vellum/workspace.yaml``, or a forge repo slug. Kept
#: narrow deliberately: a cut's ``versions`` mapping is written into the intent
#: repo's ledger and read back out of it, and a key carrying a newline or a
#: colon is a key that reshapes the file it is written into.
_PRODUCT_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?$")


class ReleaseError(Exception):
    """The command could not answer.

    No ledger directory, no ``releases.yaml``, a channel the file does not
    declare, a ``--versions`` entry that is not ``<product>=<sha>``. Exit 2
    everywhere, like every other guard's own error: 1 is the answer a caller
    acts on, and a mistyped flag arriving as one is a red nobody can find the
    cause of.
    """


class ReleaseRefused(Exception):
    """The command answered, and the answer is that this cannot proceed.

    A wave with no ledger record, a pointer that would move backwards, a
    shallow clone, a red suite at the cut. Exit 1 — deliberately a sibling of
    `ReleaseError` rather than a subclass, so no ``except`` ordering decides
    which code a caller gets.
    """


# --------------------------------------------------------------- reading

def releases_path(ledger: Path) -> Path:
    return Path(ledger) / RELEASES_RELPATH


def _ledger(checkout: str | Path, ledger_dir: str | Path | None) -> Path:
    ledger = Path(ledger_dir) if ledger_dir is not None else ledger_dir_for(checkout)
    if not ledger.is_dir():
        raise ReleaseError(
            f"{ledger}: no ledger directory. Cuts and conformance pointers live in "
            f"{RELEASES_RELPATH} inside it, so there is nothing here to read or "
            f"write; is {checkout} an intent checkout?"
        )
    return ledger


def load_releases(ledger: Path) -> dict:
    """``releases.yaml`` as a mapping, or raise.

    An absent file is an error rather than an empty default. The channels are
    declared in it, and defaulting would let ``--channel producton`` invent a
    channel, advance a pointer nothing reads, and report success — an allowlist
    turned off by a typo, which is the failure `normalise_tree` exists to
    prevent for write boundaries.
    """
    path = releases_path(ledger)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(
            f"{path}: cannot read the release pointers: {exc}. This file declares "
            f"the channels, so a cut cannot be recorded without it."
        ) from exc
    except yaml.YAMLError as exc:
        raise ReleaseError(f"{path}: not valid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ReleaseError(f"{path}: {RELEASES_RELPATH} is not a YAML mapping")
    return data


def channel_entry(data: dict, channel: str, path: Path) -> dict:
    """``channels.<channel>``, or raise naming the channels there are."""
    channels = data.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ReleaseError(
            f"{path}: declares no channels, so there is no {channel!r} to cut to."
        )
    entry = channels.get(channel)
    if not isinstance(entry, dict):
        known = ", ".join(sorted(str(k) for k in channels)) or "(none)"
        raise ReleaseError(
            f"{path}: no channel {channel!r}. Declared channels: {known}. A "
            f"channel is not created by cutting to it — a typo would otherwise "
            f"advance a pointer nothing reads."
        )
    return entry


def conformed_pointer(data: dict, channel: str, path: Path) -> str | None:
    """``channels.<channel>.spec_conformed`` as a sha, or None when unset."""
    value = channel_entry(data, channel, path).get("spec_conformed")
    if value is None:
        return None
    sha = str(value).strip().lower()
    if not SHA_RE.match(sha):
        raise ReleaseError(
            f"{path}: channels.{channel}.spec_conformed is {value!r}, which is "
            f"not a commit sha. A conformance pointer is a commit "
            f"(spec/decisions/2026-08-28-versions-are-commits.md); a name is "
            f"decoration and nothing may be decided on one."
        )
    return sha


def products(checkout: str | Path) -> dict[str, str]:
    """``{product name: repo}`` from ``.vellum/workspace.yaml``.

    A cut pins a version per product repo, so the set of products is an
    allowlist and is read from the installation rather than from the caller.
    Missing is an error, not an empty allowlist: the failure a silent default
    buys is a cut pinning ``cor=<sha>`` and saying nothing, which is precisely
    the version set a later release is composed from.
    """
    path = Path(checkout) / WORKSPACE_RELPATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(
            f"{path}: cannot read the workspace: {exc}. It maps this "
            f"installation's product repos (spec/features/repo-topology.md), and "
            f"a cut pins a version per repo, so the products it may name are read "
            f"from here rather than accepted from the command line."
        ) from exc
    except yaml.YAMLError as exc:
        raise ReleaseError(f"{path}: not valid YAML: {exc}") from exc
    entries = data.get("products") if isinstance(data, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ReleaseError(f"{path}: declares no products, so a cut can pin nothing")
    found: dict[str, str] = {}
    for name, value in entries.items():
        repo = value.get("repo") if isinstance(value, dict) else value
        found[str(name)] = str(repo) if repo is not None else ""
    return found


def parse_versions(pairs: list[str], declared: dict[str, str], where: Path) -> dict[str, str]:
    """``["core=<sha>"]`` -> ``{"core": "<sha>"}``, validated both halves.

    The product must be one the workspace declares — by its name or by its repo
    slug, since a caller composing a candidate is as likely to hold one as the
    other — and is normalised to the name, so two spellings of one repo cannot
    appear as two entries in the same cut.

    The sha is the whole forty and an abbreviation is refused rather than
    resolved, for the reason ``parse_certified_sha`` refuses one: a prefix names
    a *set* of commits, and this value is what a composed candidate is built
    from. Nothing here can resolve it either — these are commits in a product
    repo, and this command is reading the intent repo.
    """
    by_repo = {repo: name for name, repo in declared.items() if repo}
    versions: dict[str, str] = {}
    for pair in pairs:
        product, sep, sha = str(pair).partition("=")
        product, sha = product.strip(), sha.strip().lower()
        if not sep or not product or not sha:
            raise ReleaseError(
                f"--versions entry {pair!r} is not <product>=<sha>. A cut pins one "
                f"commit per product repo."
            )
        if not _PRODUCT_RE.match(product):
            raise ReleaseError(
                f"--versions names product {product!r}, which is not a product name "
                f"or a repo slug"
            )
        name = product if product in declared else by_repo.get(product)
        # A name reached through the repo slug comes out of the workspace file
        # rather than off the command line, so it has not been through
        # `_PRODUCT_RE`. It becomes a key in the cut's `versions` mapping, and a
        # mapping key is the half of this file a reader keys off — so it is held
        # to the same shape as one a caller types.
        if name is not None and not _PRODUCT_RE.match(name):
            raise ReleaseError(
                f"{where} declares a product named {name!r}, which is not a "
                f"usable product name; a cut keys its version set by that name."
            )
        if name is None:
            known = ", ".join(sorted(declared)) or "(none)"
            raise ReleaseError(
                f"--versions names {product!r}, which {where} does not declare. "
                f"Declared products: {known}."
            )
        if not FULL_SHA_RE.match(sha):
            raise ReleaseError(
                f"--versions {product}={sha!r} is not a full 40-character commit "
                f"sha. A cut pins the commit a candidate is composed from, and an "
                f"abbreviation names a set of them — so it is refused rather than "
                f"resolved, the same rule a certified sha follows."
            )
        if name in versions and versions[name] != sha:
            raise ReleaseError(
                f"--versions pins two different commits for {name!r}: "
                f"{versions[name][:12]} and {sha[:12]}"
            )
        versions[name] = sha
    if not versions:
        raise ReleaseError(
            "--versions is required: a cut pins the per-repo version set "
            "(spec/features/ledger.md), and a cut pinning nothing composes nothing."
        )
    return versions


# --------------------------------------------------------------- the cut

@dataclass
class Cut:
    """One recorded cut, and what recording it did."""

    ledger: Path
    channel: str
    cut_id: str
    at: str
    #: ``(sha, decorative name or None)`` per wave, in the order given.
    waves: list[tuple[str, str | None]]
    versions: dict[str, str]
    suite_result: str | None
    promoted: bool
    conformed_before: str | None
    conformed_after: str | None
    #: Ledger record filenames advanced to ``shipped`` by the promotion.
    shipped: list[str] = field(default_factory=list)
    #: True when this invocation found its own cut already recorded and wrote
    #: nothing new. A replay is exit 0 and silent in the file, for the reason
    #: ``open_record`` is idempotent (decision D11).
    replayed: bool = False

    def report(self) -> str:
        lines = [
            f"Cut {one_line(self.cut_id, 80)}",
            "",
            f"  channel   {one_line(self.channel, 40)}",
            f"  at        {one_line(self.at, 40)}",
        ]
        for sha, name in self.waves:
            lines.append(f"  wave      {sha[:12]}  {name or ''}".rstrip())
        for product, sha in sorted(self.versions.items()):
            lines.append(f"  version   {one_line(product, 40)} = {sha[:12]}")
        lines.append("")
        if self.replayed:
            lines.append(
                "This cut is already recorded with the same waves and versions; "
                "nothing was written."
            )
            lines.append("")
        if self.promoted:
            before = self.conformed_before[:12] if self.conformed_before else "(none)"
            lines.append(
                f"PROMOTED: {self.channel} spec_conformed {before} -> "
                f"{(self.conformed_after or '')[:12]}"
            )
            if self.shipped:
                lines.append(
                    f"  {len(self.shipped)} record(s) advanced to shipped: "
                    f"{', '.join(one_line(n, 80) for n in self.shipped)}"
                )
            else:
                lines.append("  no record needed advancing; all were already shipped")
        elif self.suite_result == "red":
            lines.append(
                "NOT PROMOTED: the enforced suite at this cut was reported red, and "
                "promotion occurs only if it passes "
                "(spec/features/certification-and-releases.md). The cut is recorded; "
                "the rollback and the regression issue the spec asks for next are "
                "the deployment's, and this command files neither."
            )
        else:
            lines.append(
                "NOT PROMOTED: no --suite-result was supplied, so the cut is "
                "recorded and the channel's pointer is untouched. The full enforced "
                "suite runs against the composed candidate on infrastructure this "
                "command does not have; pass its result to promote."
            )
        lines.append("")
        lines.append(
            f"Ledger guard: run `vellum ledger verify {self.ledger.parent} --strict` "
            f"— it reads cuts out of {RELEASES_RELPATH}, so it can only judge this "
            f"cut once the cut is recorded."
        )
        return "\n".join(lines)


def _wave_record(ledger: Path, given: str) -> tuple[Path, dict, str, str | None]:
    """``(path, record, sha, name)`` for one wave a cut names.

    A cut names waves and ``vellum ledger verify`` faults one with no record
    (``unknown-wave``), so the refusal is made here rather than written into the
    file and discovered by the guard afterwards.

    The record's ``spec_version`` is asked for twice over — that it is a sha,
    and that it agrees by prefix with the sha that reached it — which is the
    check ``vellum pin advance`` grew after a crafted ``ledger/<A>.yaml`` saying
    ``spec_version: <B>`` pinned B while the operator asked for A. This value
    becomes a channel's conformance pointer, so it is the same class of field.
    """
    sha = parse_version(given)
    path = find_record(ledger, sha)
    if path is None:
        raise ReleaseRefused(
            f"no ledger record for wave {sha}. A cut names merged waves, and a "
            f"wave with no record is what `vellum ledger verify` reports as "
            f"unknown-wave — so it is refused here rather than recorded and "
            f"faulted afterwards."
        )
    try:
        record = load(path)
    except LedgerError as exc:
        raise ReleaseRefused(f"wave {sha}: {exc}") from exc
    recorded = str(record.get("spec_version") or "").strip().lower()
    if not SHA_RE.match(recorded):
        raise ReleaseRefused(
            f"{path.name}: spec_version is {record.get('spec_version')!r}, which is "
            f"not a commit sha. A cut pins waves by commit and this record names none."
        )
    if not (recorded.startswith(sha) or sha.startswith(recorded)):
        raise ReleaseRefused(
            f"{path.name} was reached for wave {sha[:12]} but records "
            f"{recorded[:12]}. A record that does not agree about which version it "
            f"is cannot pin one."
        )
    name = record.get("name")
    name = str(name).strip() if isinstance(name, str) and name.strip() else None
    return path, record, recorded, name


def _newest(repo: Path, shas: list[str]) -> str:
    """The one sha in *shas* every other is an ancestor of.

    Ancestry, never ``max()`` and never a recorded timestamp: shas do not
    compare and a name is decoration. The reconciler's approved-time fallback
    is deliberately *not* reused here — it exists so a report can still order
    two records, and this orders a pointer *write*. A pointer set from the
    weaker ordering would arm or un-arm scenarios on the strength of two
    timestamps that may be equal.
    """
    if len(shas) == 1:
        return shas[0]
    try:
        for candidate in shas:
            if all(is_ancestor(repo, other, candidate) for other in shas):
                return candidate
    except GitUnavailable as exc:
        raise ReleaseRefused(
            f"the cut's waves cannot be ordered by ancestry in {repo}: {exc}. A "
            f"conformance pointer is the newest wave in the cut, and 'newest' is "
            f"an ancestry question this checkout cannot answer."
        ) from exc
    raise ReleaseRefused(
        f"the cut's waves do not lie on one line of ancestry "
        f"({', '.join(s[:12] for s in shas)}), so none of them is the newest and "
        f"there is no pointer to advance to. Cut the waves that were merged onto "
        f"one line, or cut them separately."
    )


def _require_full_history(checkout: str | Path) -> Path:
    """The checkout's repo root, once it is one and its history is whole."""
    try:
        repo = repo_root(Path(checkout))
    except (GitUnavailable, ValueError) as exc:
        raise ReleaseRefused(
            f"{checkout} is not inside a readable git repository, so ancestry "
            f"cannot be asked: {exc}"
        ) from exc
    if is_shallow(repo):
        raise ReleaseRefused(
            f"{repo} is a shallow clone. Below the graft `merge-base --is-ancestor` "
            f"answers 'no' for commits that really are ancestors, which makes an "
            f"enforced scenario look armed — a regression check switched off with "
            f"nothing saying so. Fetch the full history (fetch-depth: 0)."
        )
    return repo


def _cut_matches(existing: dict, waves: list[str], versions: dict[str, str], channel: str) -> bool:
    """Whether an already-recorded cut is *this* cut being replayed."""
    recorded = existing.get("waves")
    recorded = [str(w).strip().lower() for w in recorded] if isinstance(recorded, list) else []
    theirs = existing.get("versions")
    theirs = {str(k): str(v).strip().lower() for k, v in theirs.items()} if isinstance(theirs, dict) else {}
    return (
        str(existing.get("channel")) == channel
        and recorded == waves
        and theirs == versions
    )


def cut(
    checkout: str | Path,
    channel: str,
    waves: list[str],
    versions: list[str],
    ledger_dir: str | Path | None = None,
    at: str | None = None,
    suite_result: str | None = None,
) -> Cut:
    """Record a cut, and promote it when the enforced suite at it was green.

    Returns the `Cut`. Raises `ReleaseError` when it cannot answer and
    `ReleaseRefused` when the answer is that this cut cannot be made.
    """
    if suite_result is not None and suite_result not in SUITE_RESULTS:
        raise ReleaseError(
            f"--suite-result {suite_result!r} is not one of {', '.join(SUITE_RESULTS)}"
        )
    if not waves:
        raise ReleaseError(
            "--wave is required: a cut pins the merged waves "
            "(spec/features/certification-and-releases.md), and which waves are in "
            "it is not something this command may infer — an approved record is not "
            "a merged wave."
        )
    ledger = _ledger(checkout, ledger_dir)
    path = releases_path(ledger)
    data = load_releases(ledger)
    entry = channel_entry(data, channel, path)
    conformed_before = conformed_pointer(data, channel, path)

    if at is not None and parse_time(at) is None:
        raise ReleaseError(
            f"--at {at!r} is not an ISO 8601 moment (e.g. 2026-08-31T14:00:00Z)"
        )
    moment = at or ledger_now()

    pinned = parse_versions(versions, products(checkout), Path(checkout) / WORKSPACE_RELPATH)

    resolved: list[tuple[Path, dict, str, str | None]] = []
    seen: set[str] = set()
    for given in waves:
        wave_path, record, sha, name = _wave_record(ledger, given)
        if sha in seen:
            continue
        seen.add(sha)
        resolved.append((wave_path, record, sha, name))
    shas = [sha for _, _, sha, _ in resolved]

    promote = suite_result == GREEN
    conformed_after = conformed_before
    if promote:
        # A promoting cut writes `state: shipped` onto every wave it names, and
        # `shipped` is in CERTIFIABLE_STATES — so without this check a cut would
        # satisfy `vellum ledger verify`'s `uncertified-wave` finding *by having
        # been made*, which is a guard grading its own input. The states are
        # imported from `chain.py` rather than restated, so the command and the
        # guard cannot come to disagree about what a certifiable wave is.
        #
        # This is the proxy `chain.py` documents, not a certification check:
        # certification binds to a work item's PR head and a cut names waves,
        # and nothing joins the two (`harness/support/adapter.py`,
        # `certification-runner`). Do not promote it to one here either.
        unready = [
            (sha, str(record.get("state") or "").strip() or "(none)")
            for _, record, sha, _ in resolved
            if str(record.get("state") or "").strip() not in CERTIFIABLE_STATES
        ]
        if unready:
            listed = ", ".join(f"{sha[:12]} ({state})" for sha, state in unready)
            raise ReleaseRefused(
                f"{len(unready)} wave(s) this cut names have not reached "
                f"{' or '.join(CERTIFIABLE_STATES)}: {listed}. "
                f"`@id:full-suite-at-cut` is about *certified* waves merged since "
                f"the last release, and `vellum ledger verify` reports a cut naming "
                f"one of these as uncertified-wave. Promoting would write "
                f"`shipped` onto them and so make the cut pass that check by "
                f"having been made. Record the cut without --suite-result to pin "
                f"the candidate, and promote once the waves are verified."
            )
        repo = _require_full_history(checkout)
        newest = _newest(repo, shas)
        if conformed_before is not None and conformed_before != newest:
            try:
                forward = is_ancestor(repo, conformed_before, newest)
            except GitUnavailable as exc:
                raise ReleaseRefused(
                    f"cannot tell whether {newest[:12]} is ahead of the channel's "
                    f"current pointer {conformed_before[:12]}: {exc}"
                ) from exc
            if not forward:
                raise ReleaseRefused(
                    f"{channel}.spec_conformed is {conformed_before[:12]} and this "
                    f"cut's newest wave is {newest[:12]}, which is not a descendant "
                    f"of it. Moving the pointer there would re-arm every scenario "
                    f"between the two — scenarios conformance monitoring enforces "
                    f"today would stop being run as regressions."
                )
        conformed_after = newest

    cut_id = f"{channel}@{moment}"
    cuts = data.get("cuts")
    if cuts in (None, ""):
        cuts = []
    if not isinstance(cuts, list):
        raise ReleaseError(f"{path}: cuts is {cuts!r}, not a list")
    cuts = list(cuts)

    existing_index = next(
        (
            i
            for i, c in enumerate(cuts)
            if isinstance(c, dict) and str(c.get("id")) == cut_id
        ),
        None,
    )
    replayed = False
    if existing_index is not None:
        existing = cuts[existing_index]
        if not _cut_matches(existing, shas, pinned, channel):
            raise ReleaseRefused(
                f"a different cut is already recorded as {cut_id}. A cut is a "
                f"pinned candidate and rewriting one would change what a recorded "
                f"release was; pass a different --at, or read the file."
            )
        was_promoted = bool(existing.get("promoted"))
        if was_promoted and not promote:
            raise ReleaseRefused(
                f"{cut_id} is already recorded as promoted, and this invocation "
                f"would record it as not promoted. A promotion that happened is "
                f"not un-recorded by re-running the cut."
            )
        # A cut recorded with no result may gain one — the suite genuinely runs
        # elsewhere and its answer genuinely arrives later, which is the whole
        # reason `--suite-result` is a separate invocation's worth of
        # information. A result that is already recorded is not overwritten:
        # `red` quietly becoming `null` on a re-run would lose the only record
        # that the candidate was tested and failed.
        recorded_result = existing.get("suite_result")
        recorded_result = str(recorded_result) if recorded_result is not None else None
        if recorded_result is not None and recorded_result != suite_result:
            raise ReleaseRefused(
                f"{cut_id} already records suite_result {recorded_result!r} and "
                f"this invocation reports {suite_result!r}. A recorded result is "
                f"not overwritten; a new run against the same candidate is a new "
                f"cut, so pass a different --at."
            )
        replayed = was_promoted == promote and recorded_result == suite_result

    record_entry = _ordered(
        {
            "id": cut_id,
            "at": moment,
            "channel": channel,
            "waves": shas,
            "versions": dict(sorted(pinned.items())),
            "suite_result": suite_result,
            "promoted": promote,
        },
        CUT_KEYS,
    )

    shipped: list[str] = []
    if not replayed:
        if existing_index is None:
            cuts.append(record_entry)
        else:
            cuts[existing_index] = record_entry
        data["cuts"] = cuts
        if promote:
            entry["spec_conformed"] = conformed_after
            for wave_path, record, sha, _ in resolved:
                if str(record.get("state") or "").strip() == "shipped":
                    continue
                record["state"] = "shipped"
                record["release"] = cut_id
                wave_path.write_text(dump(record), encoding="utf-8")
                shipped.append(wave_path.name)
        path.write_text(
            yaml.safe_dump(_ordered(data, RELEASES_KEYS), sort_keys=False, width=100),
            encoding="utf-8",
        )

    return Cut(
        ledger=ledger,
        channel=channel,
        cut_id=cut_id,
        at=moment,
        waves=[(sha, name) for _, _, sha, name in resolved],
        versions=pinned,
        suite_result=suite_result,
        promoted=promote,
        conformed_before=conformed_before,
        conformed_after=conformed_after,
        shipped=shipped,
        replayed=replayed,
    )


def run_cut(
    checkout: str,
    channel: str,
    waves: list[str],
    versions: list[str],
    ledger_dir: str | None = None,
    at: str | None = None,
    suite_result: str | None = None,
    out=None,
) -> int:
    """Record the cut and report it. Exit 1 when the suite at it was red."""
    stream = out if out is not None else sys.stdout
    recorded = cut(
        checkout,
        channel,
        waves,
        versions,
        ledger_dir=ledger_dir,
        at=at,
        suite_result=suite_result,
    )
    print(recorded.report(), file=stream)
    if recorded.suite_result == "red":
        # An answer the caller acts on, not a failure to answer: the cut is
        # recorded and the channel was not promoted, which is exactly what
        # `@id:full-suite-at-cut` asks for. `ledger verify`'s findings share
        # this code for the same reason.
        print(
            f"vellum: the enforced suite at {recorded.cut_id} was red; "
            f"{channel} was not promoted",
            file=sys.stderr,
        )
        return 1
    return 0


# ------------------------------------------------------------- partition

@dataclass
class Scenario:
    """One scenario as the partition sees it: an id, where it lives, its version."""

    id: str
    file: str
    version: str | None


@dataclass
class Partition:
    """The suite split into armed and enforced against one channel's pointer."""

    channel: str
    conformed: str | None
    #: Why there is no pointer, when there is none. Empty otherwise.
    why_not: str
    source: str
    spec_version: str | None
    armed: list[Scenario] = field(default_factory=list)
    enforced: list[Scenario] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.armed) + len(self.enforced)

    def report(self) -> str:
        pointer = self.conformed[:12] if self.conformed else "(none)"
        lines = [
            f"Suite partition for channel {one_line(self.channel, 40)}",
            "",
            f"  spec_conformed  {pointer}",
            f"  suite           {one_line(self.source, 200)}"
            + (f" at {self.spec_version[:12]}" if self.spec_version else ""),
            f"  scenarios       {self.count} ({len(self.enforced)} enforced, "
            f"{len(self.armed)} armed)",
            "",
        ]
        lines.append(f"ENFORCED ({len(self.enforced)}) — run in conformance monitoring; "
                     f"a failure is a regression")
        for scenario in self.enforced:
            lines.append(
                f"  {one_line(scenario.id, 60)}  "
                f"{(scenario.version or '')[:12]}  {one_line(scenario.file, 60)}"
            )
        if not self.enforced:
            lines.append("  (none)")
        lines.append("")
        lines.append(f"ARMED ({len(self.armed)}) — define done for in-flight work; "
                     f"expected to fail against this channel")
        for scenario in self.armed:
            version = (scenario.version or "").strip()
            lines.append(
                f"  {one_line(scenario.id, 60)}  "
                f"{version[:12] if version else 'pending':<12}  "
                f"{one_line(scenario.file, 60)}"
            )
        if not self.armed:
            lines.append("  (none)")
        lines.append("")
        if self.conformed is None:
            lines.append(
                f"Every scenario is armed: {self.why_not}. A channel that has "
                f"conformed to nothing enforces nothing, which is the honest "
                f"reading and not a defect — record a cut to advance the pointer."
            )
        else:
            lines.append(
                "Armed and enforced are decided by ancestry against the pointer "
                "(spec/decisions/2026-08-28-versions-are-commits.md): at or below "
                "it is enforced, anything else is armed. A scenario carried by no "
                "commit in this checkout's ancestry is pending, and pending is armed."
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "spec_conformed": self.conformed,
            "source": self.source,
            "spec_version": self.spec_version,
            "scenario_count": self.count,
            "enforced": [
                {"id": s.id, "file": s.file, "version": s.version} for s in self.enforced
            ],
            "armed": [
                {"id": s.id, "file": s.file, "version": s.version} for s in self.armed
            ],
        }


def _suite_payload(checkout: str | Path, suite_path: str | Path | None) -> tuple[dict, str]:
    """``(suite.json payload, where it came from)``.

    A recorded ``ledger/suite-<sha>.json`` is a legitimate input — it is what
    the ledger guard reads, and partitioning the suite a cut was judged against
    is a different question from partitioning the working tree. Extraction is
    the default because the ordinary caller is conformance monitoring, which
    wants the tree it is monitoring.
    """
    if suite_path is not None:
        path = Path(suite_path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ReleaseError(f"{path}: cannot read the suite: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"{path}: not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ReleaseError(f"{path}: the suite is not a JSON object")
        return payload, str(path)
    try:
        return to_dict(extract(checkout)), f"extracted from {checkout}"
    except DroppedScenarios as exc:
        # The partition is a statement about the *whole* suite, so a tree that
        # extracts short cannot be partitioned honestly: the missing scenarios
        # would be absent from both halves with nothing saying so, which is the
        # silent absence `extract` refuses for.
        raise ReleaseRefused(
            f"{checkout}: {len(exc.errors)} gherkin block(s) would leave scenarios "
            f"out of the suite ({', '.join(exc.codes)}), so there is no whole suite "
            f"to partition. `vellum suite extract {checkout}` names each one."
        ) from exc


def partition(
    checkout: str | Path,
    channel: str,
    ledger_dir: str | Path | None = None,
    suite_path: str | Path | None = None,
) -> Partition:
    """Split the suite into armed and enforced against *channel*'s pointer.

    "Scenarios above a channel's ``spec-conformed`` pointer are *armed* ...; at
    or below are *enforced*" (``spec/features/scenarios-and-harness.md``), and
    ``@id:armed-not-enforced`` states the test as ancestry: "a scenario whose
    introducing commit is not an ancestor of the production conformance
    pointer". So the test is ``is_ancestor(version, spec_conformed)``, inclusive
    — a scenario introduced *at* the pointer is enforced, which is what
    ``merge-base --is-ancestor`` already answers for a commit against itself.
    """
    ledger = _ledger(checkout, ledger_dir)
    path = releases_path(ledger)
    data = load_releases(ledger)
    conformed = conformed_pointer(data, channel, path)
    why_not = "" if conformed else (
        f"{path.name} records no conformed baseline for channel {channel!r}"
    )

    payload, source = _suite_payload(checkout, suite_path)
    if payload.get("shallow"):
        raise ReleaseRefused(
            f"{source} was extracted from a shallow clone, so every scenario below "
            f"the graft is dated forward onto the truncation point. Partitioning it "
            f"would report enforced scenarios as armed — a regression check "
            f"switched off with nothing saying so. Fetch the full history."
        )

    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        raise ReleaseError(f"{source}: the suite declares no scenarios list")

    armed: list[Scenario] = []
    enforced: list[Scenario] = []
    decided: dict[str, bool] = {}
    repo = _require_full_history(checkout) if conformed else None
    for raw in scenarios:
        if not isinstance(raw, dict):
            raise ReleaseError(f"{source}: a scenario entry is {raw!r}, not an object")
        version = raw.get("version")
        version = str(version).strip().lower() if isinstance(version, str) else None
        entry = Scenario(
            id=str(raw.get("id") or ""),
            file=str(raw.get("file") or ""),
            version=version,
        )
        # No pointer, or no version: armed. A scenario the checkout's ancestry
        # does not carry is `pending` — it is in nobody's history yet, so it is
        # above every pointer there is, which is the same answer ancestry gives.
        if conformed is None or version is None or not SHA_RE.match(version):
            armed.append(entry)
            continue
        if version not in decided:
            try:
                decided[version] = is_ancestor(repo, version, conformed)
            except GitUnavailable as exc:
                raise ReleaseRefused(
                    f"cannot tell whether {version[:12]} is an ancestor of "
                    f"{conformed[:12]} in {repo}: {exc}. The partition is decided by "
                    f"ancestry, and a scenario this checkout cannot place is one it "
                    f"must not guess about — guessing 'armed' takes a regression "
                    f"check out of conformance monitoring."
                ) from exc
        (enforced if decided[version] else armed).append(entry)

    return Partition(
        channel=channel,
        conformed=conformed,
        why_not=why_not,
        source=source,
        spec_version=(
            str(payload["spec_version"])
            if isinstance(payload.get("spec_version"), str)
            else None
        ),
        armed=armed,
        enforced=enforced,
    )


def run_partition(
    checkout: str,
    channel: str,
    ledger_dir: str | None = None,
    suite_path: str | None = None,
    as_json: bool = False,
    out=None,
) -> int:
    """Report the partition. Always 0 — this reports, it does not gate.

    ``--json`` puts the payload on stdout and nothing else, the property
    ``vellum budget --json`` and ``vellum suite extract -o -`` already have, so
    a caller piping it into ``jq`` parses a payload rather than a report.
    """
    stream = out if out is not None else sys.stdout
    split = partition(checkout, channel, ledger_dir=ledger_dir, suite_path=suite_path)
    if as_json:
        json.dump(split.to_dict(), stream, indent=2)
        stream.write("\n")
    else:
        print(split.report(), file=stream)
    return 0
