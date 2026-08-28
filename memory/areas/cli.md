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
| `src/vellum/suite.py` | `extract()`, `fingerprint()`, `version_history()`, `History`, `to_dict()`. |
| `src/vellum/gitver.py` | `spec_tags()`, `markdown_at()`, `show()`. All subprocess git lives here. |
| `src/vellum/ledger.py` | `open_record()`, `advance()`, `dump()`, `RECORD_KEYS`, `ITEM_KEYS`. |

## Scenario identity

Identity is the `@id:<slug>` tag on the scenario
(`spec/decisions/2026-08-28-scenario-identity.md`), read by `_attach_id()` in
`src/vellum/gherkin_blocks.py`. **The file is only the scenario's current
home** — `version_history()` in `src/vellum/suite.py` keys by id alone, so a
scenario moving between files keeps its version. Ledger references take the
form `scenario:<id>` (`scenario_ref()`).

Lint enforces it: `GH005` missing tag, `GH006` malformed or duplicated-on-one-
scenario, `GH003` id claimed by two scenarios. Three more are unrelated to ids:
`GH007`, a `Scenario Outline` with no `Examples` rows, which parses cleanly and
then never runs; `GH008`, any feature declaring a `Background:`; and `GH009`, a
second `Feature:` in one fence. `GH003` is checked in
`_check_unique_ids()` at the `lint_tree()` level, **not** per file, because ids
are unique across the whole intent repo — putting that check back inside
`_check_gherkin()` would silently stop catching the cross-file case.

## Landmines

**The splitter is a fallback now, and deleting it still breaks the version
chain.** Since spec-v4 a fence holds exactly one `Feature:`
(`spec/decisions/2026-08-28-one-feature-per-fence.md`, `GH009`), so a
conforming block goes to the official parser whole — `_documents()` in
`src/vellum/gherkin_blocks.py` tries that first and only falls back to
`split_documents()` when the parser refuses. Do not follow that through to
deleting the splitter: `version_history()` reads every `spec-v*` tag, and
`features/certification-and-releases.md` held two Features in one fence from
spec-v1 to spec-v5. `scenarios_in()` swallows a parse error, so without the
splitter that whole fence — all three scenarios, not just the second Feature's
one — is invisible at every tag before spec-v4, and the three re-date from
version 1 to version 4, the tag where the split made them readable again.
Measured, not reasoned: stub `split_documents()` to raise and extract the
pinned tree. The count stays 20, nothing raises, nothing is pending, and three
scenarios quietly become three versions younger — which would arm scenarios the
product already satisfies. `TestBlockSplitting` in `tests/test_suite.py` covers
the splitter; `TestMultiFeatureFences` in `tests/test_lint.py` covers `GH009`.

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
that contains it, because a product repo mounts the *whole* intent repo at
`./spec` — so the tree is at `spec/spec/`, not `spec/`. Both `vellum lint spec/`
from this repo and `vellum lint spec/` inside the intent repo work. Anything
walking the tree must go through `resolve_spec_root()` first.

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
reason the spec-v1 scenarios survived the introduction of ids at spec-v2 with
their versions intact. `_Seen.consumed` stops two new scenarios inheriting from
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

**Never hard-code any fact about the pinned tree in a test** — not the version,
not the scenario count, not the file count. Use `pinned_version()`,
`pinned_scenario_count()` and `pinned_gherkin_file_count()` in
`tests/support.py`: the first reads `.vellum/product.yaml`, the same file the
`conformance` job reads; the other two read the tree itself, counting `@id:`
tag lines and gherkin fences independently of the extractor they check. A
hard-coded fact fails on every advance that touches it, which is noise that
trains people to ignore red. The version bit at spec-v2 -> spec-v3; the counts
bit at spec-v3 -> spec-v6, when spec-v5 added a twentieth scenario and four
tests asserting `19` went red at once.

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

**`fetch-depth: 0` is load-bearing.** `version_history()` in
`src/vellum/suite.py` walks `spec-v*` tags. A shallow clone has no tags, so
every scenario comes back `pending` at version 1 — wrong, and silently so. Both
workflows in `adapters/github/` set it.

## Patterns worth keeping

- **Findings, not exceptions.** `lint_tree()` returns a sorted list of
  `Finding`; only `main()` decides the exit code. That keeps lint usable as a
  library (`--json`) and makes the tests assert on codes rather than on text.
- **Lint fails on a broken block; extract skips it.** `scenarios_in()` in
  `src/vellum/suite.py` swallows `GherkinParseError` on purpose — lint is where
  a broken block fails a run, and extraction must still describe the rest of
  the tree. `test_unparseable_blocks_are_skipped_not_raised` pins this.
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

## Settled

The interim Feature+Scenario slug scheme is gone, along with `src/vellum/slug.py`
which existed to serve it. waviisoft/vellum-intent#2 was answered by
`spec/decisions/2026-08-28-scenario-identity.md` and the tree now carries
explicit ids. `suite.json` emits `id` and `ref` where it used to emit `anchor`;
anything reading the old field will read `None`.
