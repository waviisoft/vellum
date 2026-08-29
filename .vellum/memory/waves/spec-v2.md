# Wave: spec-v2 — scenarios carry explicit stable ids

Worklog for the second wave. spec-v2 answers the question the spec-v1 wave
raised (waviisoft/vellum-intent#2) with
`spec/decisions/2026-08-28-scenario-identity.md`: identity is an explicit
`@id:<slug>` tag, not an ordinal and not a name slug.

Scope delivered: pin advanced to spec-v2, the interim slug scheme replaced by
`@id:` tags, three new lint rules, the fingerprint narrowed to what the
decision says "changed" means, `suite.json` regenerated.

## Approach

Read the spec-v2 diff before touching code. It is 12 files and 41 lines: an
`@id:` tag on each of the 19 scenarios, the new decision, and two sentences in
`features/ledger.md` saying acceptance criteria are referenced as
`scenario:<id>` while prose slices keep file-path-and-heading-anchor.

The decision settles four things, and each one landed somewhere specific:

| The decision says | Where it landed |
|---|---|
| `@id:<slug>`, lint-enforced | `_attach_id()`, and `GH005`/`GH006` in `src/vellum/lint.py` |
| globally unique per intent repo | `_check_unique_ids()` at `lint_tree()` level, not per file |
| identity is the id; the file is its current home | `version_history()` keys by id alone |
| "changed" = normalized steps and example tables; titles and tags are presentation | `fingerprint()` in `src/vellum/suite.py` |

## Surprises

**Narrowing the fingerprint was not optional — it was the whole migration.**
spec-v2 adds a tag to all 19 scenarios and changes nothing else. Under the
spec-v1 fingerprint, which included tags and titles, all 19 would have jumped
to version 2: a spec change that touched no behavior would have re-armed the
entire suite. Excluding tags and titles is what makes the change
version-neutral, and the fact that the spec spells out exactly which fields
count reads, in hindsight, like it was written knowing that.

**Ids do not exist at spec-v1, so id-keyed matching alone cannot cross the
boundary.** Walking tags and keying purely by id would find nothing at spec-v1
and mark all 19 scenarios as introduced at spec-v2 — the exact outcome the
decision is trying to prevent. `version_history()` therefore falls back to
matching an unclaimed scenario with the same fingerprint when an id is new.
That fallback is not a special case for this migration; it is what "identity
before ids existed" has to mean, and it keeps working for any scenario that
gains an id later.

**"Normalized steps" needed a decision of its own.** Gherkin gives each step a
`keywordType` (Context/Action/Outcome/Conjunction). Resolving `And`/`But` to
the type above them makes `Given a / And b` identical to `Given a / Given b` —
which is right, since that rewrite changes nothing — while `Given b` -> `When b`
still registers. Recorded below as a judgment call.

**Tag order matters more than it looks.** `spec-v2` was pushed before
`spec-v1`, and with only `spec-v2` present every scenario reported as version 2
and looked plausible. Nothing failed; the numbers were just wrong. Noted as a
landmine in `.vellum/memory/areas/adapters-github.md`.

## Paths rejected

- **Keeping `anchor` alongside `id` in `suite.json`.** Two identifiers for one
  scenario invites consumers to key on the wrong one, and the anchor is exactly
  the superseded scheme. Replaced it: `id` and `ref` where `anchor` was.
- **Keeping `src/vellum/slug.py`.** `slugify()` existed only to build interim
  anchors and had no other caller. Deleted the module and moved
  `heading_anchor()` into `src/vellum/links.py`, its only consumer.
- **Per-file duplicate-id checking.** Cheaper, and wrong: the decision says
  globally unique per intent repo, and the interesting failure is two *files*
  claiming one id. `_check_unique_ids()` runs over the whole tree.
- **Validating `--satisfies scenario:<id>` in the ledger CLI.** Tempting, since
  the format is now specified. Left alone: resolving a reference against the
  suite is the ledger guard's job (v0.3), and a format check that cannot
  confirm the id exists gives false confidence. The examples in the README and
  tests were updated to the new form so nothing documents the superseded one.
- **Editing `.vellum/memory/waves/spec-v1.md` in place.** Waves are per approved
  version and their worklogs are archival; this is a new wave, so it is a new
  file. spec-v1's "Left undone" section is now answered by this one.

## Judgment calls recorded (non-blocking)

1. **Background steps are part of a scenario's fingerprint** — implemented, but
   *asked* rather than settled: see "Asked this wave" below.
2. **Steps compare by keyword type, not written keyword** — see above.
3. **Whitespace inside step text and example cells is collapsed** before
   hashing, so re-wrapping or re-indenting a step is not a change.
4. **The `Scenario` / `Scenario Outline` keyword is excluded** from the
   fingerprint. The decision names steps and example tables; converting between
   the two forms necessarily changes the example tables anyway, so nothing is
   lost.
5. **`GH007`: a `Scenario Outline` with no `Examples` rows fails lint.** Not
   asked for and not named in the spec, which describes lint as checking that
   blocks parse — but such an outline parses and then never executes, which is
   the "under-specification" failure `docs/design.md` §12 calls the suite's
   attack surface. The agent coverage review that would otherwise catch it is
   stubbed until v0.2, so a mechanical check earns its place now.
6. **`History.by_fingerprint` keeps the earliest version** among scenarios with
   identical content, so a fallback match is dated when the behavior was first
   specified. Under-dating leaves a scenario enforced; over-dating would arm one
   the product already satisfies.
7. **`suite.json` still emits scenarios that have no id** (with `id: null`),
   rather than dropping them. Lint is where a missing id fails a run; extraction
   describes what is in the tree — the same split the spec-v1 wave chose for
   unparseable blocks.

## Asked this wave

**waviisoft/vellum-intent#4 — "Question: do Background steps count as a
scenario's content?"** The decision defines "changed" as "normalized steps and
example tables" without saying whose steps. A `Background:` block's steps run
before every scenario in its feature, so the two readings diverge on a real
event: under one, editing a Background bumps every scenario in the file; under
the other, a spec author changes what the suite exercises while no version
moves and the ledger records nothing — a behavioral change the traceability
chain cannot see. That is a property of the system, not an implementation
detail, so it was raised rather than chosen.

**Answered at spec-v3**, which banned Backgrounds outright rather than settling
the fingerprint question — and recorded this wave's conservative reading as
what must hold if the ban is ever lifted. See `.vellum/memory/waves/spec-v3.md`.

Latent when raised: the tree had no `Background:` blocks at spec-v2, which is
why it was cheap to settle then. v0.1 implements the conservative direction (a
Background edit bumps the affected scenarios, so nothing changes invisibly),
isolated to one term in `fingerprint()` in `src/vellum/suite.py`.

The other choices this wave were mechanical and are recorded above rather than
raised, per the ambiguity ladder. In particular an id renamed over unchanged
content keeps its version, because the decision states the change rule
explicitly and "identity is the id" governs matching, not what counts as a
change — the corpus answers it.

## Resolved from the spec-v1 wave

`spec-v1` and `spec-v2` are both tagged and pushed on the intent repo — the
owner pushed `spec-v1` at `bc84e591` during this wave, which is what made
correct version derivation possible. The "Left undone" section of
`.vellum/memory/waves/spec-v1.md` is closed by that.
