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

`adapters/github/`. Three workflows written **for the intent repo**
(`waviisoft/vellum-intent`) and kept here so they are reviewed next to the CLI
they call. Nothing in this repo runs them; `adapters/github/README.md` has the
copy instructions.

| File | Trigger |
|---|---|
| `adapters/github/spec-ci.yml` | `pull_request` touching `spec/**`, `ledger/**`, `.vellum/config.yaml`, or the workflow file |
| `adapters/github/on-spec-merge.yml` | `push` to `main` touching `spec/**` |
| `adapters/github/harness-ci.yml` | `pull_request`, unfiltered |

## Landmines

**`harness-ci.yml` runs on every PR *because* a `paths:` filter and a required
check do not compose.** GitHub never reports a path-filtered job on a PR it
filters out, and a required check that never reports leaves the PR waiting
forever. That is also why the check the intent repo most needs could not live in
`spec-ci.yml`: that file is filtered to `spec/**`, `ledger/**` and
`.vellum/config.yaml`, and the breach being guarded — a harness session also
editing `.vellum/memory/` — is a diff that touches none of them.

**The harness job's role comes out of the diff, and the half it leaves open is
stated rather than hidden.** A PR writing `harness/` is a harness PR and must
write nothing else; a PR writing no harness path is checked against no role at
all. Nothing reads a branch name, a title or a label to decide it — that would be
enforcement derived from decoration, which
`spec/decisions/2026-08-28-versions-are-commits.md` removed. Closing the other
half is a different question ("does this diff fit inside *some* declared role's
trees?") and needs a spec slice before it needs code.

**The boundary data for the intent repo is not in this repo, and the job is red
until it exists.** `vellum verify boundaries . --boundaries-from config` reads a
`write_boundaries` block from the intent repo's `.vellum/config.yaml`; this repo
ships the reader and no data for a repo it does not own. Until the architect
authors the block, the job exits **2** on any PR writing `harness/` — "I could
not answer", which is the right colour of red. Do not soften it to a skip: a
guard with nothing to check against has not passed.

**The conformance-map check cannot compare the whole file, and the reason is
structural.** `harness/conformance.md` records the commit the suite was
extracted at, which is `head_commit(repo)` — the checkout's HEAD, not the last
spec-touching commit. So the committed file names a sha that did not exist when
it was written, and that line differs on every single run. The step strips that
one line from both sides and compares everything else exactly. Measured on
intent `main` at 8d9e228: a fresh run differs from the committed map in that
line and nothing else. If someone "fixes" the step into a plain `diff`, the
check becomes impossible rather than strict, and impossible red is how a team
learns to ignore red. The real fix belongs in `harness/run.py` — an option to
omit the header — and `harness/` is not a tree this repo may write.

**The two copies drift silently; only a `diff` catches it.** `adapters/github/`
is the upstream copy and is reviewed here, but `waviisoft/vellum-intent`'s
`.github/workflows/` is what actually runs, and nothing checks that they agree.
They have already diverged once: the installed copies were edited in place to
check the CLI out with `VELLUM_TOKEN` and `pip install ./vellum-cli`, and each
carried an `INSTALLED COPY` header note asking for the fold-back — which then
sat unfolded while a wave's worth of review happened here against files that
were not what ran. If you must change the installed copy first to unbreak CI,
fold it back in the same wave.

**The `INSTALLED COPY` headers are gone, and this note about them was itself
stale.** The history: waviisoft/vellum#5 folded the installed edits back and
dropped the note *upstream*, recording "the two sides are byte-identical" while
the installed copies still carried a seven-line `INSTALLED COPY` header. This
paragraph then said so — and `waviisoft/vellum-intent#21` ("workflows: sync the
installed copies with upstream adapters") fixed it, after which the paragraph
went on asserting a drift that no longer existed.

Measured at the start of *this* wave: `.github/workflows/spec-ci.yml` and
`on-spec-merge.yml` in the intent repo were **byte-identical** to
`adapters/github/` at vellum `main`, and neither carries the header.

So the lesson survives its own example twice over: a fold-back is not done
until the installed side is edited, and **"they are identical now" — or "they
differ" — is a claim with a short shelf life. Run the `diff`, do not read the
note.** This wave rewrites both upstream files; the architect syncs the
installed copies at landing, and the `diff` is how you will know it happened.

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
nothing has ever set a record to `shipped`. So every record counts as
unshipped. Arming the gate in that state blocks every spec merge in the
repository, including the one that would land the relief: a deadlock, not
backpressure. The step carries `continue-on-error: true` and reports into the
job summary; **delete that one line to arm it**.

**Wave F built the relief and did not arm the gate, and the reason moved
rather than went away.** `vellum release cut` exists now and a promoted cut
*does* take versions out of the window — verified on a scratch clone of intent
`main`: with the records advanced to `verified`, one cut naming all 14 takes
`vellum backpressure . --strict` from "14 of 3, BLOCKED" to "0 of 3, OK". What
is missing is not machinery any more, it is a **recorded cut**, and two things
stand between:

- `ledger/releases.yaml` on intent `main` still reads `cuts: []` and
  `channels.production.spec_conformed: null`. A cut has to be *recorded there*,
  and a wave landing in this product repo cannot record one — an implementer
  holds no intent-repo credentials (`spec/features/repo-topology.md`).
- Every one of the 14 records is `approved`, and `release cut` refuses to
  promote a wave that has not reached `verified` or `shipped`. That refusal is
  not fussiness: promotion writes `shipped`, which is one of
  `chain.CERTIFIABLE_STATES`, so a cut shipping an `approved` wave would satisfy
  `vellum ledger verify`'s own `uncertified-wave` check by having been made.

So `waviisoft/vellum-intent#41` stays OPEN, re-scoped from "wait for the release
machinery" to "wait for a recorded cut". The arming condition is a command, not
a judgement: arm when `vellum backpressure . --strict` exits 0 against intent
`main`. **Run it; do not read this note.** The measurement above was 11 records
when it was first written and is 14 now, which is the whole argument for
re-running rather than re-reading.

`set -o pipefail` in that step is load-bearing, not style. Without it the step
takes `tee`'s status, which is always 0, so deleting `continue-on-error` would
arm a gate that can never close — a guard that silently does nothing, which is
worse than none.

Two more things the armed gate depends on. It runs with **`--strict`**, so a
ledger file that cannot be read refuses the measurement instead of shrinking
the window; failing open on corruption is the wrong direction for a gate. And
**`1` from `vellum backpressure` means blocked and nothing else** — every other
non-zero exit is `2` — so a renamed `.vellum/config.yaml` can never present as
backpressure once the hold comes off.

**The job triggers on what it measures.** `spec-ci.yml`'s `paths:` carries
`.vellum/config.yaml` and `ledger/**` beside `spec/**`. Without them a PR could
raise `divergence_cap`, or add unshipped versions, without the gate that reads
them ever running.

**Only the pushing checkout keeps its credential.** `persist-credentials: false`
is on every `actions/checkout` in both files except `on-spec-merge.yml`'s
`Check out main`, which pushes the tag and the ledger commit. `actions/checkout`
defaults to persisting, and `spec-ci.yml`'s jobs run on `pull_request` in a
workspace rooted at the PR's merged tree — `VELLUM_TOKEN` reads a private repo
and has no reason to sit in `.git/config` there. If you add a checkout, decide
which of the two it is.

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

**A no-op still raises a `::notice`, and that is a separate step now.** The old
guard step emitted `::notice title=Not a spec version` / `::notice
title=Already recorded`, which is how a no-op showed up in the run summary
rather than only in a log nobody opens. `vellum mint` prints prose — it has no
business knowing this forge's annotation syntax — so `Say why nothing was
recorded` re-raises the annotation from `steps.mint.outputs.reason`. Delete it
and the two most common outcomes of this workflow become invisible.

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

**The push retries behind a rebase, and "replay it later" was never actually
available.** `waviisoft/vellum-intent#24` item 1: a racing merge makes the push
non-fast-forward, and nothing re-records the stranded version, because the mint
step accepts only the branch tip and a `workflow_dispatch` reaches only the head.
So the retry is not belt-and-braces over an idempotent step — it is the only
recovery there is short of a hand-written one. Two concurrent runs cannot
conflict on content: a record's filename is its version's sha.

**The push-range step is a detector and must not be mistaken for a recorder.**
Item 2 of the same issue: `paths: spec/**` fires on any commit in a push, and the
job mints the tip, so a spec commit below the tip is a version on `main` with no
ledger record — in a green run. The step names them and fails; it does not mint
them, because minting a range is a differently-shaped job and `vellum mint` takes
one `--ref`. It runs last (so a red cannot cost the tip its push) and
unconditionally (the case it catches is exactly the one where the tip is *not* a
spec commit, so `minted` is `no`).

**A work-item title never reaches a search query.** Item 3, second half. The
`--jq` program was already safe (`env.FULL`, from the injection fix in the
absorb-the-workflow-bodies wave); what was left was `--search "\"$full\" in:title"`,
where a title carrying a double quote makes an unbalanced phrase, matches
nothing, and the run files a duplicate — the exact defect the lookup exists to
prevent. It lists the `work-item` label and compares exactly in jq instead. The
label itself is created first, because `gh issue create --label` fails outright
when the label does not exist and it does not exist in the intent repo.

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
