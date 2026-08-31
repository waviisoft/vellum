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
reasoning behind that choice is in [`.vellum/memory/map.md`](.vellum/memory/map.md).

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

**A tree that would yield a short suite does not extract.** Two constructs cost
the suite scenarios, and either one exits 1 and writes nothing, naming each
block on stderr: a `gherkin` fence that fails to parse (`<file>:<line>: gherkin
block at line <n> does not parse: …`, which lint reports as `GH001`), and a
`Rule:`, whose nested scenarios are not admitted (`… nests <n> scenario(s)
under a banned Rule (…)`, lint's `GH010`). There is no partial-extraction flag:
those scenarios are exactly the ones a consumer of `suite.json` cannot see are
missing, so a smaller suite is never emitted in place of an error. Every word
of a refusal goes to stderr, so `extract … -o - | jq` sees an empty stream, and
an existing output file is left exactly as it was.

**It is not a second `lint`.** The refusal is scoped to that harm and no wider:
a tree lint rejects for an unresolved link, a missing `@id:`, or a fence
declaring no scenarios still extracts, because the suite it yields describes
every scenario the tree holds. Dating is the one place a dropping block is
still skipped, because it reads commits that are already in the past and nobody
can go back and fix them.

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

### `vellum mint <intent-checkout>`

The bookkeeping a spec merge leaves behind: opens the ledger record for the
version at a commit, in state `approved`, with the previous spec version as its
baseline and a derived decorative name. This is what
`adapters/github/on-spec-merge.yml` used to run as four shell steps
(`spec/features/spec-pipeline.md`: pipeline logic lives in the CLI, workflow
bodies are shims over it).

```sh
vellum mint .                                # writes the record; does not commit it
vellum mint . --ref "$GITHUB_SHA" --emit "$GITHUB_OUTPUT"
vellum mint . --commit                       # also stage and commit; never pushes
```

**There is no dry run.** The first line above writes `ledger/<sha>.yaml` — what
`--commit` adds is the `git add` and `git commit`, not the write. The line is
still safe to run twice, because a second run is a replay and leaves the record
alone, but it is not a preview: to see what would be recorded without recording
it, run it against a scratch `--ledger-dir`.

`--commit` does not lint. `on-spec-merge.yml` runs `vellum lint spec/` between
minting and committing and does its own commit, so a tree that fails lint never
reaches a commit there; a caller using `--commit` should lint first itself.

**It exits 0 on both of its no-ops** — a commit whose diff does not touch the
spec tree (a racing merge, a hand-run on a ledger commit), and a replay where a
record already exists — because both are benign and the workflow step it
replaced left the job green for both. **Read `minted` from `--emit`, not the
exit code**, to decide whether to run the steps that are *not* idempotent:
tagging, filing issues, pushing.

`--emit <path>` appends `key=value` lines — `sha`, `minted`, `reason`, `name`,
`baseline`, `record`, `committed` — which is the shape a runner reads step
outputs in and plain enough for any other runner to read.

A **shallow clone is refused** (exit 1): the first-parent walk is what decides
whether the commit is a version, what it descends from and what to call it, and
all three are wrong below the graft. It never tags and never pushes — the tag is
annotated with the head commit message, which is attacker-supplied, so it stays
in the workflow where it is passed through `env`.

### `vellum backpressure <intent-checkout>`

The divergence gate. Counts ledger records that are neither `shipped` nor
`superseded` and compares them to `budgets.divergence_cap` in
`.vellum/config.yaml`, reporting the window either way.

```sh
vellum backpressure .              # exit 1 at or past the cap, 0 below
vellum backpressure . --cap 5      # ask a what-if without editing policy
vellum backpressure . --pending 2  # plus approved spec PRs that have not landed
vellum backpressure . --strict     # refuse to answer if any record is unreadable
```

**1 means blocked and nothing else.** Every other non-zero exit from this
command is 2, "I could not measure the window" — a missing config, no ledger
directory, and under `--strict` a record that will not parse. Without that
split, an armed gate blocking because `.vellum/config.yaml` was renamed is
indistinguishable from real backpressure.

`--strict` belongs wherever the gate actually blocks: by default an unreadable
record is reported and *not counted*, which measures the window narrower than
the truth.

It blocks **at** the cap, not past it: the question is "may another version
land". `--pending` exists because an open spec PR is forge state, not
repository state — a caller that can see the forge supplies that count, and the
report says plainly when only the ledger half was measured.

### `vellum pin advance <product-checkout> --to <sha>`

Moves this repo's pin of record. Checks first that the sha is a real spec
version — a ledger record exists for it, or it is a spec-touching commit in the
intent checkout's first-parent ancestry — then replaces `pin.commit` in
`.vellum/product.yaml` in place, leaving every comment and every other field
exactly as they were. `pin.name` follows the commit, since decoration naming a
different version is worse than none.

```sh
vellum pin advance . --to 0e9f3f57fd94fa0cbbda6602da9a79c609e1c231 \
    --intent ../vellum-intent          # or set VELLUM_INTENT_REPO
```

An intent checkout is required and there is no `--force`: a pin naming a
non-version is the failure this command exists to prevent.

## The mechanical guards

Five read-only checks, each answering one question about neutral inputs. None
writes anything, none reaches a forge, and all five follow the exit-code
contract above: **1 is the answer you will not like, 2 is no answer.** A
mistyped `--role`, a renamed config or an unresolvable ref is 2, so a workflow
blocking on 1 blocks on findings and nothing else.

### `vellum verify boundaries <product-checkout> --base <ref> --head <ref>`

Checks the paths a branch changed against `write_boundaries.<role>` in the
checkout's own `.vellum/product.yaml`.

```sh
vellum verify boundaries . --base origin/main --head HEAD          # --role implementer
vellum verify boundaries . --base origin/main --head HEAD --role harness-engineer
```

A role the product file does not declare is refused (2) rather than defaulted:
an empty list would fault every honest PR and an unrestricted one would pass
every dishonest one. Boundary entries that would admit every path — `""`, `.`,
`/`, `../..` — are refused for the same reason. The comparison goes through the
merge base, so a commit somebody else landed on `main` is not charged to the
branch; where no merge base exists the wider direct diff is read and the report
says so. Renames are not detected, so a file moved *out* of a protected tree
still counts as a write to it.

### `vellum verify deps <product-checkout>`

Checks every declared dependency against `dependency_policy.registries` in the
installation's `.vellum/config.yaml`, which lives in the intent repo — so this
needs `--intent` or `VELLUM_INTENT_REPO`, exactly as `pin advance` does.

```sh
vellum verify deps . --intent ../vellum-intent
vellum verify deps . --manifest requirements.txt      # instead of the default globs
```

Reads `pyproject.toml` (`project.dependencies`, `project.optional-dependencies`,
`dependency-groups`, `build-system.requires`) and `requirements*.txt`, following
`-r` includes that stay inside the checkout. A plain requirement resolves to
whatever index is in force — `pypi.org`, unless `--index-url` changed it; a
direct or VCS reference resolves to its host; a local path resolves to no
registry and is not a finding. Hosts are compared exactly after parsing, so
neither `pypi.org.evil.invalid` nor `https://pypi.org@evil.invalid/simple` is
`pypi.org`.

### `vellum verify exit-duty <product-checkout> --base <ref> --head <ref>`

Fails when a diff changes source and nothing under `.vellum/memory/areas/`.

```sh
vellum verify exit-duty . --base origin/main --head HEAD
vellum verify exit-duty . --base origin/main --head HEAD --src lib --src src
```

It checks that *some* note changed, not that it is the right one. An area is an
editorial grouping and its name is not derivable from a source path — this
repo's own `src/vellum/` is documented by `areas/cli.md` — so a guess would
fault correct PRs and pass incorrect ones. Which note a change belongs in stays
the verifier's reading of the memory diff.

### `vellum ledger verify <intent-checkout>`

Resolves every link in the chain, exiting 1 naming the first broken one.

```sh
vellum ledger verify .
vellum ledger verify . --strict    # refuse when any record's ids went unchecked
```

Checked on **every record**: a work item with no PR, and a `satisfies:` entry
naming a scenario the suite at that version does not have. Checked only on
waves a **cut** names: that the wave resolves to a record, that it has reached
`verified`, and that every criterion it arms is claimed by some work item — an
open wave legitimately has criteria nothing claims yet, which is what an
unplanned wave is.

The suite at a version is `ledger/suite-<sha>.json`. When one is absent the id
checks are reported **unchecked**, not passed; `--strict` refuses instead.

**The certification check is a proxy, and the report says so.** The record
schema has no certification field at all, so "a cut naming an uncertified wave"
is read as "a cut wave that has not reached `verified` or `shipped`". Closing
that gap needs a spec slice.

### `vellum budget <intent-checkout>`

Sums recorded work-item spend against the caps in `.vellum/config.yaml`.

```sh
vellum budget .                     # per-item and period caps, both reported
vellum budget . --projected 12.50   # would the next item's certification exceed it?
vellum budget . --json              # the park state, for a caller that acts on it
```

Two caps, two parks: `budgets.per_item_usd` against each item's own accumulated
cost (a lifetime cap, not windowed), and `budgets.period_usd` against everything
spent inside the current `budgets.period`. Exceeding the first parks the item as
`needs-human`; hitting the second parks the queue. Nothing is written — the
marker and the spend report are for the caller that can file an issue, the same
division `mint` keeps by computing a tag and never applying one.

A cost entry carries no timestamp, so spend is attributed to the period
containing its record's `approved` time, the only clock the ledger has. A record
whose `approved` cannot be read is counted **inside** the window, and named in
the report. Certification does not exist yet, so `--projected` takes the next
item's cost from a caller that knows — the same shape as `backpressure
--pending`.

## Layout

| Path | What |
|---|---|
| `src/vellum/` | The CLI. |
| `tests/` | The `unittest` suite plus fixture spec trees, including failing ones. |
| `.github/workflows/ci.yml` | CI for **this** repo: tests, plus a conformance check on the pin. |
| `adapters/github/` | Workflows **for the intent repo** — see [`adapters/github/README.md`](adapters/github/README.md). |
| `.vellum/memory/` | Area notes, wave worklogs, and the map. Start at [`.vellum/memory/map.md`](.vellum/memory/map.md). |
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
