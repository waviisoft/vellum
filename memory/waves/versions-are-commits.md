# Wave: versions are commits, the pin is a file, minting shrinks, `Rule:` banned

Worklog for the fifth wave, and the second coalesced one: the pin sat at
spec-v6 while the intent repo reached spec-v11, so this wave carries five
landed deltas at once — plus one approved-but-unlanded spec PR, which makes it
the **first paired landing**.

Named for what it did rather than for a version. Worklogs up to `spec-v6.md`
are named for the version they landed at; a version's name is decoration now
(`spec/decisions/2026-08-28-versions-are-commits.md`), and a filename
presuming a `spec-vN` the architect has not yet attached would be a name doing
work. Later waves should follow this one.

| Delta | Spec change | What the product owed it |
|---|---|---|
| spec-v7 | Versions are main commits; names are decoration (`spec/decisions/2026-08-28-versions-are-commits.md`) | Ancestry dating in `gitver.py`/`suite.py`; `suite.json` schema 2 |
| spec-v8 | The pin of record is a file, not a submodule (`spec/decisions/2026-08-28-pin-file.md`) | Submodule removed; `.vellum/product.yaml` sole pin; CI fetches at the pin and reports divergence rather than failing; tests get the tree from `VELLUM_INTENT_REPO` |
| spec-v9 | The reviewer bench (`spec/decisions/2026-08-28-reviewer-bench.md`) | Nothing. Verified below |
| spec-v10 | Executors are fire-and-collect (`spec/decisions/2026-08-28-fire-and-collect-executors.md`) | Nothing. Verified below |
| spec-v11 | Paired landing (`spec/decisions/2026-08-28-paired-landing.md`) | Nothing in code; the shape of this PR is the delta |
| **unlanded** | `Rule:` blocks are banned (vellum-intent#20) | `GH010`, and four fixtures |

The minting simplification comes out of spec-v7 rather than being its own
delta: `adapters/github/on-spec-merge.yml` loses the next-integer arithmetic
and the already-tagged guard, and the ledger keys records by commit sha.

## The whole wave's scenario delta

Taken from the extractions rather than from reading the diff — added, removed
and changed by fingerprint, spec-v6 to the target:

- **added**: `fire-and-collect` (spec-v10), `paired-landing` (spec-v11)
- **changed**: `scenario-version-tagging`, `armed-not-enforced` (both spec-v7,
  `features/scenarios-and-harness.md`), `version-mint` (spec-v7,
  `features/spec-pipeline.md`)
- **removed**: none

Nothing lost its identity across five versions, which is the first thing to
check when the dating mechanism itself is being replaced.

## spec-v7 — ancestry replaces the tag registry

`version_history()` walks `git rev-list --first-parent --reverse <ref> -- spec`
instead of `spec-v*` tags. **The comparison inside the walk did not change at
all** — id first, fingerprint as fallback, `consumed` to stop cross-assignment,
earliest-wins among identical content. What changed is where the sequence comes
from, and so what can go missing from it.

**The proof, run the way PR #4's review ran its parser proof.** For each of the
eleven versions: the old tag walker in a clone with every tag *newer than that
version deleted* (the environment the tag walker needs to be correct at all —
it reads "newest tag present", not the pin), against the new ancestry walker on
a full clone checked out at the same commit. Compared per scenario on
`(id, ref, file, line, feature, name, keyword, tags, fingerprint, pending)` and
on the version, translated `spec-vN` -> N. All eleven agree exactly:

| version | scenarios | versions present |
|---|---|---|
| spec-v1 … spec-v4 | 19 | {1} |
| spec-v5, spec-v6 | 20 | {1, 5} |
| spec-v7 … spec-v9 | 20 | {1, 5, 7} |
| spec-v10 | 21 | {1, 5, 7, 10} |
| spec-v11 | 22 | {1, 5, 7, 10, 11} |

### What this fixes

`spec_version` is now the commit being extracted at — a checkout's pin, or a
PR head — not "the newest version visible". The tag walker read every
`spec-v*` tag *present in the repo*, so a checkout at an older pin was dated by
tags it did not contain, and the pin assertion failed the moment the intent
repo moved ahead: on every branch, including the base. That is the red check
waviisoft/vellum#4 merged over. Ancestry cannot do it, and
`test_dating_reads_the_checkout_ancestry_not_every_ref_in_the_repo` is the
regression test.

### Judgment calls, recorded

- **`spec_version` is HEAD, and `spec_head` is added beside it.** The spec says
  `spec_version` is "the version being extracted at (a checkout's pin, or a PR
  head)", which is the commit of the checkout and need not itself touch the
  spec tree — the pin this wave records is `9c8b70a`, a ledger commit. So the
  conformance check is `suite.spec_version == pin.commit`, a checkout fact.
  "What is the newest actual version this tree carries" is a different
  question and gets its own field, which is also the `spec_head` pointer name
  `features/ledger.md` uses.
- **`pending` means "uncommitted", and it shrank a lot.** Any committed spec
  change is itself a version, so it needs no tag to become datable; on a CI
  checkout nothing is pending. "Introduced or changed by this PR" is now
  `version == spec_version`, which is what `spec-ci.yml` summarises — strictly
  more accurate than the old `pending` list, which also swept in every
  merged-but-untagged commit.
- **A pending scenario's version is `null`, not a prediction.** Under integers,
  "the version this would mint" was computable. A sha is not.
- **`tagged` became `shallow`, asked directly.** See the landmine below.
- **`source_commit` is gone** — it was always the commit extracted at, which is
  `spec_version`. Two fields for one fact invite drift. `SUITE_SCHEMA` is 2.
- **Names are emitted, never read.** `names()` in `gitver.py` maps sha ->
  `spec-vN` for the shas the suite reports, and `to_dict()` puts them beside
  every sha. `TestDecorativeNames` asserts that deleting every tag, or naming
  versions out of order, changes no version.
- **`since: spec-v1` frontmatter stays a name.** Every file in the tree writes
  it and `spec/index.md` states the convention; nothing resolves the field, so
  a name is the right thing to find there. The lint message's stale "(decision
  D6)" citation is dropped, since the versions-are-commits decision superseded
  D6's sequence half.

### Landmines, re-measured rather than assumed

**The splitter is still required, for the same reason under a new mechanism.**
`features/certification-and-releases.md` held two Features in one fence from
the seed commit through `be029e6`, and those are *commits* in the ancestry now
rather than tags. Stubbing `split_documents()` to never split and extracting
`main`: count stays 22, nothing raises, nothing is pending, and the file's
three scenarios move from `bc84e591` (spec-v1) to `c4307abe` (spec-v4) — three
versions younger, arming scenarios the product already satisfies. Identical in
shape to the tag-era measurement.

**`fetch-depth: 0` is still load-bearing, and its failure got quieter.** Under
tags, a shallow clone had none, so everything came back pending — wrong, but
loudly. Under ancestry a shallow clone has *some* history, so the graft
boundary becomes a plausible-looking version and everything below re-dates
**forward** onto it. Measured: a `--depth 3` clone dated 21 of 22 scenarios to
`3e28b3b`, which is not a spec version at all — it is the truncation point.
Right count, nothing pending, nothing raised. It cannot be inferred from the
walk, so `is_shallow()` asks git (`rev-parse --is-shallow-repository`),
`suite.json` carries `shallow`, and the conformance job fails on it.

## spec-v8 — the pin file

The submodule and `.gitmodules` are removed; `.vellum/product.yaml`'s
`pin.commit` is the pin, with `pin.name` as optional decoration. The
conformance job already checked the intent repo out separately and read the
file — the gitlink was never the pin in practice, which is most of what the
decision cites — so the job needed aligning, not rebuilding.

**Divergence is reported, never failed on**, per the same delta. The
`Report divergence from spec-head` step summarises the versions landed since
the pin and exits 0, and says so plainly when the pin is not an ancestor of
`main` at all, which is the expected shape while an approved spec PR is held
for paired landing.

**How tests get the intent tree — the design call.** `VELLUM_INTENT_REPO`
names a checkout; a `./spec` mount is honoured too, since the decision keeps
one as a developer convenience. Three properties, each deliberate:

1. **Absent skips, wrong raises.** No checkout is a fact about the environment
   (the `test` job, a fresh clone) and the pinned tests skip. A checkout at
   some *other* commit is a mistake, and `WrongPin` says so — skipping past it
   would report conformance against a tree that is not the pinned one.
2. **The conformance job runs the whole suite with the variable set**, so the
   eleven skips in the `test` job are covered rather than being a hole.
3. **A path is only a checkout if it is its own work tree.** The first thing
   this hit: the leftover empty `spec/` directory made `git -C spec rev-parse
   HEAD` answer for *this* repo, so the checkout looked present and sat at the
   wrong commit. That is one of the failures the pin-file decision cites,
   reproduced within minutes of removing the gitlink. `intent_checkout()`
   compares `rev-parse --show-toplevel` against the path before believing
   anything.

**A test that was gated on the environment went quiet when the environment
changed.** `test_intent_repo_root_resolves_to_its_spec_subdirectory` — the
`<spec-dir>`-is-two-things guard — was written against `REPO_ROOT / "spec"`
and skipped itself when absent, so removing the submodule silently removed the
coverage of an ambiguity that did *not* go away. It now builds a two-level tree
in a tempdir and asserts both shapes structurally, with the live checkout as a
second, skippable case.

## spec-v9 and spec-v10 — verified, not assumed

Both decisions were read for CLI-facing statements and neither has one. The
reviewer bench is the architect's hiring policy plus `.vellum/config.yaml` in
the *intent* repo; fire-and-collect constrains executor dispatch, which is v0.2
and has no surface in this CLI. Mechanically: the extraction at spec-v9 is
byte-identical to spec-v8 (20 scenarios, versions {1, 5, 7}), and spec-v10 adds
exactly one scenario, `fire-and-collect`, correctly dated — data, not code.

## spec-v11 — paired landing, and the shape of this PR

Process, with one product-adjacent claim the wave can check: "the re-pin is a
one-line diff, and the dangling-gitlink hazard the submodule would have posed
at squash time is gone". Both hold — the pin advance is one line of
`.vellum/product.yaml`, and there is no gitlink. `paired-landing` extracts as a
scenario at spec-v11; enforcing it is the harness's job in v0.2.

**This PR is the first paired landing.** It implements intent `main` plus the
approved-unlanded vellum-intent#20, so `.vellum/product.yaml` pins main's head
(`9c8b70a`) as an interim: #20 has no merge commit yet, so there is no sha to
pin to it. The architect merges #20, pushes the one-line pin update to this
branch, CI re-runs, and this PR merges.

## The `Rule:` ban — GH010

vellum-intent#20 bans `Rule:` blocks, resolving the finding spun out of PR #4's
review (vellum-intent#16). Detected from the parsed node (`child["rule"]`), as
`GH008` and `GH009` are, so a `Rule:` in a docstring or a step's text is left
alone — the lesson `GH007` paid for.

**The finding counts the scenarios the Rule holds, because that count is the
defect.** The ban is not about a keyword; it is about a silent drop — nothing
walks a Rule's children, so a stock runner executes scenarios the suite does
not describe. Reported as "holding N scenario(s) that no suite describes".

**A Rule holds every scenario after it, not the indented block below it.**
Found while proving GH010 fires against the real tree, not just fixtures:
Gherkin is not indentation-sensitive, so a `Rule:` owns everything until the
next Rule or the end of the Feature. Inserting one `Rule:` line into
`features/repo-topology.md` took its pre-existing scenario out of the
extraction along with the smuggled one — the count went 22 -> 21 and the
finding said two. One stray line can empty a Feature.
`tests/fixtures/bad-rules/features/absorbs-what-follows.md` pins it.

Fixtures: `nested.md` (the drop, with a direct-child control),
`absorbs-what-follows.md` (the absorption), `empty-rule.md` (the decision's
"fails lint before its emptiness matters"), `mentions-a-rule.md` (the negative
control — `Rule:` in a docstring and in step text, and it must stay clean).

**Mutations, both directions.** Not recording rules turns four tests red;
keying the rule on the token instead of the node turns three red, including the
negative control.

## The ledger keys by sha

`find_record()` locates a record by its `spec_version` field, matching sha
prefixes either way, so an abbreviated `--version` reaches a record opened with
the full forty and a renamed file is still found. It deliberately does **not**
match a record whose `spec_version` is a name — reaching one by its decoration
would be reading a name to decide something.

**Consequence, and it is the architect's to resolve:** the intent repo's
`ledger/spec-v1.yaml` .. `spec-v11.yaml` carry `spec_version: spec-vN` and are
invisible to this CLI until their `spec_version` fields are rewritten to the
commits they name. Nothing in this repo depends on it — every record
`on-spec-merge.yml` writes from now on is sha-keyed under `ledger/<sha>.yaml` —
but it is worth doing while re-syncing the installed workflows.

## Minting

`on-spec-merge.yml` no longer mints. The merge commit is the version, so the
next-integer arithmetic and the already-tagged guard are gone; the replay guard
is `[ -f "ledger/${sha}.yaml" ]`, and `vellum ledger open` is idempotent
besides.

**The decorative name is derived from history**, not read back out of a tag
registry: `spec-v$(git rev-list --first-parent --count <sha> -- spec)`. It
cannot be missing, late or out of order the way `max(spec-v*) + 1` could, and
it reproduces every existing name exactly (`bc84e59` -> `spec-v1`, `be029e6` ->
`spec-v5`, `1ce87cb` -> `spec-v11`, checked). The step is `continue-on-error`
on purpose — a name is decoration, so a failed tag push must not fail a run
that has already recorded the version.

One Actions detail worth not rediscovering: the naming step runs *before*
`Open the ledger record`, because `steps.name.outputs.tag` is empty when
referenced from an earlier step. The output is written before the push, so the
record still gets its name when the push fails.

## Verification

- 144 tests OK, 11 skipped without an intent checkout; **144 OK, 0 skipped**
  with `VELLUM_INTENT_REPO` set at the pin, which is what the conformance job
  runs.
- `vellum lint` clean at the pin (`9c8b70a`) and with #20's content applied.
- `vellum suite extract` at the pin: 22 scenarios, versions
  {spec-v1, spec-v5, spec-v7, spec-v10, spec-v11}, none pending, not shallow,
  `spec_version` == the pin, `spec_head` == `1ce87cb` (spec-v11).
- With #20's content landed on top: lint clean, 22 scenarios, and the
  `id -> version` map **byte-identical** to the pin's. #20 changes no
  scenario's identity or version, as its diff (one decision file, four lines of
  prose) implies — checked rather than assumed.
- Old-vs-new side by side at all eleven versions: agree exactly. Table above.
- `vellum ledger open|advance` exercised end to end against real shas,
  including replay and abbreviated-sha lookup.

## A process note for the next wave

`git checkout <file>` to undo a mutation test discarded uncommitted work on
`src/vellum/lint.py` — the whole `GH010` rule — and the test suite was not
re-run immediately afterwards, so it went unnoticed until GH010 failed to fire
against the real tree. Two habits, either of which would have caught it: back
the file up by copying it rather than reverting it against the index, and
commit a checkpoint before mutating anything. The real save was **checking the
rule against the live intent tree and not only against its own fixtures** —
the fixtures were passing at the time, because they had been generated when
the rule existed.
