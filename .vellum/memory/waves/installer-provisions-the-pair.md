# Wave: `vellum init` provisions the repo pair

`spec/features/installation.md` part 2, and
`spec/decisions/2026-09-03-installer-transport-is-the-forge-cli.md`. Part 1
stamped caller stubs into an intent checkout whose repos already existed; this
wave built the other half — three shapes, a plan, the operator's `gh` as the
transport, a manual rung that does everything a checkout can hold, and the seed
a new installation starts from.

Five scenarios: `@id:init-plan-creates-nothing`, `@id:greenfield-seed-is-green`,
`@id:no-transport-prints-the-checklist`,
`@id:brownfield-stages-docs-for-the-surveyor`,
`@id:init-refuses-an-existing-installation`.

## What was built

| Path | What |
|---|---|
| `src/vellum/provision.py` | The whole of part 2: the conversation, the plan, the seed templates, the local half, the `gh` transport and the manual rung. |
| `src/vellum/seeds/` | `harness_files()`, and `seeds/harness/` — the harness skeleton, shipped as package data. |
| `src/vellum/cli.py` | Thirteen provisioning arguments on `init`, and the two-line fork in `main()`. |
| `tests/test_init_provision.py` | 105 tests driving the real command against local directories and a fake `gh`. |

`pyproject.toml` was **not** touched: see judgment call 3.

`src/vellum/install.py` was **not touched**. Part 1's behavior is byte for byte
what it was, and `tests/test_install.py` is unchanged and green.

## The decisions, and why

**The fork is the command line, never the directory.** `init` provisions when
`provision.requested(args)` is true — any of `PROVISIONING_ARGS` — and stamps
otherwise. The spec says the shape "is chosen by the operator, never inferred
from a directory"; inferring the *mode* from one ("no workspace file here, so
this must be a provisioning") is the same mistake one step earlier, and it would
make a mistyped path create two repositories. The consequence to know: a bare
`vellum init` in an empty directory is still part 1 refusing with "no workspace
file", not a prompt for a shape.

**`--branch` lost its argparse default.** It is shared with part 1, and a
default there would make "the operator said `main`" indistinguishable from "the
operator said nothing", so provisioning could not know whether to prompt.
`main()` substitutes `DEFAULT_BRANCH` on the stamping path, so part 1 is
unchanged.

**The plan and the checklist are one list** (`forge_steps()`), because the
manual rung is the rung people actually follow and a drifted checklist would be
wrong exactly where it is trusted most. The manual rung is not a second code
path either: it is `_perform()` with no transport.

**The whole local half happens before the transport is asked for anything.**
The spec requires the seed to lint clean and doctor green *before* it is pushed;
doctor cannot be green before the stubs exist, and a push cannot be un-pushed.
So: seed → commit (this commit is the pin) → stamp stubs → commit → `lint` +
`doctor` → and only then the forge. The greenfield `gh` rung therefore needs
exactly one push per repository, which is why it is `gh repo create --source …
--push` rather than a create followed by a `git push`: every network act on that
path goes through `gh`, which is what makes the argv trace assertable end to
end.

**One forge step runs before the local half, and only one.** The brownfield
shapes branch their adoption off the existing product repository's real
history, so `gh repo clone` has to happen before `build_product` — that is what
`ForgeStep.before` marks, and it is the only step it marks. It is still one
entry in one list; `before` says where in the run it happens, not that there are
two lists. Without a transport it stays on the checklist and `build_product`
makes a standalone repository with an empty root commit instead, which is the
half a checkout can hold; the checklist step says what to do with it.

**No secret is ever an argv element.** `ForgeStep.stdin` holds a description
and `ForgeStep.secret` the name of the variable holding the value; the value
itself lives in a local dict and reaches `gh secret set` on stdin, with **no
`--body`** — see the landmine below, which is where the first cut of this got it
wrong. `init` mints nothing.

**The staging directory is not made until the plan is confirmed**, because
"`--plan` creates nothing" has to mean nothing — an empty directory is
something — and because a plan whose paths were a fresh `mkdtemp` each run would
not be deterministic.

## Judgment calls

1. **What the harness skeleton contains** (the architect's steer: ship the
   generic runner, leave step files empty but importable). `run.py`,
   `support/{runner,registry,report,world}.py` are the intent repo's, unchanged;
   `support/adapter.py` is rewritten to separate the two jobs Vellum's own
   harness conflates — finding the `vellum` CLI (tooling, for suite extraction)
   from reaching the product under test (`no_deployment()`, providing nothing).
   `steps/__init__.py` is empty. **The consequence, stated in the seeded
   `harness/README.md` and in the root README:** a fresh seed's
   `python3 harness/run.py` reports UNDEFINED and exits 1. That is the honest
   answer to "does this suite execute?", and it is the new installation's first
   job.
2. **How much `.vellum/config.yaml` to seed** (the steer: all keys the CLI
   reads, with defaults, commented). Every key a command reads is there with the
   command named beside it — `budgets.divergence_cap` (`backpressure`),
   `budgets.{per_item_usd,period_usd,period}` (`budget`),
   `dependency_policy.registries` (`verify deps`), `questions.timebox_hours`
   (`tick`), `write_boundaries` (`verify boundaries`) — plus the reserved v1
   shape. `executors` and `roles` are seeded **empty**, not populated: a seeded
   executor would be a claim about infrastructure a new installation does not
   have.
3. **How the seed ships, without touching `pyproject.toml`.** Package data is
   carried by *declaration*, and the obvious declaration —
   `[tool.setuptools.package-data]` — lives in `pyproject.toml`, which is
   outside the implementer's write boundary. Built both ways and compared: with
   no declaration, setuptools 79 shipped every seed `.py` by its own defaults
   and **silently dropped `harness/README.md`**, which is a wheel that
   provisions an installation missing a file with nothing to say so. The
   resolution needs no crossing: `src/vellum/seeds/harness/__init__.py` makes
   the skeleton an ordinary package, so every builder ships its modules because
   it must, and `harness/README.md` became a template in `provision.py` — which
   it wanted to be anyway, since it names the product. `harness/__init__.py` is
   packaging, not seed, and `seeds.NOT_SEEDED` keeps it out of an
   installation's `harness/`.
4. **`--area` is repeatable in every shape.** The spec names "one feature area"
   for greenfield and "every area the operator names" for brownfield; one flag
   with one meaning is simpler than a flag that is singular in one shape.
   Greenfield seeds one area file with a placeholder scenario per `--area`;
   brownfield seeds each `unsurveyed` with no scenarios at all, because a
   placeholder there would be a claim about a product nobody has looked at yet.
5. **A secret with no value moves to the checklist rather than refusing.** The
   spec's "exit 2 naming the flag" is about *flags*, and there is deliberately
   no flag for a token. Nothing is silently skipped: the step is printed under
   "do these yourself", which is the manual rung's posture applied one step in.
6. **`gh api … access_level=organization` is NOT set on the product repo.** The
   wave brief asked for it and the first cut did it; review reversed it and the
   architect confirmed. That setting governs whether a repository's *own*
   workflows may be reused by others, and the workflows a caller stub resolves
   against are the **host's** (`--from`, `waviisoft/vellum`), which no
   installation owns. So the call asked the forge for a permission nothing
   needs — and on a user-owned account, where organization access does not
   exist, it fails and takes an otherwise successful provisioning down with it.
   The host's setting stays named as a step no transport takes, in the plan and
   in `install.CANNOT_KNOW`.

## Landmines

**`gh secret set --body -` sets the secret to the string `-`.** This is the one
to remember. It reads as "read the value from stdin", it was written that way
here, it passed review, and it passed its own test — and it was wrong in every
particular. `gh`'s `getBody()` returns `--body`'s value whenever that value is
non-empty and falls back to stdin only when the flag is **absent**, so both
cross-repo secrets were set to one character and every workflow in a freshly
provisioned installation would have failed authenticating with it. Nothing else
is needed to reach stdin: `subprocess.run(input=…)` is a pipe, not a terminal,
and so is `printf … |`.

The test passed because the **fake** `gh` read stdin unconditionally, which is a
general shape worth naming: a fake that is more permissive than the thing it
stands in for turns the test into a test of the fake. It now reads stdin only
for a `secret set` with no `--body`, which is the real command's actual rule —
and with that one line the old argv fails
`test_the_secret_value_arrived_on_stdin_and_never_in_argv`.

**Adoption runs in somebody else's checkout, and `-A`/`-B` are both wrong there.**
The first cut used `git add -A` and `git checkout -B`, both of which are fine in
a directory this command made and neither of which is fine in one it did not.
`-A` swept the operator's working tree — an untracked `.env` included — into the
adoption commit and then into a pull request; `-B` reset a `vellum/adopt` branch
that was already there; and the commit was parented on whatever HEAD happened to
be, so an operator mid-feature got their feature branch in the pull request. The
rule that replaced them: refuse a dirty tree, refuse an existing
`.vellum/product.yaml`, refuse an existing `vellum/adopt`, cut the branch with
`-b` off the *named base*, and add exactly the two seeded paths. The base is the
clone's `origin/HEAD` where there is one — `--branch` names the INTENT repo's
default branch, and a `master` product repo does not care what the intent repo
called its own.

**A checklist is only as good as the paths in it.** Three lines of the
no-`gh` brownfield rung were not executable as printed: the clone step named the
non-empty stand-in checkout this run had just built, the push step named a
checkout with no `origin`, and `--body-file <adopt PR body>` was never
substituted at all, because `places` knew only the two checkout keys. The
general fix is that every placeholder lives in `Plan.places` and
`test_the_checklist_carries_no_unfilled_placeholder` now scans all three shapes
for anything left in angle brackets (`<job>` excepted; it is prose).

**A transport failure is not a rollback.** `gh repo create` cannot be undone, so
a run that raised and printed nothing left the operator to guess which steps had
happened — and re-running refused at the name the forge now had. `_take()`
appends to a caller-owned list precisely so `run()` can print what was taken.

**`--plan` was contacting the forge.** `detect_gh()` runs `gh auth status`. The
command whose whole promise is "creates nothing" made a network call before
printing a word.

**The seeded index quotes its `--docs` paths, and that is load-bearing.**
`links.find_references()` treats a bare `.md` path in prose as a
cross-reference. `--docs` paths name files in the *product* repo, which the
intent tree cannot resolve, so they are written as inline code — which
`_masked_lines()` blanks. Unquote one and every `brownfield-with-docs` seed
fails its own lint check.

**Two identical provisioning runs pin different shas.** A commit's sha includes
its timestamp, so the prompt-vs-flag equivalence test blanks 40-hex shas before
comparing trees. Without that it passes or fails on whether the two runs
straddled a second boundary — which is how it was first written, and it was
flaky one run in three.

**An installed `seeds/harness/` holds `__pycache__/`.** Installing this package
byte-compiles it, so the walk in `seeds.harness_files()` met a `.pyc`, read it
as UTF-8 and took the whole command down — from a wheel, and never from the
development checkout the tests run against. It now skips the directory *and*
reads only `.py`, and `test_bytecode_beside_the_seed_is_not_seeded_as_a_file`
compiles the package to prove it. The general shape: a defect that only exists
in the installed artifact needs the installed artifact to find it, so this wave
provisioned from a built wheel in a throwaway venv as well as from `-e .`.

**A `PATH` cleanup must capture the value before the assignment.** The same
test file registered its restore from the value it had just written, which put
the stripped `PATH` back and left every later test in the process unable to find
`git`.

## The review round, and the two calls inside it

The blind and security-privacy benches blocked on two: `--body -`, and the
destructiveness of adoption over an existing checkout. Seven more were fixed
beside them (the transport-failure report, the ordering of the outward checks,
the unexecutable brownfield checklist, `--docs` resolution, `--plan` reaching
the forge, the reserved area ids, and removing the product repo's Actions
access call), each with tests; the landmines above carry the reasoning.

Two calls worth writing down, because both are places where the instruction and
the code's own shape had to be reconciled:

1. **The staging directory is removed on a failure only up to the point the seed
   goes green.** The review said "rmtree on any failure after it is made", and
   the problem it named — an empty `vellum-init-*` left behind by a refusal that
   should have come earlier — is fixed by moving the checks above `mkdtemp`. But
   the interrupted report added in the same round hands the operator the local
   checkouts and prints commands that name them, so deleting them on a transport
   failure would destroy the only thing that run has left to offer. `staged` is
   therefore set to None the moment the checkouts stop being this run's private
   scratch: a red seed (the message tells the operator to fix them there) or a
   green one whose forge steps have started.
2. **Declining the plan exits 2, not 0.** The review left the number open and
   asked for it to be documented. 2, for the reason the no-terminal case above
   it is 2: "this command did not do what it was asked to do", and a script
   reading 0 as "the installation is there" would be wrong.

## Not done, and why

- **No real `gh` run.** This environment has no `gh` and no forge credential, so
  the transport is verified against a fake `gh` on `PATH` that records its argv
  — the exact greenfield sequence, and the secret arriving on stdin rather than
  in argv. What is unverified is that the real `gh` accepts these flags; the
  first real provisioning run is the check. `--body -` is the reason to hold
  that caveat seriously rather than as boilerplate: a fake agreed with a wrong
  argv for a whole review cycle, and only reading `gh`'s own `getBody()` settled
  it. When a fake is the only witness, go and read what it is standing in for.
- **The brownfield `gh` rung is driven, but not by argv alone.** It needs `gh
  repo clone`, a `git push` of `vellum/adopt` and `gh pr create`, so unlike
  greenfield it is not all-`gh`. The fake `gh` therefore *serves* a real local
  bare repository on `repo clone`, and the assertions are about the forge's own
  copy afterwards: `vellum/adopt` exists on it, and `main` is exactly the commit
  that was there before. That is the property the spec states, asked of the
  thing that would hold it.
- **GitLab.** The core is forge-neutral and the first adapter is GitHub
  (`spec/features/installation.md`, out of scope). `forge_steps()` emits `gh`
  commands; a second forge is a second emitter beside it.
