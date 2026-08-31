# GitHub adapter

Three workflows **for the intent repo** (`waviisoft/vellum-intent`), kept here
so they are reviewed alongside the CLI they call. They are not run by this repo.

Install them by copying:

```sh
cp adapters/github/spec-ci.yml       ../vellum-intent/.github/workflows/
cp adapters/github/on-spec-merge.yml ../vellum-intent/.github/workflows/
cp adapters/github/harness-ci.yml    ../vellum-intent/.github/workflows/
```

Keep the two sides byte-identical. These files are the upstream copy and the
intent repo's `.github/workflows/` holds what actually runs, so a plain `diff`
between them is the whole drift check — if it reports anything, one side has
been edited in place and the other has not.

| File | Trigger | Does |
|---|---|---|
| `spec-ci.yml` | `pull_request` touching `spec/**`, `ledger/**`, `.vellum/config.yaml` or the workflow itself | `vellum lint` + `vellum suite extract`, uploads `suite.json`, summarises the scenarios the PR introduces or changes, and runs `vellum backpressure` (reporting, not blocking — see below). The three agent reviews are stubs. |
| `on-spec-merge.yml` | `push` to `main` touching `spec/**` | `vellum mint` opens the ledger record for the merge commit; the workflow tags the decorative name, extracts the suite, files work-item issues from `workplan.yaml`, commits and pushes. The planner is a stub. |
| `harness-ci.yml` | `pull_request`, **every** one | `vellum verify boundaries` against the harness engineer's trees on any PR that writes `harness/`, and `python3 harness/run.py` — which fails on an UNDEFINED scenario — plus a check that the committed `harness/conformance.md` matches a fresh run. |

## harness-ci.yml: the pipeline guards its own boundaries

`spec/behaviors/write-boundaries.md` says "CI enforces the same boundaries as a
backstop in colocated development contexts". Until this file that sentence was
enforced in *product* repos only, and the intent repo is where the breach
recurs: a harness session that also tidies `.vellum/memory/`, which is the
librarian's tree.

**It needs a `write_boundaries` block in `.vellum/config.yaml`.** The intent
repo has no `.vellum/product.yaml`, so its boundaries live in the installation
config, in exactly the shape a product file uses:

```yaml
write_boundaries:
  harness-engineer: [harness]
  librarian: [.vellum/memory]
```

A top-level mapping of role name to a list of repo-relative path prefixes. An
entry may name a directory or a single file; `""`, `.`, `/`, an absolute path
and anything containing `..` are all refused, because each admits every path in
a diff. **The data is the architect's to author** — this repo ships the command
that reads it and no boundary data for a repo it does not own. Until the block
exists, the `boundaries` job exits 2 ("I could not answer") on any PR that
writes `harness/`, which is the correct colour of red: a guard with nothing to
check against has not passed.

The job passes `--boundaries-from config` rather than letting the command
choose. Naming the source makes its absence an error, where the default would
silently read a product file if one ever appeared in that repo.

**The role is derived from the diff, not from a label or a branch name.** A PR
that writes `harness/` is a harness PR and must write nothing else. A PR that
writes no harness path is not checked against any role by this job — closing
that half means asking "does this diff fit inside *some* declared role's
trees?", which is CLI surface no spec change has asked for.

**Both jobs run on every PR, with no `paths:` filter.** That is about branch
protection rather than cost: a path-filtered required check never reports on
the PRs it filters out, and GitHub leaves those waiting forever. Both of these
can safely be made required.

**The conformance-map check excludes one line, deliberately.** The map's header
records the commit the suite was extracted at, which is the checkout's HEAD — so
the committed file names a sha that did not exist when it was written, and that
line differs on every run by construction. Comparing it would make the check
impossible rather than hard. Everything the map asserts — the scenario list,
each outcome, each blocked-on capability, the totals — is compared exactly. If
`harness/run.py` ever grows an option to omit the header, this step becomes a
plain `diff`.

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

**Four `run:` bodies still hold logic, deliberately.** Issue filing and the
push-range detector in `on-spec-merge.yml`, "Summarise the suite" in
`spec-ci.yml`, and the two derivations in `harness-ci.yml` (is this a harness
PR; is the committed map current) are none of them among the three commands the
spec names, and absorbing any would mean CLI surface nothing has asked for — a
forge issue API, a push-range minter, a reporting flag on `suite extract`, a
report option on `harness/run.py`, which is a tree this repo may not write.
Each carries an in-file note saying so.

## on-spec-merge robustness (waviisoft/vellum-intent#24)

Three of the four findings that issue records are closed here; the fourth
(`docs/design.md` still describing integer version minting) is intent-repo
documentation and not this repo's to fix.

- **The ledger push retries behind a rebase** (item 1). `concurrency` serialises
  runs against each other but not against anything else pushing to `main`, and a
  rejected push used to strand the record with no replay path — the guard accepts
  only the branch tip, so nothing would re-record it. The rebase moves this run's
  one ledger commit onto whatever landed; a conflict aborts and reddens rather
  than losing it, and two concurrent runs write different filenames (a record is
  keyed by its version's sha) so they do not conflict at all.
- **A spec-touching commit below the push tip is now named, not skipped**
  (item 2). Only the tip is minted, so a multi-commit direct push to `main` could
  leave a version on `main` with no ledger record, in a green run. The last step
  scans `before..after` and fails the run naming each one. It is a **detector,
  not a recorder**: minting a range means a job shaped differently from this one,
  and whether the answer is that job or branch protection against direct pushes
  is the architect's call.
- **The `work-item` label is created before an issue asks for it, and no work-item
  title reaches a search query** (item 3). `gh issue create --label` fails
  outright when the label does not exist, and it does not exist in the intent
  repo. The duplicate-issue lookup used to paste the title into GitHub's search
  grammar, where a title carrying a double quote makes an unbalanced phrase — a
  malformed query matches nothing, so the existing issue is not found and the run
  files a duplicate. It lists the `work-item` label instead and compares titles
  exactly in jq, which is where the comparison already was.

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

It runs with `--strict`, which refuses to measure at all when a ledger file
cannot be read rather than reporting it and counting a narrower window. On a
gate that is the right direction. And `1` from this command means *blocked* and
nothing else — every other non-zero exit is `2` — so an armed gate's red always
has one meaning.

The job's `pull_request` trigger includes `.vellum/config.yaml` and `ledger/**`
alongside `spec/**`, because a PR that raises `divergence_cap` or adds unshipped
versions must re-run the check that reads them.

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

- **Only one checkout keeps its credential.** `persist-credentials: false` is
  set on every `actions/checkout` in both files except `on-spec-merge.yml`'s
  `Check out main`, which is the one that pushes the tag and the ledger commit.
  It matters most in `spec-ci.yml`, where the jobs run on `pull_request` in a
  workspace whose root is the PR's merged tree, and where `VELLUM_TOKEN` reads
  a private repository.
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
