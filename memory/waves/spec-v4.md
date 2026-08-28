# Wave: spec-v4 — each gherkin fence holds exactly one Feature

Worklog for the fourth wave. spec-v4 resolves the finding this repo raised
while building `vellum lint` (waviisoft/vellum-intent#6) with
`spec/decisions/2026-08-28-one-feature-per-fence.md`: the tree's only
two-Feature fence is split, and the rule is stated in
`scenarios-and-harness.md`.

Scope delivered: pin advanced to spec-v4, `GH009` rejecting any fence that
declares a second `Feature:`, the clean fixture migrated off the banned
shape, and the stale "the spec tree does this" prose updated in the module
docstring and `TestBlockSplitting`.

## The judgment call

**Kept `split_documents()`.** With the tree conforming, the splitter never
splits, so deleting it was the obvious move — and wrong for the same shape of
reason the spec-v3 wave kept `background_steps`. The splitter is what lets
lint report the banned shape as `GH009` at the real spec-file line instead of
surfacing the Cucumber parser's raw `CompositeParserException`, and what
keeps `vellum suite extract` describing every scenario in a non-conforming
tree — lint fails the tree, extraction still maps it. A linter that can only
parse conforming input reports its best findings on exactly the trees that
need them least. `extra_feature_lines()` reuses the same `_FEATURE_RE`, so
detection and splitting cannot drift apart.

**One finding per extra Feature, not per fence and not per scenario.**
Mirrors the GH008 call recorded in the spec-v3 worklog: the extra `Feature:`
line is the defect; the fence is fine, and the scenarios under both Features
are well-formed. `test_the_scenarios_themselves_are_not_faulted` pins it.

## Surprises

**The clean fixture carried the banned construct — again.**
`tests/fixtures/good/features/auth.md` held two Features in one fence, so
`GH009` turned the lint-clean fixture into a failing one, exactly as the
Backgrounds ban did to the same fixture in the spec-v3 wave. Split it into
two fences — the migration the decision asks of authors. The pattern is now
2-for-2: any wave that adds a lint rule should expect the good fixture to be
its first offender, because the fixture was written to exercise breadth, not
to anticipate future bans.

**The submodule pin names a squash commit, so tags must be fetched before
checkout.** `git submodule update --init` clones at the recorded gitlink;
advancing needs an explicit `git fetch origin main --tags` inside `spec/`
first, or the new commit is "not a tree". Routine, but easy to trip on.

## Paths rejected

- **Deleting `split_documents()` as dead code.** Reasoned above.
- **Detecting the second Feature from the parse result.** The parser cannot
  represent two Features in one document — it raises on the second — so
  detection has to be textual and pre-parse. `extra_feature_lines()` shares
  the regex with the splitter rather than growing a second definition of
  "top-level Feature line".
- **A finding on every scenario under the second Feature.** The scenarios are
  not the defect; same reasoning as GH008.

## Resolved from earlier waves

waviisoft/vellum-intent#6 is resolved (spec side; this wave is the product
side). The finding→spec-change→conformance loop has now run end to end once,
alongside the two question→spec-change loops before it.
