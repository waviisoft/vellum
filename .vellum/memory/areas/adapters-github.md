# Area: the GitHub adapter

Two trees, easily confused:

- **`.github/workflows/ci.yml`** runs in *this* repo — tests on 3.10 and 3.12,
  plus a `conformance` job that fetches the intent repo at the pin, lints it,
  extracts the suite, re-runs the whole test suite with `VELLUM_INTENT_REPO`
  set, and asserts the suite was extracted at the pin with nothing pending and
  a full history. That job is what makes "this checkout conforms to its pin" a
  checked property rather than a README claim, and running the tests inside it
  is what stops the other job's skips from being a hole.

  **Divergence is reported, never failed on.** `spec/features/repo-topology.md`
  at spec-v8: conformance CI's job is the checkout against its pin, so a pin
  behind `spec-head` is divergence to summarise and the backpressure acts at
  spec approval instead. This is not a style preference — the old shape failed
  the `conformance` job on *every branch including the base* the moment the
  intent repo moved ahead, which is what happened to waviisoft/vellum#4. The
  `Report divergence from spec-head` step prints the versions landed since the
  pin and exits 0; the pin not being an ancestor of `main` at all is also
  reported, because that is the expected shape while an approved spec PR is
  held for paired landing.

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
  `actions/checkout` with `token: ${{ secrets.SPEC_TOKEN }}` into `intent/`,
  moves it to `pin.commit` from `.vellum/product.yaml`, then lints, extracts
  and tests. Without the secret the job takes the `Conformance NOT VERIFIED`
  step and says so rather than passing quietly — observed, on the attempts
  before the secret existed. If that step is ever skipped *and* the pin steps
  are skipped too, the `steps.cred` guard has broken.

  This was already the real pin mechanism before the submodule was removed:
  the job never used the gitlink, it read the file. That is a good part of why
  `spec/decisions/2026-08-28-pin-file.md` ruled the submodule ceremony.

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
were not what ran. If you must change the installed copy first to unbreak CI,
fold it back in the same wave.

**Measured again at the start of this wave, and they were still not identical.**
waviisoft/vellum#5 folded the change back and dropped the note *upstream*, and
recorded "the two sides are byte-identical" — but nobody removed the note from
the installed copies, so both still carry a seven-line `INSTALLED COPY` header
asking for a fold-back that has already happened. Only comments differ; the
steps are the same. Two lessons, and the second is the one that costs: a
fold-back is not done until the installed side is also edited, and **"they are
identical now" is a claim with a short shelf life — run the `diff`, do not
read the note.** The header is doubly stale as of this wave, since it explains
itself by the private `spec` submodule, which no longer exists.

**The CLI is checked out, not `pip install`ed from its git URL, and that shape
is load-bearing.** `waviisoft/vellum` is private, so the intent repo's own job
token cannot read it — hence the `VELLUM_TOKEN` secret, which both workflows
assert before use so a missing secret fails with a named error instead of an
opaque pip failure. The private-repo half is the whole reason now and it is
sufficient on its own — a token has to be supplied either way, and `pip install
"vellum @ git+https://..."` has nowhere to put one. The *original* reason was
narrower and is spent: pip's VCS install runs
`git submodule update --init --recursive`, which cloned the private `spec`
submodule with no credentials and failed. That submodule is gone
(`spec/decisions/2026-08-28-pin-file.md`) — one of the failures that cost it
its job — so do not cite it as a live constraint, and do not read its removal
as permission to "simplify" this back into a one-line pip install.

**Backpressure is real now and deliberately not blocking.** `vellum
backpressure` counts records that are neither `shipped` nor `superseded`, and
nothing has ever set a record to `shipped` because releases do not exist yet
(`ledger/releases.yaml`: `spec_conformed: null`, no cuts). So every record
counts as unshipped — measured, 11 against a cap of 3. Arming the gate in that
state blocks every spec merge in the repository, including the one that would
land the release machinery: a deadlock, not backpressure. The step carries
`continue-on-error: true` and reports into the job summary; **delete that one
line to arm it** once shipped versions actually leave the window.

`set -o pipefail` in that step is load-bearing, not style. Without it the step
takes `tee`'s status, which is always 0, so deleting `continue-on-error` would
arm a gate that can never close — a guard that silently does nothing, which is
worse than none.

**The stubs pass vacuously.** Coherence review, coverage review, impact report
(job `agent-review` in `spec-ci.yml`) and the "Plan the wave" step in
`on-spec-merge.yml` all `echo` and exit 0. A green `spec-ci` in
v0.1 means the spec lints, every scenario parses, and the divergence window was
reported — nothing more. Each stub carries a `STUB — NOT IMPLEMENTED (v0.2)`
banner and emits a `::warning` so it is visible in the run, not just in the
file.

**The bodies are shims over the CLI, and that is a spec requirement.**
`spec/features/spec-pipeline.md`: "Pipeline logic lives in the product CLI, and
forge workflow bodies are single-command shims over it." The four steps that
held the version guard, the baseline walk, the name derivation and the ledger
write are one `vellum mint` call; the `backpressure` stub is one `vellum
backpressure` call. The reason is testability — logic in a workflow body can
only be exercised by running this forge, and the same logic in a command is
driven in a sandbox, which is what makes pipeline behavior PASS-able rather
than a deployment property (`spec/features/scenarios-and-harness.md`). Do not
move a guard back into a `run:` body to "keep it visible"; it becomes
ungradeable there.

**Gate on `steps.mint.outputs.minted`, never on the exit code.** `vellum mint`
exits 0 on both no-ops — a commit that does not touch `spec/`, and a replay —
exactly as `proceed=no` left the job green before. The guard's job is to skip
the steps that are *not* idempotent (tagging, filing issues, pushing), not to
redden a re-run of an idempotent one (decision D11).

**There is no minting step, and there must not be one again.** The merge commit
IS the version (`spec/decisions/2026-08-28-versions-are-commits.md`), so the
next-integer arithmetic and the already-tagged guard are both gone. What is
left is bookkeeping about a version that already exists — which is what `vellum
mint` does. The replay guard is a ledger record existing for this commit, and
`open_record()` is idempotent besides, so a replay is harmless even if the
guard is wrong. Do not reintroduce a "compute the next version" step: two of
the old machinery's failure modes (lexical `sort -n` hazards, a tag pushed out
of order re-dating every scenario under it) existed only because a second
version system was maintained beside git.

**Two `run:` bodies still hold logic, and leaving them was the call.** Issue
filing here, and "Summarise the suite" in `spec-ci.yml`. Neither is one of the
three commands the spec names, and absorbing them means CLI surface no spec
change has asked for — a forge issue API in one case, a reporting flag on
`suite extract` in the other. Issue filing is also dead today: its gate is
`hashFiles('workplan.yaml')` and only the stub planner would write that file.
Both carry an in-file note saying this, so the next reader does not take them
for oversights.

**The name is derived from history, and its push is allowed to fail.** The
`Name the version` step computes
`spec-v$(git rev-list --first-parent --count <sha> -- spec)` and pushes it as a
tag. Derived, not read back: it cannot be missing, late or out of order the way
`max(spec-v*) + 1` could. Verified to reproduce every existing name exactly —
`bc84e59` -> `spec-v1`, `be029e6` -> `spec-v5`, `1ce87cb` -> `spec-v11`. The
step is `continue-on-error: true` **on purpose**: a name is decoration, and a
failed tag push must never fail a run that has already recorded the version.
Do not "fix" that by making it fatal.

**It runs *after* minting now, and the old ordering constraint is gone.** The
name used to travel from this step into `--name`, which forced it earlier
(`steps.name.outputs.tag` is empty if referenced from an earlier step). It
travels the other way now: `vellum mint` derives the name and writes it into
the record, and this step reads `steps.mint.outputs.name` to tag. Mint reports
the name on a replay too, so a version recorded by a run whose tag push failed
can still be named later without recomputing the count by hand.

**The head commit message stays in this file, and that is the security
boundary.** It is attacker-supplied text and its only use is the tag
annotation, so it is passed through `env` here and never reaches the CLI at
all. The only message `vellum mint` writes is `ledger: open <name>`, derived
from what it computed itself. Do not "tidy" tagging into the CLI — that hands
an injection surface to the process that writes the ledger.

**Issue filing is keyed by title, not by position.** The filing loop searches
for an existing issue titled `<label>: <work item title>` — the decorative name
when there is one, `spec-<sha>` otherwise — and reuses it. That title is the one
place a name may appear, because nothing reads it back. Work
item numbers only exist after filing, so the issue is created first and
`vellum ledger advance --item <number>` records it — which is what makes a
replay idempotent (decision D11).

**`fetch-depth: 0` in both checkouts.** The history *is* the version sequence
and `vellum suite extract` walks it. The requirement did not change when tags
did; the failure mode got quieter, because a shallow clone now re-dates
scenarios forward onto the graft instead of marking them all pending. See
`.vellum/memory/areas/cli.md`.

**`on-spec-merge.yml` pushes to `main`.** It needs `contents: write` and branch
protection that lets the workflow token push, or the "Commit the ledger record"
step fails — leaving a version with no committed record. This is less bad than
it was: the version exists whether or not anything is written, because the
version is the commit. The missing record is bookkeeping to replay, not a
version that never got minted.

## The history is the version sequence

The intent repo's spec versions are its `main` commits touching `spec/**`; the
`spec-v1`..`spec-v11` tags are names for eleven of them and nothing more. A
missing, late or wrong tag now changes nothing — which retires the hazard this
section used to carry (during the spec-v2 wave `spec-v2` was pushed before
`spec-v1` and every scenario briefly reported as version 2). What replaced it
is truncation: see `fetch-depth: 0` above.

**The ledger migration is done — this section used to ask for it.** The entry
here said `ledger/spec-v1.yaml`..`spec-v11.yaml` still carried
`spec_version: spec-vN` and wanted rewriting. `waviisoft/vellum-intent#22`
("ledger: key the records by commit sha") did it. Measured on intent `main`
while this wave was written: eleven records, every filename a sha, no
`spec_version: spec-v*` anywhere. This is the second time in this file a
"someone should do this" note outlived the doing of it — see the `INSTALLED
COPY` header above — so the lesson repeats: **run the check, do not read the
note.**

It matters more than housekeeping now. `vellum backpressure` counts these
records, and a name-keyed leftover is not a version the CLI recognises: it is
reported as unreadable and not counted, so a half-migrated ledger would have
measured the divergence window short and let a merge through.
