# vellum

The product repo for **Vellum**, a spec-driven product engineering system: the
specification is the product and the code is a build artifact of it. This repo
holds the `vellum` CLI and the forge adapters. Intent — the spec, its
scenarios, the harness and the ledger — lives in
[waviisoft/vellum-intent](https://github.com/waviisoft/vellum-intent).

`.vellum/product.yaml` **is** the pin: `pin.commit` names the spec version this
code implements, so conformance is a property of the checkout. Nothing is
mounted here — build and CI fetch the intent repo at the pin, and a submodule
or subtree is an optional developer convenience that nothing treats as
authoritative.

A spec version is a commit of the intent repo whose diff touches `spec/`; its
identity is the sha and its order is ancestry. `spec-vN` names survive as
decoration and nothing reads them, so a missing, late or wrong name changes no
behavior.

This is the v0.1 milestone — the hand-built loop. It proves spec CI, scenario
extraction and the ledger format. The agent reviews, the planner, the harness
and auto-merge are v0.2 and are stubbed, loudly, in `adapters/github/`.

## Setup

```sh
git clone https://github.com/waviisoft/vellum
cd vellum
python3 -m venv .venv && .venv/bin/pip install -e .
```

To run the handful of tests that read the real spec tree, and to lint or
extract it, clone the intent repo separately and move it to the pin:

```sh
git clone https://github.com/waviisoft/vellum-intent ../vellum-intent
git -C ../vellum-intent checkout "$(python3 -c "import yaml;print(yaml.safe_load(open('.vellum/product.yaml'))['pin']['commit'])")"
export VELLUM_INTENT_REPO=$(cd ../vellum-intent && pwd)
```

Clone it in full: dating walks the history, and a shallow clone silently
re-dates every scenario below the graft. Without `VELLUM_INTENT_REPO` those
tests skip; set to a checkout at some *other* commit they fail loudly rather
than reporting conformance against the wrong tree. A checkout left at `./spec`
is picked up without the variable.

Python 3.10+, with two dependencies (`PyYAML`, `gherkin-official`). The
reasoning behind that choice is in [`memory/map.md`](memory/map.md).

Tests: `PYTHONPATH=tests .venv/bin/python -m unittest discover -s tests -t tests`

## Commands

Each takes a spec tree, which may be given either as the tree itself or as the
intent repo containing it — `vellum lint ../vellum-intent` works on a checkout,
and `vellum lint spec/` works from inside the intent repo, where `spec/` is the
tree.

### `vellum lint <spec-dir>`

Checks frontmatter against the schema (`decisions/` files carry a `date`,
every other file a `since: spec-v<integer>`), resolves every cross-reference,
parses every fenced `gherkin` block, requires every scenario to carry exactly
one well-formed `@id:<slug>` tag unique across the whole intent repo, and
rejects four shapes the spec bans: a `Background:` block, because scenarios
are self-contained and shared setup belongs in the harness as a compound step;
a `Rule:` block, because rule text is shared meaning hovering over every
scenario nested under it — and because those nested scenarios are dropped from
the suite while a stock runner executes them; a second `Feature:` in one fence,
because a fence is one Gherkin document and a stock Cucumber parser stops at
the second; and a scenario that parses and can never run, such as a
`Scenario Outline` with no `Examples` rows, which reads as coverage while
pinning nothing. Prints one line per finding as
`path:line: CODE message` and exits non-zero if there are any, which is what
lets spec CI block a merge the way a failing test does. Silent on success.
`--json` emits findings as JSON.

```sh
vellum lint "$VELLUM_INTENT_REPO"
```

### `vellum suite extract <spec-dir>`

Walks the tree and writes `suite.json`: every scenario with its `@id`, its
`scenario:<id>` ledger reference, its current file, line, steps, examples, and
the commit that introduced or last changed it. `spec_version` is the commit the
suite was extracted at — a checkout's pin, or a PR head — and `spec_head` is the
newest spec version in that checkout's ancestry.

A scenario is identified by its id — the file is only its current home, so a
scenario moving between files keeps its version. "Changed" means the
fingerprint changed, and the fingerprint covers normalized steps and example
tables only: renaming, re-tagging, re-indenting or moving a scenario is
presentation and does not advance its version. Content that exists only in the
working tree is marked `pending` with no version, because the version it will
belong to has no sha yet.

Dating walks the spec-touching commits in the checkout's ancestry, so a commit
the checkout does not contain cannot affect it, however new. Decorative names
are reported alongside every sha (`version_name`, `spec_version_name`) and read
for nothing.

```sh
vellum suite extract "$VELLUM_INTENT_REPO" --output suite.json   # - writes to stdout
```

**Needs full history.** A shallow clone has none of the commits below its
graft, so every scenario they introduced re-dates *forward* — right count,
nothing pending, silently wrong, and wrong in the direction that arms scenarios
the product already satisfies. `suite.json` reports `shallow: true` when this
has happened; do not rely on catching it there, clone in full.

### `vellum ledger open|advance`

Creates and updates the per-version traceability records — one YAML file per
spec version, **keyed by the version's commit sha**, holding that commit, an
optional decorative name, approval time, spec PR, baseline, labels, state, and
work items with their spec slices, PR, target repo and cost.

```sh
V=6ee23f4fb04746c2b7e163ccb42ed59e81d30e7a          # the version is a commit
vellum ledger open --version "$V" --spec-pr 118 --name spec-v7 \
    --baseline 2906dfb4a92e66e42cf07bd7e7e6e2e72f6dc66b --label spec:feature
vellum ledger advance --version "$V" --state implementing
vellum ledger advance --version 6ee23f4 --item 121 --title "Session expiry" --repo app \
    --satisfies 'scenario:auth-idle-session-expires'
vellum ledger advance --version "$V" --item 121 --item-state merged --pr 124 \
    --attempts 1 --tokens 412000 --usd 3.10 --executor claude-actions
vellum ledger advance --version "$V" --state shipped --release r58
```

An abbreviated sha reaches the same record; `--name` is written, displayed and
never read, so a record with none resolves exactly as well.

`open` is idempotent — replaying an approval will not overwrite a record whose
wave has advanced — and that idempotence is the whole replay guard the minting
workflow needs: the record either exists for this commit or it does not. Cost flags **add** to what is recorded, because every agent
invocation records into the same work-item entry. Records default to `./ledger`;
use `--ledger-dir` to point elsewhere.

## Layout

| Path | What |
|---|---|
| `src/vellum/` | The CLI. |
| `tests/` | The `unittest` suite plus fixture spec trees, including failing ones. |
| `.github/workflows/ci.yml` | CI for **this** repo: tests, plus a conformance check on the pin. |
| `adapters/github/` | Workflows **for the intent repo** — see [`adapters/github/README.md`](adapters/github/README.md). |
| `memory/` | Area notes, wave worklogs, and the map. Start at [`memory/map.md`](memory/map.md). |
| `.vellum/product.yaml` | Backref to the intent repo, and **the pin of record**. |

## CI and the private spec

`.github/workflows/ci.yml` runs the tests on 3.10 and 3.12 without the spec
tree — the suite builds its own fixtures, and the few checks that need the real
spec skip themselves.

The `conformance` job does need it, and the intent repo is private: a
workflow's default `GITHUB_TOKEN` only reaches its own repository. Set a
repository secret **`SPEC_TOKEN`** with read access to `waviisoft/vellum-intent`
and the job fetches the intent repo, moves it to the pin, lints it, extracts
the suite, re-runs the tests the other job skipped, and asserts the suite was
extracted at the pin with nothing pending. Without the secret it reports
`Conformance NOT VERIFIED` rather than passing quietly.

**A pin behind `spec-head` is reported, never failed on.** Conformance CI's job
is the checkout against its pin; divergence is summarised with the versions
that have landed since, and the backpressure on divergence acts at spec
approval instead (`spec/features/repo-topology.md`). A red that fires on every
spec merge, on every branch including the base, trains people to ignore red.

Jobs run on Blacksmith runners (`blacksmith-2vcpu-ubuntu-2204`). This
organisation does not schedule GitHub-hosted runners: `ubuntu-latest` is never
assigned one, and the job fails in seconds with no logs rather than saying so.

## Acceptance criteria in the ledger

Work items reference acceptance criteria by scenario id, never by file
position: `--satisfies scenario:<id>`. Prose slices are still referenced by
file path and heading anchor.
