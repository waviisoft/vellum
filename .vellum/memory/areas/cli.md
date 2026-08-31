# Area: the `vellum` CLI

`src/vellum/`. Fourteen commands — `lint`, `suite extract`,
`ledger open|advance|verify`, `certify record|check`, `tick`, the three pipeline
commands `mint`, `backpressure`, `pin advance`, and the five mechanical guards
`verify boundaries|deps|exit-duty`, `ledger verify` and `budget` — dispatched
from `build_parser()` in `src/vellum/cli.py`. Every claim below names a file or
symbol you can grep for.

## Module map

| Module | Holds |
|---|---|
| `src/vellum/cli.py` | `build_parser()`, `main()`. The only place argparse appears. |
| `src/vellum/specfile.py` | `resolve_spec_root()`, `parse_spec_text()`, `find_fences()`, `iter_spec_files()`. |
| `src/vellum/gherkin_blocks.py` | `_documents()`, `split_documents()`, `parse_block()` -> `Block`, `_attach_id()`, `scenario_ref()`. |
| `src/vellum/links.py` | `find_references()`, `resolve()`, `heading_anchor()`, `heading_anchors()`. |
| `src/vellum/lint.py` | `lint_tree()`, `Finding`, the `_check_*` functions. |
| `src/vellum/suite.py` | `extract()`, `scan_file()`, `scenarios_in()`, `BlockError`, `DroppedScenarios`, `fingerprint()`, `version_history()`, `History`, `to_dict()`. |
| `src/vellum/gitver.py` | `spec_commits()`, `names()`, `is_shallow()`, `markdown_at()`, `show()`. All subprocess git lives here. |
| `src/vellum/ledger.py` | `open_record()`, `advance()`, `find_record()`, `dump()`, `parse_time()`, `upsert_plan()`, `RECORD_KEYS`, `ITEM_KEYS`. Also the certification and lease half: `certify()`, `certification_authorizes()`, `take_lease()`, `clear_lease()`, `active_lease()`. |
| `src/vellum/certify.py` | `check()` -> `Authorization`, `run_check()`, `run_record()`. The merge gate's evidence. |
| `src/vellum/mint.py` | `mint()` -> `Mint`, `_commit_record()`. The `on-spec-merge` bookkeeping. |
| `src/vellum/backpressure.py` | `measure()` -> `Window`, `run()`, `SETTLED_STATES`. The divergence gate. |
| `src/vellum/pin.py` | `advance()` -> `Advance`, `verify_version()`, `_rewrite()`. The pin close. |
| `src/vellum/config.py` | `load()`, `divergence_cap()`, `INTENT_ENV`. Reads `.vellum/config.yaml`. |
| `src/vellum/product.py` | `load()`, `write_boundaries()`, `normalise_tree()`, `under()`, `PRODUCT_RELPATH`. Reads `.vellum/product.yaml`. |
| `src/vellum/boundaries.py` | `check()` -> `Boundaries`, `run()`. The write-boundary guard. |
| `src/vellum/exitduty.py` | `check()` -> `ExitDuty`, `run()`, `AREAS_TREE`. The memory-update guard. |
| `src/vellum/deps.py` | `check()` -> `Policy`, `registries()`, `host_of()`, `_scan_toml_arrays()`. The dependency-registry guard. |
| `src/vellum/chain.py` | `verify()` -> `Chain`, `Finding`, `CERTIFIABLE_STATES`. The ledger guard. |
| `src/vellum/budget.py` | `measure()` -> `Spend`, `window_for()`, `parse_time()`, `PARK_MARKER`. The spend guard. |
| `src/vellum/reconcile.py` | `reconcile()` -> `Tick`, `run()`, `Action`, `Observed`, `read_observed()`, `corpus_answer()`, `question_terms()`, `_Reconciler`, `ACTION_KINDS`. The stateless reconciler. |

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
`DroppedScenarios` before it touches git, so a refusal costs no history walk
and one run names every offending fence rather than one per invocation. But
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

`scan_file()` takes the parsed `SpecFile`, not its text. Extraction hands it the
one `iter_spec_files()` already built, which is the same object `lint_tree()`
walks — so the fences the two commands judge are the same set by construction
rather than because two `parse_spec_text()` calls happened to agree. Only
`scenarios_in()` still parses text, because *its* caller has text: the history
walk reads blobs out of git, not files off disk.

**Measured before it was changed, not assumed:** every gherkin fence at every
spec commit on waviisoft/vellum-intent main parses today — 16 spec commits, 542
markdown file-revisions, 196 fences, zero `GherkinParseError`. So no historical
tree currently leans on the walk's tolerance, and `suite.json` extracted at the
pin (`666df3d3`) and at intent main (`d1e91e0f`) is byte-identical before and
after this change. The tolerance is kept for what it protects against, not for
anything it is carrying now. Re-measure it rather than re-reading this line: the
script is a walk of `spec_commits()` calling `parse_block()` on each fence, which
is `_scenarios_at()` with the failures counted instead of dropped.

**The refusal is keyed on the drop, not on `GherkinParseError` and not on lint.**
Two constructs cost the suite scenarios and both refuse: a fence that does not
parse (`GH001`) and a `Rule:` with children (`GH010`), whose nested scenarios
`parse_block()` deliberately does not admit — that block parses perfectly and
still hands back fewer scenarios than a stock runner executes, which is the same
silent absence reached the other way. Keying on the exception alone left that
half open: `bad-rules` extracted exit 0 with three scenarios missing while lint
rejected the same tree with `GH010`, the exact defect waviisoft/vellum#7 closed
for the parse half.

The scope is the harm and nothing wider, and the boundary is load-bearing in
both directions. A `Rule:` holding **no** scenarios does not refuse: it fails
lint on its own account and costs the suite nothing, so `rule.scenarios` is
tested before a `BlockError` is made. Nor do findings that drop nothing — an
unresolved link, a missing `@id:`, `GH002` on a fence declaring no scenarios.
Widening this to "a tree lint rejects does not extract" would make `extract` a
second `lint` and stop the harness on trees whose suite is complete;
`TestFindingsThatDropNothingStillExtract` and
`test_a_rule_holding_nothing_does_not_refuse` in `tests/test_suite.py` are what
hold the line. `BlockError.code` carries `GH001`/`GH010` so the refusal points
at the finding rather than restating it.

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
invocation can still do it, so the tests assert `1` rather than "non-zero" — a
drift to 2 would tell a caller the wrong thing about its own invocation and
still pass a non-zero assertion. The message names the file, the offending
block's opening fence line, and the fault line inside it — `<file>:<line>:
gherkin block at line <n> …` — because "which block" and "where in it" are
different questions and the second one alone does not tell you which fence to
go and look at.

**Every word of a refusal goes to stderr, and an existing output file is not
touched.** `-o -` puts the suite on stdout, so a diagnostic printed there would
be parsed as part of the JSON by `extract … -o - | jq`; and `on-spec-merge.yml`
extracts over `ledger/suite-<sha>.json` on a main that may already carry one, so
truncating it on a refusal would take the tree from "no answer" to "a wrong
answer already committed". Both are properties nothing forced — `run()` returns
before it opens the file at all — so both are asserted directly:
`test_a_refusal_writes_nothing_to_stdout` and
`test_an_existing_output_file_is_left_exactly_as_it_was`.

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
`scan_file()` reads the same `Rule.scenarios` count to decide whether extraction
refuses, so the finding and the refusal cannot disagree about how many.
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

## The pipeline commands

`spec/features/spec-pipeline.md`: "Pipeline logic lives in the product CLI, and
forge workflow bodies are single-command shims over it — minting is `vellum
mint`, the divergence gate is `vellum backpressure`, the pin close is `vellum
pin advance`." All three arrived in one wave, absorbing what
`adapters/github/on-spec-merge.yml` and `spec-ci.yml` used to run as shell.

The reason is testability, and it is the whole reason: logic in a workflow body
can only be exercised by running that forge, so the pipeline's behavior was a
deployment property nothing could grade. Driven as commands it is a PASS-able
one (`spec/features/scenarios-and-harness.md`). That the forge's trigger
*causes* the invocation stays a deployment property, and no harness may
re-implement the workflow to grade it.

**Exit codes are a contract: 1 is an answer you will not like, 2 is no
answer.** 0 worked or decided there was nothing to do. `suite` already used 1
for "the tree is the problem" and `SpecTreeError`/`LedgerError` already meant
2, and the pipeline commands were fitted to that rather than inventing a third
scheme — which is why `vellum pin advance --to spec-v1` exits 2 (that is
`LedgerError`, unwrapped on purpose) while `--to <a commit that is not a
version>` exits 1.

The line matters most for `backpressure`, and it is why `BackpressureError`
exits **2** rather than 1: the moment `spec-ci.yml` drops its
`continue-on-error`, 1 has to mean "blocked" and nothing else. Sharing it with
"I could not find the config" would make a renamed `.vellum/config.yaml` block
every spec merge while reading as backpressure — a red nobody can find the
cause of. `test_blocked_is_the_only_thing_that_exits_one` pins it. Tests assert
the number, not "non-zero".

### `vellum mint`

**Three questions, one `rev-list`.** `spec_commits()` is `rev-list
--first-parent --reverse <ref> -- <prefix>`, and mint reads its last two
entries and its length: is this a version (the list's last entry), what is its
baseline (the one before), what is it called (`spec-v<len>`). The workflow ran
that walk twice and reasoned about the relationship in a prose comment; reading
all three off one list makes the guard and the baseline agree by construction.
Do not "optimise" this into a `-1` query plus a separate count — that is the
shape whose disagreement the comment was worrying about.

**Both no-ops exit 0, and that is preserved behavior, not a softening.** A
commit that does not touch `spec/` (a `workflow_dispatch` on a ledger commit, a
racing merge) and a replay both exit 0 having written nothing, exactly as the
guard step's `proceed=no` left the job green. The guard exists so the steps
that are *not* idempotent — tagging, filing issues, pushing — are skipped, not
so the run reddens; reddening a re-run of a deliberately idempotent job
(decision D11) trains people to ignore red. **A caller reads `minted`/`reason`
from `--emit`, never the exit code.** `adapters/github/on-spec-merge.yml` gates
every downstream step on `steps.mint.outputs.minted == 'yes'`.

**A shallow clone is the one refusal, and it is exit 1.** Measured, not
reasoned: a `--depth 3` clone of the real intent repo counts **1** spec commit
where the full history counts 16, so the sixteenth version would be minted as
`spec-v1` — a name already used by the seed commit — with a baseline naming the
truncation point. All three answers are wrong below the graft, which is why the
check is asked before anything is decided.

**The head commit message never reaches the CLI.** It is attacker-supplied text
— anyone who can land a commit on main writes it — and its only use is
annotating the decorative tag, so tagging stayed in the workflow where the
message is already passed through `env` rather than `${{ }}`. `vellum mint`
does not read it, and the only message it writes (`ledger: open <name>`) is
derived from what it computed itself. Do not "simplify" by having mint tag.

**`--commit` exists and the adapter does not use it.** It stages and commits
under the fixed message and never pushes. `on-spec-merge.yml` commits itself
instead, because the `suite-<sha>.json` extracted after minting belongs in the
same commit as the record it describes. The flag is the right contract for a
caller that mints and does nothing else, which is what the `workflow_call`
shims will be.

**An unreadable record at the sha's own filename counts as a replay.**
`find_record()` skips a record it cannot parse; treating that as "no record"
would mint straight over it. `_existing()` checks the direct path too.

**Its invocation failures exit 2, not 1.** An unresolvable `--ref` and an empty
`--emit` both used to report as 1 — the code that means "a shallow clone; this
cannot proceed". They are "no answer", which is 2 everywhere else in this CLI,
and `resolve()` is now called on its own so the two cases can be told apart
from a history that genuinely cannot be walked. The split is not pedantry: two
different things sharing a number is how a caller learns to read only
"non-zero", and `backpressure`'s gate is the thing that cannot survive that
habit. The shallow refusal keeps 1, and there is a test either side of the
line.

### `vellum backpressure`

Counts ledger records whose state is neither `shipped` nor `superseded` —
those two are the only ways a version leaves the window — and exits 1 at or
past `budgets.divergence_cap`. **At, not past**: the question is "may another
version land", and `@id:backpressure-blocks-merge` states a cap of 3 with 3
unshipped versions blocking the next merge.

**It cannot see the whole window the spec describes.**
`spec/features/spec-pipeline.md` counts approved-but-unlanded spec *PRs*
alongside landed-but-unshipped versions. An open PR is forge state, not
repository state, so `--pending <n>` takes that count from a caller that can
see the forge, and the report says plainly when only the ledger half was
measured. Do not reach for a forge API here to close the gap.

**It reads states, not release pointers, and today that means it blocks
everything.** Nothing has ever set a record to `shipped` — releases do not
exist yet, `ledger/releases.yaml` carries `spec_conformed: null` — so all
eleven records on intent `main` count as unshipped against a cap of 3.
`spec-ci.yml` therefore runs the real command with `continue-on-error: true`
and reports; arming it before releases exist would block the very merge that
lands the release machinery. Delete that line when shipped versions actually
leave the window — tracked as `waviisoft/vellum-intent#41`, which schedules
arming into Wave F. The hold is a scheduled item, not an intention living in a
comment. The `set -o pipefail` beside it is load-bearing: without it
the step takes `tee`'s status and arming the gate produces a check that can
never close.

**A name-keyed record is reported, not counted.** `ledger/spec-v1.yaml` style
records carry `spec_version: spec-v1`, which is not a sha and not a version
this CLI recognises; counting one would let a pre-commit-era leftover hold the
gate closed. The intent repo's own migration is done
(`waviisoft/vellum-intent#22`) — measured on `main`, all eleven records are
sha-keyed — so this guard is protecting a fresh installation, not that one.

**Its report is a workflow-command channel, so two fields are narrowed
before they reach it.** `spec-ci.yml` pipes `report()` straight into
`$GITHUB_STEP_SUMMARY`, and a record's `state` and `name` come from the intent
repo's `ledger/`, which anyone who can land a merge there writes. A newline in
either starts a line of its own, and a line of its own is all `::error`,
`::notice` or `::add-mask` needs. `name` now goes through the same `TAG_RE`
check `pin.py` gives the same field, and `state` is narrowed to its first
whitespace-separated token. Order matters in one place: **settled-ness is
decided on the whole value and only then is the value narrowed**, so a state of
`shipped` plus junk still counts as unshipped. Collapsing first would let a
crafted record walk out of the divergence window, which is the one direction a
gate must not fail in. `TestTheReportIsNotAWorkflowCommandChannel` covers both
fields and that ordering. `sha` needs nothing: `SHA_RE` already accepts only
hex.

**`--strict` is for wherever the gate blocks.** By default a ledger file that
cannot be read is reported and *not counted*, which makes the window narrower
than the truth — a gate failing open on corruption. That is the right default
for a report (a name-keyed leftover is a migration to do, not a merge to
block) and the wrong one for a gate, so `spec-ci.yml` passes `--strict` and
gets a refusal instead. "I could not read three records" must never arrive as
"there is room for three more versions".

### `vellum pin advance`

**Two sufficient answers, not one checked twice.** A sha is a version if a
ledger record exists for it *or* it is a spec-touching commit in the intent
checkout's first-parent ancestry. The ancestry half is what makes paired
landing work — the pin advances to a commit whose record may still be a minute
away — and the ledger half is what lets a shallow or stale checkout still
vouch. There is no `--force`: a pin naming a non-version is the failure the
command exists to prevent.

**The file is edited a line at a time, and the edit is verified.**
`.vellum/product.yaml` is mostly load-bearing comments; a `safe_load`/`safe_dump`
round-trip deletes every one of them and reflows what it keeps. `_rewrite()`
replaces the value on the `commit:` (and `name:`) line inside the `pin:` block
and touches nothing else, then `advance()` re-parses the result and compares
every top-level field against the original, raising with the file unchanged if
anything outside `pin` moved. Two `pin:` blocks, or two `commit:` lines, refuse
rather than guess.

**`pin.name` follows the commit.** Decoration, but a `name` reading `spec-v16`
beside a commit that is a different version is decoration that has become a
lie, and the reader it misleads is the reader it was for. It is set from the
ledger record's name, or `null` when there is none. A pin file with no `name:`
key is not given one — only lines already in the block are rewritten.

**The rewrite is scoped to the pin block's own indent, and it has to be.**
`_rewrite()` walks from `pin:` to the next column-zero key, and it used to
rewrite *any* line matching `^\s+(commit|name):` at any depth. YAML requires a
block scalar's body to be indented deeper than its key, so a `note: |` holding
the word `name:` in its prose got that line rewritten — and **every check in
`advance()` passed**: `drifted` skips `pin` entirely, and comparing the pin's
*key set* cannot see a changed value. Found in review, with a working repro.
The fix is two halves and both are load-bearing: `_rewrite` matches only at the
block's own indent, and `advance()` now compares the pin's other *values*, not
just its keys. `TestTheRewriteCannotReachNestedContent` covers it.

**A ledger record's `spec_version` is the load-bearing field, and it was the
unchecked one.** `verify_version` validated `name` and not `commit` — the
field that becomes `pin.commit`, which product CI hands to `git checkout`. Two
things let it through: `find_record` returns an exact filename hit *without
parsing it*, so the `SHA_RE` check on its glob branch never ran; and
`verify_version` returned `full or recorded`, so whenever the intent checkout
could not resolve the sha itself the record's own text became the answer. A
crafted `ledger/<sha>.yaml` holding `spec_version: "$(id > /tmp/pwned)"`
reached `pin.commit` verbatim, at exit 0. Found by the bench with a working
repro, on both reviewers' lists independently. `_from_record()` now asks two
things of the field — that it parses as a sha, and that it agrees by prefix
with the sha that reached it — and refuses either way. The disagreement half is
not decoration: `find_record`'s glob branch matches on prefix and so cannot
disagree, but the filename branch never compared them, so `ledger/<A>.yaml`
saying `spec_version: <B>` pinned B while the operator asked for A. Both
refusals are `PinError`, exit 1, matching every other malformed-record refusal
in that block — a record being malformed is one answer, not two exit codes
depending on which field is wrong.

**The pin file's newlines and the pin block's end are both preserved
literally.** Two smaller ones from the same review. `read_text`/`write_text`
translate newlines, so advancing the pin in a CRLF file reflowed the *whole
file* to LF — a one-value change arriving as a whole-file diff, which is
exactly what the line-at-a-time edit exists to avoid; the read and the write
now go through `open(..., newline="")` and `_split()` keeps each line's `\r`
off the match and puts it back on the rewrite. (`open`, not `Path.read_text`:
that only grew a `newline` argument in 3.13 and the floor here is 3.10.) And
the scan used to end the block on `^[A-Za-z_][\w-]*:` — a *Python identifier*,
not a YAML key. `2024-report:` and `"quoted":` are both valid at column zero
and matched neither, so the scan ran on into the next block and refused a
well-formed file with `pin.commit appears twice`, naming a `commit:` belonging
to something else. It ends on indent now: content at column zero ends the
block, whatever it is called. `TestFilesThatAreNotWhatTheScanAssumed` covers
all four.

**A ledger record's `name` is not trusted, because `ledger/` is not ours.**
It is interpolated into YAML by f-string, and it arrives from the intent repo's
`ledger/`, which anyone who can land a merge there writes. A `name` of
`"spec-v9\n  ref: forged"` wrote a *second key* into the pin block, and neither
check caught it — the key set grew by one the comparison was not looking for,
and the pin is what the product's CI fetches the spec at. `_decorative_name()`
now validates against `TAG_RE` and drops anything else (a name is decoration,
so dropping beats raising), and `_rewrite` refuses a multi-line value outright.
Two layers on purpose: the second is what protects a future caller that reaches
`_rewrite` another way.

**`find_record` short-circuits without parsing, so `pin` must guard the read.**
An exact filename hit returns immediately (`ledger.py`), so a corrupt
`ledger/<sha>.yaml` arrives at `verify_version` unparsed and `yaml.safe_load`
raised straight out of `main()` — a traceback, not one of the two exit codes.
`mint.py` already handled the same case; `pin.py` did not until review.

**A test that unsets `VELLUM_INTENT_REPO` disarms the conformance job.**
`run_cli` calls `main()` **in-process** and `_candidate()` reads the variable at
call time, so a bare `os.environ.pop` leaks into every module discovered after
it — and discovery is alphabetical, so `test_pin` precedes `test_suite`.
Measured: `test_suite` alone with the variable set runs 77 tests in 14s with
zero skips; with a leaking `test_pin` in front of it, 8 skip in 3s. The whole
point of running the suite inside the `conformance` job is that those skips are
not a hole, so this made the job green and hollow. `addCleanup(os.environ.pop,
...)` is the same bug by another route — it *deletes* rather than restores, so
it only bites in the shape where the variable was set. Use
`unittest.mock.patch.dict`. `PinCase` now asserts `os.environ` is unchanged
after every test in the file, which is what caught the second instance.

**`VELLUM_INTENT_REPO` has one definition now.** It lives in
`src/vellum/config.py` and `tests/support.py` re-exports it. `pin advance`
reads the same variable the pinned-tree tests do, deliberately: an installation
should have one answer to "where is the intent repo checked out", and two
spellings is how the tests and the command come to disagree.

## The mechanical guards

Five read-only commands, each reading neutral inputs and answering one
question: `verify boundaries`, `verify deps`, `verify exit-duty`,
`ledger verify` and `budget`. None writes anything and none reaches a forge.
They arrived in one wave against the five scenarios that had no product behind
them at all — `@id:implementer-cannot-touch-harness`,
`@id:unlisted-registry-fails`, `@id:exit-duty-required`,
`@id:chain-resolution-fails-release` and `@id:global-cap-parks-queue`, each of
which the intent repo's harness reports CANNOT RUN YET against a named
capability in `harness/support/adapter.py`.

**`.vellum/product.yaml` has two readers now and one writer.**
`src/vellum/product.py` reads it (`write_boundaries`, and only that — a schema
written ahead of a reader is a second place for the shape to drift);
`src/vellum/pin.py` is still the only thing that *writes* it, a line at a time,
and now imports `product_path` from `product.py` rather than defining it. Do
not grow a writer in `product.py`: the line-level rewrite in `pin.py` and the
verification around it are what keep that file's comments, which are its
documentation.

**A guard's own error is 2, always.** `BoundaryError`, `ChainError`,
`BudgetError`, `DependencyError` and `ExitDutyError` all map to 2 in `main()`,
in one `except` clause. 1 is reserved for the finding each guard exists to
report, because 1 is what a workflow blocks a merge on — a mistyped `--role`
arriving as "this PR wrote outside its trees" is the same unfindable red the
`backpressure` split exists to prevent. Every guard's tests assert the number,
not "non-zero".

**An allowlist has one dangerous direction, and every judgment call leans the
other way.** For `verify boundaries`: a role `.vellum/product.yaml` does not
declare is refused rather than defaulted — neither default is safe, an empty
list faults every honest PR and an unrestricted one passes every dishonest one;
`normalise_tree()` refuses `""`, `.`, `/` and `../..`, each of which admits
every path in a diff under a naive prefix test; and `under()` compares path
*components*, so `src` does not admit `srcs/evil.py`.
`TestBoundariesThatWouldTurnTheGuardOff` covers the four.

**`changed_paths()` passes `--no-renames`, and that is load-bearing.** With
rename detection on, `git mv harness/steps.py src/steps.py` is reported as one
path — the new one — and the write to the tree the file *left* disappears from
the diff the guard reads.
`test_moving_a_file_out_of_a_protected_tree_still_names_that_tree` pins it.
`-z` is there for the same class of reason: a path carrying a newline or a
quote is otherwise emitted quoted and escaped, and a guard that unquotes it
wrongly reads a path that is not the one in the tree.

**The comparison goes through the merge base, and falls back *wider*.** Diffing
two refs directly also reports, inverted, everything that landed on `base`
since the branch left it — so `main` gaining a harness commit while a PR is
open faults the implementer for somebody else's work
(`test_a_commit_landing_on_base_is_not_charged_to_the_branch`). Where no merge
base exists — a shallow CI clone, unrelated histories — `changed_paths()` reads
the direct diff, which is a *superset* of the branch's own changes, and returns
`basis="two-dot"` so the report can say so. A guard may be wrong loudly; it may
not be wrong quietly.

**`verify exit-duty` deliberately does not check *which* note changed, and this
area note is the counter-example.** `src/vellum/` is documented by
`.vellum/memory/areas/cli.md`; there is no derivation from `src/vellum` to
`cli`. Areas are editorial groupings the librarian names, so a guessed mapping
would fault correct PRs — the note it wanted exists under another name — and
pass incorrect ones. The mechanical half is enforced mechanically and the
editorial half stays where `spec/features/memory-and-briefings.md` puts it: the
verifier reviewing the memory diff. The report says which of the two it
checked, so a green run is not misread. A path inside `AREAS_TREE` is never
also counted as source, so an installation that lists `.vellum/memory` in
`product.trees`, as this repo does, cannot make the memory diff satisfy itself.

**`--src` goes through `normalise_tree()`, the same function a write boundary
uses.** A source tree is an allowlist read the other way round — a path is
source when it lies *under* one — so a malformed entry turns the guard **off**
rather than widening it, and that is the quieter failure and therefore the
worse one. `--src .` and `--src src/` both left `under(p, tree)` false for
every path in the diff, so exit duty was never owed and the run exited 0 while
looking configured; the default `--src` was fine, which is exactly why nothing
noticed. Normalising trims `src/` and `./src/` to `src` and refuses `""`, `.`,
`/` and `../..` as `ExitDutyError` (exit 2) — the same four `normalise_tree()`
already refused for `verify boundaries`, for the same reason, now stated once
in one function. `TestSourceTreesAreNormalised` covers them.
`test_a_note_is_memory_first_and_never_also_source` used to pass `--src .` and
so asserted nothing at all; it names `.vellum/memory` now, a tree that really
does contain the note's path, so the memory-first rule is what does the work.

**`ledger verify` scopes two checks to the record and three to the cut, because
the spec's sentence does.** "The ledger guard fails a *release* whose chain does
not resolve." A work item with no PR, and a `satisfies:` naming a scenario the
suite does not have, are wrong the moment they are written, so they are asked of
every record. Coverage — "a criterion no work item claims" — and certification
are asked only of waves a cut names: an open wave legitimately has criteria
nothing claims yet, that being what an unplanned wave *is*, and asking it
everywhere would fault every version between approval and its work plan.
Measured on intent `main` at the pin: 11 records, 0 cuts, no findings, seven
records unchecked for want of a suite file.

**Coverage asks only about the criteria the version *armed*.** `armed` is the
scenarios `ledger/suite-<sha>.json` dates to that very commit
(`scenario["version"] == sha`). The rest of the suite belongs to earlier waves
and was claimed — or not — there; re-faulting it at every cut makes the guard
noisier the longer an installation runs.
`test_only_the_criteria_this_version_armed_are_asked_about` pins it.

**Certification has no field, so `uncertified-wave` is a proxy and says so.**
`vellum.ledger.RECORD_KEYS` has no certification key, and
`harness/support/adapter.py` says the same thing under `certification-runner`:
"the ledger record schema in vellum.ledger has no certification field at all".
The check reads `state in CERTIFIABLE_STATES` instead, and both `Finding.kind`
and `Chain.report()` label it as the proxy it is. **Do not quietly promote it to
a real certification check** — that needs a spec slice (a `certification:` field
on the record or the work item), and a guard that inferred one would be
enforcing its own reading rather than recorded fact.

**An absent `suite-<sha>.json` is *unchecked*, not passed.** Seven of the eleven
records on intent `main` have no suite beside them, so refusing outright would
make the command unusable there. A link nobody looked at is not a link that
resolved, so the report says so and `--strict` refuses instead — the same split
`backpressure --strict` draws, and for the same reason: "I could not resolve
three records" must never arrive as "the chain is sound".

**`budget` attributes spend by the record's `approved` time, because that is the
only clock the ledger has.** `COST_KEYS` is attempts, tokens, usd, executor — no
timestamp — so a period window has to hang off something, and the record's
approval is it. A record whose `approved` cannot be read is counted **inside**
the window: a cost this cannot prove belongs to an earlier period is one the cap
must not let through, and `Spend.undated` names every record treated that way.
PyYAML turns an unquoted timestamp into a `datetime` before `parse_time()` sees
it — the same trap `_is_iso_date()` in `lint.py` exists for — so both shapes are
handled and both are tested.

**The two caps read two different words in one behavior, and both are read as
written.** "Exceeding a per-item cap parks the item; **hitting** the global cap
parks the queue" — so the per-item test is `>` and the queue test is `>=`. The
queue half is the same reading `backpressure` gives its own cap: the question is
whether the next work item may run, and the scenario states it as a cap of $100
with $99 recorded.

**A missing `per_item_usd` leaves that half unchecked and the report says so; a
missing `period_usd` or `period` is an error.** Not a softening of "missing is an
error, not a default" but the same rule applied per cap: `period_usd` is the
command's headline job, and the harness's own sandbox config
(`harness/support/sandbox.py::write_config`) declares no `per_item_usd` at all,
so refusing on it would make the ordinary shape unrunnable. The absence is
stated in the output rather than defaulted in the code.

**`vellum budget --json` puts the payload on stdout and every diagnostic on
stderr**, so `| jq` still parses a parked run — the property `suite extract -o -`
already had, asserted directly by
`test_a_parked_run_writes_nothing_but_json_to_stdout`.

**A non-finite cost is unreadable, and `_number()` reads it as $0.00 like any
other.** NaN was the exploitable one: it poisons the window's sum, and
`committed >= cap` is *false* for NaN, so a single `cost.usd: .nan` in a ledger
record — which the threat model treats as attacker-influenceable — turned the
period cap off entirely while the report still read OK. $130 of real in-window
spend against a $100 cap exited **0**. `inf` was never exploitable (it summed to
inf and parked, which is fail-closed) but it was not a measurement either, and
letting it through put a value no caller can use into the total, the report and
`--json` — `json.dumps` spells the two `NaN` and `Infinity`, neither of which is
JSON, so the stdout/stderr split above was being undone by the payload itself.
`int(float('nan'))` also *raises*, where `attempts` and `tokens` are read, so an
unreadable count there took the whole measurement down instead of listing the
item. Both go to 0.0. Zero stays the conservative reading only because the item
is still **listed** — the poisoned item appears in the report at $0.00 beside
its attempts — and real spend is untouched and still parks the queue. Note the
deliberate change of behavior: an `inf` record no longer parks on its own.
`--projected` is a caller's number rather than repository data and is *not*
coerced here; `--projected nan` is still an unguarded way to say "no cap", and
is an invocation-surface question rather than this one.

**`--projected` and `--pending` are the same idea twice.** Where a guard needs a
number only a forge or a not-yet-built runner can supply, the caller passes it
and the report says plainly when it was not supplied. `budget` cannot project a
certification cost because certification does not exist. Do not reach for a
forge API to close either gap.

**`deps` reads `pyproject.toml` without `tomllib` on 3.10, and refuses rather
than under-reporting.** `tomllib` arrived in 3.11, the floor here is 3.10, and
the dependency policy is itself the reason not to add `tomli`. So
`_scan_toml_arrays()` is not a TOML parser with gaps: inside the tables it cares
about, a key whose value it cannot read *exactly* raises `DependencyError`
(exit 2, "no answer") rather than being skipped into a shorter answer that reads
like a pass. `TestTheTomlFallbackAgreesWithTheRealParser` asserts it against
`tomllib` on 3.11+, including on this repo's own `pyproject.toml`. **Measured,
not assumed:** the whole suite was run under a real `python3.10` — 486 tests,
green, with `TestReadingPyproject` executing through the fallback and only the
twelve agreement tests skipping — and both readers return byte-identical
requirement lists for this repo's `pyproject.toml` and `requirements.txt`.

**The two TOML readers are held to the same *strictness*, not just to the same
answer.** Having the real parser is what made `_from_parsed()` the *weaker* of
the two: it admitted an array only when every element was a string and dropped
it whole otherwise, so a `[dependency-groups]` list mixing a requirement with a
valid PEP 735 `{include-group = "..."}` entry — TOML `tomllib` parses without
complaint — took the string requirements down with it. `vellum verify deps`
exited **0** on a tree declaring `evil @ https://evil.invalid/x.tar.gz`, on the
*default* interpreter, while 3.10 refused the same file. Two readers that
return equal dicts on every input both accept can still disagree like that, and
`agree()` could not see it because the input it feeds is input both accept —
`agree_refuses()` is the missing half. Both readers now raise on a non-string
inside a table they care about, and both name the string requirements they read
beside it, because those are exactly what a silent drop would have hidden. A
cared-about table may still hold ordinary scalar keys (`[project]` always has
`name` and `version`): the rule is about an array's *contents*, and a non-array
value is ignored by both, which is what the fallback's `_ARRAY_RE` does anyway.
`TestAMixedDependencyGroupEndToEnd` asserts the whole command on **every**
interpreter rather than skipping below 3.11 — a regression that put the drop
back would otherwise only show up in the class 3.10 skips.

**Registry hosts are compared exactly, after parsing.** `host_of()` goes through
`urlsplit().hostname` rather than a regex, so it strips userinfo and port:
`https://pypi.org@evil.invalid/simple` is a request to `evil.invalid`, and
`pypi.org.evil.invalid` is not `pypi.org`. Both are the shapes a substring test
lets through, and there is a test for each. A policy entry naming no host is
refused rather than dropped — dropping shortens the allowlist, which fails
*closed*, sending a reviewer hunting a supply-chain finding that is really a
typo in policy.

**`-r` includes are followed, and contained.** The path is text out of the
repository, so following it unguarded makes the guard a file-read primitive
aimed by whoever writes the manifest. `_contained()` refuses anything resolving
outside the checkout, and a cycle of includes terminates on a `seen` set.
`requirements*.txt` is a glob rather than one filename because a dev dependency
is executed on a machine holding credentials just as a runtime one is.

**pip options apply to the file, not to the lines below them.** An
`--index-url` written after a requirement still serves it, so
`read_requirements_txt()` re-attributes every plain requirement once the whole
file's options are known. `--extra-index-url` and `--find-links` are counted as
registries in use for the same reason: every plain requirement in that file may
be served from them.

**`registries: [npmjs.org]` was founding-template residue, and it is gone.**
`harness/NOTES.md` finding 2 recorded that the policy admitted npmjs while the
product it governs is a Python package — so under the policy as written, this
repo's own dependencies were from an unlisted registry. It reads `[pypi.org]`
now, and `test_the_governed_products_own_dependencies_pass_the_live_policy` pins
that this guard's first real run against this repo is not a false red.

**A test that changes `VELLUM_INTENT_REPO` disarms the conformance job, and
`verify deps` reads it too.** `run_cli` calls `main()` in-process and `_verify`
reads the variable at call time, so the landmine `test_pin` already documented
now has a second door. `DepsCase` asserts `os.environ` is unchanged after every
test in the file, and the one test that needs the variable absent uses
`unittest.mock.patch.dict` — which restores — never `addCleanup(os.environ.pop)`,
which deletes.


## Certification and leases

`spec/features/ledger.md` gives a work item two fields that are about a *run*
rather than about the work, and they behave nothing like each other:
`certification: {sha, run, at, result}` and `lease: {executor, taken, expires}`.
The schema lives in `src/vellum/ledger.py`; the command is
`src/vellum/certify.py`.

**Both fields are optional, and the split that makes them optional is worth
stating precisely: `new_item()` writes them as null, `dump()` never *inserts*
them.** The constructor sets defaults — the way a record writes `line` and
`locks` — so recording the first certification edits a key that is already
there. The serialiser only ever reorders what it was handed, so a work item
written before this wave keeps every byte of its shape. Measured, not assumed:
all twelve sha-keyed records on intent `main` re-dump byte-identical, and
`TestOptionalFieldsAreBackwardCompatible` in `tests/test_ledger.py` carries an
old-shape record as literal text and asserts the same thing on every run.
Write that fixture as text, never through `new_item()` — a fixture built by the
constructor gains the two keys and silently stops being the thing under test.

Note what the real ledger could *not* verify: **no record in the intent repo's
history has ever carried a work item at all** (14 ledger commits, zero items),
so the real round-trip exercises `RECORD_KEYS` and never `ITEM_KEYS`. The
literal fixture is the only coverage of the item half. Re-measure rather than
re-reading this line: the check is `load()` then `dump()` over
`ledger/*.yaml`, skipping `NOT_A_RECORD`.

**Certification binds to a sha, and that is the whole of both scenarios.**
`@id:no-self-certified-merge` says a PR whose in-session checks pass, with
nothing recorded for its head, does not merge — so the default is deny and
in-session results are not an input the command even accepts.
`@id:new-commit-invalidates-cert` says a certification does not survive a new
commit — implemented as "the head is not the certified sha", which needs no
notion of *later* and so cannot be fooled by a force-push that rewrites what
"subsequent" means. `certification_authorizes()` returns `(bool, reason)` and
every non-green shape — absent, red, another sha, a corrupt field — is the same
answer with a different sentence.

**A denial is exit 1, not 2.** It is an answer, and a merge gate that cannot
tell "this head is uncertified" from "that is not an intent checkout" reports
the second as the first the moment a path is mistyped. A corrupt certification
field denies rather than refusing to answer, for the same reason: "no green
certification exists at this head" is *true* of a malformed block, and the spec
says so in as many words — uncertified "whatever the record says it once was".

**`--sha` and `--head` take the full forty; an abbreviation is refused, not
resolved.** `SHA_RE` accepts git's 7-character floor because a human types a
*version* to look a record up, and `find_record()` catches an ambiguous prefix
and reports it. This is the other kind of sha: the one an authorization is
decided on. A prefix names a set of commits, so a certification stored or
checked against one authorizes every commit in that set — including commits
nobody proved anything about. `FULL_SHA_RE` and `parse_certified_sha()` keep the
comparison exact, and `TestAbbreviationsAreRefused` pins it at the CLI *and*
below it, so it cannot be reintroduced in the library.

**The ledger does not know a PR's head, so the caller supplies it.** A work
item records the PR's *number*, not its head commit. `certify record` therefore
cannot check that `--sha` is the head, and does not pretend to; the comparison
the spec asks for happens in `certify check --head`, which is given the head by
a caller that can see the forge — the same division as `backpressure --pending`
and `budget --projected`. Do not reach for a forge API to close this.

**Recording a red exits 0.** The recorder has not failed at anything; the
denial is `check`'s to give. A red that could not be written would leave the
ledger unable to say a run happened at all.

**Certification is on the work item; a *wave* still has none.** `ledger verify`
goes on using its record-state proxy for "a cut naming an uncertified wave" and
its docstring now says why that is still right: the spec binds certification to
a merge candidate's sha, cuts name waves, and summing item certifications would
be inventing the aggregation rule rather than reading one. Do not "finish the
job" in `chain.py`.

**The lease has helpers and no command, deliberately.** Nothing in the spec
asks a scenario of the lease that a caller could drive today —
`@id:fire-and-collect` turns on "an executor mid-run on a claimed work item",
and the party that claims, reports and lapses is the reconciler (Wave E). So
`take_lease()`/`clear_lease()`/`active_lease()` land with the schema and the
tests drive them directly. `active_lease()` is where "treats an expired lease
as no lease" is resolved, once, rather than in each caller: an item is claimed
exactly when it returns something, and "mid-run means holding an unexpired
lease" is the same sentence read the other way.

Two judgment calls inside it, both leaning the same way. Expiry is
**exclusive** — a lease is held *until* it expires, so one expiring exactly now
is not held. And a lease whose `expires` cannot be read is **absent**, like an
expired one: reading it as held strands the item behind a claim no clock can
retire, which is the failure the expiry exists to prevent, while reading it as
free costs at most a second executor restarting from the last pushed commit —
which is what a lapsed lease already means here. `new_lease()` refuses an
unreadable expiry at write time so that direction is a mistake you are told
about rather than a claim that silently never happened. `clear_lease()` writes
null rather than deleting the key: a released claim and a field that was never
there read differently in a diff.

**`parse_time()` moved from `budget.py` to `ledger.py`, and `budget.py`
re-exports it.** Lease expiry reads the ledger's own timestamps and so does the
spend window; two definitions of how this project reads a recorded moment is
how those two come to disagree. It sits next to the `_now()` that writes them.
`from vellum.budget import parse_time` still works, which is what `cli.py` and
`tests/test_budget.py` do.

## The reconciler

`vellum tick <intent-checkout>`, in `src/vellum/reconcile.py`. One pass of
`spec/features/orchestration.md`'s loop: read desired state, read observed
state, compute the convergent next actions, act. Not a daemon and not a watcher
— it holds nothing between runs, which is what
`spec/decisions/2026-08-28-reconciler.md` means by stateless.

**Desired state is repository state; observed state is supplied.** The ledger,
the spec tree and `releases.yaml` are read off disk. Everything only a forge can
see — which issues exist, which questions are open, which clarify PR merged,
what direction was recorded — arrives as `--observed <file>`. That is the same
division `backpressure --pending`, `budget --projected` and `certify check
--head` already draw, and it is the reason the reconciler's behavior is a
PASS-able property instead of a deployment one. **Do not reach for a forge API
here.** The command reaches no network and takes no credential.

**Every action carries whether the tick performed it.** `ACTION_KINDS` maps each
kind to `taken`. The four writes are `commit-plan`, `supersede`, `claim` and
`record-direction`, all of them ledger edits; `plan`, `file-issue`,
`open-question`, `answer-question`, `draft-clarify`, `close-question` and
`dispatch` are decided and emitted for the caller, and `hold` is a statement
about why nothing happened. The set is closed on purpose — an action nothing in
the spec asks for is a spec question, not a new string.

**`active_lease` is asked before `take_lease`, and `_claim()` is the only place
either is reached from.** That ordering is the whole lease mutex and the Wave D
bench asked for it by name: a claimed, unexpired item is not re-dispatched, and
an expired one returns to the queue. Making `_claim` the single door means a
later caller cannot get the order wrong by writing a second one.
`TestTheLeaseIsAMutex` asserts both directions, and
`test_the_holders_lease_is_not_overwritten_by_the_tick` asserts it on the
**bytes** — the double-claim being forbidden is a write, not just a dispatch, and
a test that only checked the action list would pass while the ledger recorded
the wrong holder.

**A claim is stamped with the tick's own `--now`, not `new_lease`'s wall
clock.** A tick reconciles *as of* a moment — that is what resolves every expiry
it reads — so a claim stamped from a different clock puts `taken` after
`expires` whenever an old moment is replayed, and the lease's own window then
describes no interval at all.

**Parking is observed, not stored, and that is a reading of the reconciler
decision rather than a shortcut.** `ITEM_STATES` has no `parked`, and none was
invented. Under "the forge and the repos are the database", a parked item is
exactly one the observed state shows an open question issue against, recomputed
every tick — so nothing can go stale and no migration is owed. Whether the
ledger *should* carry a park is flagged as a spec question in the wave PR; do
not settle it by adding a state here.

**There is no mid-run channel, so direction is recorded and the lease is left to
lapse.** `directions()` writes the new briefing onto the item — `briefing` is a
real ledger field, "what the agent knew" — and does **not** clear a live lease
or dispatch beside it. The fresh run carrying the direction is the *next* tick's
dispatch, after expiry. That is `spec/decisions/2026-08-28-fire-and-collect-executors.md`
read literally, and the failure it protects against is the one that decision was
written about.

**Coalescing reads "unstarted" as state *and* lease.** `planned` with no live
lease. An item an executor is mid-run on is not unstarted; "a superseded
in-flight item stops" is a different sentence with a different remedy — the
lease lapses — and marking a running item superseded would take an executor's
work out of the ledger while it was still being done.

**Overlap is decided on criteria, and the newer version's claims are not
enough.** The scenario's `When` is an *approval*, which happens before anyone
plans against it, so a newer wave usually has no items yet. `coalesce()` unions
the newer record's own `satisfies:` with the scenarios `ledger/suite-<sha>.json`
dates to that commit — `_armed()`, the same reading of "armed" `chain.py` uses.
Deleting the armed half makes `@id:coalescing-supersedes` pass only for a wave
that was already planned, which is the case the scenario is not about.

**Ordering is ancestry, with `approved` times as a reported fallback.** Shas do
not compare, so `_newer()` asks `gitver.is_ancestor` first. A record for a commit
the checkout does not have — a shallow clone, a record copied in — cannot be
ordered that way, and the fallback is the records' own `approved` times, the only
other clock the ledger has. Without an order, two records claiming the same
criterion would supersede *each other*, or whichever the filename sort reached
first, which is a coin flip on the sha.

**A shape the observed file cannot be read in is refused, never read as "nothing
there".** `_mappings()` raises rather than skipping. The two are opposite
instructions: "nothing is filed" makes a tick file every issue again, and "nobody
holds a lease" makes it dispatch an item somebody is already running. A caller's
mistyped key must not arrive as a confident tick.
`TestObservedStateIsRefusedRatherThanMisread` covers the shapes. An *empty* file
is still supplied observed state holding nothing, which is a different fixture
and a different answer.

**Idempotence is asserted on the bytes.** A record is written only when the pass
actually changed its `dump()`, so a second tick over an unchanged world writes
nothing. The way this fails in practice is a record rewritten identically every
tick — invisible in a report, very visible in a git history —
so `test_a_second_tick_over_an_unchanged_world_writes_nothing` compares
`read_bytes()`. **Measured on the real ledger, not assumed:** a tick over
waviisoft/vellum-intent at the pin reads 12 records, reaches 12 `plan` actions
(every record is `approved` with an empty work plan — no record in that repo has
ever carried a work item), writes nothing, and leaves the directory
byte-identical.

**Exit 1 means a parked wave and nothing else.** The same discipline
`backpressure` needs: 1 is what a caller blocks on, so it is reserved for the one
answer the spec says stops a wave — "past the timebox (default 24h) the wave
parks". Every way the command can fail to answer is 2, including an unreadable
`--observed`, an unreadable `--now`, `--plan` without `--version`, a `--version`
naming no record, and `--lease-minutes 0`.
`test_a_parked_wave_is_the_only_thing_that_exits_one` pins it, and the tests
assert the number rather than "non-zero".

**Two numbers in here are the command's own and say so.** No spec sentence and
no config key gives a **lease duration** (`--lease-minutes`, default 60) or a
**corpus match threshold** (`--corpus-match`, default 1.0 — every significant
term). Both are named in the report rather than left to look derived, and both
are knobs so an installation can move them without a code change. The corpus
default is the strict end deliberately: a question wrongly bounced hands an
agent an answer that is not the answer and the mistake lands in code, while a
question wrongly escalated costs a human one glance at an issue. The threshold
is the part a spec slice would pin.

**`_conformed_baseline()` reports and never writes.** `mint` sets a record's
`baseline` to the previous spec *version*; `spec/features/orchestration.md` says
a plan is produced against the *conformed* baseline, which is
`releases.yaml`'s `channels.<ch>.spec_conformed`. Those are different commits and
the record has one field, so the tick names the conformed baseline in its `plan`
action and leaves `baseline` alone. On the real ledger `spec_conformed` is
`null`, and the action says "none recorded" rather than guessing — flagged as a
spec question in the wave PR.

**Every outside string the report prints goes through `_one_line()` first.** The
report is printed and a caller may pipe it into a step summary the way
`spec-ci.yml` pipes `backpressure`'s; a value carrying a newline starts a line of
its own, and a line of its own is all `::add-mask` needs. Executor names,
question text, briefings and paths all arrive from an observed file or from the
intent repo's `ledger/`, both written by whoever can land a merge there.
`TestTheReportIsNotAWorkflowCommandChannel` covers three of them, and the fourth
test in that class covers the *other* direction: a briefing reaches the ledger
through `ledger.dump`, never an f-string, so a briefing shaped like `ok\nstate:
superseded` does not forge a key — the failure `pin.py` had with a record's
`name`.

**`upsert_plan()` is a new public seam on `ledger.py`, and it exists because the
reconciler batches writes.** `advance()` reads, merges and writes in one call,
which is what a single `ledger advance --plan` wants; a tick holds several
records open and writes each once at the end, so it needs the merge without the
read/write around it. Both go through `_upsert_planned`, so the two cannot come
to disagree about what committing a plan means.


## Patterns worth keeping

- **Findings, not exceptions.** `lint_tree()` returns a sorted list of
  `Finding`; only `main()` decides the exit code. That keeps lint usable as a
  library (`--json`) and makes the tests assert on codes rather than on text.
- **Lint and extraction agree about a block that drops scenarios: both refuse it.**
  `extract()` in `src/vellum/suite.py` raises `DroppedScenarios` naming every
  such fence, and `run()` turns that into exit 1 with nothing written. That
  reversed the older rule — extraction used to skip a broken block so it could
  "still describe the rest of the tree" — for the reason under Landmines above.
  `TestUnparseableBlocksAreRefused` and `TestRuleNestedScenariosAreRefused` in
  `tests/test_suite.py` pin the two kinds, and each asserts the lint/extract
  disagreement itself rather than only the new code. They agree about *these*
  blocks, not about every finding: see the scope note under Landmines.
- **Untagged means "the version this would mint".** A working-tree scenario no
  tag carries gets `latest + 1` and `pending: true` (`extract()`), which is
  exactly what spec CI needs when it runs on a PR before the tag exists.
- **`open_record()` is idempotent, `advance()` accumulates.** The reconciler
  may replay an approval (decision D11), so a second `open` leaves the record
  alone. Cost is additive because every agent invocation records into the same
  work-item entry.
- **Fixed key order on write.** `RECORD_KEYS`/`ITEM_KEYS`/`COST_KEYS`,
  `CERTIFICATION_KEYS`/`LEASE_KEYS` and `_ordered()` in `src/vellum/ledger.py`
  make a state change a one-line diff and a read/write round-trip byte-stable
  (`test_a_record_reread_and_redumped_is_byte_identical`). New keys are
  **appended** to `ITEM_KEYS`, so an item that gains one gains lines at the end
  instead of moving every line below an insertion point.
- **An optional field is optional in the serialiser, not just in the reader.**
  `_ordered_present()` orders a nested mapping only where the item already has
  it, and deliberately does not copy `cost`'s `or {}` idiom — that turns absent
  and null into `{}`, and for `certification`/`lease` absent, null and `{}` are
  three different claims. Materialising a key into an old record is what would
  cost the byte-identical round-trip.

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
