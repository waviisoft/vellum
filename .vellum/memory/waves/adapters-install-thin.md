# Wave: the adapters install thin

`spec/features/installation.md` (new, spec-v23), issue
waviisoft/vellum-intent#23 **part 1 only** — part 2, provisioning the repo pair
itself, is its own wave.

## What changed

The three adapter workflows stopped being files you copy and became files you
call.

- `adapters/github/{spec-ci,on-spec-merge,harness-ci}.yml` moved to
  `.github/workflows/` of this repo and became `workflow_call` workflows. They
  keep every guard, comment and detector they had; what changed is the `on:`
  block, the `env: VELLUM_REF` (now the `vellum-ref` input), and the removal of
  `concurrency` (now the caller's).
- `adapters/github/` now holds the three **caller stubs** and a rewritten
  README.
- `vellum init` stamps those stubs into an intent checkout; `vellum doctor`
  checks that what is installed is what ships.
- `src/vellum/workspace.py` is the one reader of `.vellum/workspace.yaml`;
  `release.products()` delegates to it rather than reading the file a second
  time.
- `tests/test_install.py` and `tests/test_workflows.py` are new. The second is
  the wave's real safety net: every check a checkout can make about workflow
  text, made permanent.

## Why

Full copies were bootstrap expedience and had already produced two measured
incidents with a single repo pair — both recorded in
`areas/adapters-github.md`, both a note about drift outliving the drift. A
second product pair would have doubled the surface. A stub holds no logic, so
it has nothing to drift; upgrading an installation is bumping a ref.

## Judgment calls made here

Each is recorded at length in `areas/cli.md` and `areas/adapters-github.md`;
this is the index.

1. **The stubs are rendered from a table, not read from `adapters/`.** An
   installed CLI is a wheel and cannot read repository paths. `adapters/github/`
   is the committed rendering and a test asserts byte-identity — the second
   artifact gets an equality check, which is the lesson of the copies applied to
   the thing that replaced them.
2. **`concurrency` and `permissions` live in the stub.** A concurrency group is
   scoped to the repository it serialises; a called workflow's token can only be
   narrowed by the callee, so the grant must be made where the run starts.
   Neither can drift into a *wrong answer* — a wrong trigger does not run, a
   wrong permission is refused.
3. **The ref appears twice per stub** (`uses: ...@<ref>` and `vellum-ref:`), is
   stamped equal, and `doctor` reports a mismatch.
   `${{ github.job_workflow_sha }}` would collapse them and was rejected: an
   installation's CLI version has to be readable in the repo that runs it.
4. **`init` never overwrites a differing stub without `--force`.** Writing is
   `init`'s job; judging is `doctor`'s.
5. **`doctor` reads release tags only when handed a checkout.** No auto-discovery
   of a sibling vellum clone — magic that sometimes finds a checkout would make
   the currency report depend on where somebody cloned things.
6. **`workspace.forge()` defaults to `github`.** Every workspace file written
   before the installer has no `forge` key. Safe only because a forge that IS
   named and unadapted is refused.

## What review caught, and what it says about the shape

An independent review of this diff found five things worth recording, because
four of them are the same mistake:

- **`doctor` checked less than the sentence "installed matches shipped" claims.**
  It read the `uses:` job and nothing else, so a stub whose trigger, permission
  grant or concurrency group had been edited exited 0 — including the exact
  `paths:`-filter landmine `harness-ci`'s own header documents. Fixed:
  `CALLER_HALF` in `install.py`, compared parsed. **The lesson: a command that
  claims a broad property has to be tested against the broad property.** Every
  test written before review edited the *delegation*, which is the half the code
  already checked.
- **Four separate paths returned the wrong exit code**: `--from` unvalidated
  (a newline in it opened a second job with a `run:` body, in a file stamped
  with `pull-requests: write`); a non-UTF-8 stub raised `UnicodeDecodeError`,
  which is a `ValueError` and not an `OSError`, so both commands exited 1 with a
  traceback; `doctor --forge <x>` short-circuited the workspace read and
  reported a bare directory as three findings; an unwritable tree tracebacked.
  **The lesson: the 1/2 split is only load-bearing if every escape from the
  command goes through it, and "every escape" includes the exceptions you did
  not think of.** `init` in particular is documented as never returning 1, which
  makes any uncaught exception a contract violation by construction.

Both are now covered: `DoctorChecksTheCallerHalf` and the four exit-code tests
in `InitCannotAnswer` / `DoctorOverAFreshCheckout`.

## What the review bench caught on the open PR, and what it says

A blind reviewer and a security-privacy reviewer over PR #20 found no blocking
defect and six should-fixes. Five of them are the *first* lesson above happening
again one level in: **`doctor` still checked less than "installed matches
shipped" claims**, and every test written for it had edited the half the code
already read.

- **The delegating job's own keys were never read.** `if: false` — a *skipped*
  job reports *success* to branch protection, so the write-boundary gate went
  green having run nothing — plus `strategy:` (N minters racing one ledger
  push), `needs:`, a job-level `permissions:`, `timeout-minutes`,
  `continue-on-error`, `env:`, `container:`, and a renamed job id (check names
  derive from it). Fixed as an allowlist, `JOB_KEYS`, plus `renamed-job`.
- **A remapped secret passed.** `VELLUM_TOKEN: ${{ secrets.ORG_ADMIN_PAT }}`
  satisfies a check made by key alone. `secret-remapped` reads the referenced
  name back out of the expression.
- **The check fired on installations for being themselves, twice.** The drift
  compare hard-coded `branches: [main]`, so a repository whose default branch is
  `trunk` could never be doctor-green; and `_walk(data)` scanned the whole
  document for `run:`, so a legal top-level `defaults: {run: {shell: bash}}` was
  a `carries-logic` finding. Fixed by `init --branch` plus an exemption narrowed
  to `on.push.branches`, and by walking `jobs` only. **This is the counterweight
  to the first lesson and has to be held with it: a check that claims a broad
  property must be tested against the broad property, AND it must not fail a
  correct installation for being itself. Widening on the first without the
  second is how a check gets ignored instead of fixed.**
- **A stamped installation could fail its own doctor.** `vellum-ref:` was an
  unquoted scalar, so `--ref 1.10` came back as the float `1.1` and `010` as the
  int `10` while the `@<ref>` half stayed a string. Quoted, and `REF_RE` is now
  `git check-ref-format`'s rules.
- **`doctor` opened only the files it stamps**, so a retired full copy renamed
  aside (`spec-ci-legacy.yml`) went on running on every PR unseen — the copies
  coming back by not being stubs. `stray-workflow`.
- Nits: `doctor` reads `intent:` through the accessor `init` uses, so a
  workspace lacking it is exit 2 from both rather than three findings from one;
  `tests/test_workflows.py` reads `install.HOST_REPO` rather than spelling the
  slug a second time.

## Open, and named rather than fixed

- **`waviisoft/vellum` has cut no `v*` tag**, so `vellum init`'s default ref
  (`v<__version__>`) currently names a tag that does not exist. Both commands
  say they cannot confirm it; install with `--ref main` until a release is cut.
- **Reuse of a private repo's workflows needs an Actions setting** on
  `waviisoft/vellum` ("Accessible from repositories in the organization"). No
  checkout can see it and neither could this wave; `doctor` says so.
- **The Blacksmith runner labels are now in the shipped workflows**, so an
  installation outside this organisation cannot override them from its stub. A
  `runs-on` input is the fix when a second organisation needs one.
- **`.github/workflows/` is outside the implementer's declared trees** in
  `.vellum/product.yaml`. The reusable workflows had to land there; the
  architect widens `write_boundaries.implementer` at landing.
- **The pins are mutable tags.** `uses: ...@v0.1.0` and every
  `actions/checkout@v4` inside the reusable workflows name tags, not shas.
  Centralising the bodies did not create that, but it widened the blast radius
  from one hand-copied file to every installation at once — and
  `on-spec-merge` runs with `contents: write` and `issues: write`. Tag
  protection or sha pins are the two narrowings; neither is enforced here, and
  `adapters/github/README.md` names it as a prerequisite.
- **`ci.yml`'s three checkouts still persist their credentials**, one of them
  holding `SPEC_TOKEN`. Pre-existing, out of scope, and now an explicitly
  narrowed test rather than an unnoticed hole — see `areas/adapters-github.md`.
