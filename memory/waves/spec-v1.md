# Wave: spec-v1 — the hand-built loop (v0.1)

Worklog for the first wave: the scaffold, the `vellum` CLI, and the two GitHub
Actions workflows. Built by hand, before the pipeline it implements existed —
docs/design.md §10 calls this "implementation still manual-ish".

Scope delivered: submodule pin, `memory/` tree, `.vellum/product.yaml`,
`vellum lint`, `vellum suite extract`, `vellum ledger open|advance`,
`adapters/github/spec-ci.yml`, `adapters/github/on-spec-merge.yml`, 74 tests.

## Approach

Read the spec tree in index order first, then decisions, then `docs/design.md`
— and treated the spec as canonical wherever the design doc differed. Two
places it differed and the spec won:

- `docs/design.md` §3 shows `since: spec-v0.2.0`. Decision D6 and all 26 files
  in the tree use bare integers, so `SINCE_RE` in `src/vellum/specfile.py`
  requires `spec-v<integer>` and rejects the design doc's example form.
- `docs/design.md` §6 shows `cost: {...}` and `labels: [...]` inline. Records
  are emitted in block style instead (`dump()` in `src/vellum/ledger.py`):
  formatting is not specified, and a line per field makes advancing a state a
  one-line diff, which suits a tree whose append-only-ness is git history.

Wrote the checks against the real tree from the start rather than against
fixtures alone, which is how the two-`Feature:` block and the `datetime.date`
frontmatter problem surfaced at all.

## Surprises

**The spec tree contains a Gherkin document that standard Gherkin rejects.**
`spec/features/certification-and-releases.md` puts `Feature: Certification
gates auto-merge` and `Feature: Release cuts` in one fence. One Feature per
document is a Gherkin rule, and the official parser raises on the second. Since
the spec is canonical and lint must pass against spec-v1, the parser had to
accommodate the tree: `split_documents()` cuts blocks at column-zero `Feature:`
lines. The alternative — editing the spec to split the fence — would have been
a spec change, which is not an implementer's to make.

**There are no markdown links in the spec tree at all.** `index.md` names files
as bare table text (`features/spec-pipeline.md`) and mentions `docs/design.md`
in prose. A link checker that only understood `[text](target)` would have had
nothing to check and would have passed vacuously. So a reference is *any* `.md`
path outside code, resolved against the referring file's directory, then the
spec root, then the spec root's parent. That third fallback is what resolves
`docs/design.md`, which lives above the tree.

**`vellum lint spec/` is ambiguous about what `spec/` is.** In the intent repo
it is the spec tree. In this product repo the submodule mounts the whole intent
repo there, so the tree is `spec/spec/`. Rather than special-case either,
`resolve_spec_root()` accepts both.

**PyYAML parses unquoted dates into `datetime.date`.** All thirteen decision
files failed the first version of the date check. Caught because lint was run
against the real tree, not only fixtures.

**Writing a *genuinely* failing Gherkin fixture took several attempts.** Gherkin
is more permissive than expected — a stray step under `Feature:` becomes the
feature description, `Examples:` without a Scenario Outline parses, and a bare
table row parses. `tests/fixtures/bad-gherkin/features/broken.md` ends up using
an unterminated docstring and a stray `Nonsense:` keyword line, both of which
genuinely raise.

## Paths rejected

- **Hand-written Gherkin parser.** Would have removed a dependency, but D5
  chose Gherkin *because* mature parsers exist; a lookalike would drift from
  whatever runs the suite in v0.2, and the drift would show up as a scenario
  that lints clean and then fails to run. Took `gherkin-official` instead.
- **Hand-rolled YAML subset.** Frontmatter and ledger records are simple enough
  to tempt it. Rejected: it works until someone writes a construct it silently
  mis-reads, and "silently" is the problem. Took `PyYAML`.
- **Go or Node.** Both need a toolchain or `node_modules` in CI and a YAML
  dependency anyway. Python is already on every runner. Reasoning kept in
  `memory/map.md`.
- **`git blame` / `git log -L` for scenario versions.** Line-level history is
  fragile against reformatting and moved blocks, and would report a re-indented
  scenario as changed. Walking the `spec-v*` tags and comparing content
  fingerprints (`version_history()` in `src/vellum/suite.py`) is
  O(tags × files) and exactly matches "introduced or last changed".
- **Failing extraction on an unparseable block.** Rejected so the two commands
  keep distinct jobs: lint fails the run, extract describes what it can.
- **A `ledger/` directory in this repo.** Created it, then removed it: the
  ledger lives in the intent repo and implementers may not write it
  (`spec/behaviors/write-boundaries.md`). `--ledger-dir` is an argument, and
  the workflow that passes it runs in the intent repo.

## Judgment calls recorded (non-blocking, per the ambiguity ladder)

None of these change observable behavior, so they were decided rather than
asked. The verifier should review them.

1. **Frontmatter schema is per-directory**, not per-file-kind:
   `decisions/` requires `id/title/date`, everything else `id/title/since`,
   with `status` optional for the index's `unsurveyed` marking. Derived from
   the tree's own shape; nothing in the spec states it.
2. **Unknown frontmatter keys are an error (`FM003`)**, not a warning. The spec
   calls the check a "frontmatter schema"; a schema that ignores extra keys is
   not one.
3. **Lint does not check that every spec file is listed in `index.md`.** The
   index is called load-bearing and gets a staleness audit from the librarian
   (v0.3); mechanising it now would guess at the audit's shape.
4. **Block style and fixed key order for ledger YAML**, as above.
5. **Cost fields accumulate** rather than being overwritten, since every agent
   invocation records into the same work-item entry and the design doc's
   worked example shows `attempts: 2`.

## Asked, not guessed

**waviisoft/vellum-intent#2 — "Question: scenario identity and anchor scheme".**
"Introduced or last changed" needs a stable scenario identity across versions,
and the only worked example in the corpus (`docs/design.md` §6's
`features/auth.md#acceptance-3`) is an ordinal, which decision D4 rejects for
renumbering churn. Behavioral: it changes `suite.json` ids, what the ledger's
`satisfies:` can point at, and whether a rename counts as a change.
Implemented option (b), a Feature+Scenario slug, as a documented default so the
wave continued; it is isolated in `assign_anchors()` and `src/vellum/slug.py`.

## Left undone, and why — CLOSED in the spec-v2 wave

At the time of writing, `spec-v1` did not exist as a tag on the intent repo:
the owner approved creating it, but this session's git credential accepted
branch refs only (403 on `refs/tags/*`) and no create-ref tool was available.
The submodule was pinned to `bc84e591` — the commit `spec-v1` names — so the
pin was correct either way, and version derivation was written to work before
and after the tag existed.

**Resolved.** The owner pushed `spec-v1` at `bc84e591` during the spec-v2
wave; both `spec-v1` and `spec-v2` are now on the intent repo. See
`memory/waves/spec-v2.md`.
