"""``vellum`` — the v0.1 command line.

Commands, one per feature spec: ``lint`` (spec-pipeline), ``suite extract``
(scenarios-and-harness), ``ledger open|advance`` (ledger), and the three
pipeline commands the forge workflows are shims over — ``mint``,
``backpressure`` and ``pin advance``. Each returns a process exit code; failure
detail goes to stderr and nothing else is printed on success beyond what was
asked for.

Pipeline logic lives here rather than in a workflow body
(``spec/features/spec-pipeline.md``): a forge job that holds logic can only be
tested by running that forge, and the same logic in a command can be driven in
a sandbox — which is what makes the pipeline's behavior a PASS-able property
rather than a deployment one (``spec/features/scenarios-and-harness.md``).

Exit codes, in the one place they are all visible:

* ``0`` — it worked, or the command decided there was nothing to do.
* ``1`` — the command answered, and the answer is bad news: a fence that drops
  scenarios, a shallow clone, a divergence window at its cap.
* ``2`` — the command could not answer: the path is not a spec tree, the sha is
  not a sha, the pin file is not a pin file, the config has no cap.

The line is between *an answer you will not like* and *no answer*, and it is
load-bearing for ``vellum backpressure`` in particular: the moment its
``continue-on-error`` comes off in ``spec-ci.yml``, 1 has to mean "blocked" and
nothing else, or a renamed ``.vellum/config.yaml`` reads as backpressure and
the gate blocks for a reason nobody can find. Tests assert the number rather
than "non-zero".
"""

from __future__ import annotations

import argparse
import sys

from vellum import __version__
from vellum.backpressure import BackpressureError
from vellum.backpressure import run as backpressure_run
from vellum.config import INTENT_ENV
from vellum.ledger import LedgerError, advance, load_plan, open_record, parse_version
from vellum.lint import run as lint_run
from vellum.mint import MintError, mint as mint_run
from vellum.pin import PinError, advance as pin_advance
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

    _add_mint(sub)
    _add_backpressure(sub)
    _add_pin(sub)

    return parser


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
            return suite_run(args.spec_dir, args.output)
        if args.command == "ledger":
            return _ledger(args)
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
    return 2


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
            raise MintError(
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
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in pairs.items():
            fh.write(f"{key}={value}\n")


def _pin(args: argparse.Namespace) -> int:
    import os

    intent = args.intent or os.environ.get(INTENT_ENV)
    if not intent:
        # An invocation problem, not a repository one: nothing was pointed at.
        raise SpecTreeError(
            f"no intent checkout: pass --intent, or set {INTENT_ENV}. A pin names a "
            f"spec version, and only the intent repo can say whether {args.to} is one."
        )
    print(pin_advance(args.product_checkout, args.to, intent).report())
    return 0


def _ledger(args: argparse.Namespace) -> int:
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
