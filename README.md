# vellum

The product repo for **Vellum**, a spec-driven product engineering system: the
specification is the product and the code is a build artifact of it. This repo
holds the `vellum` CLI and the forge adapters. Intent — the spec, its
scenarios, the harness and the ledger — lives in
[waviisoft/vellum-intent](https://github.com/waviisoft/vellum-intent), mounted
here as a read-only submodule at `./spec`.

The submodule commit **is** the pin: it names the spec version this code
implements, so conformance is a property of the checkout. Currently pinned to
`bc84e591`, the commit `spec-v1` names.

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
and parses every fenced `gherkin` block. Prints one line per finding as
`path:line: CODE message` and exits non-zero if there are any, which is what
lets spec CI block a merge the way a failing test does. Silent on success.
`--json` emits findings as JSON.

```sh
vellum lint spec/
```

### `vellum suite extract <spec-dir>`

Walks the tree and writes `suite.json`: every scenario with its source file,
anchor, line, steps, examples, and the spec version that introduced or last
changed it. Versions come from walking the `spec-v*` tags and comparing content
fingerprints, so moving or reformatting a scenario keeps its version while
changing a step advances it. A scenario no tag carries yet is marked `pending`
and takes the version its spec PR would mint.

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
    --satisfies 'features/auth.md#session-expiry/idle-session-expires'
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
| `tests/` | 74 tests plus fixture spec trees, including failing ones. |
| `adapters/github/` | Workflows **for the intent repo** — see [`adapters/github/README.md`](adapters/github/README.md). |
| `memory/` | Area notes, wave worklogs, and the map. Start at [`memory/map.md`](memory/map.md). |
| `.vellum/product.yaml` | Backref to the intent repo and the pin. |

## Known gap

`spec-v1` does not yet exist as a tag on the intent repo, so
`on-spec-merge.yml` would mint it against the wrong commit on the next spec
merge. The one command that fixes it is in
[`adapters/github/README.md`](adapters/github/README.md).
