# harness/

The acceptance suite: the scenarios in the intent repo's spec tree, executed
against a deployment of the product.

`vellum init` seeded everything here. The machinery — `run.py`,
`support/runner.py`, `support/registry.py`, `support/report.py`,
`support/world.py` — is generic and is the same in every Vellum installation.
Two files are yours:

| File | What you write |
|---|---|
| `steps/` | One module per spec file that carries scenarios, imported by `steps/__init__.py`. Seeded empty. |
| `support/adapter.py` | How this harness reaches the product. Seeded with `no_deployment()`. |

## Running it

    python3 harness/run.py

Exit codes: 0 when nothing failed or errored, 1 when a scenario FAILED,
ERRORED or had an UNDEFINED step, 2 when the harness could not start.

**A fresh seed exits 1**, because no sentence in the seeded spec tree has a
step definition yet and an unexecutable suite is not a suite. It is not broken;
it is telling you what to do next. The two steps, in order:

1. Write step definitions until nothing reports UNDEFINED. With no deployment
   yet, a definition's body is `world.require("deployment")` — the scenario
   then reports CANNOT RUN YET naming what is missing, which is an honest
   answer rather than a skip or a fake pass.
2. Write a real deployment in `support/adapter.py` and declare what it
   provides. The scenarios waiting on `deployment` stop waiting.

## The report

`run.py` prints a conformance map: every scenario, its outcome, and — for the
ones that cannot run — the capability that is missing and the sentence naming
it. The report is deterministic by construction: no timestamps, no durations,
no absolute paths, and scenarios in the extracted suite's own order, so two
runs at one commit produce byte-identical output.

## The write boundary

This tree belongs to the harness engineer and nothing else does
(`.vellum/config.yaml`, `write_boundaries.harness-engineer`). `run.py` enforces
half of it itself: it compares the intent repo's working tree before and after
the run and exits 2 if the run left a trace.
