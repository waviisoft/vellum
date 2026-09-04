"""``vellum`` — the v0.1 command line.

Commands, one per feature spec: ``lint`` (spec-pipeline), ``suite
extract|partition`` (scenarios-and-harness), ``ledger open|advance``,
``certify record|check`` and ``release cut`` (ledger,
certification-and-releases), ``tick`` (orchestration, question-protocol), the
three pipeline commands the forge workflows are shims over — ``mint``,
``backpressure`` and ``pin advance`` — and the mechanical guards: ``verify
boundaries``, ``verify deps``, ``verify exit-duty``, ``ledger verify`` and
``budget``, and the two installer commands ``init`` and ``doctor``
(installation). Each returns a process exit code; failure detail goes to stderr
and nothing else is printed on success beyond what was asked for.

A guard is a command that reads neutral inputs — a checkout, two refs, a role —
and answers one question about them. None of them writes anything, and none of
them reaches a forge: where a guard needs a number only a forge or a
not-yet-built runner can supply, the caller passes it (``backpressure
--pending``, ``budget --projected``) and the report says plainly when it was not
supplied. ``release cut --suite-result`` is the same division applied to a
command that *writes*: the enforced suite runs against a composed candidate on
infrastructure this CLI does not have, so its result arrives as an argument and
the cut is recorded without promoting when none was supplied.

``tick`` is the one command here that *writes* and is not a pipeline shim, and it
keeps the same division rather than escaping it: desired state is the checkout it
is pointed at, observed state is ``--observed``, and every forge action it
reaches is emitted for the caller instead of performed. Ledger writes are its
own, because the ledger is repository state.

Pipeline logic lives here rather than in a workflow body
(``spec/features/spec-pipeline.md``): a forge job that holds logic can only be
tested by running that forge, and the same logic in a command can be driven in
a sandbox — which is what makes the pipeline's behavior a PASS-able property
rather than a deployment one (``spec/features/scenarios-and-harness.md``).

Exit codes, in the one place they are all visible:

* ``0`` — it worked, or the command decided there was nothing to do.
* ``1`` — the command answered, and the answer is bad news: a fence that drops
  scenarios, a shallow clone, a divergence window at its cap, a head no green
  certification covers, a wave parked past the question timebox, a cut whose
  enforced suite was red or whose waves cannot be pinned, an installed stub that
  is not what ships.
* ``2`` — the command could not answer: the path is not a spec tree, the sha is
  not a sha, the pin file is not a pin file, the config has no cap, a ``--ref``
  names no commit, ``--emit`` was handed an empty path, ``certify`` was pointed
  at a checkout with no ledger or named a work item that is not in the record,
  ``tick`` was handed observed state in a shape it could not read, ``release
  cut`` was pointed at a channel ``releases.yaml`` does not declare or a
  product ``.vellum/workspace.yaml`` does not, ``init`` or ``doctor`` was
  pointed at a checkout with no workspace file or a forge they have no stubs
  for.

``init`` has no ``1`` at all: it writes or finds nothing to do, and "there is
something wrong with this installation" is ``doctor``'s sentence to pass.
``doctor``'s ref-currency section contributes to neither code — it is reported
and never failed on (``spec/features/installation.md``), the posture
``.github/workflows/ci.yml`` takes to a pin behind spec-head.

The line is between *an answer you will not like* and *no answer*, and it is
load-bearing for ``vellum backpressure`` in particular: the moment its
``continue-on-error`` comes off in ``spec-ci.yml``, 1 has to mean "blocked" and
nothing else, or a renamed ``.vellum/config.yaml`` reads as backpressure and
the gate blocks for a reason nobody can find. Tests assert the number rather
than "non-zero".

The split is the CLI's, not each command's. ``mint`` used to report both of its
invocation failures — an unresolvable ``--ref``, an empty ``--emit`` — as 1,
which is the code that means "a shallow clone; this cannot proceed". Two
different things sharing a number is how a caller learns to read only
"non-zero", and the moment that habit sets in the paragraph above stops being
true of ``backpressure`` too.
"""

from __future__ import annotations

import argparse
import os
import sys

from vellum import __version__
from vellum.backpressure import BackpressureError
from vellum.backpressure import run as backpressure_run
from vellum.boundaries import SOURCE_CHOICES, BoundaryError
from vellum.boundaries import run as boundaries_run
from vellum.budget import BudgetError
from vellum.budget import run as budget_run
from vellum.certify import run_check as certify_check
from vellum.certify import run_record as certify_record
from vellum.chain import ChainError
from vellum.chain import run as chain_run
from vellum.config import INTENT_ENV
from vellum.budget import PERIODS as BUDGET_PERIODS
from vellum.budget import parse_time
from vellum.deps import DEFAULT_MANIFESTS, DependencyError
from vellum.deps import run as deps_run
from vellum.exitduty import AREAS_TREE, DEFAULT_SOURCE_TREES, ExitDutyError
from vellum.exitduty import run as exitduty_run
from vellum.install import DEFAULT_BRANCH, HOST_REPO, FORGES, InstallError
from vellum.install import run_doctor as doctor_run
from vellum.install import run_init as init_run
from vellum.ledger import (
    CERTIFICATION_RESULTS,
    LedgerError,
    advance,
    load_plan,
    open_record,
    parse_version,
)
from vellum.lint import run as lint_run
from vellum.mint import MintError, mint as mint_run
from vellum.pin import PinError, advance as pin_advance
from vellum.provision import SHAPES, VISIBILITIES, ProvisionError
from vellum.seeds import SeedsMissing
from vellum.provision import requested as provision_requested
from vellum.provision import run_provision as provision_run
from vellum.reconcile import DEFAULT_CORPUS_MATCH, DEFAULT_LEASE_MINUTES, TickError
from vellum.release import SUITE_RESULTS, ReleaseError, ReleaseRefused
from vellum.release import run_cut, run_partition
from vellum.reconcile import run as tick_run
from vellum.specfile import SpecTreeError
from vellum.suite import run as suite_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vellum", description="Spec-driven product engineering tooling."
    )
    parser.add_argument("-V", "--version", action="version", version=f"vellum {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    lint = sub.add_parser(
        "lint",
        help="check frontmatter, cross-references and gherkin blocks in a spec tree",
    )
    lint.add_argument("spec_dir", help="the spec tree, or the intent repo containing it")
    lint.add_argument("--json", action="store_true", help="emit findings as JSON")

    suite = sub.add_parser("suite", help="the acceptance suite")
    suite_sub = suite.add_subparsers(dest="suite_command", required=True)
    extract = suite_sub.add_parser(
        "extract", help="collect every scenario in the tree into suite.json"
    )
    extract.add_argument("spec_dir", help="the spec tree, or the intent repo containing it")
    extract.add_argument(
        "-o", "--output", default="suite.json", help="output path, or - for stdout"
    )
    part = suite_sub.add_parser(
        "partition",
        help="split the suite into armed and enforced against a channel's pointer",
        description=(
            "\"Scenarios above a channel's spec-conformed pointer are armed ...; at "
            "or below are enforced\" (spec/features/scenarios-and-harness.md). The "
            "test is ancestry against ledger/releases.yaml's "
            "channels.<name>.spec_conformed, never a tag or a name "
            "(spec/decisions/2026-08-28-versions-are-commits.md). A channel that has "
            "conformed to nothing enforces nothing, and a pending scenario is armed. "
            "Reports; it does not gate."
        ),
    )
    part.add_argument("checkout", help="the intent repo checkout")
    part.add_argument("--channel", required=True, help="the channel to partition against")
    part.add_argument("--ledger-dir", help="ledger directory (default: <checkout>/ledger)")
    part.add_argument(
        "--suite",
        dest="suite_path",
        help="a suite.json to partition, e.g. a recorded ledger/suite-<sha>.json "
             "(default: extract from the checkout)",
    )
    part.add_argument("--json", action="store_true", help="emit the partition as JSON")

    ledger = sub.add_parser("ledger", help="per-version traceability records")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)

    opener = ledger_sub.add_parser("open", help="open a record for an approved version")
    _add_common_ledger_args(opener)
    opener.add_argument("--spec-pr", type=int, help="the spec PR that was merged")
    opener.add_argument("--baseline", help="conformed version (commit) the wave is planned against")
    opener.add_argument(
        "--name",
        help="decorative name for this version, e.g. spec-v12; never read to "
             "decide anything, so it may be omitted",
    )
    opener.add_argument("--label", action="append", default=[], dest="labels")
    opener.add_argument("--line", default="main", help="maintenance line (reserved; default main)")
    opener.add_argument("--approved", help="approval time, ISO 8601 (default: now, UTC)")

    adv = ledger_sub.add_parser("advance", help="advance a record, or update a work item")
    _add_common_ledger_args(adv)
    adv.add_argument("--state", help="record state")
    adv.add_argument("--release", help="the cut that shipped this version")
    adv.add_argument("--plan", help="workplan.yaml to commit into the record")
    adv.add_argument("--item", type=int, help="work item issue number to add or update")
    adv.add_argument("--title", help="work item title (required when adding)")
    adv.add_argument("--repo", help="target product repo (required when adding)")
    adv.add_argument("--satisfies", action="append", default=[], help="spec slice; repeatable")
    adv.add_argument("--item-state", help="work item state")
    adv.add_argument("--pr", type=int, help="implementation PR number")
    adv.add_argument("--briefing", help="the briefing the agent was given")
    adv.add_argument("--attempts", type=int, default=0, help="attempts to add to cost")
    adv.add_argument("--tokens", type=int, default=0, help="tokens to add to cost")
    adv.add_argument("--usd", type=float, default=0.0, help="usd to add to cost")
    adv.add_argument("--executor", help="executor that performed the work")

    ver = ledger_sub.add_parser(
        "verify",
        help="resolve every link in the ledger chain",
        description=(
            "Exits 1 naming the first broken link: a work item with no PR, a "
            "satisfies: entry naming a scenario the suite at that version does not "
            "have, a cut naming a wave with no record or one that has not reached "
            "verified, or a criterion a cut wave arms and no work item claims. "
            "Records whose suite-<sha>.json is absent are reported unchecked, not "
            "passed; --strict refuses instead."
        ),
    )
    ver.add_argument("checkout", help="the intent repo checkout")
    ver.add_argument("--ledger-dir", help="ledger directory (default: <checkout>/ledger)")
    ver.add_argument(
        "--strict",
        action="store_true",
        help="refuse to pronounce the chain sound when any record's scenario "
             "references could not be resolved; belongs wherever the guard blocks",
    )

    _add_certify(sub)
    _add_tick(sub)
    _add_mint(sub)
    _add_backpressure(sub)
    _add_pin(sub)
    _add_release(sub)
    _add_budget(sub)
    _add_verify(sub)
    _add_install(sub)

    return parser


def _add_certify(sub) -> None:
    """``certify record|check`` — the recorded proof an auto-merge is gated on.

    Two subcommands rather than one command with a ``--check`` flag, for the
    reason ``ledger`` and ``verify`` have subcommands: writing a certification
    and asking whether one authorizes a merge take disjoint arguments, and a
    single parser would have to accept ``--result`` beside ``--head`` and then
    reject the combination in code. The required flags being required is worth
    more here than in most places, because the thing being invoked wrong is a
    merge gate.
    """
    certify = sub.add_parser(
        "certify",
        help="record a certification run, or ask whether one authorizes a merge",
    )
    certify_sub = certify.add_subparsers(dest="certify_command", required=True)

    rec = certify_sub.add_parser(
        "record",
        help="record a certification run against a work item",
        description=(
            "Writes `certification: {sha, run, at, result}` onto the work item, "
            "replacing any earlier one — certification binds to a sha, so a run "
            "against an earlier commit is not evidence about this one. Recording "
            "a red result is not a failure of this command and exits 0; what a "
            "red does is deny the merge, which is `certify check`'s answer."
        ),
    )
    _add_certify_common(rec)
    rec.add_argument(
        "--sha",
        required=True,
        help="the commit the run certified: the PR head at the time, in full",
    )
    rec.add_argument(
        "--result",
        required=True,
        choices=list(CERTIFICATION_RESULTS),
        help="what the run found",
    )
    rec.add_argument("--run", help="reference to the recorded run (a URL or id)")
    rec.add_argument("--at", help="when the run finished, ISO 8601 (default: now, UTC)")

    chk = certify_sub.add_parser(
        "check",
        help="does a recorded green certification authorize a merge at this head?",
        description=(
            "Exits 0 when a green certification is recorded at exactly --head, and "
            "1 when it is not: no certification, a red one, or one bound to another "
            "commit (`@id:no-self-certified-merge`, `@id:new-commit-invalidates-cert`). "
            "Reaches no forge and runs nothing — the head is supplied by the caller "
            "that can see the PR. It is never told whether in-session checks passed, "
            "because the examined party's report about itself is not the evidence."
        ),
    )
    _add_certify_common(chk)
    chk.add_argument(
        "--head",
        required=True,
        help="the PR's current head commit, in full; an abbreviation is refused "
             "rather than resolved, since a prefix names a set of commits and an "
             "authorization is about exactly one",
    )


def _add_certify_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("checkout", help="the intent repo checkout")
    p.add_argument(
        "--version",
        required=True,
        help="spec version: the commit sha of the record holding the work item",
    )
    p.add_argument("--item", type=int, required=True, help="work item issue number")
    p.add_argument("--ledger-dir", help="ledger directory (default: <checkout>/ledger)")


def _add_tick(sub) -> None:
    """``tick`` — one pass of the stateless reconciler.

    The only *writing* command here that is not a pipeline shim, and the split
    that keeps it honest is the one every guard already uses: desired state is
    the checkout it is pointed at, and observed state — the forge half — is
    supplied by the caller through ``--observed``, the way ``backpressure
    --pending`` and ``certify check --head`` are. A tick reaches no forge, so
    every forge action it reaches is *emitted* for the caller and every ledger
    write is performed.
    """
    t = sub.add_parser(
        "tick",
        help="run one reconciler pass over an intent checkout",
        description=(
            "Reads desired state (ledger records, the spec tree, releases.yaml) "
            "and observed state (--observed, supplied by a caller that can see the "
            "forge), computes the convergent next actions idempotently, and takes "
            "the ledger half of them: commits a work plan, marks coalesced items "
            "superseded, claims an item under a lease, records new direction as a "
            "briefing. Forge actions — file an issue, open or close a question, "
            "draft a spec:clarify PR, spawn a run — are reported for the caller and "
            "never performed. Exits 1 when a wave is parked past the question "
            "timebox, and 0 otherwise; running it twice over an unchanged world "
            "writes nothing the second time."
        ),
    )
    t.add_argument("checkout", help="the intent repo checkout")
    t.add_argument("--ledger-dir", help="ledger directory (default: <checkout>/ledger)")
    t.add_argument(
        "--observed",
        help="YAML file of observed forge state: issues, questions, raised, "
             "directions. Absent means nothing was seen, which the report "
             "distinguishes from nothing being there",
    )
    t.add_argument(
        "--plan",
        help="workplan.yaml to commit into the record named by --version, the way "
             "`ledger advance --plan` does",
    )
    t.add_argument(
        "--version",
        help="reconcile only the record this sha names (required with --plan)",
    )
    t.add_argument(
        "--executor",
        help="the executor a claim is taken for. Without it a ready item is "
             "reported as dispatchable and no lease is written, since a lease "
             "names its holder. Pair it with --observed: a claim is a write, and "
             "without observed state nothing confirms the item's issue was ever "
             "filed, so the tick leases it anyway and says so in the report",
    )
    t.add_argument(
        "--lease-minutes",
        type=int,
        default=DEFAULT_LEASE_MINUTES,
        help=f"how long a claim lasts (default: {DEFAULT_LEASE_MINUTES}). No spec "
             f"sentence and no config key gives a lease duration, so this number is "
             f"the command's own and the report says so",
    )
    t.add_argument(
        "--timebox-hours",
        type=float,
        help="override questions.timebox_hours from .vellum/config.yaml",
    )
    t.add_argument(
        "--corpus-match",
        type=float,
        default=DEFAULT_CORPUS_MATCH,
        help=f"fraction of a question's significant terms a corpus document must "
             f"carry before the question is answered mechanically rather than "
             f"escalated (default: {DEFAULT_CORPUS_MATCH}, i.e. all of them). The "
             f"spec states the duty and not the rule, so this is a knob and the "
             f"default is the strictest setting",
    )
    t.add_argument(
        "--channel",
        default="production",
        help="release channel whose spec_conformed is the baseline to plan against",
    )
    t.add_argument(
        "--now",
        help="reconcile as of this ISO 8601 moment (default: now, UTC). Lease "
             "expiry and the question timebox are both read against it",
    )
    t.add_argument(
        "--dry-run",
        action="store_true",
        help="compute the actions and write nothing",
    )
    t.add_argument("--json", action="store_true", help="emit the tick as JSON")


def _add_mint(sub) -> None:
    m = sub.add_parser(
        "mint",
        help="open the ledger record for the spec version at a commit",
        description=(
            "The bookkeeping a spec merge leaves behind. Exits 0 and writes nothing "
            "when the commit is not a spec version (a racing merge, a hand-run on a "
            "ledger commit) or when a record already exists (a replay); read "
            "`minted` from --emit to tell those apart. Refuses a shallow clone, "
            "which makes all three of its answers wrong. Never tags and never "
            "pushes: the decorative tag is annotated with an attacker-supplied "
            "commit message and stays with the caller."
        ),
    )
    m.add_argument("checkout", help="the intent repo checkout, or its spec tree")
    m.add_argument("--ref", default="HEAD", help="the commit to mint (default: HEAD)")
    m.add_argument("--ledger-dir", help="ledger directory (default: <repo>/ledger)")
    m.add_argument("--spec-pr", type=int, help="the spec PR that was merged")
    m.add_argument("--label", action="append", default=[], dest="labels")
    m.add_argument(
        "--commit",
        action="store_true",
        help="stage and commit the record under a fixed message; never pushes",
    )
    m.add_argument(
        "--emit",
        help="write `key=value` lines here (sha, minted, reason, name, baseline, "
             "record, committed) — the shape a runner reads step outputs in",
    )


def _add_backpressure(sub) -> None:
    b = sub.add_parser(
        "backpressure",
        help="count unshipped spec versions against the divergence cap",
        description=(
            "Exits 1 at or past the cap — landing one more version would put the "
            "window past it — and 0 below, reporting the window either way. Counts "
            "ledger records that are neither shipped nor superseded; approved-but-"
            "unlanded spec PRs are forge state, so pass --pending to include them."
        ),
    )
    b.add_argument("checkout", help="the intent repo checkout")
    b.add_argument("--ledger-dir", help="ledger directory (default: <checkout>/ledger)")
    b.add_argument(
        "--cap",
        type=int,
        help="override budgets.divergence_cap from .vellum/config.yaml",
    )
    b.add_argument(
        "--pending",
        type=int,
        default=0,
        help="approved spec PRs not yet landed, counted alongside the ledger's",
    )
    b.add_argument(
        "--strict",
        action="store_true",
        help="refuse to measure at all when any ledger file cannot be read, "
             "rather than reporting it and counting a narrower window; belongs "
             "wherever the gate actually blocks",
    )


def _add_pin(sub) -> None:
    pin = sub.add_parser("pin", help="the product repo's pin of record")
    pin_sub = pin.add_subparsers(dest="pin_command", required=True)
    adv = pin_sub.add_parser(
        "advance",
        help="move .vellum/product.yaml's pin.commit to a spec version",
        description=(
            "Checks the sha is a real spec version — a ledger record exists for it, "
            "or it is a spec-touching commit in the intent checkout's first-parent "
            "ancestry — then replaces pin.commit in place, leaving every comment and "
            "every other field of the file exactly as it was. pin.name follows the "
            "commit, since decoration that names a different version is worse than "
            "none."
        ),
    )
    adv.add_argument("product_checkout", help="the product repo checkout")
    adv.add_argument("--to", required=True, help="the spec version to pin: a commit sha")
    adv.add_argument(
        "--intent",
        help="an intent repo checkout to validate against "
             f"(default: ${INTENT_ENV})",
    )


def _add_release(sub) -> None:
    """``release cut`` — the bookkeeping half of a cut.

    A subcommand rather than a bare ``vellum release`` because the spec already
    names more than one act on this file — cuts, promotion, conformance
    monitoring, the ``spec_head`` pointer nothing writes yet — and a verb-less
    command is the one that has to grow a ``--mode`` flag later.
    """
    release = sub.add_parser("release", help="release cuts and channel pointers")
    release_sub = release.add_subparsers(dest="release_command", required=True)
    cut = release_sub.add_parser(
        "cut",
        help="record a release cut, and promote the channel when it was green",
        description=(
            "Pins the merged waves and the per-repo version set into "
            "ledger/releases.yaml. Running the full enforced suite against the "
            "composed candidate needs a deployment this command does not have, so "
            "its result is supplied: with --suite-result green the channel's "
            "spec_conformed advances to the cut's newest wave (by ancestry) and "
            "every wave the cut names goes to shipped; with red the cut is recorded "
            "and nothing is promoted (exit 1); with neither the cut is recorded and "
            "the report says the suite result was not supplied. Rollback and the "
            "regression issue are the deployment's — this writes no forge state."
        ),
    )
    cut.add_argument("checkout", help="the intent repo checkout")
    cut.add_argument("--ledger-dir", help="ledger directory (default: <checkout>/ledger)")
    cut.add_argument(
        "--channel",
        required=True,
        help="the channel to cut to; must already be declared in releases.yaml",
    )
    cut.add_argument(
        "--wave",
        action="append",
        default=[],
        dest="waves",
        required=True,
        help="a merged wave this cut pins: a spec version (commit sha). Repeatable; "
             "each must have a ledger record",
    )
    cut.add_argument(
        "--versions",
        action="append",
        default=[],
        help="<product>=<full sha>, the version this cut pins for that product repo. "
             "Repeatable, and comma-separated entries are accepted. The product must "
             "be one .vellum/workspace.yaml declares",
    )
    cut.add_argument(
        "--suite-result",
        choices=SUITE_RESULTS,
        help="the result of the full enforced suite against the composed candidate, "
             "from the runner that executed it. Omit it and the cut is recorded "
             "without promoting",
    )
    cut.add_argument(
        "--at",
        help="the moment of the cut, ISO 8601 (default: now, UTC). The cut's id is "
             "<channel>@<at>, so passing one makes a cut replayable",
    )


def _add_budget(sub) -> None:
    b = sub.add_parser(
        "budget",
        help="sum recorded spend against the installation's caps",
        description=(
            "Exits 1 when a cap parks something: an item past budgets.per_item_usd, "
            "or committed spend at or past budgets.period_usd for the current "
            "budgets.period. Spend is attributed to a period by its record's "
            "`approved` time, the only clock the ledger has. Certification does not "
            "exist yet, so the next item's cost is an input: pass --projected to ask "
            "whether it would exceed the cap. Writes nothing — the park marker and "
            "the spend report are for the caller that can file an issue."
        ),
    )
    b.add_argument("checkout", help="the intent repo checkout")
    b.add_argument("--ledger-dir", help="ledger directory (default: <checkout>/ledger)")
    b.add_argument(
        "--projected",
        type=float,
        default=0.0,
        help="what the next work item is expected to cost, from a caller that can "
             "project it; counted alongside recorded spend",
    )
    b.add_argument("--period-cap", type=float, help="override budgets.period_usd")
    b.add_argument("--item-cap", type=float, help="override budgets.per_item_usd")
    b.add_argument(
        "--period",
        help=f"override budgets.period ({', '.join(BUDGET_PERIODS)})",
    )
    b.add_argument(
        "--as-of",
        help="measure the period containing this ISO 8601 moment (default: now, UTC)",
    )
    b.add_argument("--json", action="store_true", help="emit the measurement as JSON")


def _add_verify(sub) -> None:
    """The mechanical guards that read a *product* checkout.

    Grouped under one verb because that is what they are to a caller: four
    questions asked of a pull request, none of which needs the intent repo's
    ledger. ``ledger verify`` and ``budget`` stay where their subject is — the
    ledger and the installation's caps — rather than being pulled in here for
    the sake of a tidy noun.
    """
    verify = sub.add_parser("verify", help="mechanical guards over a product checkout")
    verify_sub = verify.add_subparsers(dest="verify_command", required=True)

    bound = verify_sub.add_parser(
        "boundaries",
        help="check a diff against the checkout's write_boundaries.<role>",
        description=(
            "Exits 1 naming every changed path outside the trees the role may write. "
            "The boundaries are data the checkout declares: a product repo in its "
            "`.vellum/product.yaml`, the intent repo — which has no product file — in "
            "its `.vellum/config.yaml`. The first of those that exists is the source, "
            "with no fallback from one to the other, so a repo whose boundaries were "
            "deleted is refused rather than judged against another repo's policy. A "
            "role the source does not declare is refused rather than defaulted, for "
            "the same reason: a boundary that can turn itself off is not one. Renames "
            "are not detected, so a file moved out of a protected tree still counts "
            "as a write to it."
        ),
    )
    bound.add_argument(
        "checkout", help="the repo checkout to check: a product repo, or the intent repo"
    )
    bound.add_argument("--base", required=True, help="the ref the branch left")
    bound.add_argument("--head", required=True, help="the branch head")
    bound.add_argument(
        "--role", default="implementer", help="the role whose boundaries apply"
    )
    bound.add_argument(
        "--boundaries-from",
        choices=SOURCE_CHOICES,
        default="auto",
        help=(
            "which file declares the boundaries (default: auto — the product file "
            "if there is one, else the installation config). Naming one makes its "
            "absence an error rather than a silent switch to the other."
        ),
    )

    deps = verify_sub.add_parser(
        "deps",
        help="check declared dependencies against dependency_policy.registries",
        description=(
            "Exits 1 when a declared dependency resolves to a registry the "
            "installation's `.vellum/config.yaml` does not list. A plain requirement "
            "resolves to whatever index is in force (pypi.org unless a requirements "
            "file overrides it); a direct URL or VCS reference resolves to its host; "
            "a local path resolves to no registry and is not a finding."
        ),
    )
    deps.add_argument("product_checkout", help="the product repo checkout")
    deps.add_argument(
        "--intent",
        help=f"an intent repo checkout holding the policy (default: ${INTENT_ENV})",
    )
    deps.add_argument(
        "--manifest",
        action="append",
        default=[],
        dest="manifests",
        help="manifest to read, relative to the checkout; repeatable. Default: "
             f"{', '.join(DEFAULT_MANIFESTS)}",
    )

    duty = verify_sub.add_parser(
        "exit-duty",
        help="check that a diff touching source also updates an area note",
        description=(
            "Exits 1 when a diff changes source and nothing under "
            f"{AREAS_TREE}. Checks that *some* note changed, not that it is the "
            "right one: an area is an editorial grouping whose name is not derivable "
            "from a source path, so which note a change belongs in stays the "
            "verifier's reading of the memory diff."
        ),
    )
    duty.add_argument("product_checkout", help="the product repo checkout")
    duty.add_argument("--base", required=True, help="the ref the branch left")
    duty.add_argument("--head", required=True, help="the branch head")
    duty.add_argument(
        "--src",
        action="append",
        default=[],
        dest="source_trees",
        help="source tree exit duty is owed for, as a repo-relative path; "
             "repeatable. An entry naming no tree ('.', '/', '') is refused "
             "rather than read as 'everywhere', which would match nothing and "
             f"turn the guard off (default: {', '.join(DEFAULT_SOURCE_TREES)})",
    )


def _add_install(sub) -> None:
    """``init`` and ``doctor``: the two halves of installing the adapters.

    One writes and one judges, and the split is deliberate. ``init`` over a
    checkout whose stubs differ from what ships reports that and leaves them
    alone — restamping somebody's file is not a thing a command should do
    quietly — and ``doctor`` is where a difference becomes a finding with an
    exit code. So neither has to guess at the other's job.
    """
    init = sub.add_parser(
        "init",
        help="stamp the forge's caller stubs into an intent checkout",
        description=(
            "Run in an intent checkout whose repos already exist. Reads the intent "
            "slug, the products and the forge from `.vellum/workspace.yaml` and "
            "writes one caller stub per shipped workflow, pinned to --ref or, by "
            "default, this CLI's own version. Idempotent: over an installed "
            "checkout it writes nothing and says so. A stub that exists and "
            "differs is reported and left alone unless --force is given. Exits 0 "
            "whether it wrote or had nothing to do, and 2 when it cannot answer — "
            "no workspace file, a forge it has no stubs for."
        ),
    )
    init.add_argument(
        "checkout", nargs="?", default=".", help="the intent repo checkout (default: .)"
    )
    init.add_argument(
        "--ref",
        help="the Vellum ref the stubs pin (default: v<this CLI's version>). "
             "Whether that ref exists is not knowable from an intent checkout, "
             "so the report says so rather than the command guessing one that does",
    )
    init.add_argument(
        "--branch",
        # No argparse default, so `resolve` can tell "the operator said `main`"
        # from "the operator said nothing" and prompt for the one and not the
        # other. Stub-stamping substitutes DEFAULT_BRANCH below, so part 1's
        # behavior with no `--branch` is what it always was.
        help=f"the default branch on-spec-merge watches (default: {DEFAULT_BRANCH}). "
             f"Installation data, not logic: an installation whose default branch "
             f"is not {DEFAULT_BRANCH} is not a drifted one, and `doctor` exempts "
             f"the branch list from its `on:` comparison for that reason",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="restamp a stub that exists and differs; this is how a ref is bumped",
    )
    _add_provision(init)
    _add_install_common(init)

    doctor = sub.add_parser(
        "doctor",
        help="check that the installed stubs are what this CLI ships",
        description=(
            "Verifies installed-matches-shipped from the checkout alone: every "
            "shipped workflow has a stub, each stub parses, names the shipped "
            "workflow from a job with the shipped id, pins a ref, passes its "
            "secret by name AND to the secret of that name, carries no logic of "
            "its own — no second job, no `run:`, and nothing on the delegating "
            "job but `uses`, `with` and `secrets` — and its caller half, the "
            "`on:`, `permissions:` and `concurrency:` blocks, each of which "
            "fails silently when wrong, is what ships. Any OTHER file under the "
            "workflows directory that delegates here or runs `vellum` is "
            "reported as a stray. Comments are not compared, and neither is the "
            "branch list, which is the installation's own (`init --branch`). "
            "Exits 1 on a finding, 2 when it cannot answer, 0 "
            "when every stub matches. Ref currency is REPORTED, never failed on "
            "(spec/features/installation.md), and what a checkout cannot know — "
            "that the secret is set, that the forge permits reuse of a private "
            "repo's workflows in the organization — is said rather than passed "
            "over."
        ),
    )
    doctor.add_argument(
        "checkout", nargs="?", default=".", help="the intent repo checkout (default: .)"
    )
    _add_install_common(doctor)


def _add_provision(p: argparse.ArgumentParser) -> None:
    """``init``'s provisioning mode (installation, part 2).

    Every one of these is also a prompt, and that equivalence is the point:
    ``spec/features/installation.md`` — "every prompt is answerable by a flag, so
    an unattended run is the same command with no prompts left". They are on
    ``init`` rather than on a second command because provisioning and stamping
    are two ends of one act; which end a run is, is decided by
    ``vellum.provision.requested`` from these arguments alone and never from the
    directory, because the shape "is chosen by the operator, never inferred from
    a directory".
    """
    group = p.add_argument_group(
        "provisioning (part 2)",
        "Given any of these, `init` provisions a repo pair instead of stamping "
        "stubs into an existing installation. Given none of them it is part 1's "
        "command, unchanged.",
    )
    group.add_argument(
        "--shape",
        choices=SHAPES,
        help="which shape this installation is: `greenfield` creates both repos "
             "and seeds a skeletal spec; the two brownfield shapes create the "
             "intent repo beside an EXISTING product repo and start the "
             "surveyor's path, `brownfield-with-docs` staging the documentation "
             "`--docs` names as the survey's sources",
    )
    group.add_argument("--product", help="the product's name; a lowercase slug")
    group.add_argument("--org", help="the forge organization or user that owns both repos")
    group.add_argument(
        "--intent-repo", dest="intent_repo",
        help="the intent repository's name (default: <product>-intent)",
    )
    group.add_argument(
        "--product-repo", dest="product_repo",
        help="the product repository's name (default: <product>). For a "
             "brownfield shape this names the repository that already exists",
    )
    group.add_argument(
        "--visibility", choices=VISIBILITIES,
        help="visibility for both repositories (default: private)",
    )
    group.add_argument(
        "--intent-visibility", dest="intent_visibility", choices=VISIBILITIES,
        help="override --visibility for the intent repository",
    )
    group.add_argument(
        "--product-visibility", dest="product_visibility", choices=VISIBILITIES,
        help="override --visibility for the product repository",
    )
    group.add_argument(
        "--area", action="append", default=[], dest="areas",
        help="a feature area's name, a lowercase slug; repeatable. Greenfield "
             "seeds one spec file per area with a placeholder scenario; a "
             "brownfield shape seeds each one `unsurveyed`",
    )
    group.add_argument(
        "--docs", action="append", default=[],
        help="an existing documentation path to stage as a survey source; "
             "repeatable, and for --shape brownfield-with-docs only. The path "
             "must exist",
    )
    group.add_argument(
        "--into",
        help="provision into local directories under this one — <into>/<intent-repo> "
             "and <into>/<product-repo>, each git-initialised — and reach no forge "
             "at all. This is the half a checkout can hold, and it is how the "
             "acceptance suite drives provisioning without a forge",
    )
    group.add_argument(
        "--plan", action="store_true",
        help="print the plan and stop, having created nothing; exits 0",
    )
    group.add_argument(
        "--yes", action="store_true",
        help="accept the defaults and the plan without confirming. The plan is "
             "still printed — this skips the confirmation, not the plan",
    )


def _add_install_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--from",
        dest="host",
        default=HOST_REPO,
        help=f"the repo hosting the reusable workflows (default: {HOST_REPO}); "
             f"for a fork or an internal mirror",
    )
    p.add_argument(
        "--forge",
        choices=FORGES,
        help="override the forge `.vellum/workspace.yaml` names",
    )
    p.add_argument(
        "--releases-from",
        help="a waviisoft/vellum checkout to read `v*` release tags from, so the "
             "pinned ref can be compared with the newest release. Without one "
             "the report says currency was not checked rather than implying it "
             "was; currency is reported either way and never failed on",
    )


def _add_common_ledger_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--version",
        required=True,
        help="spec version: the commit sha of the approved spec change",
    )
    p.add_argument("--ledger-dir", default="ledger", help="ledger directory (default: ledger)")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "lint":
            return lint_run(args.spec_dir, as_json=args.json)
        if args.command == "suite":
            return _suite(args)
        if args.command == "ledger":
            return _ledger(args)
        if args.command == "certify":
            return _certify(args)
        if args.command == "tick":
            return _tick(args)
        if args.command == "mint":
            return _mint(args)
        if args.command == "backpressure":
            return backpressure_run(
                args.checkout,
                ledger_dir=args.ledger_dir,
                cap=args.cap,
                pending=args.pending,
                strict=args.strict,
            )
        if args.command == "pin":
            return _pin(args)
        if args.command == "release":
            return _release(args)
        if args.command == "budget":
            return _budget(args)
        if args.command == "verify":
            return _verify(args)
        if args.command == "init":
            # Two commands behind one name, and the fork is the command line
            # alone. With any provisioning argument this is part 2; with none it
            # is part 1, whose behavior is untouched.
            if provision_requested(args):
                return provision_run(args)
            return init_run(
                args.checkout,
                ref=args.ref,
                host=args.host,
                forge=args.forge,
                force=args.force,
                releases_from=args.releases_from,
                branch=args.branch or DEFAULT_BRANCH,
            )
        if args.command == "doctor":
            return doctor_run(
                args.checkout,
                host=args.host,
                forge=args.forge,
                releases_from=args.releases_from,
            )
    except SpecTreeError as exc:
        print(f"vellum: {exc}", file=sys.stderr)
        return 2
    except LedgerError as exc:
        print(f"vellum: {exc}", file=sys.stderr)
        return 2
    # A window could not be measured at all — no ledger directory, no cap in
    # the config — which is "no answer", not "blocked". Sharing code 1 with
    # `blocked` would make an armed gate indistinguishable from a broken one.
    except BackpressureError as exc:
        print(f"vellum: {exc}", file=sys.stderr)
        return 2
    # A shallow clone, an unreadable spec history, a sha that is not a version:
    # the command answered, and the answer is that this cannot proceed.
    except MintError as exc:
        print(f"vellum: {exc}", file=sys.stderr)
        return 1
    except PinError as exc:
        print(f"vellum: {exc}", file=sys.stderr)
        return 1
    # Every guard's own error means the same thing: it was pointed at something
    # it could not read — a checkout with no product file, a role the file does
    # not declare, a ref that names no commit, a config with no cap. That is "no
    # answer", and it must not share a code with the answer each guard exists to
    # give. A boundary crossing, an unlisted registry, an unmet exit duty, a
    # broken chain link and a parked queue are all 1, and 1 is what a workflow
    # blocks a merge on; a mistyped `--role` reaching a caller as "this PR wrote
    # outside its trees" would be a red nobody can find the cause of.
    # `SeedsMissing` belongs in this set for the same reason: an install whose
    # wheel carries no harness skeleton cannot seed one, which is a command that
    # could not answer rather than a finding about anybody's spec — and reaching
    # a caller as a traceback would make it look like a crash in the seed.
    except (BoundaryError, ChainError, BudgetError, DependencyError, ExitDutyError,
            InstallError, ProvisionError, SeedsMissing, TickError,
            ReleaseError) as exc:
        print(f"vellum: {exc}", file=sys.stderr)
        return 2
    # A cut that cannot be made, a pointer that would move backwards, a shallow
    # clone, a suite the partition cannot place: the command answered, and the
    # answer is that this cannot proceed. `ReleaseRefused` is a sibling of
    # `ReleaseError` rather than a subclass precisely so this clause's position
    # relative to the one above decides nothing.
    except ReleaseRefused as exc:
        print(f"vellum: {exc}", file=sys.stderr)
        return 1
    return 2


def _tick(args: argparse.Namespace) -> int:
    now = None
    if args.now:
        now = parse_time(args.now)
        if now is None:
            # The same refusal `budget --as-of` makes, for the same reason: the
            # caller asked to reconcile at a moment and named none this can find.
            # Reconciling at "now" instead would resolve every lease and every
            # timebox against a clock nobody asked about.
            raise TickError(
                f"--now {args.now!r} is not an ISO 8601 moment "
                f"(e.g. 2026-08-31T00:00:00Z)"
            )
    if args.plan is not None and not args.version:
        raise TickError(
            "--plan needs --version: a work plan is produced for one approved "
            "version, and committing it into every open record would file the same "
            "work several times."
        )
    return tick_run(
        args.checkout,
        ledger_dir=args.ledger_dir,
        observed=args.observed,
        plan=load_plan(args.plan) if args.plan else None,
        version=args.version,
        executor=args.executor,
        lease_minutes=args.lease_minutes,
        timebox_hours=args.timebox_hours,
        channel=args.channel,
        corpus_match=args.corpus_match,
        now=now,
        dry_run=args.dry_run,
        as_json=args.json,
    )


def _certify(args: argparse.Namespace) -> int:
    if args.certify_command == "check":
        return certify_check(
            args.checkout,
            args.version,
            args.item,
            args.head,
            ledger_dir=args.ledger_dir,
        )
    return certify_record(
        args.checkout,
        args.version,
        args.item,
        args.sha,
        args.result,
        run=args.run,
        at=args.at,
        ledger_dir=args.ledger_dir,
    )


def _mint(args: argparse.Namespace) -> int:
    result = mint_run(
        args.checkout,
        ref=args.ref,
        ledger_dir=args.ledger_dir,
        spec_pr=args.spec_pr,
        labels=args.labels,
        commit=args.commit,
    )
    print(result.report())
    if args.emit is not None:
        # Not `if args.emit:`. `--emit "$GITHUB_OUTPUT"` with the variable
        # unset expands to the empty string, and treating that as "no --emit
        # was asked for" takes the whole job green with every downstream step
        # skipped — the failure this flag exists to prevent, arriving silently.
        if not args.emit:
            # Exit 2, not 1. Nothing was measured and no answer was reached —
            # the command was told to write somewhere and given nowhere, which
            # is the same class as `pin advance` with no intent checkout below.
            # 1 is reserved for an answer the caller will not like, and
            # `backpressure` is the reason that line has to stay clean.
            raise SpecTreeError(
                "--emit was given an empty path. If this is "
                '`--emit "$GITHUB_OUTPUT"`, the variable is unset.'
            )
        _emit(args.emit, result.emit())
    return 0


def _emit(path: str, pairs: dict[str, str]) -> None:
    """Append ``key=value`` lines to *path*, one per pair.

    Appended rather than written, because the file a caller passes is usually a
    runner's accumulating step-output file and truncating it would drop what
    earlier steps wrote.

    A value carrying a newline would forge a second key in that file, so it is
    refused rather than escaped. Nothing this command computes can contain one
    today — shas, ``spec-vN``, ``yes``/``no``, a path — which is exactly why
    the check is cheap to keep and worth keeping before something that can is
    added to the set.
    """
    bad = sorted(k for k, v in pairs.items() if "\n" in v or "\r" in v)
    if bad:
        raise MintError(f"refusing to emit {bad}: a value spanning lines would forge a key")
    # An unwritable `$GITHUB_OUTPUT` is a failure of this command, reported the
    # way its other failures are. Letting `OSError` out printed a traceback and
    # a Python exit code into the one step every downstream `if:` reads its
    # answer from, which is the least legible place in the pipeline to be told
    # that a path is wrong.
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in pairs.items():
                fh.write(f"{key}={value}\n")
    except OSError as exc:
        raise MintError(f"{path}: cannot write the emitted step outputs: {exc}") from exc


def _pin(args: argparse.Namespace) -> int:
    intent = args.intent or os.environ.get(INTENT_ENV)
    if not intent:
        # An invocation problem, not a repository one: nothing was pointed at.
        raise SpecTreeError(
            f"no intent checkout: pass --intent, or set {INTENT_ENV}. A pin names a "
            f"spec version, and only the intent repo can say whether {args.to} is one."
        )
    print(pin_advance(args.product_checkout, args.to, intent).report())
    return 0


def _suite(args: argparse.Namespace) -> int:
    if args.suite_command == "partition":
        return run_partition(
            args.checkout,
            args.channel,
            ledger_dir=args.ledger_dir,
            suite_path=args.suite_path,
            as_json=args.json,
        )
    return suite_run(args.spec_dir, args.output)


def _release(args: argparse.Namespace) -> int:
    return run_cut(
        args.checkout,
        args.channel,
        args.waves,
        _split_versions(args.versions),
        ledger_dir=args.ledger_dir,
        at=args.at,
        suite_result=args.suite_result,
    )


def _split_versions(values: list[str]) -> list[str]:
    """``--versions core=a,web=b`` and repeated ``--versions`` mean the same thing.

    A comma is accepted because a caller composing a candidate has the set in
    one string, and the alternative is that ``--versions core=a,web=b`` is read
    as one product named ``core`` at a sha containing a comma — which
    ``parse_versions`` would then refuse with a message about sha length rather
    than about the comma. Splitting here means the refusal a caller gets names
    the entry that is actually wrong.
    """
    return [part.strip() for value in values for part in str(value).split(",") if part.strip()]


def _budget(args: argparse.Namespace) -> int:
    as_of = None
    if args.as_of:
        as_of = parse_time(args.as_of)
        if as_of is None:
            # An unreadable `--as-of` is an invocation problem: the caller asked
            # for a window and named no moment this can find one from. Answering
            # for "now" instead would report a window nobody asked about, under a
            # heading that says otherwise.
            raise BudgetError(
                f"--as-of {args.as_of!r} is not an ISO 8601 moment "
                f"(e.g. 2026-08-31T00:00:00Z)"
            )
    return budget_run(
        args.checkout,
        ledger_dir=args.ledger_dir,
        period_cap=args.period_cap,
        item_cap=args.item_cap,
        period=args.period,
        projected=args.projected,
        as_of=as_of,
        as_json=args.json,
    )


def _verify(args: argparse.Namespace) -> int:
    if args.verify_command == "boundaries":
        return boundaries_run(
            args.checkout, args.base, args.head, args.role, args.boundaries_from
        )
    if args.verify_command == "exit-duty":
        return exitduty_run(
            args.product_checkout, args.base, args.head, args.source_trees or None
        )
    intent = args.intent or os.environ.get(INTENT_ENV)
    if not intent:
        # The same refusal `pin advance` makes, for the same reason: the policy
        # is installation policy and lives in the intent repo, so nothing about
        # a product checkout alone can answer the question.
        raise SpecTreeError(
            f"no intent checkout: pass --intent, or set {INTENT_ENV}. "
            f"dependency_policy.registries is installation policy and lives in "
            f"the intent repo's .vellum/config.yaml."
        )
    return deps_run(args.product_checkout, intent, args.manifests or None)


def _ledger(args: argparse.Namespace) -> int:
    if args.ledger_command == "verify":
        return chain_run(args.checkout, ledger_dir=args.ledger_dir, strict=args.strict)
    sha = parse_version(args.version)
    if args.ledger_command == "open":
        baseline = parse_version(args.baseline) if args.baseline else None
        path, created = open_record(
            args.ledger_dir,
            sha,
            spec_pr=args.spec_pr,
            baseline=baseline,
            labels=args.labels,
            line=args.line,
            approved=args.approved,
            name=args.name,
        )
        print(path if created else f"{path} (already open)")
        return 0

    plan = load_plan(args.plan) if args.plan else None
    path = advance(
        args.ledger_dir,
        sha,
        state=args.state,
        release=args.release,
        plan=plan,
        issue=args.item,
        title=args.title,
        repo=args.repo,
        satisfies=args.satisfies,
        item_state=args.item_state,
        pr=args.pr,
        briefing=args.briefing,
        attempts=args.attempts,
        tokens=args.tokens,
        usd=args.usd,
        executor=args.executor,
    )
    print(path)
    return 0
