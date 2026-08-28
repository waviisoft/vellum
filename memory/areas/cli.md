# Area: the `vellum` CLI

`src/vellum/`. Three commands — `lint`, `suite extract`, `ledger open|advance`
— dispatched from `build_parser()` in `src/vellum/cli.py`. Every claim below
names a file or symbol you can grep for.

## Module map

| Module | Holds |
|---|---|
| `src/vellum/cli.py` | `build_parser()`, `main()`. The only place argparse appears. |
| `src/vellum/specfile.py` | `resolve_spec_root()`, `parse_spec_text()`, `find_fences()`, `iter_spec_files()`. |
| `src/vellum/gherkin_blocks.py` | `split_documents()`, `parse_block()`, `assign_anchors()`. |
| `src/vellum/links.py` | `find_references()`, `resolve()`, `heading_anchors()`. |
| `src/vellum/lint.py` | `lint_tree()`, `Finding`, the `_check_*` functions. |
| `src/vellum/suite.py` | `extract()`, `fingerprint()`, `version_history()`, `to_dict()`. |
| `src/vellum/gitver.py` | `spec_tags()`, `markdown_at()`, `show()`. All subprocess git lives here. |
| `src/vellum/ledger.py` | `open_record()`, `advance()`, `dump()`, `RECORD_KEYS`, `ITEM_KEYS`. |
| `src/vellum/slug.py` | `slugify()`, `heading_anchor()`. |

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
- **Fingerprints exclude position.** `fingerprint()` in `src/vellum/suite.py`
  hashes keyword, name, tags, background, steps and examples — never line
  numbers or surrounding prose — so moving or reformatting a scenario does not
  read as a behavioral change. This is the whole reason version derivation
  walks tags instead of using `git blame`.
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

## Open

The anchor scheme (`assign_anchors()`, `src/vellum/slug.py`) is a documented
default, not a settled decision — see waviisoft/vellum-intent#2. If it changes,
it changes here and in nothing else.
