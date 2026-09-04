#!/usr/bin/env python3
"""Run the acceptance suite and print the conformance map.

    python3 harness/run.py                      # markdown to stdout
    python3 harness/run.py --format json        # the same run, as JSON
    python3 harness/run.py --out report.md      # write it instead

The suite is whatever ``vellum suite extract`` reports for the intent repo, so
this runs the real suite at the checkout's head — never a copy, never a
hand-maintained list.

Seeded by ``vellum init``. Two things are yours to write, in this order:

1. ``harness/steps/`` — one module per spec file that carries scenarios,
   imported by ``harness/steps/__init__.py``. Until a sentence has a
   definition the runner reports it UNDEFINED and this exits 1, which is the
   correct answer: a suite that does not execute is not a suite.
2. ``harness/support/adapter.py`` — how this harness reaches the product.
   Until it names a real deployment every scenario reports CANNOT RUN YET
   naming ``deployment``, which is also the correct answer.

Environment:

``VELLUM_INTENT_REPO``
    The intent repo to extract the suite from. Defaults to the repository this
    file lives in, so a plain checkout needs no configuration.

``VELLUM_BIN``
    The ``vellum`` executable, when it is not on ``PATH``.

Exit codes: 0 when the run produced a report and nothing failed or errored;
1 when a scenario FAILED, ERRORED, or had an UNDEFINED step; 2 when the harness
could not start, or when the run touched the spec tree.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
sys.path.insert(0, str(HARNESS))

# Set before the first import below, not just before `import steps`: the run
# must leave no trace in the intent repo, and CPython would otherwise write
# harness/support/__pycache__/ and harness/steps/__pycache__/ inside it.
sys.dont_write_bytecode = True

from support import report as reporting  # noqa: E402
from support.adapter import AdapterError, no_deployment, run_vellum, vellum_cli  # noqa: E402
from support.runner import CANNOT_RUN, ERROR, FAIL, UNDEFINED, run_suite  # noqa: E402


def intent_repo() -> Path:
    named = os.environ.get("VELLUM_INTENT_REPO")
    if named:
        path = Path(named).resolve()
        if not (path / "spec" / "index.md").is_file():
            raise AdapterError(
                f"VELLUM_INTENT_REPO={named} has no spec/index.md, so it is not "
                f"an intent repo"
            )
        return path
    return HARNESS.parent


def _porcelain(repo: Path) -> str:
    """The intent repo's working tree, as the write-boundary self-check sees it.

    ``--ignored=matching`` because the check is "the run left no trace", and an
    ignored path is still a trace.

    Never swallows a failure. Returning ``""`` when git cannot run would make
    ``before == after`` vacuously true, which turns the one guard against the
    harness writing into the tree it is extracting from into a no-op.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--ignored=matching"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AdapterError(
            f"the write-boundary self-check cannot run: `git status` exited "
            f"{proc.returncode} in {repo}: {proc.stderr.strip()}"
        )
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--out", help="write the report here instead of stdout")
    parser.add_argument(
        "--strict", action="store_true",
        help="also exit non-zero when a scenario cannot run yet",
    )
    parser.add_argument(
        "--only", action="append", default=[],
        help="run only these scenario ids; repeatable (for developing steps)",
    )
    args = parser.parse_args(argv)

    try:
        repo = intent_repo()
        cli = vellum_cli()
        deployment = no_deployment()
        before = _porcelain(repo)
    except AdapterError as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return 2

    # Preflight, in the order spec CI runs it: the tree must lint before its
    # suite means anything.
    lint = run_vellum(cli, "lint", str(repo))
    if lint.returncode != 0:
        print(f"harness: the spec tree does not lint, so the suite is not "
              f"trustworthy:\n{lint.stdout}{lint.stderr}", file=sys.stderr)
        return 2

    extract = run_vellum(cli, "suite", "extract", str(repo), "-o", "-")
    if extract.returncode != 0:
        print(f"harness: vellum suite extract failed:\n{extract.stderr}", file=sys.stderr)
        return 2
    suite = json.loads(extract.stdout)

    if args.only:
        suite = dict(suite, scenarios=[s for s in suite["scenarios"]
                                       if s["id"] in args.only])
        suite["scenario_count"] = len(suite["scenarios"])

    import steps  # noqa: F401  — importing registers every step definition

    results = run_suite(deployment, suite)

    try:
        after = _porcelain(repo)
    except AdapterError as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return 2
    if after != before:
        print(
            "harness: the run changed the intent repo working tree, which it must "
            f"never do.\nbefore:\n{before}\nafter:\n{after}",
            file=sys.stderr,
        )
        return 2

    render = reporting.to_markdown if args.format == "markdown" else reporting.to_json
    text = render(suite, deployment.name, results)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    bad = {FAIL, ERROR, UNDEFINED}
    if args.strict:
        bad = bad | {CANNOT_RUN}
    return 1 if any(r.outcome in bad for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
