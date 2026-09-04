# Area: the GitHub adapter

Two trees, and the split between them changed in the installer wave. Read this
paragraph before anything else in the file:

- **`.github/workflows/` now holds four workflows, not one.** `ci.yml` runs in
  *this* repo. The other three — `spec-ci.yml`, `on-spec-merge.yml`,
  `harness-ci.yml` — hold the logic every installation's INTENT repo runs, as
  **reusable `workflow_call` workflows**. They have no trigger of their own, so
  they never run for this repo; a PR here that breaks one is caught by
  `tests/test_workflows.py` and by review, not by a red check.
- **`adapters/github/` now holds caller stubs**, one per shipped workflow: a
  dozen lines naming the reusable workflow at a pinned ref and passing
  `VELLUM_TOKEN` by name. They are a *rendering* of `vellum.install.SHIPPED`,
  not a second source; see `areas/cli.md`.

`spec/features/installation.md` and waviisoft/vellum-intent#23 part 1. What
replaced full copies is the point of the whole wave, and the reason is two
paragraphs down.

## `.github/workflows/ci.yml` — this repo's own CI

Tests on 3.10 and 3.12, plus a `conformance` job that fetches the intent repo at
the pin, lints it, extracts the suite, re-runs the whole test suite with
`VELLUM_INTENT_REPO` set, and asserts the suite was extracted at the pin with
nothing pending and a full history. That job is what makes "this checkout
conforms to its pin" a checked property rather than a README claim, and running
the tests inside it is what stops the other job's skips from being a hole.

**Divergence is reported, never failed on.** `spec/features/repo-topology.md` at
spec-v8: conformance CI's job is the checkout against its pin, so a pin behind
`spec-head` is divergence to summarise and the backpressure acts at spec
approval instead. This is not a style preference — the old shape failed the
`conformance` job on *every branch including the base* the moment the intent
repo moved ahead, which is what happened to waviisoft/vellum#4. The `Report
divergence from spec-head` step prints the versions landed since the pin and
exits 0; the pin not being an ancestor of `main` at all is also reported,
because that is the expected shape while an approved spec PR is held for paired
landing. `vellum doctor`'s ref-currency section is the same posture applied to
an installation's pinned ref, deliberately.

**`SPEC_TOKEN` reads the private intent repo, and is required.** The
`conformance` job checks `waviisoft/vellum-intent` out as a separate
`actions/checkout` with `token: ${{ secrets.SPEC_TOKEN }}` into `intent/`, moves
it to `pin.commit` from `.vellum/product.yaml`, then lints, extracts and tests.
Without the secret the job takes the `Conformance NOT VERIFIED` step and says so
rather than passing quietly — observed, on the attempts before the secret
existed. If that step is ever skipped *and* the pin steps are skipped too, the
`steps.cred` guard has broken.

`ci.yml` was **not** changed in behaviour by the installer wave: only its header
comment, which used to describe `adapters/github/` as the other tree.

**Its three checkouts do not set `persist-credentials: false`, and one of them
carries `SPEC_TOKEN`** — the credential that reads the private intent repo. That
predates this wave and was left alone because `ci.yml`'s behaviour was not this
change's to alter, so `test_only_the_pushing_checkout_keeps_its_credential`
scopes itself to the three shipped workflows. **That narrowing is an OPEN ITEM,
not a rule**: none of the three checkouts pushes, so all three should carry the
line, and the test should widen to every workflow in the tree in the wave that
adds it. Recorded here so the scope in that docstring is a decision somebody
made rather than a hole nobody noticed.

## Why the copies are gone

`adapters/github/` used to hold full copies of the three workflows, upstream of
`waviisoft/vellum-intent`'s `.github/workflows/`, kept equal by nothing but a
`diff` somebody remembered to run. With ONE repo pair that produced two measured
incidents, both recorded here before this rewrite and both worth keeping:

- **A fold-back sat unfolded through a wave of review.** The installed copies
  were edited in place to check the CLI out with `VELLUM_TOKEN` and `pip install
  ./vellum-cli`; each carried an `INSTALLED COPY` header asking for the
  fold-back, and that note sat there while a wave's worth of review happened
  here against files that were not what ran.
- **The note about the drift outlived the drift.** waviisoft/vellum#5 folded the
  edits back and recorded "the two sides are byte-identical" while the installed
  copies still carried the header; waviisoft/vellum-intent#21 then fixed the
  installed side, after which this file went on asserting a drift that no longer
  existed. Twice, a "someone should do this" note outlived the doing of it.

A second product pair would have doubled that surface, which is what issue #23
was opened about. **The lesson that survives is the general one: "they are
identical now" — or "they differ" — is a claim with a short shelf life. Run the
check, do not read the note.** What changed is that there is now a check to run:
`vellum doctor`, and `tests/test_install.py`'s byte-identity assertion over the
committed stubs.

**Do not reintroduce a copy.** If an installation needs behaviour a shipped
workflow does not have, that is a change to the shipped workflow. A local edit
to a stub is the thing that used to drift, and `doctor` reports it as
`carries-logic`, named by file.

**A copy does not have to be a stub to be a copy.** The first version of
`doctor` opened the three files it stamps and nothing else, so the copies could
come back by simply not being stubs: rename one aside as `spec-ci-legacy.yml`
and it still triggers on every PR, still holds the logic, and the command whose
whole job is noticing that never opens it. `stray-workflow` closes it — any
other file under `.github/workflows/` that delegates to `waviisoft/vellum`'s
workflows or runs `vellum` in a body of its own. **The general lesson is the
same one this section already carries, applied to the check instead of the
files: a check scoped to the shape you expect misses the shape you retired.**

**The stub's delegating job carries three keys and the rest are findings.**
`uses:`, `with:`, `secrets:` — an allowlist, because the interesting failures
here *report success*. `if: false` is the one to remember: a **skipped** job
reports **success** to branch protection, so `if: false` on the `harness-ci`
stub is a write-boundary gate that is green on every PR and has run nothing. A
`strategy:` matrix is the second: it runs the reusable workflow N times inside
one caller run, and on `on-spec-merge` that is N `vellum mint` runs racing the
same ledger push. Neither reddens anywhere. The full table is in
`adapters/github/README.md`; the reasoning is in
`.vellum/memory/areas/cli.md`.

**The job id is a required-check name.** `<job id> / <called job name>` is how a
called workflow's checks are reported, which is already recorded below as the
thing that has to be renamed in branch protection at install time. It is also
why `doctor` requires the shipped job id: a renamed job is the same breakage
arrived at from inside the repo instead of from the branch-protection settings,
and nothing in a checkout can see those settings to catch it twice.

**The branch list is the installation's, not this product's.** `on-spec-merge`
watches the repository's default branch; `vellum init --branch` stamps it and
`doctor` exempts `on.push.branches` from its `on:` comparison. Hard-coding
`main` made an installation on `trunk` one that could never be doctor-green —
the check reporting the repository's own correct configuration as drift. Only
the branch list is exempt: `push` must be present, its `paths:` are compared,
and a trigger added beside it is still drift.

**`vellum-ref:` is quoted in the stub.** A bare `1.10` reads back as the float
`1.1` and `010` as the int `10`, so an installation pinned to either failed its
own doctor the moment it was stamped, while the `@<ref>` on the `uses:` line —
part of a longer scalar — stayed a string. **Any workflow value that is a
version, a ref or a number-like name gets quoted**; this file's runner labels
and image tags are the other places that rule bites.

## Reusable-workflow mechanics, learned writing these

Nothing here could be verified by running a forge — an implementer holds no
intent-repo credentials — so each of these is a documented GitHub behaviour the
shape depends on, and each is worth re-checking the first time an installation
actually runs.

**`doctor` compares the CALLER HALF, and an early version did not.** The three
blocks a stub carries besides its `uses:` job — `on:`, `permissions:`,
`concurrency:` — each fail *silently* when wrong: a trigger narrowed to `paths:`
is a required check that never reports and a PR that waits forever, a
`permissions` block narrowed below what a job asks for is a run refused at the
point of use, a renamed `concurrency` group stops serialising what it exists to
serialise. Review of this wave found all three exiting 0. They are `drifted`
findings now (`CALLER_HALF` in `src/vellum/install.py`), compared **parsed**
rather than as text — a stub somebody annotated has not drifted — and against a
render whose ref does not matter, because none of the three blocks carries the
ref or the host. That last property is what keeps currency (reported) and drift
(failed) from bleeding into each other, and
`test_a_stub_pinned_to_an_old_ref_is_not_drift` pins it.

**The one part of a stub `doctor` still does not compare is its comments**, and
that is deliberate — see above.

**CHECK NAMES CHANGE, AND BRANCH PROTECTION READS CHECK NAMES.** A job that
calls a reusable workflow reports its checks as `<calling job>/<called job
name>` — so `Lint and extract the suite` becomes `spec-ci / Lint and extract the
suite`, `Harness PRs stay in harness/` becomes `harness-ci / Harness PRs stay in
harness/`, and so on. **Every required status check in the intent repo's branch
protection has to be renamed when the stubs are installed**, or the rules go on
requiring checks that no longer report and every PR waits forever — which is the
same failure mode as a path-filtered required check, arriving by a different
door. The calling job in each stub is named for the workflow (`spec-ci:`,
`on-spec-merge:`, `harness-ci:`) so the prefix is predictable rather than
`build/`. This is the one operational step installing the stubs cannot do for
you, and `vellum doctor` cannot see branch protection to warn about it.

**`actions/checkout` with no `repository:` checks out the CALLER.** In all three
shipped workflows that is what every body wants: the intent repo's spec tree,
ledger, config and diff. The one checkout that names a repository is the CLI's,
and `tests/test_workflows.py` asserts that it is the only one and that it takes
its ref from `inputs.vellum-ref`.

**Workflow-level `env:` does NOT propagate from caller to called workflow.**
That is why `VELLUM_REF: main` is gone and the ref is a declared `workflow_call`
input. Do not "restore" it as an `env` block in the stub; it would silently be
nothing on the other side.

**A called workflow's token can only be NARROWED by the callee, never widened.**
So `permissions:` has to be granted in the stub, and the shipped workflow's own
`permissions:` can only take away from that. A stub granting less than a job
asks for produces a run refused at the point of use — invisible in either file
on its own, which is why `TheStubGrantsWhatTheWorkflowAsks` in
`tests/test_workflows.py` is a cross-file test. `on-spec-merge` needs
`contents: write` + `issues: write`; `spec-ci` needs `pull-requests: write` for
its `agent-review` job.

**`concurrency` lives in the stub, deliberately.** A group serialises the runs
of one repository, and two installations sharing a group would serialise
unrelated repositories against each other. It is also the keyword most likely to
behave differently in a called workflow, and there is no forge here to find out
on. `on-spec-merge`'s group is a bare `on-spec-merge` string, so it is
per-repository by virtue of where it is declared and nothing else.

**The ref appears TWICE per stub and that is not redundancy.** `uses: ...@<ref>`
pins the workflow file; `with: vellum-ref: <ref>` pins the CLI the workflow
checks out and installs. The `@ref` alone does not pin the CLI, because the
checkout of `waviisoft/vellum` inside the body needs a ref it can be handed.
`vellum init` stamps them equal and `doctor` reports `ref-mismatch` when they
come apart. `${{ github.job_workflow_sha }}` would collapse them into one line
and was rejected: an installation's CLI version has to be readable in the
repository that runs it, not derived at runtime.

**`secrets:` by name, never `secrets: inherit`.** `spec/features/installation.md`
makes this least-authority, not style: the reusable workflow holds exactly the
credential its jobs name and nothing else in the installation. `doctor` treats
`inherit` as a finding. `github.token` is still available inside a called
workflow for the issue-filing step, scoped to the caller with the permissions
the stub granted, and needs no declaration.

**`VELLUM_TOKEN` IS OPTIONAL — `required: false` — and the checkout falls back.**
The go-live wave (waviisoft/vellum-intent#74) turned the secret from mandatory
into a thing an installation may or may not need, because `waviisoft/vellum` is
being made public and a public repo is readable by the caller's own
`github.token`. Three things moved together and none of them works alone:

- `secrets: VELLUM_TOKEN: required: false` in all three `workflow_call` blocks.
  `required: true` refuses the *run* of a caller that omits the key.
- `token: ${{ secrets.VELLUM_TOKEN || github.token }}` on every checkout of
  `waviisoft/vellum`. **An empty `token:` is not "use the default"** —
  `actions/checkout` reads it as a required input, so an unset secret produced
  "Input required and not supplied: token" before the clone was attempted. That
  is the whole reason the secret used to be mandatory in fact as well as in
  declaration. The `||` supplies the default explicitly.
- the "Require the VELLUM_TOKEN secret" step became "Say whether VELLUM_TOKEN
  was supplied", a `::notice` and no `exit 1`. Left as a gate it would have kept
  the secret mandatory from inside the body whatever `secrets:` said.
  `test_no_shipped_workflow_fails_a_run_for_a_missing_token` pins that, matched
  on what such a step would have to *do* rather than on its name.

**Why not two checkout steps with opposite `if:` conditions.** Because the
`secrets` context is not available in an `if:` at all — GitHub's context
availability table does not list it for `jobs.<id>.steps.if` — so that shape
needs the secret laundered through a step output or an env var first, and a
mis-set output runs both checkouts or neither *without reddening*. One
expression in `with:`, where the `secrets` context IS available, has no such
half-state. Both values are registered secrets and masked in the log either way.

**The old note this replaces was true and is now beside the point**, and it is
worth keeping the fact it carried: `required: true` never caught an EMPTY
secret, only an omitted key. That is still why the notice step exists — it
reports the case the declaration cannot.

**UNVERIFIED, like everything else in this section: that the caller's own
`github.token` reads a PUBLIC `waviisoft/vellum` from another organisation.** It
is documented GitHub behaviour — a repo-scoped token is a valid credential and a
public repo is readable by any credential, or none — and it is `actions/checkout`'s
own default for cross-repo public checkouts. No forge was available to run it.
It settles on the first installation run, and it fails loudly at the checkout if
it is wrong.

**Until `waviisoft/vellum` is actually public, an installation that passes no
token still fails at that checkout.** The change is safe to land ahead of the
repo flipping because every existing stub goes on passing the secret by name;
what it buys is that the stub is no longer *required* to.

## Landmines

**Runners are Blacksmith, not GitHub-hosted — a WAVIISoft hosting choice, not a
Vellum requirement.** WAVIISoft, the organisation that publishes this repo,
schedules its Actions on Blacksmith, so in *its* setup `runs-on: ubuntu-latest`
is never assigned a runner: the job is accepted and then fails in 3-8 seconds
with `conclusion: failure`, no logs (the download 404s), no `steps` array, and
`runner_id: 0` with an empty `runner_name`, while the workflow reports
`state: active`. Five runs and one re-run failed that way. It reads like an
infrastructure blip and is not one — do not "fix" the workflow in response to
it, and never swap the label back to `ubuntu-latest` *here*. Confirmed working:
`blacksmith-2vcpu-ubuntu-2204`. A healthy job shows a real `runner_name` (e.g.
`blacksmith-01m13gdj...-2vcpu`), a populated `steps` array, and Blacksmith's
`job_completed.sh` hook in the log. `test_no_job_asks_for_a_github_hosted_runner`
now pins it — and note what that test therefore asserts: this organisation's
labels, not a portable property. A fork on GitHub-hosted runners has to change
that test with the workflows.

**The labels are in the SHIPPED workflows now, and an installation cannot
change them from its stub.** That is a real cost of hosting the bodies
centrally: an installation in an organisation without the Blacksmith app has no
way to override the label, and inherits labels its runners will never answer to.
Today its options are to install Blacksmith, or to fork this repo, edit
`runs-on:` in the three files and point its stubs at the fork with
`vellum init . --from <owner>/<fork>`. The proper fix when a second organisation
needs it is a `runs-on` input with a default; nothing has asked yet, and
inventing the input ahead of the ask is how these files acquire configuration
nobody uses. Named in `adapters/github/README.md` rather than hidden.

**`harness-ci.yml` runs on every PR *because* a `paths:` filter and a required
check do not compose.** GitHub never reports a path-filtered job on a PR it
filters out, and a required check that never reports leaves the PR waiting
forever. That is also why the check the intent repo most needs could not live in
`spec-ci.yml`: that file is filtered to `spec/**`, `ledger/**` and
`.vellum/config.yaml`, and the breach being guarded — a harness session also
editing `.vellum/memory/` — is a diff that touches none of them. The stub's
trigger is therefore a bare `pull_request:`, and it must stay bare.

**`harness/conformance.md` is excluded from the harness-PR classifier.** A spec
PR that adds a scenario must regenerate the map in the same PR (the suite job
compares it to a fresh run), and reading that as a harness PR would fail it for
also writing `spec/`. Measured need: the installation and question-routing spec
PRs (waviisoft/vellum-intent#64, #65) could not go green under the original rule.

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
guard with nothing to check against has not passed. **So `harness-ci`'s stub
must not be installed ahead of that block**, which `adapters/github/README.md`
says under Prerequisites.

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

**The CLI is checked out, not `pip install`ed from its git URL — and BOTH of the
original reasons are now spent.** They are recorded because a later wave will
otherwise re-derive them wrongly. (1) pip's VCS install runs `git submodule
update --init --recursive`, which cloned the private `spec` submodule with no
credentials and failed; that submodule is gone
(`spec/decisions/2026-08-28-pin-file.md`). (2) `waviisoft/vellum` was private, so
a credential had to be supplied and `pip install "vellum @ git+https://..."` has
nowhere to put one; it is going public. What keeps the checkout is smaller and
still true: it takes a ref directly, so `inputs.vellum-ref` pins the CLI in one
readable place, and it is the one shape that works whether or not a token is
supplied. **This is now a shape somebody could reasonably propose simplifying**,
and the honest answer is "it would work, and it would move the pin into a pip
URL". That is a call for a wave that wants to make it, not a hole to fall into.

**Reuse of a private repo's workflows is an ACTIONS SETTING on this repo.**
While `waviisoft/vellum` is private, `uses: waviisoft/vellum/...` from another
repo resolves only when Settings > Actions > General > Access is "Accessible
from repositories in the organization" — and that phrasing is also the limit:
only repositories in the SAME organization can call them, however the setting is
turned. Without it the caller's run fails at `uses:` with a resolution error
before any step runs. **No checkout can see this setting**, so `vellum doctor`
says it cannot check it — and neither could the wave that wrote these files. It
is the single most likely first-run failure and it is not a defect in them.
Making the repo public retires both halves at once: any repository may then call
these workflows, and the setting stops applying. Do not delete this paragraph
when that happens — a repo can be made private again, and the failure it
describes would arrive looking like a new one.

**Backpressure is real now and deliberately not blocking.** `vellum
backpressure` counts records that are neither `shipped` nor `superseded`, and
nothing has ever set a record to `shipped`. So every record counts as unshipped.
Arming the gate in that state blocks every spec merge in the repository,
including the one that would land the relief: a deadlock, not backpressure. The
step carries `continue-on-error: true` and reports into the job summary;
**delete that one line to arm it**.

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
worse than none. `test_pipefail_precedes_any_pipe_into_tee` now pins it, and it
matches a *pipe into* `tee` rather than the three letters, because one of these
bodies contains the word "guarantee".

Two more things the armed gate depends on. It runs with **`--strict`**, so a
ledger file that cannot be read refuses the measurement instead of shrinking
the window; failing open on corruption is the wrong direction for a gate. And
**`1` from `vellum backpressure` means blocked and nothing else** — every other
non-zero exit is `2` — so a renamed `.vellum/config.yaml` can never present as
backpressure once the hold comes off.

**The job triggers on what it measures.** The `spec-ci` stub's `paths:` carries
`.vellum/config.yaml` and `ledger/**` beside `spec/**`. Without them a PR could
raise `divergence_cap`, or add unshipped versions, without the gate that reads
them ever running. The trigger is in the stub now and the gate is in the shipped
workflow, so a stub edit can break it — which is why `doctor` compares the
caller half; see below.

**Only the pushing checkout keeps its credential.** `persist-credentials: false`
is on every `actions/checkout` in the three shipped workflows except
`on-spec-merge.yml`'s `Check out main`, which pushes the tag and the ledger
commit. `actions/checkout` defaults to persisting, and `spec-ci`'s jobs run on
`pull_request` in a workspace rooted at the PR's merged tree — `VELLUM_TOKEN`
reads a private repo and has no reason to sit in `.git/config` there. If you add
a checkout, decide which of the two it is; `PUSHES` in
`tests/test_workflows.py` is the list, and adding to it is a decision somebody
has to make in a diff.

**No `${{ }}` inside any `run:` body, in any of these files.** `${{ }}` pastes
its value into the script *before* the shell parses the line, so a commit
message or a work-item title carrying a quote and a `;` runs as the step's own
code. Every such value travels through `env:`. The rule is enforced over ALL
bodies rather than the ones handling prose, because an exception is how the next
one carrying a title gets written the unsafe way —
`test_no_run_body_interpolates` makes it mechanical.

**The stubs pass vacuously.** Coherence review, coverage review, impact report
(job `agent-review` in `spec-ci.yml`) and the "Plan the wave" step in
`on-spec-merge.yml` all `echo` and exit 0. A green `spec-ci` in v0.1 means the
spec lints, every scenario parses, and the divergence window was reported —
nothing more. Each stub carries a `STUB — NOT IMPLEMENTED (v0.2)` banner and
emits a `::warning` so it is visible in the run, not just in the file. (These
are v0.2 *feature* stubs and have nothing to do with the caller stubs in
`adapters/github/`; the word does two jobs in this area and there is no better
one for either.)

**The bodies are shims over the CLI, and that is a spec requirement.**
`spec/features/spec-pipeline.md`: "Pipeline logic lives in the product CLI, and
forge workflow bodies are single-command shims over it." The four steps that
held the version guard, the baseline walk, the name derivation and the ledger
write are one `vellum mint` call; the `backpressure` stub is one `vellum
backpressure` call. The reason is testability — logic in a workflow body can
only be exercised by running this forge, and the same logic in a command is
driven in a sandbox, which is what makes pipeline behavior PASS-able rather than
a deployment property (`spec/features/scenarios-and-harness.md`). Do not move a
guard back into a `run:` body to "keep it visible"; it becomes ungradeable
there. Hosting the bodies centrally does not change this: a reusable workflow is
still a workflow, and still only exercisable by running a forge.

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
mint` does. Do not reintroduce a "compute the next version" step: two of the
old machinery's failure modes (lexical `sort -n` hazards, a tag pushed out of
order re-dating every scenario under it) existed only because a second version
system was maintained beside git.

**Three `run:` bodies still hold logic, and leaving them was the call.** Issue
filing and the push-range detector in `on-spec-merge.yml`, and "Summarise the
suite" in `spec-ci.yml` (plus the two derivations in `harness-ci.yml`). None is
one of the three commands the spec names, and absorbing any means CLI surface no
spec change has asked for — a forge issue API, a push-range minter, a reporting
flag on `suite extract`, a report option on `harness/run.py`, which is a tree
this repo may not write. Each carries an in-file note saying so. Issue filing is
also dead today: its gate is `hashFiles('workplan.yaml')` and only the stub
planner would write that file.

**The head commit message stays in the workflow, and that is the security
boundary.** It is attacker-supplied text and its only use is the tag annotation,
so it is passed through `env` there and never reaches the CLI at all. The only
message `vellum mint` writes is `ledger: open <name>`, derived from what it
computed itself. Do not "tidy" tagging into the CLI — that hands an injection
surface to the process that writes the ledger.

**A work-item title never reaches a search query.** waviisoft/vellum-intent#24
item 3. The `--jq` program was already safe (`env.FULL`); what was left was
`--search "\"$full\" in:title"`, where a title carrying a double quote makes an
unbalanced phrase, matches nothing, and the run files a duplicate — the exact
defect the lookup exists to prevent. It lists the `work-item` label and compares
exactly in jq instead. The label itself is created first, because `gh issue
create --label` fails outright when the label does not exist and it does not
exist in the intent repo.

**The name is derived from history, and its push is allowed to fail.** `vellum
mint` computes `spec-v$(count of spec versions in the ancestry)` and the
workflow pushes it as a tag. Derived, not read back: it cannot be missing, late
or out of order the way `max(spec-v*) + 1` could. Verified to reproduce every
existing name exactly — `bc84e59` -> `spec-v1`, `be029e6` -> `spec-v5`,
`1ce87cb` -> `spec-v11`. The step is `continue-on-error: true` **on purpose**: a
name is decoration, and a failed tag push must never fail a run that has already
recorded the version. Do not "fix" that by making it fatal.

**The push retries behind a rebase, and "replay it later" was never actually
available.** waviisoft/vellum-intent#24 item 1: a racing merge makes the push
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

**`fetch-depth: 0` on every checkout of the CALLER.** The history *is* the
version sequence and `vellum suite extract` walks it. A shallow clone re-dates
scenarios *forward* onto the graft — right count, nothing pending, nothing
raised, and wrong in the direction that arms scenarios the product already
satisfies. `test_every_checkout_of_the_history_is_unshallow` pins it, scoped to
the caller's checkouts: the CLI checkout is a pip install source and nothing
reads its history. See `.vellum/memory/areas/cli.md`.

**`on-spec-merge` pushes to the caller's `main`.** It needs `contents: write` —
granted in the stub — and branch protection that lets the workflow token push,
or the "Commit the ledger record" step fails, leaving a version with no
committed record. This is less bad than it was: the version exists whether or
not anything is written, because the version is the commit. The missing record
is bookkeeping to replay, not a version that never got minted.

## What could not be verified in this wave

An implementer holds no intent-repo credentials and cannot run the intent repo's
Actions (`spec/features/repo-topology.md`), so nothing below was observed
running:

- that a caller stub resolves `uses: waviisoft/vellum/...@<ref>` at all, which
  needs the org Actions access setting above;
- that `permissions` granted in the stub reach the callee's jobs as expected;
- that top-level `concurrency` in the stub behaves as it did when it was in the
  workflow;
- that `github.event.before`, `hashFiles('workplan.yaml')` and
  `github.event.pull_request.*` read the caller's event inside a called
  workflow (documented behaviour, unobserved here).

What *was* checked, mechanically and repeatably, is in `tests/test_workflows.py`
and `tests/test_install.py`. The first installation run is where the list above
gets settled; if one of them is wrong, the failure will be at `uses:` or in the
first seconds of a job, and it will be loud.

## The history is the version sequence

The intent repo's spec versions are its `main` commits touching `spec/**`; the
`spec-v*` tags are names for some of them and nothing more. A missing, late or
wrong tag changes nothing — which retires the hazard this section used to carry
(during the spec-v2 wave `spec-v2` was pushed before `spec-v1` and every
scenario briefly reported as version 2). What replaced it is truncation: see
`fetch-depth: 0` above.

**The ledger migration is done — this section used to ask for it.**
waviisoft/vellum-intent#22 rewrote `ledger/spec-vN.yaml` into `ledger/<sha>.yaml`.
That matters more than housekeeping now that `vellum backpressure` counts them: a
name-keyed leftover is not a version this CLI recognises, so it is reported as
unreadable rather than counted, and a ledger half-migrated would have measured
the window short. This is the third time in this file a "someone should do this"
note outlived the doing of it, so the lesson repeats once more: **run the check,
do not read the note.**
