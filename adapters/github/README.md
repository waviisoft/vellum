# GitHub adapter — the caller stubs

Three **caller stubs** for the intent repo (`waviisoft/vellum-intent`). Each is
a dozen lines that name a reusable workflow in this repo at a pinned ref and
pass the one secret it needs. The logic lives in
[`.github/workflows/`](../../.github/workflows/) of this repo, as
`workflow_call` workflows, and is reviewed there alongside the CLI it calls.

| Stub | Reusable workflow | Trigger | Does |
|---|---|---|---|
| `spec-ci.yml` | [`../../.github/workflows/spec-ci.yml`](../../.github/workflows/spec-ci.yml) | `pull_request` touching `spec/**`, `ledger/**`, `.vellum/config.yaml` or the stub itself | `vellum lint` + `vellum suite extract`, uploads `suite.json`, summarises the scenarios the PR introduces or changes, and runs `vellum backpressure` (reporting, not blocking). The three agent reviews are stubs. |
| `on-spec-merge.yml` | [`../../.github/workflows/on-spec-merge.yml`](../../.github/workflows/on-spec-merge.yml) | `push` to `main` touching `spec/**` | `vellum mint` opens the ledger record for the merge commit; the workflow tags the decorative name, extracts the suite, files work-item issues from `workplan.yaml`, commits and pushes. The planner is a stub. |
| `harness-ci.yml` | [`../../.github/workflows/harness-ci.yml`](../../.github/workflows/harness-ci.yml) | `pull_request`, **every** one | `vellum verify boundaries` against the harness engineer's trees on any PR that writes `harness/`, and `python3 harness/run.py` — which fails on an UNDEFINED scenario — plus a check that the committed `harness/conformance.md` matches a fresh run. |

## Installing

```sh
cd ../vellum-intent
vellum init .                    # pins this CLI's own version
vellum init . --ref main         # or pin something else
vellum doctor .                  # what is installed is what ships
```

`vellum init` reads `.vellum/workspace.yaml` — the intent slug, the products,
and the forge — and writes one stub per shipped workflow into
`.github/workflows/`. It is idempotent: run again over an installed checkout it
writes nothing and says so. A stub that exists and *differs* is reported and
left alone; `--force` restamps it, which is also how a ref is bumped.

Copying by hand works too — the three files here are exactly what `init`
writes, and `tests/test_install.py` asserts that byte for byte. **But the
committed files pin `v0.1.0`, and `waviisoft/vellum` has cut no `v*` tag yet**,
so a hand-copied stub resolves to nothing until one exists. Edit the two ref
lines to `main`, or run `vellum init . --ref main`.

## Upgrading is bumping a ref

```sh
vellum init . --ref v0.2.0 --force
```

Two lines change per stub: the `@<ref>` on `uses:`, and the `vellum-ref:` input
that the workflow checks the CLI out at. They are stamped equal and
`vellum doctor` reports when they have come apart. The `@<ref>` alone does not
pin the CLI: the checkout of `waviisoft/vellum` inside the workflow body needs a
ref it can be handed, and an installation's CLI version has to be readable in
the repository that runs it.

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
  style note.

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

- **`waviisoft/vellum` must allow its workflows to be reused within the
  organization.** It is a private repo, so this is an Actions setting on *it*:
  Settings > Actions > General > Access > "Accessible from repositories in the
  organization". Without it the caller's run fails at `uses:` with a resolution
  error. **Neither checkout can see this setting**, so `vellum doctor` says it
  cannot check it rather than passing over it.
- **The intent repo needs a `VELLUM_TOKEN` secret** holding a token that can
  read `waviisoft/vellum`. This repo is private, so the intent repo's own job
  token cannot read it. Every shipped workflow asserts the secret in its first
  step and fails with a named error when it is empty, rather than failing later
  and less legibly inside pip. **A checkout cannot see whether a secret is
  set**, so doctor says that too.
- **The pinned ref has to exist in `waviisoft/vellum`.** `vellum init` defaults
  to `v<this CLI's version>` and *cannot confirm from an intent checkout that
  the tag exists*, so it says so rather than guessing a ref that does.
  **This repo has cut no `v*` tag yet**: until it does, install with
  `vellum init . --ref main`, or the stubs resolve to nothing. Pass
  `--releases-from <a vellum checkout>` to have either command read the `v*`
  tags and report currency.
- **The pins are MUTABLE tags, and that is the trust model.** `uses:
  waviisoft/vellum/...@v0.1.0` names a tag, not a sha, and so does every
  `actions/checkout@v4` inside the reusable workflows. Whoever can move a tag in
  `waviisoft/vellum` changes what runs in every installation that pins it —
  including `on-spec-merge`, which runs with `contents: write` and
  `issues: write` on the intent repo. That was true of the copied workflows too;
  what centralising changes is the blast radius, from one hand-copied file to
  every installation at once. Tag protection on `waviisoft/vellum`, or pinning a
  sha (`vellum init . --ref <sha>`, which both commands accept), are the two
  ways to narrow it. Nothing here enforces either.
- **Runners are Blacksmith** (`blacksmith-2vcpu-ubuntu-2204`). This
  organisation never assigns a runner to `ubuntu-latest`: the job is accepted
  and then fails in seconds with `runner_id: 0`, no logs and no steps, which
  reads like an infrastructure blip and is not one. The labels are in the
  *shipped* workflows now, which is a real limit of hosting the bodies
  centrally: an installation outside this organisation cannot change them from
  its stub. Making the label an input is the fix when a second organisation
  needs it; nothing has asked yet.
- **`on-spec-merge` needs `contents: write` and `issues: write`** — granted in
  its stub — and branch protection on `main` that lets the workflow token push,
  or the ledger commit step fails.
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
