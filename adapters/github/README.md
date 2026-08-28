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
| `spec-ci.yml` | `pull_request` touching `spec/**` | `vellum lint` + `vellum suite extract`, uploads `suite.json`, summarises the scenarios the PR introduces or changes. Three agent reviews and the backpressure check are stubs. |
| `on-spec-merge.yml` | `push` to `main` touching `spec/**` | Opens the ledger record for the merge commit, attaches a decorative name tag, extracts the suite, files work-item issues from `workplan.yaml`, commits the record. The planner is a stub. |

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

Stubbed for v0.2: coherence review, coverage review, impact report,
backpressure against the divergence cap, and the planner that writes
`workplan.yaml`. Until the planner lands, a hand-written `workplan.yaml` at the
intent repo root exercises the issue-filing path end to end.

## Prerequisites

- **Nothing depends on the tags.** A spec version is a `main` commit whose diff
  touches `spec/**` (`spec/decisions/2026-08-28-versions-are-commits.md`); the
  `spec-v*` tags are names for eleven of them, and a missing, late or wrong one
  changes no behavior.

- **The installed ledger records are still name-keyed.** `ledger/spec-vN.yaml`
  carry `spec_version: spec-vN`, and the CLI keys records by commit sha, so it
  does not see them. Nothing here depends on the old records — every record
  written from now on is sha-keyed under `ledger/<sha>.yaml` — but rewriting
  those `spec_version` fields to the commits they name is worth doing while
  re-syncing these files.

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
