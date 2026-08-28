# Area: the `vellum` CLI

`src/vellum/`. Three commands — `lint`, `suite extract`, `ledger open|advance`
— dispatched from `build_parser()` in `src/vellum/cli.py`. Every claim below
names a file or symbol you can grep for.

## Module map

| Module | Holds |
|---|---|
| `src/vellum/cli.py` | `build_parser()`, `main()`. The only place argparse appears. |
| `src/vellum/specfile.py` | `resolve_spec_root()`, `parse_spec_text()`, `find_fences()`, `iter_spec_files()`. |
| `src/vellum/gherkin_blocks.py` | `split_documents()`, `parse_block()`, `_attach_id()`, `scenario_ref()`. |
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
scenario, `GH003` id claimed by two scenarios. `GH003` is checked in
`_check_unique_ids()` at the `lint_tree()` level, **not** per file, because ids
are unique across the whole intent repo — putting that check back inside
`_check_gherkin()` would silently stop catching the cross-file case.

## Landmines

**A fenced gherkin block may hold more than one `Feature:`.**
`spec/features/certification-and-releases.md` does, at spec-v1. Gherkin allows
one Feature per document and the official parser raises
`CompositeParserException` on the second one. `split_documents()` in
`src/vellum/gherkin_blocks.py` cuts a block at column-zero `Feature:` lines and
parses each piece separately. If you ever call `Parser().parse()` directly on a
fence body, you will reintroduce this bug. It is covered by
`TestBlockSplitting` in `tests/test_suite.py`.

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
