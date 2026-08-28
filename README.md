# vellum

The product repo for **Vellum**, a spec-driven product engineering system: the
specification is the product and the code is a build artifact of it. This repo
holds the `vellum` CLI and the forge adapters. Intent — the spec, its
scenarios, the harness and the ledger — lives in
[waviisoft/vellum-intent](https://github.com/waviisoft/vellum-intent), mounted
here as a read-only submodule at `./spec`.

The submodule commit **is** the pin: it names the spec version this code
implements, so conformance is a property of the checkout. Currently pinned to
`spec-v4` (`c4307ab`).

This is the v0.1 milestone — the hand-built loop. It proves spec CI, scenario
extraction and the ledger format. The agent reviews, the planner, the harness
and auto-merge are v0.2 and are stubbed, loudly, in `adapters/github/`.

## Setup

```sh
git clone --recurse-submodules https://github.com/waviisoft/vellum
cd vellum
python3 -m venv .venv && .venv/bin/pip install -e .
```

Already cloned without submodules: `git submodule update --init`.

Python 3.10+, with two dependencies (`PyYAML`, `gherkin-official`). The
reasoning behind that choice is in [`memory/map.md`](memory/map.md).

Tests: `PYTHONPATH=tests .venv/bin/python -m unittest discover -s tests -t tests`

## Commands

Each takes a spec tree, which may be given either as the tree itself or as the
intent repo containing it — `vellum lint spec/` works from here, where `./spec`
is the whole intent repo, and from inside the intent repo, where `spec/` is the
tree.

### `vellum lint <spec-dir>`

Checks frontmatter against the schema (`decisions/` files carry a `date`,
every other file a `since: spec-v<integer>`), resolves every cross-reference,
parses every fenced `gherkin` block, requires every scenario to carry exactly
one well-formed `@id:<slug>` tag unique across the whole intent repo, and
rejects `Background:` blocks — scenarios are self-contained, so shared setup
belongs in the harness as a compound step. Prints one line per finding as
`path:line: CODE message` and exits non-zero if there are any, which is what
lets spec CI block a merge the way a failing test does. Silent on success.
`--json` emits findings as JSON.

```sh
vellum lint spec/
```

### `vellum suite extract <spec-dir>`

Walks the tree and writes `suite.json`: every scenario with its `@id`, its
`scenario:<id>` ledger reference, its current file, line, steps, examples, and
the spec version that introduced or last changed it.

A scenario is identified by its id — the file is only its current home, so a
scenario moving between files keeps its version. "Changed" means the
fingerprint changed, and the fingerprint covers normalized steps and example
tables only: renaming, re-tagging, re-indenting or moving a scenario is
presentation and does not advance its version. A scenario no tag carries yet is
marked `pending` and takes the version its spec PR would mint.

```sh
vellum suite extract spec/ --output suite.json   # - writes to stdout
```

Needs full history: run it on a clone with tags, not a shallow one.

### `vellum ledger open|advance`

Creates and updates the per-version traceability records — one YAML file per
spec version, holding the version, approval time, spec PR, baseline, labels,
state, and work items with their spec slices, PR, target repo and cost.

```sh
vellum ledger open --version 42 --spec-pr 118 --baseline 38 --label spec:feature
vellum ledger advance --version 42 --state implementing
vellum ledger advance --version 42 --item 121 --title "Session expiry" --repo app \
    --satisfies 'scenario:auth-idle-session-expires'
vellum ledger advance --version 42 --item 121 --item-state merged --pr 124 \
    --attempts 1 --tokens 412000 --usd 3.10 --executor claude-actions
vellum ledger advance --version 42 --state shipped --release r58
```

`open` is idempotent — replaying an approval will not overwrite a record whose
wave has advanced. Cost flags **add** to what is recorded, because every agent
invocation records into the same work-item entry. Records default to `./ledger`;
use `--ledger-dir` to point elsewhere.

## Layout

| Path | What |
|---|---|
| `spec/` | The intent repo, pinned. Read-only here. |
| `src/vellum/` | The CLI. |
| `tests/` | 107 tests plus fixture spec trees, including failing ones. |
| `.github/workflows/ci.yml` | CI for **this** repo: tests, plus a conformance check on the pin. |
| `adapters/github/` | Workflows **for the intent repo** — see [`adapters/github/README.md`](adapters/github/README.md). |
| `memory/` | Area notes, wave worklogs, and the map. Start at [`memory/map.md`](memory/map.md). |
| `.vellum/product.yaml` | Backref to the intent repo and the pin. |

## CI and the private spec

`.github/workflows/ci.yml` runs the tests on 3.10 and 3.12 without the
submodule — the suite builds its own fixtures, and the few checks that need the
real spec skip themselves.

The `conformance` job does need it, and the intent repo is private: a
workflow's default `GITHUB_TOKEN` only reaches its own repository. Set a
repository secret **`SPEC_TOKEN`** with read access to `waviisoft/vellum-intent`
and the job checks the pinned spec out, lints it, and asserts every scenario
extracts at the version the tags say. Without the secret it reports
`Conformance NOT VERIFIED` rather than passing quietly.

Jobs run on Blacksmith runners (`blacksmith-2vcpu-ubuntu-2204`). This
organisation does not schedule GitHub-hosted runners: `ubuntu-latest` is never
assigned one, and the job fails in seconds with no logs rather than saying so.

## Acceptance criteria in the ledger

Work items reference acceptance criteria by scenario id, never by file
position: `--satisfies scenario:<id>`. Prose slices are still referenced by
file path and heading anchor.
