# GitHub adapter

Two workflows **for the intent repo** (`waviisoft/vellum-intent`), kept here so
they are reviewed alongside the CLI they call. They are not run by this repo.

Install them by copying:

```sh
cp adapters/github/spec-ci.yml       ../vellum-intent/.github/workflows/
cp adapters/github/on-spec-merge.yml ../vellum-intent/.github/workflows/
```

| File | Trigger | Does |
|---|---|---|
| `spec-ci.yml` | `pull_request` touching `spec/**` | `vellum lint` + `vellum suite extract`, uploads `suite.json`, summarises the scenarios the PR would mint. Three agent reviews and the backpressure check are stubs. |
| `on-spec-merge.yml` | `push` to `main` touching `spec/**` | Mints the next integer tag, opens the ledger record, extracts the suite, files work-item issues from `workplan.yaml`, commits the record. The planner is a stub. |

## What is real and what is not

Every stub is marked in-file with a `STUB — NOT IMPLEMENTED (v0.2)` banner, a
`::warning` annotation at runtime, and a comment naming the role contract that
will replace it. **The stubbed checks pass vacuously** — a green `spec-ci` in
v0.1 means the spec lints and every scenario parses, and nothing more. It is
not evidence that a spec change was reviewed for coherence, coverage or impact.

Real in v0.1:

- lint and suite extraction, and their non-zero exit codes blocking a merge
- next-integer version minting, including the "already tagged" replay guard
- opening and updating the ledger record
- filing work-item issues from a `workplan.yaml` (reusing an existing issue of
  the same title rather than duplicating it)

Stubbed for v0.2: coherence review, coverage review, impact report,
backpressure against the divergence cap, and the planner that writes
`workplan.yaml`. Until the planner lands, a hand-written `workplan.yaml` at the
intent repo root exercises the issue-filing path end to end.

## Prerequisites

- The intent repo carries `spec-v1` and `spec-v2`, so `on-spec-merge.yml` will
  mint `spec-v3` on the next spec merge. Both tags must stay pushed: the
  sequence is `max(spec-v*) + 1`, and a missing tag silently re-dates every
  scenario introduced at it.

- Both workflows `pip install` the CLI from this repo's `main`
  (`env.VELLUM_REF`). Point it at a tag once this repo cuts one.
- `on-spec-merge.yml` needs `contents: write` (to push the tag and the ledger
  commit) and `issues: write` (to file work items). Branch protection on `main`
  must allow the workflow token to push, or the ledger commit step fails.
- Both check out with `fetch-depth: 0`: the version sequence lives in tags, and
  `vellum suite extract` dates scenarios by walking them. A shallow clone
  silently reports every scenario as new.
