# Wave: spec-v6 — one Feature per fence, runnable scenarios, the hierarchy

Worklog for the fourth wave, and the first coalesced one: the pin sat at
spec-v3 while the intent repo reached spec-v6, so this wave carries three
deltas at once.

| Delta | Spec change | What the product owed it |
|---|---|---|
| spec-v4 | Each fenced `gherkin` block holds exactly one Feature (`spec/decisions/2026-08-28-one-feature-per-fence.md`) | `GH009`, and a fixture that carried the banned shape |
| spec-v5 | Lint rejects scenarios that parse but can never run (`spec/decisions/2026-08-28-runnable-scenarios.md`) | Nothing — `GH007` already did it. Verified, and the verification made a test |
| spec-v6 | The PO/PA/CA operating hierarchy (`spec/decisions/2026-08-28-po-pa-ca-hierarchy.md`) | Nothing. Process, not product |

Closing the wave: submodule `spec` at `2906dfb`, `.vellum/product.yaml` at
spec-v6, README pin text. The tree lints clean at the new pin and extracts 20
scenarios at versions {1, 5}, none pending.

## spec-v4 — GH009, and what the splitter is now for

The spec now says what `split_documents()` used to work around: a fence holds
one Gherkin document, so a stock Cucumber parser reads it unmodified. The
accommodation was never the problem — it was that the accommodation lived only
in this CLI, so the banned shape passed lint here and would have silently lost
scenarios in the v0.2 harness. `GH009` closes that: the shape is rejected
rather than quietly absorbed.

**Is a lint rule authorized?** The runnable-scenarios decision was written
about a rule that had no spec sentence behind it — invariant 1 in reverse —
so the question is worth asking rather than assuming. It is: spec-v4 puts the
rule in `spec/features/scenarios-and-harness.md` as a sentence about the shape
of the tree, and lint is the mechanical guard on the shape of the tree. `GH008`
is the precedent — the Background ban lives in a decision file and in no lint
sentence anywhere, and lint enforces it. What would need a new sentence is
rejecting something that *does* run, or changing what counts as a scenario.
Neither is here.

**Detection comes from parsed Feature nodes, not from counting `Feature:`
lines.** `parse_block()` now records a `Feature` per document it parses and
lint faults `block.features[1:]`, so the finding carries the real line and the
real names — the same move the spec-v3 wave made for `GH008` and for the same
reason ("the token can appear inside a docstring or a step's text"). A
candidate implementation on the closed branch
(`claude/vellum-product-architecture-9gei24`, PR #3) used a textual
`extra_feature_lines()` helper instead. I reimplemented rather than
cherry-picked, and dropped that helper.

**A prior review flagged a "GH009 docstring false positive" on that branch. It
is real, and it was already half-real before GH009 existed.** Checked against
the diff rather than taken on trust:

- A docstring line reading `Feature: …` at column zero is literal text — one
  Feature to the parser, two to the splitter. On `main` today the splitter cuts
  that sound block in half and lint reports the resulting unterminated
  docstring as `GH001`. A textual `GH009` would have added a second wrong
  finding on top of the first.
- A *step* line beginning `Feature:` is not the same case. Gherkin ignores
  indentation outside docstrings, so that really is a second Feature and
  `GH009` is right to fault it. I wrote that as a test first, watched it fail
  for the right reason, and rewrote it as the docstring case.

So the fix is not a narrower `GH009` — it is to stop cutting first.
`_documents()` hands the whole body to the official parser and only falls back
to `split_documents()` when the parser refuses. A conforming fence is now never
split at all, which is a more honest test of the rule the spec states than any
count of `Feature:` lines, and the docstring block goes from a spurious `GH001`
to clean. The splitter stays, and the reason it stays has changed: it is no
longer an accommodation for today's tree but the reader for yesterday's, since
`version_history()` walks every `spec-v*` tag and
`features/certification-and-releases.md` held two Features in one fence from
spec-v1 to spec-v5.

**Confirmation the decision's own claim holds.** The decision asserts that
splitting a fence "moves no version and re-dates no scenario". Extraction at
the new pin agrees: every scenario written before spec-v5 is still at version
1, across the very file spec-v4 re-fenced.

**The clean fixture carried the banned construct**, exactly as it did at
spec-v3 with the Background: `tests/fixtures/good/features/auth.md` held
Session expiry and Sign-out in one fence. Split into two fences — the migration
the decision asks authors to make — and the banned shape moved to
`tests/fixtures/bad-multi-feature/`, where failing is the point.

## spec-v5 — no code change, and a verification that stayed

`GH007` already implements this decision, which is unsurprising: the decision
exists *because* this CLI shipped the rule and the implementer flagged it as
unsupported. The spec caught up to the code. Verified rather than assumed:

- The decision names the canonical member as "an outline whose Examples table
  has a header and no data rows". That is a stricter case than the fixture had
  — `bad-ids` only ever carried an outline with no `Examples:` section at all.
  A header-only table parses into a real Examples node, so a truthiness check
  on `examples` would pass it. `GH007` tests `ex["rows"]`, so it catches both.
  It was right; nothing here proved it until now.
- Both members now sit in `tests/fixtures/bad-unrunnable/` with a test class
  named for the decision, matching how `bad-backgrounds` isolates `GH008`. The
  outline moved out of `bad-ids`, where it was never about ids.

The decision widens the rule's licence — "declared but unrunnable" is a class,
and lint may grow members of it (a `Rule:` with no scenarios is the example)
without new spec sentences. Not taken up: nothing in the tree needs it, and a
rule with no case to reject is a rule with no test.

## The judgment call I was asked to make

**Derived the pinned-tree counts instead of bumping `19` to `20`.** Four tests
hard-coded facts about the pinned tree — the scenario count twice, the
gherkin-file count, and "every scenario is version 1" — and spec-v5's twentieth
scenario turned all four red at once. Bumping the literals is one character
each and puts the same trap back for the next wave that adds a scenario.

`memory/areas/cli.md` already carries the landmine one size down: *never
hard-code the pinned spec version in a test*, because "a hard-coded version
fails on every pin advance, which is noise that trains people to ignore red."
A hard-coded count is that landmine wearing a different hat, and it had already
bitten by the time I read the note. So `pinned_scenario_count()` and
`pinned_gherkin_file_count()` join `pinned_version()` in `tests/support.py`,
reading the tree by counting `@id:` tag lines and gherkin fences — oracles
independent of the extractor they check. `test_every_scenario_is_version_one`
became `test_no_scenario_is_dated_past_the_pin`, which asserts the invariant
that was actually meant: nothing is dated beyond the pin, and the spec-v1 tree
is still at 1.

The cost is that a count oracle can itself drift — a decision file quoting an
`@id:` tag alone on a line would inflate it. That failure is loud and points at
the tree, which is the right direction to be wrong in; a stale literal is
silent about everything except itself.

## Surprises

**`main` was already red before this wave touched it.** `test_the_suite_is_extracted_at_the_pinned_version`
fails on a clean clone at the spec-v3 pin, because `spec_version` is the newest
`spec-v*` tag *in the submodule*, not the tag at the checked-out commit — and
the intent repo has carried spec-v4 through spec-v6 since before this wave
opened. The conformance job reads the same field, so it has been failing on
`main` for the same reason.

That is arguably the pin doing its job: the product repo says out loud that it
is behind the spec, and advancing the pin is what quiets it. But it is a red
build that no product change caused and that no product change but this one can
fix, and it will recur on every future spec merge — the intent repo goes green,
the product repo goes red, and stays red until the wave lands. Whether that
belongs as backpressure signal, as a distinct "pin is stale" state, or as
nothing at all is a spec question about what conformance CI reports, not an
implementation detail, so I have not touched it. Flagged for the PA.

## Paths rejected

- **Cherry-picking the closed branch.** Its pin advance stops at spec-v4, its
  `GH009` is textual, and its `good`-fixture migration is the one edit I would
  have made anyway. Reimplemented; the fixture split is identical because there
  is only one way to split a fence.
- **A narrower textual `GH009`** — excluding lines inside docstrings by
  tracking `"""` state while scanning. That is a second, weaker Gherkin parser
  living next to the real one, and the spec-v3 wave already rejected exactly
  this shape of fix for `GH008`.
- **Deleting `split_documents()` now that the shape is banned.** It reads the
  tags the version chain walks. I measured this rather than argued it — stub it
  to raise, extract the pinned tree — and the result is worse than it sounds:
  the suite still reports 20 scenarios, nothing raises and nothing is pending,
  but the three scenarios in `certification-and-releases.md` move from version 1
  to version 4, because the fence is unreadable at every tag before the split.
  Three scenarios the product already satisfies would be armed, and the only
  evidence is a number in `suite.json`.
- **Adding a lint member for `Rule:` with no scenarios.** Licensed by the
  decision, unmotivated by the tree.
- **Bumping `19` to `20`.** Reasoned above.

## Asked this wave

Nothing went out as a question issue. Both judgment calls sat inside the
spec's envelope — how to detect a shape the spec names, and how a test reads a
fact it must not hard-code — so they are recorded here and in the PR rather
than asked. The stale-pin CI observation above is a spec question if it is
anyone's, and it is the PA's to route.
