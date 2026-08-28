# Area: the GitHub adapter

Two trees, easily confused:

- **`.github/workflows/ci.yml`** runs in *this* repo — tests on 3.10 and 3.12,
  plus a `conformance` job asserting the pinned spec lints and that every
  scenario extracts at the version its tags say. That job is what makes "this
  checkout conforms to its pin" a checked property rather than a README claim.

  **CI has never actually executed in this repo.** Every run so far — three,
  across three commits — completed in 4-8 seconds with `conclusion: failure`,
  no logs (the log download 404s), no `steps` array, and `runner_id: 0` with an
  empty `runner_name`. That is a run rejected before dispatch: no runner was
  ever assigned. The workflow itself is `state: active` and parses. This is an
  org-level Actions setting — billing/spending limit for private repos, or an
  Actions policy — and nothing in the workflow file can fix it. Do not
  "fix" ci.yml in response to it; check the org's Actions billing first.
  A re-run of the same run (`run_attempt: 2`) failed identically in 3 seconds,
  so it is not a transient runner shortage.

  **Unconfirmed lead:** the repo has the Blacksmith (`[code]smith`) app
  installed — it appends a footer to PR bodies — and Blacksmith supplies
  Actions runners. If runner migration is enabled there it can rewrite or
  intercept `runs-on: ubuntu-latest`, and an incomplete setup would produce
  this exact signature. Not verified; org settings are not visible from here.
  Worth checking alongside billing.

  **Design constraint, believed but NOT yet observed:** a workflow's default
  `GITHUB_TOKEN` is scoped to the repository it runs in, and
  `waviisoft/vellum-intent` is private, so `actions/checkout` with
  `submodules: recursive` is expected to fail auth here. This is a documented
  GitHub behaviour and a structural consequence of decision D3 — under split
  repos a product repo's CI needs a credential for the intent repo — but it has
  **not** been confirmed against this repo, because no job has ever reached its
  first step. ci.yml is already built for it (`test` takes no submodule;
  `conformance` uses `secrets.SPEC_TOKEN`), which is correct on its own merits.
  Verify it the first time CI actually runs; until then treat it as reasoning,
  not observation.

- **`adapters/github/`** (below) is written *for the intent repo* and never
  runs here.

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

## Tags are the version sequence

The intent repo carries `spec-v1` and `spec-v2`; the next spec merge mints
`spec-v3`. Both tags must stay pushed. A missing tag does not fail loudly — it
silently re-dates every scenario introduced at it, because `version_history()`
only sees the tags that exist. This bit during the spec-v2 wave, when
`spec-v2` was pushed before `spec-v1` and every scenario briefly reported as
version 2.
