# Area: the `vellum` CLI

`src/vellum/`. Three commands — `lint`, `suite extract`, `ledger open|advance`
— dispatched from `build_parser()` in `src/vellum/cli.py`. Every claim below
names a file or symbol you can grep for.

## Module map

| Module | Holds |
|---|---|
| `src/vellum/cli.py` | `build_parser()`, `main()`. The only place argparse appears. |
| `src/vellum/specfile.py` | `resolve_spec_root()`, `parse_spec_text()`, `find_fences()`, `iter_spec_files()`. |
| `src/vellum/gherkin_blocks.py` | `_documents()`, `split_documents()`, `parse_block()` -> `Block`, `_attach_id()`, `scenario_ref()`. |
| `src/vellum/links.py` | `find_references()`, `resolve()`, `heading_anchor()`, `heading_anchors()`. |
| `src/vellum/lint.py` | `lint_tree()`, `Finding`, the `_check_*` functions. |
| `src/vellum/suite.py` | `extract()`, `scan_file()`, `scenarios_in()`, `UnparseableBlocks`, `fingerprint()`, `version_history()`, `History`, `to_dict()`. |
| `src/vellum/gitver.py` | `spec_commits()`, `names()`, `is_shallow()`, `markdown_at()`, `show()`. All subprocess git lives here. |
| `src/vellum/ledger.py` | `open_record()`, `advance()`, `find_record()`, `dump()`, `RECORD_KEYS`, `ITEM_KEYS`. |

## Scenario identity

Identity is the `@id:<slug>` tag on the scenario
(`spec/decisions/2026-08-28-scenario-identity.md`), read by `_attach_id()` in
`src/vellum/gherkin_blocks.py`. **The file is only the scenario's current
home** — `version_history()` in `src/vellum/suite.py` keys by id alone, so a
scenario moving between files keeps its version. Ledger references take the
form `scenario:<id>` (`scenario_ref()`).

Lint enforces it: `GH005` missing tag, `GH006` malformed or duplicated-on-one-
scenario, `GH003` id claimed by two scenarios. Four more are unrelated to ids:
`GH007`, a `Scenario Outline` with no `Examples` rows, which parses cleanly and
then never runs; `GH008`, any feature declaring a `Background:`; `GH009`, a
second `Feature:` in one fence; and `GH010`, any feature declaring a `Rule:`.
`GH003` is checked in
`_check_unique_ids()` at the `lint_tree()` level, **not** per file, because ids
are unique across the whole intent repo — putting that check back inside
`_check_gherkin()` would silently stop catching the cross-file case.

## Versions are commits

A spec version is a main commit whose diff touches the spec tree
(`spec/decisions/2026-08-28-versions-are-commits.md`). `spec_commits()` in
`src/vellum/gitver.py` is `git rev-list --first-parent --reverse <ref> -- <prefix>`
and `version_history()` walks what it returns. The comparison inside the walk
did not change at all in the transition — id first, fingerprint as fallback,
`consumed` to stop cross-assignment. What changed is where the sequence comes
from, and therefore what can go missing from it.

Three consequences worth holding:

- **`spec_version` is the commit extracted at**, not "the newest version
  visible". That is the whole fix for the red conformance check on `main`
  (waviisoft/vellum#4): the tag walker read every `spec-v*` tag *present in the
  repo*, so a checkout at an older pin was dated by tags it did not contain and
  the pin assertion failed the moment the intent repo moved ahead — on every
  branch, including the base. Ancestry cannot do that.
  `test_dating_reads_the_checkout_ancestry_not_every_ref_in_the_repo` pins it.
- **`pending` shrank to mean "uncommitted".** Any committed spec change is
  itself a version, so it needs no tag to become datable. On a CI checkout
  nothing is pending. "Introduced or changed by this PR" is now
  `version == spec_version`, which is what `spec-ci.yml` summarises.
- **"Earliest" is ancestry rank, not `min()`.** Shas do not compare.
  `History.order` carries the rank, and the fingerprint fallback uses it.

## Landmines

**The splitter is a fallback now, and deleting it still breaks the version
chain.** Since spec-v4 a fence holds exactly one `Feature:`
(`spec/decisions/2026-08-28-one-feature-per-fence.md`, `GH009`), so a
conforming block goes to the official parser whole — `_documents()` in
`src/vellum/gherkin_blocks.py` tries that first and only falls back to
`split_documents()` when the parser refuses. Do not follow that through to
deleting the splitter. **This transferred intact from tags to commits and was
re-measured, not assumed:** `version_history()` now reads every spec-touching
*commit*, and `features/certification-and-releases.md` held two Features in one
fence from the seed commit through `be029e6`. `scenarios_in()` swallows a parse
error, so without the splitter that whole fence — all three scenarios, not just
the second Feature's one — is invisible at every commit before `c4307ab`, and
the three re-date from the seed to `c4307ab`, the commit where the split made
them readable again.

Re-measured on the real tree during this wave, by stubbing `split_documents()`
to never split and extracting `main`: count stays 22, nothing raises, nothing is
pending, and `features/certification-and-releases.md` moves from `bc84e591`
(spec-v1) to `c4307abe` (spec-v4) — three versions younger, which would arm
scenarios the product already satisfies. `TestBlockSplitting` in
`tests/test_suite.py` covers the splitter,
`test_re_fencing_a_block_did_not_re_date_the_scenarios_in_it` covers the
consequence against the pinned tree, and `TestMultiFeatureFences` in
`tests/test_lint.py` covers `GH009`.

**A clean whole-fence parse does not mean one Feature.** The Cucumber parser
refuses a second `Feature:` only where it reaches one *as a declaration*.
Reached where free text is legal — a Feature's description, a Scenario's
description, i.e. any point before the first step — it absorbs the line into
that description as prose. The fence then parses with **no error at all**, one
Feature short, the second Feature's scenarios re-parented onto the first and
its name gone from `suite.json`. So `_documents()` trusts a whole parse only
when the body holds at most one top-level `Feature:` line, and otherwise splits
to find out. Do not simplify that back to "if it parses, it conforms": `GH009`
then silently stops firing for exactly the fences an author is most likely to
write by accident. `test_a_second_feature_the_parser_absorbs_as_prose_is_still_found`
and `tests/fixtures/bad-multi-feature/features/absorbed.md` cover both
absorption sites.

**Ask the parser before cutting on `Feature:` lines.** `_FEATURE_RE` matches
column zero, and Gherkin ignores indentation everywhere *except* inside a
docstring, where the text is literal. So a docstring line reading `Feature: …`
at column zero is one Feature to the parser and two to the splitter, and
cutting first reported the resulting unterminated docstring as `GH001` against
a block that parses. Parsing whole first is what makes that block clean, and it
is also the honest test of the rule the spec actually states — a stock Cucumber
parser reads the fence unmodified. Pinned by
`test_a_block_the_stock_parser_reads_whole_is_never_split`. A step line
beginning `Feature:` is *not* this case: indentation is not significant there,
so it really is a second Feature and `GH009` is right to fault it.

**`<spec-dir>` is two different things.** `resolve_spec_root()` in
`src/vellum/specfile.py` accepts either the spec tree itself or the intent repo
that contains it — the tree may be at `<path>/spec/`, not `<path>/`. Anything
walking the tree must go through `resolve_spec_root()` first; `intent_spec_tree()`
in `tests/support.py` does, rather than appending `spec` itself.

The submodule is gone (`spec/decisions/2026-08-28-pin-file.md`) and the
ambiguity is not: CI hands the CLI the checkout it fetched, a developer hands
it either. **The test that covered this went quiet the moment the submodule
went away** — it was written against `REPO_ROOT / "spec"` and skipped itself
when that was absent, so removing the submodule silently removed the coverage.
It now builds a two-level tree in a tempdir and asserts both shapes structurally,
with the live checkout as a second, skippable case. Any test whose subject is a
property of the code should not be gated on a property of the environment.

**PyYAML turns an unquoted `2026-08-27` into a `datetime.date`, not a string.**
Every `decisions/` file in the spec tree has an unquoted date, so a naive
`isinstance(value, str)` check fails all thirteen of them. See `_is_iso_date()`
in `src/vellum/lint.py`.

**Paths inside fenced blocks are prose, not cross-references.**
`spec/features/orchestration.md` names `features/auth.md` inside a Gherkin
block as an illustration; that file does not exist. `_masked_lines()` in
`src/vellum/links.py` blanks fenced blocks and inline code spans before
scanning. Removing that mask makes `vellum lint spec/` fail against spec-v1.

**The fingerprint deliberately ignores almost everything.** `fingerprint()` in
`src/vellum/suite.py` hashes normalized steps and example tables and nothing
else — not the title, not the tags, not the keyword, not line numbers. That is
the spec's definition of "changed", and it is load-bearing: spec-v2 added an
`@id:` tag to all nineteen scenarios, and had tags been in the fingerprint,
every scenario would have jumped to version 2. `test_adding_a_tag_does_not_change_it`
pins this.

**Steps are compared by keyword *type*, not keyword.** `Step.keyword_type` in
`src/vellum/gherkin_blocks.py` resolves `And`/`But` to the type of the step
above (`Conjunction` -> the previous `Context`/`Action`/`Outcome`), so
rewriting `And` as `Given` is presentation while `Given` -> `When` is a change.

**`version_history()` falls back to fingerprint matching.** A scenario whose id
is absent from the previous tag — because it has just been given one — is
matched to an unclaimed scenario with the same fingerprint, which is the only
reason the seed commit's scenarios survived the introduction of ids at
`13afa40` with their versions intact. `_Seen.consumed` stops two new scenarios inheriting from
one old one. Covered by `test_giving_an_existing_scenario_an_id_keeps_its_version`.

**`background_steps` is always empty, and that is deliberate.** Backgrounds are
banned (`spec/decisions/2026-08-28-no-backgrounds.md`) and `GH008` rejects
them, so the term in `fingerprint()` (`src/vellum/suite.py`) can never fire in
a conforming tree. It is kept because that same decision records the semantic
for any future relaxation — a Background's steps belong to every affected
scenario's fingerprint, and the opposite reading is "rejected outright as a
violation of invariant 4". Do not delete it as dead code; deleting it discards
a working implementation of a written decision.
`test_background_steps_would_count_toward_the_fingerprint` pins it directly,
since no fixture can carry a Background any more.

**Extraction refuses the tree it was handed; the walk behind it does not.**
`extract()` scans every spec file through `scan_file()` and raises
`UnparseableBlocks` before it touches git, so a refusal costs no history walk
and one run names every failing fence rather than one per invocation. But
`version_history()` dates scenarios by re-parsing *old* trees — `_scenarios_at()`
runs `scenarios_in()`, the tolerant half of `scan_file()`, over every
spec-touching commit in the checkout's ancestry — and those go on skipping. The
asymmetry is the design, not squeamishness: a commit is already in the past,
nobody can go back and fix a fence that failed to parse there, and a strict walk
would mean one bad commit anywhere in main's history renders every descendant of
it permanently unextractable. The tree an author was actually pointed at is the
one they can repair, so that is the one that fails.
`TestHistoryStillToleratesABrokenBlock` pins the tolerant side by committing a
broken block and then a good one and extracting; delete it and a later tidy-up
that "makes `scenarios_in` consistent" will look harmless.

**Measured before it was changed, not assumed:** every gherkin fence at every
spec commit on waviisoft/vellum-intent main parses today — 16 spec commits, 542
markdown file-revisions, 196 fences, zero `GherkinParseError`. So no historical
tree currently leans on the walk's tolerance, and `suite.json` extracted at the
pin (`666df3d3`) and at intent main (`d1e91e0f`) is byte-identical before and
after this change. The tolerance is kept for what it protects against, not for
anything it is carrying now. Re-measure it rather than re-reading this line: the
script is a walk of `spec_commits()` calling `parse_block()` on each fence, which
is `_scenarios_at()` with the failures counted instead of dropped.

**Silent absence was the failure class, so there is no partial-extraction flag.**
waviisoft/vellum#7: `lint` exited 1 with `GH001` while `extract` exited 0 on the
same tree, emitting a suite short exactly the scenarios in the broken fence — and
a consumer (the harness, a briefing assembler, the ledger's armed-scenario
accounting) reads that smaller suite as the whole intent, with nothing anywhere
saying something was dropped. Spec CI was protected only because `lint` gated the
same PR; anything invoking `extract` alone was not, which is what blocked the
harness wave from asserting extraction as spec CI's second required check. An
opt-in "extract what you can" flag would re-open the hole for whoever sets it
once in a workflow and forgets, so it was not added.
`tests/fixtures/bad-gherkin-mixed/` is the fixture that shows the shape: one good
block and one broken one, so refusing is visibly *not* the same as "found
nothing". `bad-gherkin` cannot show it — nothing in that tree parses, and an
empty suite is at least conspicuous.

**Exit 1, not 2.** `run()` in `src/vellum/suite.py` uses lint's code for "the
tree is the problem"; 2 stays what `SpecTreeError` and `LedgerError` mean, "the
path you gave me is not a spec tree". A caller telling a bad spec from a bad
invocation can still do it. The message names the file, the failing block's
opening fence line, and the parser's own fault line — `<file>:<line>: gherkin
block at line <n> does not parse: …` — because "which block" and "where in it"
are different questions and the second one alone does not tell you which fence
to go and look at.

**Never hard-code any fact about the pinned tree in a test** — not the pin, not
the scenario count, not the file count. Use `pinned_commit()`,
`pinned_scenario_count()` and `pinned_gherkin_file_count()` in
`tests/support.py`: the first reads `.vellum/product.yaml`, the same file the
`conformance` job reads; the other two read the tree itself, counting `@id:`
tag lines and gherkin fences independently of the extractor they check. A
hard-coded fact fails on every advance that touches it, which is noise that
trains people to ignore red. The version bit at spec-v2 -> spec-v3; the counts
bit at spec-v3 -> spec-v6, when spec-v5 added a twentieth scenario and four
tests asserting `19` went red at once. The same reasoning is why the pinned
tests ask ancestry questions ("is every version an ancestor of the pin?",
"is the seed still the version of something?") rather than naming shas.

**A directory with no git metadata answers for its *parent* repo, silently.**
One of the failures that cost the submodule its job
(`spec/decisions/2026-08-28-pin-file.md`), and it bit again the moment the
gitlink was removed: the leftover empty `spec/` directory made
`git -C spec rev-parse HEAD` return *this* repo's HEAD, so the intent checkout
looked present and sat at a commit that was not the pin. `intent_checkout()` in
`tests/support.py` compares `rev-parse --show-toplevel` against the path itself
before believing anything else it is told.

**An absent intent checkout skips; a wrong one raises.** `intent_checkout()`
returns None when nothing is supplied — the ordinary case for the `test` job
and a fresh clone — and raises `WrongPin` when `VELLUM_INTENT_REPO` (or a
`./spec` mount) is at some other commit. The distinction is the point: absence
is a fact about the environment, a wrong commit is a mistake, and skipping past
it would report conformance against a tree that is not the pinned one. The
`conformance` job runs the whole suite with the variable set, which is what
stops the `test` job's eleven skips from being a hole.

**A `Rule:` holds every scenario after it, not the indented block below it.**
Gherkin is not indentation-sensitive, so a `Rule:` owns every scenario until
the next Rule or the end of the Feature, whatever the layout suggests. One
stray `Rule:` line above a Feature's existing scenarios therefore takes all of
them out of the suite at once. Measured on the real tree while `GH010` was
written: inserting a single `Rule:` line into `features/repo-topology.md`
dropped its pre-existing scenario along with the smuggled one, and the finding
counted two. This is why `GH010`'s message names how many scenarios the Rule
holds — that count is the defect (waviisoft/vellum-intent#16), not the keyword
— and why `tests/fixtures/bad-rules/features/absorbs-what-follows.md` exists.
Detection reads `child["rule"]` from the parsed node, as `GH008` and `GH009` do,
so a `Rule:` in a docstring or a step's text is left alone; the negative
control for that is `features/mentions-a-rule.md`.

**The ledger key is the sha, and only the sha.** `find_record()` in
`src/vellum/ledger.py` locates a record by its `spec_version` field, matching
sha prefixes in either direction, so an abbreviated `--version` reaches a record
opened with the full forty and a renamed file is still found. It deliberately
does **not** match a record whose `spec_version` is a name like `spec-v6`:
reaching a record by its decoration would be reading a name to decide
something, which is the practice the versions-are-commits decision removed.
Consequence, and it is a real one: the intent repo's existing
`ledger/spec-v1.yaml`..`spec-v11.yaml` are name-keyed and invisible to this CLI
until someone rewrites their `spec_version` field to the commit sha. That
migration is the architect's — the ledger lives in the intent repo and is
written only by automation — and nothing in this repo depends on it, because
every record `on-spec-merge.yml` writes from now on is sha-keyed.

**Renaming an id over unchanged content keeps the version.** A scenario whose
id disappears and whose content reappears under a new id is dated by the
fingerprint fallback, so it stays at its original version. That follows from
the spec's own rule — "changed" is the fingerprint, and this content was
already specified — but it surprises on first reading. Pinned by
`test_renaming_an_id_over_unchanged_content_keeps_the_version`. `History.by_fingerprint`
keeps the *earliest* version among identical content: dating a fallback match
too early leaves it enforced, which is the safe direction; too late would arm a
scenario the product already satisfies.

**A bare path absorbs the sentence's full stop.** `...see auth.md#acceptance.`
would otherwise yield the fragment `acceptance.`. `find_references()` rstrips
`.,;:!?` from bare-path fragments only — a markdown link's fragment is
delimited by `)` and must not be stripped.

**`fetch-depth: 0` is load-bearing, and the failure got quieter.** Under tags a
shallow clone had none, so every scenario came back `pending` at version 1 —
wrong, but at least everything was pending. Under ancestry a shallow clone has
*some* history, so the graft boundary becomes a plausible-looking version and
every scenario below it re-dates **forward** onto it: right count, nothing
pending, nothing raised. Measured on the real tree: a `--depth 3` clone dated 21
of 22 scenarios to `3e28b3b`, which is not even a spec version — it is the
truncation point. Forward is the dangerous direction, because it arms scenarios
the product already satisfies.

It cannot be inferred from the walk — a short history and a truncated one look
identical — so `is_shallow()` in `src/vellum/gitver.py` asks git directly
(`rev-parse --is-shallow-repository`) and `suite.json` carries `shallow`. All
three workflows set `fetch-depth: 0`, and the conformance job fails on
`shallow: true`. Treat the flag as the last line, not the guard.
`TestShallowHistory` in `tests/test_suite.py` pins it.

**A Gherkin keyword can have a synonym, and the parsed node will not tell you.**
`GH007` matched the literal string `Scenario Outline`, so an unrunnable
`Scenario Template` — the same construct under Gherkin's other English keyword —
drew zero findings and extracted as coverage that pins nothing. There is no
outline flag on the node to fall back to: `Scenario`, `Scenario Outline` and
`Scenario Template` parse identically apart from `keyword`, and all three carry
`examples == []` when no `Examples:` section is written, so the keyword string is
the only signal there is. Match it against the parser's own dialect rather than
against literals — `_OUTLINE_KEYWORDS` in `src/vellum/gherkin_blocks.py` is
`Dialect.for_name("en").scenario_outline_keywords`, and `Scenario.is_outline` is
what `GH007` now tests. That API is stable across the pinned range
(`gherkin-official>=29,<43`; checked at both ends). Any future rule keyed on a
keyword has the same hole waiting: `Example` is a synonym of `Scenario`, and
`Rule:`, `Background:` and the step keywords all localise. `TestUnrunnableScenarios`
in `tests/test_lint.py` covers both spellings and keeps a runnable template as a
negative control, so the rule cannot regress into faulting the keyword instead of
the unrunnability.

## Patterns worth keeping

- **Findings, not exceptions.** `lint_tree()` returns a sorted list of
  `Finding`; only `main()` decides the exit code. That keeps lint usable as a
  library (`--json`) and makes the tests assert on codes rather than on text.
- **Lint and extraction agree about a broken block: both refuse it.**
  `extract()` in `src/vellum/suite.py` raises `UnparseableBlocks` naming every
  failing fence, and `run()` turns that into exit 1 with nothing written. That
  reversed the older rule — extraction used to skip a broken block so it could
  "still describe the rest of the tree" — for the reason under Landmines above.
  `TestUnparseableBlocksAreRefused` in `tests/test_suite.py` pins it, and
  asserts the lint/extract disagreement itself rather than only the new code.
- **Untagged means "the version this would mint".** A working-tree scenario no
  tag carries gets `latest + 1` and `pending: true` (`extract()`), which is
  exactly what spec CI needs when it runs on a PR before the tag exists.
- **`open_record()` is idempotent, `advance()` accumulates.** The reconciler
  may replay an approval (decision D11), so a second `open` leaves the record
  alone. Cost is additive because every agent invocation records into the same
  work-item entry.
- **Fixed key order on write.** `RECORD_KEYS`/`ITEM_KEYS`/`COST_KEYS` and
  `_ordered()` in `src/vellum/ledger.py` make a state change a one-line diff
  and a read/write round-trip byte-stable
  (`test_a_record_reread_and_redumped_is_byte_identical`).

**`spec_version` and `spec_head` are different questions.** `spec_version` is
the commit the suite was extracted at — a checkout's pin, a PR head — and need
not touch the spec tree at all (the current pin is `9c8b70a`, a ledger commit).
`spec_head` is the newest commit in that ancestry that *is* a version. CI
compares the pin against `spec_version`, which is a checkout fact; anything
asking "what is the newest intent this tree carries" wants `spec_head`.

## Settled

`spec_tags()` and `BASE_VERSION` are gone from `src/vellum/gitver.py`, and with
them the idea that a tree can have a version without a commit. `suite.json` is
schema 2: `version`/`spec_version` are shas, `version_name`/`spec_version_name`
carry the decoration, `spec_head` is new, `tagged` became `shallow`, and
`source_commit` is gone because it was always the commit extracted at — which
is `spec_version`. Anything reading the old integer fields will read a sha, and
anything reading `tagged` or `source_commit` will read nothing.

The interim Feature+Scenario slug scheme is gone, along with `src/vellum/slug.py`
which existed to serve it. waviisoft/vellum-intent#2 was answered by
`spec/decisions/2026-08-28-scenario-identity.md` and the tree now carries
explicit ids. `suite.json` emits `id` and `ref` where it used to emit `anchor`;
anything reading the old field will read `None`.
