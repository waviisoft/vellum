# Area: the GitHub adapter

Two trees, easily confused:

- **`.github/workflows/ci.yml`** runs in *this* repo — tests on 3.10 and 3.12,
  plus a `conformance` job asserting the pinned spec lints and that every
  scenario extracts at the version its tags say. That job is what makes "this
  checkout conforms to its pin" a checked property rather than a README claim.

  **Runners are Blacksmith, not GitHub-hosted.** `runs-on: ubuntu-latest` is
  never assigned a runner in this organisation: the job is accepted and then
  fails in 3-8 seconds with `conclusion: failure`, no logs (the download
  404s), no `steps` array, and `runner_id: 0` with an empty `runner_name`,
  while the workflow reports `state: active`. Five runs and one re-run failed
  that way. It reads like an infrastructure blip and is not one — do not
  "fix" the workflow in response to it, and never swap the label back to
  `ubuntu-latest`. Confirmed working:
  `blacksmith-2vcpu-ubuntu-2204`, used in `.github/workflows/ci.yml` and in
  both files under `adapters/github/`. A healthy job shows a real
  `runner_name` (e.g. `blacksmith-01m13gdj...-2vcpu`), a populated `steps`
  array, and Blacksmith's `job_completed.sh` hook in the log.

  **`SPEC_TOKEN` reads the private intent repo, and is required.** The
  `conformance` job checks `waviisoft/vellum-intent` out as a separate
  `actions/checkout` with `token: ${{ secrets.SPEC_TOKEN }}`, moves it to the
  commit in `.vellum/product.yaml`, then lints and extracts. Observed working:
  the checkout, `vellum lint`, `vellum suite extract` and the pin assertion
  all pass, and `suite.json` uploads as an artifact. Without the secret the
  job takes the `Conformance NOT VERIFIED` step and says so rather than
  passing quietly — also observed, on the attempts before the secret existed.
  If that step is ever skipped *and* the pin steps are skipped too, the
  `steps.cred` guard has broken.

  **Still unverified:** whether `actions/checkout` with `submodules: recursive`
  would fail on the private submodule under the default `GITHUB_TOKEN`. It is
  documented GitHub behaviour (the token is scoped to its own repository) and
  is why `ci.yml` is shaped the way it is, but no job in this repo has ever
  attempted it on a working runner. Do not cite it as an observation.

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

**The two copies drift silently; only a `diff` catches it.** `adapters/github/`
is the upstream copy and is reviewed here, but `waviisoft/vellum-intent`'s
`.github/workflows/` is what actually runs, and nothing checks that they agree.
They have already diverged once: the installed copies were edited in place to
check the CLI out with `VELLUM_TOKEN` and `pip install ./vellum-cli`, and each
carried an `INSTALLED COPY` header note asking for the fold-back — which then
sat unfolded while a wave's worth of review happened here against files that
were not what ran. Folded back now, and the note dropped, so the two sides are
byte-identical: `diff` them before trusting either. If you must change the
installed copy first to unbreak CI, fold it back in the same wave.

**The CLI is checked out, not `pip install`ed from its git URL, and that shape
is load-bearing.** `waviisoft/vellum` is private, so the intent repo's own job
token cannot read it — hence the `VELLUM_TOKEN` secret, which both workflows
assert before use so a missing secret fails with a named error instead of an
opaque pip failure. And it is a checkout rather than `pip install
"vellum @ git+https://..."` because pip's VCS install runs
`git submodule update --init --recursive`, which tries to clone the private
`spec` submodule with no credentials and fails. The CLI does not need the spec
pin to build, and `actions/checkout` takes no submodules by default. Do not
"simplify" this back into a one-line pip install.

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
