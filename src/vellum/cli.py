"""``vellum`` — the v0.1 command line.

Three commands, one per feature spec: ``lint`` (spec-pipeline), ``suite
extract`` (scenarios-and-harness), ``ledger open|advance`` (ledger). Each
returns a process exit code; failure detail goes to stderr and nothing else
is printed on success beyond what was asked for.
"""

from __future__ import annotations

import argparse
import sys

from vellum import __version__
from vellum.ledger import LedgerError, advance, load_plan, open_record, parse_version
from vellum.lint import run as lint_run
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

    return parser


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
    except SpecTreeError as exc:
        print(f"vellum: {exc}", file=sys.stderr)
        return 2
    except LedgerError as exc:
        print(f"vellum: {exc}", file=sys.stderr)
        return 2
    return 2


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
