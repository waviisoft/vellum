# GitHub adapter

Two workflows **for the intent repo** (`waviisoft/vellum-intent`), kept here so
they are reviewed alongside the CLI they call. They are not run by this repo.

Install them by copying:

```sh
cp adapters/github/spec-ci.yml       ../vellum-intent/.github/workflows/
cp adapters/github/on-spec-merge.yml ../vellum-intent/.github/workflows/
```

Keep the two sides byte-identical. These files are the upstream copy and the
intent repo's `.github/workflows/` holds what actually runs, so a plain `diff`
between them is the whole drift check — if it reports anything, one side has
been edited in place and the other has not.

| File | Trigger | Does |
|---|---|---|
| `spec-ci.yml` | `pull_request` touching `spec/**` | `vellum lint` + `vellum suite extract`, uploads `suite.json`, summarises the scenarios the PR introduces or changes, and runs `vellum backpressure` (reporting, not blocking — see below). The three agent reviews are stubs. |
| `on-spec-merge.yml` | `push` to `main` touching `spec/**` | `vellum mint` opens the ledger record for the merge commit; the workflow tags the decorative name, extracts the suite, files work-item issues from `workplan.yaml`, commits and pushes. The planner is a stub. |

## The bodies are shims

`spec/features/spec-pipeline.md`: "Pipeline logic lives in the product CLI, and
forge workflow bodies are single-command shims over it — minting is `vellum
mint`, the divergence gate is `vellum backpressure`, the pin close is `vellum
pin advance`." What used to be four shell steps in `on-spec-merge.yml` — the
version guard, the baseline walk, the name derivation, the ledger write — is
one `vellum mint` call, and the `backpressure` stub is one `vellum backpressure`
call.

The point is testability, not brevity. Logic in a workflow body can only be
exercised by running this forge; the same logic in a command is driven in a
sandbox, which is what makes the pipeline's behavior a PASS-able property
rather than a deployment one (`spec/features/scenarios-and-harness.md`). Every
guard that moved is covered in `tests/test_mint.py` and
`tests/test_backpressure.py`, named for what it protects.

**Read `steps.mint.outputs.minted`, never the exit code.** `vellum mint` exits
0 on both of its no-ops — a commit that does not touch `spec/`, and a replay —
exactly as the guard step it replaced did, because a racing merge and a re-run
of an idempotent job are both benign. `minted=no` is what tells the workflow to
skip the steps that are *not* idempotent.

**Two `run:` bodies still hold logic, deliberately.** Issue filing in
`on-spec-merge.yml` and "Summarise the suite" in `spec-ci.yml` are not among
the three commands the spec names, and absorbing them would mean CLI surface
nothing has asked for — a forge issue API in the first case, a reporting flag
on `suite extract` in the second. Each carries an in-file note saying so.

## What is real and what is not

Every stub is marked in-file with a `STUB — NOT IMPLEMENTED (v0.2)` banner, a
`::warning` annotation at runtime, and a comment naming the role contract that
will replace it. **The stubbed checks pass vacuously** — a green `spec-ci` in
v0.1 means the spec lints and every scenario parses, and nothing more. It is
not evidence that a spec change was reviewed for coherence, coverage or impact.

Real in v0.1:

- lint and suite extraction, and their non-zero exit codes blocking a merge
- opening and updating the ledger record, keyed by the merge commit's sha —
  which is also the entire replay guard, since the record either exists for
  this commit or it does not
- naming the version, as decoration: a `spec-vN` tag derived from the count of
  spec versions in the commit's ancestry. Nothing reads it, so the step is
  `continue-on-error` and a run whose tag push fails has still recorded the
  version
- filing work-item issues from a `workplan.yaml` (reusing an existing issue of
  the same title rather than duplicating it)
- counting the divergence window (`vellum backpressure`) — real, and
  **reporting only** until releases exist; see below

Stubbed for v0.2: coherence review, coverage review, impact report, and the
planner that writes `workplan.yaml`. Until the planner lands, a hand-written
`workplan.yaml` at the intent repo root exercises the issue-filing path end to
end.

### Backpressure runs for real and does not block yet

`vellum backpressure` counts ledger records that are neither `shipped` nor
`superseded` and exits non-zero at or past `budgets.divergence_cap`. Nothing
has ever set a record to `shipped`, because releases do not exist yet
(`ledger/releases.yaml` carries `spec_conformed: null` and no cuts), so every
record in the intent repo counts as unshipped — 11 against a cap of 3 when this
was measured.

Arming the gate in that state would block every spec merge in the repository,
including the one that lands the release machinery: a deadlock, not
backpressure. So the step runs, reports into the job summary, and carries
`continue-on-error: true`. **Delete that one line to arm it**, once shipped
versions actually leave the window. The `set -o pipefail` beside it is
load-bearing — without it the step's status is `tee`'s, and arming the gate
would produce a check that can never close.

## Prerequisites

- **Nothing depends on the tags.** A spec version is a `main` commit whose diff
  touches `spec/**` (`spec/decisions/2026-08-28-versions-are-commits.md`); the
  `spec-v*` tags are names for eleven of them, and a missing, late or wrong one
  changes no behavior.

- **The ledger records are sha-keyed, and the migration is done.** This entry
  used to ask for it. `waviisoft/vellum-intent#22` ("ledger: key the records by
  commit sha") rewrote `ledger/spec-vN.yaml` into `ledger/<sha>.yaml`; measured
  on `main` while this wave was written, all eleven records are sha-keyed and
  none carries `spec_version: spec-v*`. That matters more than housekeeping now
  that `vellum backpressure` counts them: a name-keyed leftover is not a
  version this CLI recognises, so it is reported as unreadable rather than
  counted, and a ledger half-migrated would have measured a window short.

- The intent repo needs a **`VELLUM_TOKEN`** secret holding a token that can
  read `waviisoft/vellum`. This repo is private, so the intent repo's own job
  token cannot read it. Both workflows check the secret first and fail with an
  explicit message when it is missing, rather than failing later and less
  legibly inside pip.
- Both workflows check this repo out at `env.VELLUM_REF` (`main`) and
  `pip install` that path. Point it at a tag once this repo cuts one. They
  check the CLI out rather than installing from its git URL because the repo is
  private and a token has to be supplied, which `pip install
  "vellum @ git+https://..."` has nowhere to take. (It was also, originally,
  because pip's VCS install recursively initialized the private `spec`
  submodule and failed — that submodule is gone, and the private-repo reason
  stands on its own.)
- `on-spec-merge.yml` needs `contents: write` (to push the tag and the ledger
  commit) and `issues: write` (to file work items). Branch protection on `main`
  must allow the workflow token to push, or the ledger commit step fails.
- Both check out with `fetch-depth: 0`: the version sequence *is* main's
  history, and `vellum suite extract` dates scenarios by walking it. A shallow
  clone silently re-dates every scenario below its graft **forward**, onto the
  truncation point — right count, nothing pending, nothing raised, and wrong in
  the direction that arms scenarios the product already satisfies. `suite.json`
  carries `shallow: true` when it happens; treat that as the last line, not the
  guard.
