# vellum

The product repo for **Vellum**, a spec-driven product engineering system: the
specification is the product and the code is a build artifact of it. This repo
holds the `vellum` CLI and the forge adapters. It is MIT licensed — see
[`LICENSE`](LICENSE).

Vellum works on a **pair** of repositories: a product repo like this one, and an
*intent* repo holding the spec, its scenarios, the harness and the ledger. This
repo's own intent repo is
[waviisoft/vellum-intent](https://github.com/waviisoft/vellum-intent), which is
private — Vellum's own spec is not published. An installation of Vellum has its
own pair, and none of the slugs below are shared with it.

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
and auto-merge are v0.2 and are stubbed, loudly, in the reusable workflows
under `.github/workflows/`.

**The forge adapters ship once and install thin.** This repo hosts the real
logic of each adapter workflow — `spec-ci`, `on-spec-merge`, `harness-ci` — as
a `workflow_call` workflow under `.github/workflows/`, and an intent repo
carries one caller stub per workflow naming it at a pinned ref. `vellum init`
stamps the stubs; `vellum doctor` checks that what is installed is what ships.
Installing them **renames the required status checks** — a job calling a
reusable workflow reports as `<calling job>/<called job name>` — so branch
protection has to be updated with them. See
[`adapters/github/README.md`](adapters/github/README.md).

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

### `vellum init [<intent-checkout>]`

Two commands behind one name, and **which one a run is, is decided by the
command line alone**: with any of the provisioning arguments below it
provisions a repo pair; with none of them it is the stub-stamping command
described here. Nothing is ever inferred from the directory — the shape of an
installation is the operator's to choose.

Stamps the forge's caller stubs into an intent checkout whose repos already
exist. Reads the intent slug, the products and the forge from
`.vellum/workspace.yaml`, and writes one stub per shipped workflow into
`.github/workflows/`, pinned to `--ref` or, by default, this CLI's own version.

```sh
cd ../vellum-intent
vellum init .                            # pins v<this CLI's version>
vellum init . --ref main                 # or pin something else
vellum init . --branch trunk             # if the default branch is not `main`
vellum init . --ref v0.2.0 --force       # upgrading is bumping the ref
```

`--branch` is the branch `on-spec-merge` watches, and it is installation *data*,
not this product's shape: an installation whose default branch is not `main` is
not a drifted one, and `doctor` exempts the branch list from its `on:`
comparison for exactly that reason.

Idempotent: run again over an installed checkout it writes nothing and says so.
A stub that exists and *differs* is reported and left alone — writing is this
command's job and judging is `doctor`'s — and `--force` restamps it. Exit 0
whether it wrote or had nothing to do; 2 when it cannot answer (no workspace
file, a forge it has no stubs for).

**The default ref may not exist, and the report says so rather than guessing
one that does.** Nothing in an intent checkout can see the product repo's tags;
pass `--releases-from <a vellum checkout>` to have it read them. **This repo has
cut no `v*` tag yet**, so install with `--ref main` until it does.

### `vellum init --shape …` — provisioning a new installation

Run where no installation exists, `init` creates the repo pair, seeds the tree
spec CI needs to run once, installs the stubs and wires the cross-repo secrets.
Three shapes, each a path `docs/design.md` already names:

| `--shape` | What it is |
|---|---|
| `greenfield` | Both repos are new. The intent repo is seeded with a skeletal spec — `product.md`, an index, one feature area per `--area` with a placeholder scenario — and the product repo is created and pinned at that seed. |
| `brownfield` | The product repo **already exists**: `init` clones it, so the adoption branch sits on its real history. The intent repo is created beside it, every `--area` is seeded `unsurveyed`, and the product's `.vellum/` arrives on a `vellum/adopt` branch as a pull request — never as a push to its default branch. Without a forge CLI, the two files are built in a standalone checkout and the checklist says to move them onto a clone of the real one. |
| `brownfield-with-docs` | As `brownfield`, and the existing documentation `--docs` points at is listed in the seeded index under **Survey sources**, so the surveyor finds it. |

**A conversation with a plan.** `init` prompts for what it needs, and every
prompt is answerable by a flag — so an unattended run is the same command with
no prompts left:

| Flag | Prompt | Default |
|---|---|---|
| `--shape` | which shape | *(none — always answered)* |
| `--product` | the product's name, a lowercase slug | *(none)* |
| `--org` | the forge organization or user | *(none)* |
| `--intent-repo` | the intent repository's name | `<product>-intent` |
| `--product-repo` | the product repository's name (for a brownfield shape, the existing one) | `<product>` |
| `--visibility` | `public` or `private`, for both repos | `private` |
| `--intent-visibility`, `--product-visibility` | per repo, overriding `--visibility` | |
| `--branch` | the default branch | `main` |
| `--area` | a feature area's name, a lowercase slug; **repeatable** | *(none)* |
| `--docs` | an existing documentation path, inside the product checkout; repeatable, `brownfield-with-docs` only | *(none)* |

`--yes` accepts the defaults **and** the plan. `--plan` prints the plan and
stops, having created nothing, exit 0. Prompts are asked only on a TTY; without
one, an unanswered prompt exits 2 **naming the flag** that answers it. Every
value is validated *before* the plan, so a run either has a complete plan or has
not started.

```sh
# the whole conversation as flags — no prompts left
vellum init --shape greenfield --product acme --org waviisoft --area billing --yes

# see what it would do, and do none of it
vellum init --shape greenfield --product acme --org waviisoft --area billing --plan

# adopt an existing repo, staging its docs for the surveyor
vellum init --shape brownfield-with-docs --product legacy --org waviisoft \
    --area billing --area accounts \
    --docs docs/architecture.md --docs docs/api.md --yes
```

**The plan** names both repositories and their visibility, every file to be
seeded, every stub, the secret pair and which repo each is set on, and **every
step the transport cannot take**. It is shown before anything is created and
confirmed; `--yes` skips the confirmation, not the plan, and declining it exits
2 having created nothing. `--plan` reaches no forge at all — not even to ask
whether your `gh` is logged in, because that question is a call to the forge and
`--plan` creates nothing.

Everything the plan cannot validate on its own — a repository name the forge
already has, a directory that is already something — is checked **before** the
confirmation, so a run either refuses before you agree to it or does what you
agreed to.

**The transport is your forge CLI.** With `gh` on PATH and `gh auth status`
succeeding, `init` creates the repositories, pushes the seeds and sets the
secret pair. The secret values come from `$VELLUM_TOKEN` and `$SPEC_TOKEN`, or a
hidden prompt, and reach `gh` **on stdin** — never as an argv element, which is
world-readable on the machine and lands in shell history. `init` never mints a
credential.

Note the shape of the secret steps, in the plan and in the checklist:

```sh
printf %s "$VELLUM_TOKEN" | gh secret set VELLUM_TOKEN --repo waviisoft/acme-intent
```

**No `--body`, deliberately.** `gh secret set` reads the value from stdin only
when `--body` is *absent*; given the flag it uses the flag's value, so
`--body -` sets the secret to the literal string `-` rather than to what is on
the pipe. Both the transport and the checklist leave it off.

**Nothing is changed on either repo's Actions settings.**
`actions/permissions/access` governs whether a repository's *own* workflows may
be reused by others, and the workflows your caller stubs resolve against live in
the Vellum repo (`--from`), which your installation does not own. That setting
is the one that has to be right, it is the host repo's, and the plan names it as
a step no transport takes.

**If a forge step fails part way through**, `init` prints what it took before
the failure and hands back every step from there onward as a checklist, then
exits 2. Nothing rolls back a repository that has already been created, so the
report is what is left: the local checkouts are still where it said, and the
commands name them.

**Without an authenticated `gh` there is no second tool to install.** `init`
does everything a checkout can hold — both seeds, both commits, the stubs, and
both checks — in a staging directory it names, then prints the forge steps as an
ordered checklist with the exact commands, and exits 0 for the half it did.
That checklist is the same list the plan carried, so it cannot have drifted from
what `init` would have done. `vellum doctor` afterwards verifies the whole.

**The secret pair, and which way each reads:**

| Secret | Set on | Reads |
|---|---|---|
| `VELLUM_TOKEN` | the intent repo | the product repo — it is what the caller stubs pass to the reusable workflows |
| `SPEC_TOKEN` | the product repo | the intent repo — its conformance job fetches the spec tree at the pin |

**Adoption is a guest in a repo it did not create.** The brownfield shapes
commit into a checkout somebody else owns, so `init` refuses rather than write
over it: a checkout with **uncommitted changes** (they would land in the
adoption commit and then in the pull request — an untracked `.env` included), a
checkout that already carries **`.vellum/product.yaml`** (it is already an
installation, and seeding it again replaces its pin), and a checkout that
already has a **`vellum/adopt` branch** (it is somebody's, most likely an
adoption in review). Each is exit 2 naming what it found. The commit it does
make adds exactly `.vellum/product.yaml` and `.vellum/memory/map.md`, and is cut
from the repository's own default branch — `origin/HEAD` where there is a clone
— never from whatever happened to be checked out.

**`--into <dir>` provisions into local directories** — `<dir>/<intent-repo>` and
`<dir>/<product-repo>`, each `git init`ed — and reaches **no forge at all**, not
even to look for one. It is the half a checkout can hold, and it is how the
acceptance suite drives provisioning without a forge.

**The seed is checked before it is pushed.** `vellum lint` runs over the seeded
spec tree and `vellum doctor` over the whole intent checkout once the stubs are
stamped; a red seed is reported and **nothing is pushed**, exit 1. That is the
one finding `init` can report, and it is still doctor's sentence to pass.

**The pin** is the intent repo's *first* commit touching `spec/` — the commit
that made this installation's spec exist. The stubs land in a second commit, so
they do not date it.

**What is seeded, greenfield:**

```
<intent repo>/  spec/product.md, spec/index.md, spec/features/<area>.md
                .vellum/config.yaml      every key the CLI reads, with the command that reads it named
                .vellum/workspace.yaml   the intent slug, the forge, the product map
                ledger/releases.yaml     one channel, spec_conformed: null, cuts: []
                harness/                 the generic runner; steps/ empty, adapter names no deployment
                .github/workflows/       the three caller stubs
<product repo>/ .vellum/product.yaml     the backref and the pin
                .vellum/memory/map.md
```

The seeded harness is honest about being a skeleton: `harness/steps/` is empty,
so `python3 harness/run.py` reports every scenario UNDEFINED and exits 1 until
step definitions exist, and `harness/support/adapter.py` names no deployment, so
a scenario whose steps exist reports CANNOT RUN YET rather than a fake pass.
Both are the first two jobs of a new installation, and `harness/README.md` in
the seed says so.

**Refusals**, all exit 2: a checkout that already carries
`.vellum/workspace.yaml` (that is the stamping case, not a provisioning); a
repository name the forge already has, unless it is the product repo of a
brownfield shape; a local directory that already exists and is not empty, on the
same rule; and any value that will not validate.

### `vellum doctor [<intent-checkout>]`

Verifies installed-matches-shipped from the checkout alone: every shipped
workflow has a stub, each stub parses, names the shipped workflow, pins a ref,
and passes any secret it does pass by name **and by value** — `VELLUM_TOKEN: ${{
secrets.ORG_ADMIN_PAT }}` satisfies any check made by key alone and hands the
reusable workflow a different credential under the name it audits, so it is a
`secret-remapped` finding.

**A stub that passes no secret at all is not a finding.** `VELLUM_TOKEN` is
declared `required: false` by every shipped workflow, which check the CLI out
with the calling repository's own `github.token` when nothing is passed. The
secret is for reading a `waviisoft/vellum` that is private, or a fork of it that
is; an installation that needs neither passes nothing. `secrets: inherit` is
still a finding — that rule is about the secrets a stub *does* hand over.

**A stub edited in place to carry logic — a job of its own, or any `run:` — is a
finding, named by file.** So is anything on the delegating job beyond `uses:`,
`with:` and `secrets:`, which is an allowlist because the ways to be wrong there
are open-ended and several of them *report success*: `if: false` makes a skipped
job, and a skipped job reports success to branch protection, so a write-boundary
gate goes green having run nothing. `strategy:` runs the reusable workflow N
times — for `on-spec-merge`, N minters racing one ledger push. `needs:`,
`timeout-minutes`, `continue-on-error`, a job-level `permissions:`, `env:` and
`container:` are each a finding for the same reason. So is a **renamed
delegating job**: the forge derives the check name from the job id, so a rename
leaves branch protection requiring names that never report.

So is a caller half that has drifted: the `on:`, `permissions:` and
`concurrency:` blocks are compared against what ships, because each of the three
fails *silently* when wrong — a narrowed trigger is a required check that never
reports, a narrowed permission is a job refused at the point of use. Comments
are not compared, and neither is `on.push.branches`, which is the installation's
own default branch (`init --branch`) rather than this product's shape.

Finally, a **`stray-workflow`** finding for any *other* file under
`.github/workflows/` that delegates to this repo's workflows or runs `vellum` in
a body of its own — the place a retired full copy (`spec-ci-legacy.yml`) could
go on running on every PR unseen, which is the shape this installer exists to
replace.

```sh
vellum doctor .                                     # 1 on a finding, 0 when every stub matches
vellum doctor . --releases-from ../vellum           # + compare the pinned ref to the newest release
```

**Ref currency is reported, never failed on**, mirroring the divergence posture
(`spec/features/repo-topology.md`): an installation behind the newest release is
divergence to summarise, not a broken install.

**What a checkout cannot know, doctor says it cannot check** rather than passing
over — whether the `VELLUM_TOKEN` secret is set, and whether the forge allows
this repo's workflows to be reused by the repository calling them (an Actions
setting on this repo for as long as it is private, and one that also limits
callers to the same organization). Both are forge state, and both are printed on
a green run too.

## The mechanical guards

Five read-only checks, each answering one question about neutral inputs. None
writes anything, none reaches a forge, and all five follow the exit-code
contract above: **1 is the answer you will not like, 2 is no answer.** A
mistyped `--role`, a renamed config or an unresolvable ref is 2, so a workflow
blocking on 1 blocks on findings and nothing else.

### `vellum verify boundaries <checkout> --base <ref> --head <ref>`

Checks the paths a branch changed against `write_boundaries.<role>` declared in
the checkout. A product repo declares its boundaries in `.vellum/product.yaml`;
the intent repo — which has no product file — declares its own in a
`write_boundaries` block in `.vellum/config.yaml`, in the same shape.
`--boundaries-from {auto,product,config}` selects the source (`auto`, the
default, takes the product file if one exists and the config otherwise; a
checkout that declares neither is refused, not treated as unrestricted). A named
source whose file is absent is an error, never a silent fall-through to the
other file.

```sh
vellum verify boundaries . --base origin/main --head HEAD          # --role implementer
vellum verify boundaries . --base origin/main --head HEAD --role harness-engineer
# the intent repo, whose boundaries live in .vellum/config.yaml:
vellum verify boundaries ../vellum-intent --role harness-engineer \
  --boundaries-from config --base origin/main --head HEAD
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
| `.github/workflows/{spec-ci,on-spec-merge,harness-ci}.yml` | The **reusable** (`workflow_call`) workflows every installation's intent repo calls. No trigger of their own, so they never run for this repo. |
| `adapters/github/` | The **caller stubs** those installations carry, and their README — see [`adapters/github/README.md`](adapters/github/README.md). |
| `.vellum/memory/` | Area notes, wave worklogs, and the map. Start at [`.vellum/memory/map.md`](.vellum/memory/map.md). |
| `.vellum/product.yaml` | Backref to the intent repo, and **the pin of record**. |

## CI, and this repo's private spec

`.github/workflows/ci.yml` runs the tests on 3.10 and 3.12 without the spec
tree — the suite builds its own fixtures, and the few checks that need the real
spec skip themselves. Everything in this section is about **this** repo's own
CI; an installation's product repo has an equivalent of its own, pointed at its
own intent repo.

The `conformance` job does need the spec tree, and `waviisoft/vellum-intent` is
private and stays private: a workflow's default `GITHUB_TOKEN` only reaches its
own repository. So this repo sets a repository secret **`SPEC_TOKEN`** with read
access to it, and the job fetches the intent repo, moves it to the pin, lints
it, extracts the suite, re-runs the tests the other job skipped, and asserts the
suite was extracted at the pin with nothing pending. Without the secret it
reports `Conformance NOT VERIFIED` rather than passing quietly. A fork sets its
own `SPEC_TOKEN` for its own intent repo, or drops the job; this one grants
nothing to anybody else.

**A pin behind `spec-head` is reported, never failed on.** Conformance CI's job
is the checkout against its pin; divergence is summarised with the versions
that have landed since, and the backpressure on divergence acts at spec
approval instead (`spec/features/repo-topology.md`). A red that fires on every
spec merge, on every branch including the base, trains people to ignore red.

Jobs run on Blacksmith runners (`blacksmith-2vcpu-ubuntu-2204`). That is a
hosting choice by WAVIISoft, the organisation that publishes this repo, and not
something Vellum requires: WAVIISoft schedules its Actions on Blacksmith rather
than on GitHub-hosted runners, so in its setup `ubuntu-latest` is never assigned
one and the job fails in seconds with no logs rather than saying so. A fork in
an organisation without the Blacksmith app changes every `runs-on:` in these
files to labels its own runners answer to. The same applies to the three
reusable workflows, where it costs more, because an installation cannot override
the label from its stub — see
[`adapters/github/README.md`](adapters/github/README.md).

## Acceptance criteria in the ledger

Work items reference acceptance criteria by scenario id, never by file
position: `--satisfies scenario:<id>`. Prose slices are still referenced by
file path and heading anchor.
