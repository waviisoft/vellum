# GitHub adapter — the caller stubs

Four **caller stubs**, three for an installation's intent repo and one for its
product repo. Each is a dozen lines that name a reusable workflow in this repo
at a pinned ref, and pass `VELLUM_TOKEN` if the installation has one to pass —
that secret is optional (see [Prerequisites](#prerequisites)). The logic lives
in [`.github/workflows/`](../../.github/workflows/) of this repo, as
`workflow_call` workflows, and is reviewed there alongside the CLI it calls.

The committed copies here are stamped for **this** repo's own installation,
whose intent repo is `waviisoft/vellum-intent`; every path and slug specific to
it is something `vellum init` restamps for yours. What is not restamped, and
what a fork has to change by hand, is listed under
[Prerequisites](#prerequisites).

| Stub | Side | Reusable workflow | Trigger | Does |
|---|---|---|---|---|
| `spec-ci.yml` | intent | [`../../.github/workflows/spec-ci.yml`](../../.github/workflows/spec-ci.yml) | `pull_request` touching `spec/**`, `ledger/**`, `.vellum/config.yaml` or the stub itself | `vellum lint` + `vellum suite extract`, uploads `suite.json`, summarises the scenarios the PR introduces or changes, and runs `vellum backpressure` (reporting, not blocking). The three agent reviews are stubs. |
| `on-spec-merge.yml` | intent | [`../../.github/workflows/on-spec-merge.yml`](../../.github/workflows/on-spec-merge.yml) | `push` to `main` touching `spec/**` | `vellum mint` opens the ledger record for the merge commit; the workflow tags the decorative name, extracts the suite, files work-item issues from `workplan.yaml`, commits and pushes. The planner is a stub. |
| `harness-ci.yml` | intent | [`../../.github/workflows/harness-ci.yml`](../../.github/workflows/harness-ci.yml) | `pull_request`, **every** one | `vellum verify boundaries` against the harness engineer's trees on any PR that writes `harness/`, and `python3 harness/run.py` — which fails on an UNDEFINED scenario — plus a check that the committed `harness/conformance.md` matches a fresh run. |
| `release-cut.yml` | **product** | [`../../.github/workflows/release-cut.yml`](../../.github/workflows/release-cut.yml) | `push` to the default branch, no `paths:` filter | `vellum release tag` reads the `release:` block in `.vellum/product.yaml` and names the tag the declared version mints; when that name is unused the workflow creates `v<version>` at the pushed commit and pushes it. A used name is a notice and a no-op. |

## The product side's stub

`release-cut.yml` is the one stub that is **not** the intent repo's, and it is
the only file Vellum stamps into a product repo. Three things follow from that
and none of them is guesswork on the command's part:

* **It is stamped by `vellum init` run in the product checkout.** `vellum init`
  reads which side of the pair a checkout is — `.vellum/workspace.yaml` makes it
  the intent half, `.vellum/product.yaml` the product half — and stamps that
  side's stubs. Provisioning (`vellum init --shape …`) stamps the intent half
  only, so a freshly provisioned pair needs a second stamp; its report names the
  command, and `vellum doctor` in the product checkout reports the stub as
  missing until it is made.
* **It needs a `release:` block to do anything.** The block lives in
  `.vellum/product.yaml` and names `version_source:` and, optionally,
  `changelog:` — see [Release tags](../../README.md#release-tags). Without it
  `vellum release tag` exits 2 and the workflow fails saying so, rather than
  inferring a version from a file that happens to be lying there.
* **Its `permissions: contents: write` is the caller's to grant.** A called
  workflow's token can only be narrowed by the callee, so the grant has to be in
  the stub; a stub that grants less makes a job refused at the push. A tag
  protection rule on `v*` withholds it even when the grant is right, and that is
  forge state no checkout can see.

`vellum doctor <intent-checkout> --product <product-checkout>` verifies all four
in one report, which is the only way to ask about an installation rather than
about one of its halves.

## Installing

Into an intent repo that **already exists**. To create the repo pair from
nothing — repos, seed, stubs and the cross-repo secrets in one command — see
`vellum init --shape …` in the [root README](../../README.md); it ends by
stamping exactly the stubs below.

```sh
cd /path/to/your-intent-repo     # `../vellum-intent`, in this repo's own layout
vellum init .                    # pins this CLI's own version
vellum init . --ref main         # or pin something else
vellum init . --branch trunk     # if the default branch is not `main`

cd /path/to/your-product-repo    # the other half of the pair
vellum init . --ref <same ref>   # the release-cut stub

vellum doctor /path/to/your-intent-repo --product .   # both halves, one report
```

`vellum init` writes one stub per shipped workflow **for the side the checkout
is**. In an intent checkout it reads `.vellum/workspace.yaml` — the intent slug,
the products, and the forge — and writes the three that run there; in a product
checkout it reads `.vellum/product.yaml` for the intent slug and writes
`release-cut`. It is idempotent: run again over an installed checkout it writes
nothing and says so. A stub that exists and *differs* is reported and left
alone; `--force` restamps it, which is also how a ref is bumped.

**`--branch` is the branch `on-spec-merge` watches**, and it is the one piece of
a trigger that belongs to the installation rather than to this product. It
defaults to `main`. Hard-coding it made an installation on `trunk` one that
could never be doctor-green — the check reporting the repository's own correct
configuration as drift — so the branch list is stamped from `--branch` and
`doctor` exempts it from the `on:` comparison. Only it: `push` must still be
there, its `paths:` are still compared, and a trigger added beside it is still
drift.

Copying by hand works too — the four files here are exactly what `init`
writes, and `tests/test_install.py` asserts that byte for byte (three into the
intent repo, `release-cut.yml` into the product repo). The committed
files pin **this checkout's own version**, which is what `init` pins when it is
given no `--ref`; a copy taken from a checkout ahead of the newest cut release
pins a tag that does not exist yet, and resolves to nothing until it does. `git
tag -l 'v*'` in `waviisoft/vellum` says which do.

## Upgrading is bumping a ref

```sh
vellum init . --ref v0.2.0 --force
```

Two lines change per stub: the `@<ref>` on `uses:`, and the `vellum-ref:` input
that the workflow checks the CLI out at. **The input is quoted** — `vellum-ref:
"v0.2.0"` — because a bare `1.10`, `010`, `null`, `true` or `on` is not a string
to a YAML reader, and a stub carrying an unquoted one fails its own doctor with
`ref-mismatch` or `no-cli-ref`. The `@<ref>` half was never affected: it is part
of a longer scalar. They are stamped equal and
`vellum doctor` reports when they have come apart. The `@<ref>` alone does not
pin the CLI: the checkout of `waviisoft/vellum` inside the workflow body needs a
ref it can be handed, and an installation's CLI version has to be readable in
the repository that runs it.

**The stubs are one of three things a release moves, and `vellum upgrade` moves
all three.**

```sh
vellum upgrade . --to v0.3.0 --from ../vellum --plan   # see it first
vellum upgrade . --to v0.3.0 --from ../vellum
```

It re-stamps the stubs at the new ref, rewrites every other file
`.vellum/install.yaml` names as Vellum's, records the release in that manifest,
and lands the lot on `vellum/upgrade-v0.3.0` as a pull request — never as a push
to the default branch every stub watches. An owned file this installation has
edited stops it: exit 1 naming the file, nothing written. `vellum init --ref
<new> --force` above is still the right command when the *stubs alone* are what
you are moving.

## Why stubs, and what a stub may not become

`spec/features/installation.md`: "A stub holds no logic, so it has nothing to
drift; upgrading an installation is bumping the ref in each stub, reviewable
like any change."

The full-copy shape this replaces produced two measured incidents with a single
repo pair, both recorded in
[`.vellum/memory/areas/adapters-github.md`](../../.vellum/memory/areas/adapters-github.md):
a fold-back that sat unfolded through a wave of review against files that were
not what ran, and a set of `INSTALLED COPY` headers that outlived their own
fold-back note. A second pair would have doubled the surface
(waviisoft/vellum-intent#23).

So `vellum doctor` treats **a `run:` body or a second job in a stub as a
finding, named by file**. If an installation needs something the shipped
workflow does not do, that is a change to the shipped workflow, not a local
edit: a local edit is precisely the thing that used to drift.

**The delegating job carries `uses:`, `with:` and `secrets:` and nothing else.**
An allowlist, because the ways to add logic beside a delegation are open-ended
and several of them *report success while doing it*:

| Added key | What it does that nothing else would catch |
|---|---|
| `if:` | A **skipped** job reports **success** to branch protection. `if: false` on `harness-ci` is a green write-boundary gate that ran nothing. |
| `strategy:` | Runs the reusable workflow N times. On `on-spec-merge` that is N minters racing the same ledger push inside one run. |
| `needs:` | The job never starts when its dependency does not. |
| `permissions:` | Job-level, below the shipped grant: refused at the point of use, and the top-level block still compares equal. |
| `timeout-minutes`, `continue-on-error` | A required check that reports the wrong answer, or none. |
| `env:`, `container:` | Reach the callee's environment. |

**And the job's *id* is the shipped one.** A job calling a reusable workflow
reports its checks as `<job id> / <called job name>`, so renaming
`spec-ci:` to anything else leaves branch protection requiring names that no
longer report — see "Installing changes your required check names" below, which
is the same failure arrived at from the other direction.

**A stray workflow beside the stubs is a finding too.** Any *other* file under
`.github/workflows/` that delegates to `waviisoft/vellum`'s workflows, or runs
`vellum` in a `run:` body of its own, is reported as `stray-workflow`. That is
where a retired full copy hides: rename one aside as `spec-ci-legacy.yml` and it
goes on running on every PR, holding logic nothing keeps equal to what ships,
invisible to a check that only opens the files it stamped. An intent repo's —
or a product repo's — own unrelated CI is not reported. The set doctor knows
about is the stubs of the SIDE it is looking at, so a `release-cut.yml` sitting
in an intent repo is a stray rather than a stub it recognises.

**What a stub does carry, and why none of it can drift into a wrong answer:**

- **Triggers.** They are statements about the caller's repository, and a
  reusable workflow has no trigger but `workflow_call`. A wrong trigger does
  not run; it does not answer wrongly.
- **`permissions`.** A called workflow's token can only be *narrowed* by the
  callee, never widened, so the grant has to be made where the run starts. A
  permission that is too small is refused at the point of use.
- **`concurrency`.** A group serialises the runs of one repository. Two
  installations sharing a group would serialise unrelated repositories against
  each other, so the group belongs to the caller.
- **`secrets:` by name — never `secrets: inherit`.** `spec/features/installation.md`:
  "a stub passes each secret by name and never inherits the caller's whole
  secret set, so a reusable workflow holds exactly the credential its job names
  and nothing else in the installation." `inherit` is a doctor finding, not a
  style note. **By name means the value too**: `VELLUM_TOKEN: ${{
  secrets.ORG_ADMIN_PAT }}` satisfies any check made by key alone while handing
  the reusable workflow a different — very possibly wider — credential under the
  name it audits. Doctor reads the referenced secret back out of the expression
  and compares it to the key, so spacing an operator changed is not a finding
  and a remap is (`secret-remapped`). The rule is about the secrets a stub
  *does* pass: passing none at all is a valid installation, because
  `VELLUM_TOKEN` is `required: false`.

## Installing changes your required check names

A job that calls a reusable workflow reports its checks as
`<calling job>/<called job name>`. So `Lint and extract the suite` becomes
`spec-ci / Lint and extract the suite`, and `Harness PRs stay in harness/`
becomes `harness-ci / Harness PRs stay in harness/`.

**Rename every required status check in the intent repo's branch protection when
you install these**, or the rules go on requiring checks that no longer report
and every PR waits forever. The calling job in each stub is named for the
workflow, so the prefix is `spec-ci/`, `on-spec-merge/` or `harness-ci/` and
nothing else. `vellum doctor` cannot see branch protection and does not warn
about this.

## Prerequisites

- **`waviisoft/vellum` must allow its workflows to be reused by the calling
  repository.** While it is a private repo this is an Actions setting on *it* —
  Settings > Actions > General > Access > "Accessible from repositories in the
  organization" — and it also bounds who may call: only repositories in the same
  organization, whatever the setting says. Once the repo is public, any
  repository can call them and the setting stops applying. Without it the
  caller's run fails at `uses:` with a resolution error. **Neither checkout can
  see this setting**, so `vellum doctor` says it cannot check it rather than
  passing over it.
- **`VELLUM_TOKEN` is OPTIONAL.** It holds a token that can read
  `waviisoft/vellum`, which is the repo the CLI is installed from. Supply it
  while that repo is private, or to reach a fork of it that is not public; leave
  it unset otherwise, and each shipped workflow checks the CLI out with the
  calling repository's own `github.token` instead, raising a `::notice` that
  says which of the two it used. It is a `required: false` secret, so a stub may
  pass it or omit it and `vellum doctor` reports neither. What doctor still
  reports is a stub that passes a *different* secret under this name, and one
  that uses `secrets: inherit`. **A checkout cannot see whether a secret is
  set**, so doctor says that too.
- **The pinned ref has to exist in `waviisoft/vellum`.** `vellum init` defaults
  to `v<this CLI's version>` and *cannot confirm from an intent checkout that
  the tag exists*, so it says so rather than guessing a ref that does.
  This repo has cut `v0.1.0` and `v0.2.0`, and `v0.3.0` is this checkout's
  version: a stub stamped from a checkout whose version is not yet tagged
  resolves to nothing until the owner tags it, so pin the newest cut release
  until then. Pass `--releases-from <a vellum checkout>` to have either command
  read the `v*` tags and report currency.
- **The pins are MUTABLE tags, and that is the trust model.** `uses:
  waviisoft/vellum/...@v0.1.0` names a tag, not a sha, and so does every
  `actions/checkout@v4` inside the reusable workflows. Whoever can move a tag in
  `waviisoft/vellum` changes what runs in every installation that pins it —
  including `on-spec-merge`, which runs with `contents: write` and
  `issues: write` on the intent repo. That was true of the copied workflows too;
  what centralising changes is the blast radius, from one hand-copied file to
  every installation at once. And since `release-cut`, those tags are
  **machine-pushed**: `v<version>` is created by a workflow holding
  `contents: write` on the merge that bumps the version, so the set of people
  who can move an installation's pin is now the set of people who can land a
  commit on `waviisoft/vellum`'s default branch — no longer only those who can
  push a tag by hand. The mitigation is a tag protection rule on `v*` **on
  `waviisoft/vellum` itself**, not on the installations: an installation's own
  rule protects its own release names and says nothing about the ref its stubs
  pin. Pinning a sha (`vellum init . --ref <sha>`, which both commands accept)
  narrows it from the other end. Nothing here enforces either — and a `v*` rule
  on `waviisoft/vellum` also withholds the tag push from that workflow's own
  token, which is the trade the decision names.
- **Runners are Blacksmith** (`blacksmith-2vcpu-ubuntu-2204`), and that is a
  hosting choice rather than something Vellum requires. WAVIISoft — the
  organisation that publishes this repo — schedules its Actions on Blacksmith,
  so in *its* setup `ubuntu-latest` is never assigned a runner: the job is
  accepted and then fails in seconds with `runner_id: 0`, no logs and no steps,
  which reads like an infrastructure blip and is not one. **An installation in
  an organisation without the Blacksmith app inherits those labels and cannot
  change them from its stub**, because they live in the *shipped* workflows —
  a real limit of hosting the bodies centrally. Today the only ways round it are
  to install the Blacksmith app, or to fork this repo and edit `runs-on:` in the
  four workflow files and point the stubs at the fork with
  `vellum init . --from <owner>/<fork>`. Making the label a `workflow_call`
  input with a default is the proper fix; it is not built, because nothing has
  asked for it yet and inventing configuration ahead of the ask is how these
  files acquire options nobody uses.
- **`on-spec-merge` needs `contents: write` and `issues: write`** — granted in
  its stub — and branch protection on `main` that lets the workflow token push,
  or the ledger commit step fails.
- **`release-cut` needs `contents: write`** on the PRODUCT repo — granted in its
  stub — and no tag protection rule on `v*` that refuses the workflow token. The
  rule is forge state on the calling repository: nothing in either checkout can
  see it, so the push step says so when it is refused rather than leaving an
  operator to guess. It needs one more thing no stub carries: a `release:` block
  in `.vellum/product.yaml`. Without one the run fails at `vellum release tag`
  with exit 2, which is the command saying it has nothing to go on.
- **Both spec-side workflows check out with `fetch-depth: 0`.** The version
  sequence *is* main's history, and `vellum suite extract` dates scenarios by
  walking it. A shallow clone silently re-dates every scenario below its graft
  **forward**, onto the truncation point — right count, nothing pending,
  nothing raised, and wrong in the direction that arms scenarios the product
  already satisfies. `suite.json` carries `shallow: true` when it happens;
  treat that as the last line, not the guard.
- **`harness-ci` must not be installed ahead of a `write_boundaries` block in
  the intent repo's `.vellum/config.yaml`.** With nothing to check against, its
  boundary job exits 2 ("I could not answer") on any PR that writes `harness/`
  — which is the correct colour of red, and not a state to install into
  deliberately. The block is a top-level mapping of role to repo-relative path
  prefixes:

  ```yaml
  write_boundaries:
    harness-engineer: [harness]
    librarian: [.vellum/memory]
  ```

  **The data is the architect's to author** — this repo ships the command that
  reads it and no boundary data for a repo it does not own.

## What is real and what is not

Every stub is marked in-file with a `STUB — NOT IMPLEMENTED (v0.2)` banner in
the *shipped* workflow, a `::warning` annotation at runtime, and a comment
naming the role contract that will replace it. **The stubbed checks pass
vacuously** — a green `spec-ci` in v0.1 means the spec lints and every scenario
parses, and nothing more. It is not evidence that a spec change was reviewed
for coherence, coverage or impact.

Real in v0.1: lint and suite extraction; opening and updating the ledger record;
naming the version as decoration; filing work-item issues from a `workplan.yaml`;
and counting the divergence window (`vellum backpressure`) — real, and
**reporting only** until releases exist.

Stubbed for v0.2: coherence review, coverage review, impact report, and the
planner that writes `workplan.yaml`.

### Backpressure runs for real and does not block yet

`vellum backpressure` counts ledger records that are neither `shipped` nor
`superseded` and exits non-zero at or past `budgets.divergence_cap`. Nothing has
ever set a record to `shipped`, so every record in the intent repo counts as
unshipped. Arming the gate in that state would block every spec merge in the
repository, including the one that lands the relief: a deadlock, not
backpressure. So the step runs, reports into the job summary, and carries
`continue-on-error: true` in the shipped workflow. **Delete that one line to arm
it**, once `vellum backpressure . --strict` exits 0 against intent `main` — run
it, do not read this note. `waviisoft/vellum-intent#41` tracks the hold.

The `set -o pipefail` beside it is load-bearing: without it the step's status is
`tee`'s, and arming the gate would produce a check that can never close.

## Where the details live

Everything that used to be in this file about *how* each workflow works now sits
in the workflow it describes — the guards, the injection boundaries, the
detectors, and why each `run:` body that remains was left alone. Read
[`../../.github/workflows/`](../../.github/workflows/), and
[`.vellum/memory/areas/adapters-github.md`](../../.vellum/memory/areas/adapters-github.md)
for the landmines.
