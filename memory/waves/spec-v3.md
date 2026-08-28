# Wave: spec-v3 — scenarios are self-contained; Backgrounds are banned

Worklog for the third wave. spec-v3 answers the question the spec-v2 wave
raised (waviisoft/vellum-intent#4) with
`spec/decisions/2026-08-28-no-backgrounds.md`, taking the third option the
issue offered: ban `Background:` blocks outright rather than settle whether
their steps count toward a scenario's fingerprint.

Scope delivered: pin advanced to spec-v3, `GH008` rejecting any feature that
declares a Background, a decision on the now-unreachable `background_steps`
fingerprint term, and a fix for a test that would have broken on every future
pin advance.

## The judgment call I was asked to make

**Kept `background_steps` in `fingerprint()`.** The ban makes the term provably
empty in any conforming tree, so deleting it was the obvious move. I kept it,
because the decision that banned Backgrounds does not merely ban them — its
last paragraph records what must hold if the ban is ever lifted:

> if Backgrounds are ever admitted, their steps are part of every affected
> scenario's fingerprint (a Background edit bumps every scenario in the
> feature). Behavioral change must never be invisible to the version chain;
> the alternative reading — Background edits bump nothing — is rejected
> outright as a violation of invariant 4.

That is exactly what the term implements. Deleting it would discard a working
implementation of a written decision and leave whoever relaxes the ban to
re-derive it — with a coin-flip chance of reaching for the simpler reading the
decision rejects outright. The cost of keeping it is one concatenation over an
empty list; the cost of the wrong re-derivation is behavioral change invisible
to the version chain, which is an invariant-4 breach.

So the term stays, with a comment at `fingerprint()` in `src/vellum/suite.py`
naming the decision, and
`test_background_steps_would_count_toward_the_fingerprint` pins the semantic
directly rather than through a fixture — a fixture cannot carry a Background
any more, because lint rejects it.

## Surprises

**A test hard-coded the pinned version.** `test_the_suite_is_extracted_at_spec_v2`
asserted `spec_version == 2`, so advancing the pin to spec-v3 failed it. It
surfaced on `main` immediately after the v0.1 merge, in a clean-clone check —
not in CI, because the conformance job reads the pin from
`.vellum/product.yaml` and the test did not. Replaced with
`pinned_version()` in `tests/support.py`, which reads the same file the
conformance job does; the test is now
`test_the_suite_is_extracted_at_the_pinned_version` and survives every future
pin advance. A test that hard-codes the pin fails on every advance, which is
noise, not a defect, and noise that trains people to ignore red.

**The clean fixture carried the banned construct.** `tests/fixtures/good/`
used a `Background:` to demonstrate background handling, so the ban turned the
lint-clean fixture into a lint-failing one. Rewrote that scenario to inline
its setup step — which is precisely the migration the decision asks authors
to make — and moved the Background into `tests/fixtures/bad-backgrounds/`,
where failing is the point.

## Paths rejected

- **Deleting the `background_steps` term.** Reasoned above.
- **Detecting `Background:` textually.** Cheap, and wrong: the token can appear
  inside a docstring or a step's text. `parse_block()` now returns a `Block`
  with `.scenarios` and `.backgrounds`, so the finding comes from the parsed
  node and carries its real line.
- **Leaving `parse_block()` returning a bare list and adding a second entry
  point** for Backgrounds. Two functions parsing the same block invites them to
  disagree. One return type, four call sites updated.
- **Reporting the Background once per affected scenario.** One finding per
  Background block: the Background is the defect, and the scenarios under it
  are well-formed. `test_the_scenarios_themselves_are_not_faulted` pins that.

## Resolved from the spec-v2 wave

waviisoft/vellum-intent#4 is answered. The spec-v2 worklog's "Asked this wave"
section is closed by this one. Both questions this project has raised —
scenario identity, and Background scope — went out as question issues, were
answered by spec changes, and came back as code. That loop is now the only
part of Vellum that has run end to end twice.
