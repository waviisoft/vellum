# Wave: the CLI absorbs the workflow bodies

Wave B of the completion push. Commanded by `waviisoft/vellum-intent#38`, which
added to `spec/features/spec-pipeline.md`:

> Pipeline logic lives in the product CLI, and forge workflow bodies are
> single-command shims over it — minting is `vellum mint`, the divergence gate
> is `vellum backpressure`, the pin close is `vellum pin advance` — so the
> pipeline is portable across action-style runners and its behavior is testable
> by driving the commands.

and to `spec/features/scenarios-and-harness.md` the half that says why: the
**command contract** is executable in a sandbox and passing that way is a pass;
that a deployment's trigger *causes* the invocation is a deployment property,
and a harness must never re-implement a workflow's logic to grade it.

Landed paired with that spec PR: the spec merges first, the pin advances, this
lands.

## What moved

`adapters/github/on-spec-merge.yml` had four steps holding logic — the version
guard, the baseline walk, the name derivation, the ledger write. They are one
`vellum mint` call. `spec-ci.yml`'s `backpressure` job echoed a stub; it is one
`vellum backpressure` call. `vellum pin advance` is new surface with no
workflow shim yet — the architect runs it at paired landing, and Wave G's
`workflow_call` shims will call it.

Nothing about the guards changed. Each one is a branch in the command now
rather than a step in the file, and `tests/test_mint.py` names each test for
what the guard protects rather than for the step it came from, so a later
rewrite cannot drop one and still look covered.

## Judgment calls, and what they cost

**Both of mint's no-ops exit 0.** A commit that does not touch `spec/` and a
replay both write nothing and exit 0, which is exactly what the guard step's
`proceed=no` did — it set an output and left the job green. Making either
non-zero would redden every re-run of a job that is idempotent by design
(decision D11) and every racing merge, which is benign. The consequence is that
**the exit code is not the signal** and a caller must read `minted`/`reason`
from `--emit`. That is written into the adapter, the README and `cli.md`,
because it is the one thing about this command that will surprise someone.

**`--commit` exists; the adapter does not use it.** The task that commanded
this wave offered a choice — print what to commit, or take a `--commit` flag —
and the answer turned out to be both: the default prints, the flag stages and
commits under a fixed message and never pushes. `on-spec-merge.yml` uses the
default, because the `suite-<sha>.json` extracted after minting belongs in the
same commit as the record it describes and one `git add ledger/` is smaller
than two commits. The flag is the right contract for a caller that mints and
does nothing else, which is what the `workflow_call` shims will be. A flag with
no caller is a smell; this one has a documented reason and a test.

**Tagging stayed in the workflow, on purpose.** The decorative tag is annotated
with the head commit message, which is attacker-supplied text — anyone who can
land a commit on main writes it. It is already passed through `env` rather than
`${{ }}` there. Pulling tagging into the CLI would hand that string to the
process that writes the ledger, for no gain: mint emits the computed *name* and
the workflow does the tagging. `vellum mint` never reads a commit message, and
the only message it writes is one it derived itself.

**Backpressure reports and does not block.** Wired for real, then held with
`continue-on-error: true`. Nothing has ever set a ledger record to `shipped` —
releases do not exist yet — so all eleven records on intent `main` count as
unshipped against a cap of 3, and arming the gate would block every spec merge
in the repository including the one that lands the release machinery. That is a
deadlock, not backpressure. One line removes the hold. This is the architect's
call to make and it is flagged where they will see it.

**The pin file is edited a line at a time.** The commanding task said comment
preservation was not required. It is done anyway, because a
`safe_load`/`safe_dump` round-trip would delete every comment in
`.vellum/product.yaml` — which is mostly load-bearing documentation — and turn
a one-value change into a whole-file diff. It also satisfies "preserve the
file's other fields exactly" by never rewriting them. The edit is re-parsed and
compared field by field before it is kept, because a line-level rewrite is the
right tool here and the wrong one to take on faith.

**`pin.name` follows the commit.** Beyond the letter of the task, which named
only `pin.commit`. A `name` reading `spec-v16` beside a commit that is a
different version is decoration that has become a lie, and the reader it
misleads is exactly the reader it was for.

## What was left, and why

Two `run:` bodies still hold logic: issue filing in `on-spec-merge.yml` and
"Summarise the suite" in `spec-ci.yml`. Neither is one of the three commands
the spec names. Absorbing them would mean CLI surface no spec change has asked
for — a forge issue API in the first case, a reporting flag on `suite extract`
in the second — and the issue-filing one is dead today anyway, gated on a
`workplan.yaml` only the stub planner would write. Both carry an in-file note,
so the next reader does not take them for oversights.

The spec's own wider rule for the cap — it "counts approved-but-unlanded spec
PRs together with landed-but-unshipped versions" — is half-implemented on
purpose. An open PR is forge state, not repository state. `--pending <n>` takes
that count from a caller that can see the forge, and the report says plainly
when only the ledger half was measured, rather than implying the whole window.

## Measured, not assumed

- `vellum mint` re-minting the record the live workflow wrote for `0e9f3f5`
  produced a file **byte-identical** to `ledger/0e9f3f57….yaml` on intent
  `main` apart from the `approved` timestamp, which is necessarily now. Same
  keys, same order, same quoting. `name: spec-v16` and
  `baseline: 6786cc88…` were both derived, and both match.
- Against intent `main` (a memory commit) mint no-ops as `not-a-spec-version`;
  against `0e9f3f5` it no-ops as `replay`. Neither writes anything.
- A `--depth 3` clone of the real intent repo counts **1** spec commit where
  the full history counts 16 — so without the shallow refusal the sixteenth
  version would have been minted as `spec-v1`, over a name the seed commit
  already holds. That is why the refusal is exit 1 and not a warning.
- Against the wave-b spec tree (`1fea506`, PR #38's head) mint computes
  `spec-v17` with baseline `0e9f3f5` — which is what will actually happen when
  that PR merges.
- `vellum backpressure` against intent `main`: 11 of 3, BLOCKED, exit 1. This
  is the finding behind the `continue-on-error` hold.
- The **installed workflow copies were byte-identical** to `adapters/github/`
  at vellum `main`, and neither carries the `INSTALLED COPY` header that
  `adapters-github.md` warned was still there — `waviisoft/vellum-intent#21`
  removed them. That note about a stale note had itself gone stale.
- The **ledger migration this file's sibling asked for is already done**
  (`waviisoft/vellum-intent#22`): eleven records, every filename a sha, no
  `spec_version: spec-v*` anywhere. Both `adapters/github/README.md` and
  `.vellum/memory/areas/adapters-github.md` were asking for it. Second time a
  "someone should do this" note in that file outlived the doing — run the
  check, do not read the note.

## What the bench found, and what it cost

An independent review of the diff before landing. Four real defects, all in
`vellum pin advance` or its tests, none in the guards that moved:

1. **A test unset `VELLUM_INTENT_REPO` in-process and never put it back**,
   which silently disarmed eight of `test_suite`'s pinned-tree assertions — in
   the CI job whose entire purpose is to stop those skips from being a hole.
   Measured: `test_suite` alone with the variable set is 77 tests in 14s with
   zero skips; behind the leaking test, 8 skip in 3s. **The wave's own first
   report of "conformance green" was true and hollow**, and the fix moved shape
   two from `skipped=8` to `skipped=0`.
2. **`_rewrite` could edit a line nested inside a `pin:` block scalar**, and
   every check in `advance()` passed — `drifted` skips `pin`, and comparing key
   sets cannot see a changed value. Latent today only because the real
   `product.yaml` is flat.
3. **A ledger record's `name` was interpolated raw into YAML**, so a name
   containing a newline wrote a second key into the pin block undetected.
   `ledger/` is written by anyone who can land a merge on the intent repo, and
   the pin is what product CI fetches the spec at.
4. **`yaml.safe_load` on a ledger record was unguarded in `pin.py`** — a
   traceback rather than an exit code. `mint.py` already handled it.

The guard added for (1) — `PinCase` asserting `os.environ` is unchanged after
every test — immediately caught a *second* instance the review had not spotted:
`addCleanup(os.environ.pop, ...)` deletes rather than restores, so it only
bites in the shape where the variable was already set. Guards that pay for
themselves within the same commit are worth writing.

Also taken from the review: `backpressure` errors now exit **2**, leaving `1`
to mean "blocked" and nothing else once the gate is armed; `--strict` so the
gate refuses to answer rather than measuring a short window over an unreadable
record; `--emit ''` is an error rather than a silent skip; `--commit` scopes
the committer identity with `git -c` instead of writing it into the developer's
`.git/config`; `persist-credentials: false` on every checkout that does not
push; `spec-ci.yml` triggers on `.vellum/config.yaml` and `ledger/**`; and the
`::notice` annotations the old guard step emitted are raised again from
`steps.mint.outputs.reason`.

## Landmines this wave planted

**`set -o pipefail` in the backpressure step.** Without it the step's status is
`tee`'s, so deleting `continue-on-error` would arm a gate that can never close.

**One definition of `VELLUM_INTENT_REPO`.** It moved into `src/vellum/config.py`
and `tests/support.py` re-exports it, because `pin advance` now reads the same
variable the pinned-tree tests do. Two spellings is how the tests and the
command come to disagree about where the intent repo is.

**A hand-written all-digit sha in a fixture is a number to YAML.**
`spec_version: 0000…0001` unquoted loads as `1`, fails the sha check, and the
record reads as unreadable. `ledger.dump()` quotes it correctly, so no real
record hits this — but a fixture written by hand does, and it looked like a
counting bug for a minute. The test shas carry a leading `a` and say why.
