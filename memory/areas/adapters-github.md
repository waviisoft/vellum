# Area: the GitHub adapter

`adapters/github/`. Two workflows written **for the intent repo**
(`waviisoft/vellum-intent`) and kept here so they are reviewed next to the CLI
they call. Nothing in this repo runs them; `adapters/github/README.md` has the
copy instructions.

| File | Trigger |
|---|---|
| `adapters/github/spec-ci.yml` | `pull_request` touching `spec/**` |
| `adapters/github/on-spec-merge.yml` | `push` to `main` touching `spec/**` |

## Landmines

**The stubs pass vacuously.** Coherence review, coverage review, impact report
(job `agent-review` in `spec-ci.yml`), the `backpressure` job, and the "Plan the
wave" step in `on-spec-merge.yml` all `echo` and exit 0. A green `spec-ci` in
v0.1 means the spec lints and every scenario parses — nothing more. Each stub
carries a `STUB — NOT IMPLEMENTED (v0.2)` banner and emits a `::warning` so it
is visible in the run, not just in the file.

**Minting must not double-fire.** The `guard` step in `on-spec-merge.yml` runs
`git tag --points-at HEAD --list 'spec-v*'` and skips every subsequent step if
the commit is already tagged. Without it a re-run or a `workflow_dispatch`
mints a second integer for the same commit and the version sequence stops
meaning anything.

**Version arithmetic is numeric, not lexical.** The `version` step pipes through
`sort -n`; a lexical sort makes `spec-v9` the latest when `spec-v10` exists. The
`sed -n 's/^spec-v\([0-9]\{1,\}\)$/\1/p'` anchors both ends so `spec-v1.2` and
other malformed tags are ignored rather than parsed as `1`.

**Issue filing is keyed by title, not by position.** The filing loop searches
for an existing issue titled `spec-vN: <work item title>` and reuses it. Work
item numbers only exist after filing, so the issue is created first and
`vellum ledger advance --item <number>` records it — which is what makes a
replay idempotent (decision D11).

**`fetch-depth: 0` in both checkouts.** Tags are the version sequence and
`vellum suite extract` walks them. See `memory/areas/cli.md`.

**`on-spec-merge.yml` pushes to `main`.** It needs `contents: write` and branch
protection that lets the workflow token push, or the "Commit the ledger record"
step fails after the tag has already been pushed — leaving a minted version
with no committed record.

## Prerequisite not yet met

`spec-v1` does not exist as a tag on the intent repo. `on-spec-merge.yml` mints
`max(spec-v*) + 1`, so with no tags the *next* spec merge would mint `spec-v1`
and attach it to the wrong commit. The tag command is in
`adapters/github/README.md` and in `memory/waves/spec-v1.md`.
