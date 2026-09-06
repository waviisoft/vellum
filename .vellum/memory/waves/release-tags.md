# Wave: a release tag is a decorative name the forge mints, on both sides of the pair

`spec/features/release-tags.md` and
`spec/decisions/2026-09-06-release-tags-are-minted-by-the-forge.md`. Vellum's
own `v0.1.0` and `v0.2.0` were tagged by the owner's hand, because the
architect's session cannot push tags and a product repo had no workflow that
could. The intent side has never needed a hand — `on-spec-merge` tags `spec-vN`
with the workflow's own credential — and the product side of the pair was the
half Vellum had not yet reached: a product repo `vellum init` provisioned
carried no workflow at all.

Four scenarios: `@id:release-tag-is-minted-on-the-default-branch`,
`@id:release-tag-leaves-a-used-name-alone`,
`@id:release-tag-refuses-a-missing-changelog-entry`,
`@id:release-cut-stub-is-stamped-on-the-product-side`.

## What was built

| Path | What |
|---|---|
| `src/vellum/tag.py` | `vellum release tag`: the `release:` block reader, the three version sources, the changelog check, and the plan that names a tag and a commit and applies neither. |
| `.github/workflows/release-cut.yml` | The reusable workflow that applies it: checkout with tags, install the CLI at `vellum-ref`, `vellum release tag . --plan --json`, and the three exit codes turned into a tag pushed, a notice, or a failed job. |
| `.github/workflows/release-cut-caller.yml` | This repo's own caller, by **local path** — it cannot pin the tag it is about to mint — with `vellum-ref: ${{ github.sha }}`. |
| `adapters/github/release-cut.yml` | The rendered product-side caller stub, held byte-identical to what `vellum init` writes. |
| `src/vellum/install.py` | `Shipped.side`, `shipped_for(side)`, `side_of()`, `installation_name()`; `init` and `doctor` stamp and check the side they are run in; `doctor --product` and `Doctor.paired`. |
| `src/vellum/owned.py` | Stub rows take their side from the shipped table; `stub_paths(forge, side)`; `INTENT`/`PRODUCT` re-exported from `install`. |
| `src/vellum/upgrade.py` | `side_of` delegates to `install.side_of` rather than reimplementing it. |
| `src/vellum/provision.py` | The intent side's stubs only, and the report names the second stamp the product half needs. |
| `src/vellum/cli.py` | `release tag` beside `release cut`; `doctor --product`; the `TagError`/`TagRefused` exit codes. |
| `.vellum/product.yaml` | This repo's own `release:` block: `pyproject.toml`, and `CHANGES.yaml` as the changelog. |
| `tests/test_tag.py` | 28 tests: the name, the used name, the refusal, the three sources, the exit-2 set, and that this repo's own declaration reads. |

Suite: 1080 → 1108 tests, green. Version bumped to `0.4.0` with its
`CHANGES.yaml` entry, and `adapters/github/` re-rendered at `v0.4.0`.

## The decisions, and why

**The command computes; the forge applies.** `vellum release tag` never runs
`git tag`, never pushes, and writes nothing — which is the division `vellum
mint` already keeps one repo over, and it is a *checked* property rather than a
docstring promise: the file bytes, the whole ref table and `HEAD` are compared
across a run, because a command whose subject is a tag can write nothing into a
working tree and still move a name.

**1 and 2 are different codes and it matters here more than usual.** A declared
changelog with no entry for the version is 1 — the command answered, and the
answer is that a release nobody described is not one to name yet. An unreadable
source, a missing `release:` block, a version the source cannot yield are 2. The
workflow fails a job on 1 by telling an operator *which entry to write*, and it
must never say that about a repository it could not read.

**The changelog is a substring test, both spellings.** A changelog is prose in
whatever shape its project keeps — a Markdown heading, a YAML key under
`releases:`, a row in a table — and a reader that understood one of those would
refuse the other two. `v0.5.0` or `0.5.0`, either satisfies it.

**Which version reader runs is decided by the file's NAME.** A `pyproject.toml`
that failed to parse must be a refusal, never a fall back to "the trimmed
contents of that file", which would report most of a TOML file as a version.
`tomli` joins the dependencies below 3.11 for the same reason the floor is 3.10.

**A shipped workflow names its side, and four things stopped keeping their own
list.** `init`, `doctor`, `upgrade` and the ownership table all iterated
`SHIPPED`, and that was true only while every stub was the intent repo's. One
field on `Shipped` was the whole change; the alternative was four lists that
agree until one of them does not.

**Which side a checkout is, is read and never given.** `.vellum/workspace.yaml`
is the intent half, `.vellum/product.yaml` the product half, both or neither is
exit 2. `vellum.upgrade` had that reader first and now calls `install.side_of`:
three commands reading one fact through two implementations is how they come to
disagree about the checkout that carries both files.

**`doctor --product` takes a path because no file holds one.**
`.vellum/workspace.yaml` names the product *repository*; where it is checked out
on this machine is not a checkout fact, which is the same reason
`--releases-from` and `vellum upgrade --from` take theirs. One report, one exit
code: an installation is the pair, and a run that exited 0 because the intent
half was clean is what the option exists to stop.

**This repo calls the workflow by local path.** A stub pins a Vellum ref, and
this repo cannot pin the tag it is about to mint. `vellum-ref` is `github.sha`
rather than `main`: the CLI that runs must be the one this commit ships, and
`main` is a moving target another push can advance between the run starting and
its CLI checkout.

## Judgment calls

1. **The product-side stub is owned before it exists.** Provisioning stamps the
   intent half only — the spec has the stub "stamped by `vellum init` on the
   product side", a run in that checkout — so a freshly provisioned pair has
   `.github/workflows/release-cut.yml` in the product manifest's `owned:` and
   not on disk. It is owned so that `vellum upgrade` can re-stamp it; it is
   absent because nobody has stamped it. `vellum doctor` in that checkout
   reports it as a `missing` finding, and the provisioning report names the
   command that writes it. That is the one row in the ownership table where
   owned and present come apart, and `tests/test_init_provision.py` names it
   rather than leaving it to be discovered.
2. **`--plan` changes nothing about what the command does.** It never applies a
   tag either way, so the flag is the caller stating what it is asking for. The
   spec asks for it in as many words ("`--plan` is the same answer, stated as
   one") and a test holds the two outputs equal.
3. **The changelog is checked before the name is looked up.** The two sentences
   only meet in one odd case — a tag that exists and a changelog entry since
   deleted — and "a missing entry is a refusal" is written without an exception.
   Idempotency does not pay for it: a name that was minted was minted over a
   changelog that described it, so the workflow's re-run finds the entry.
4. **A bare integer is not a plausible version.** `7` is what an unrelated
   one-line file looks like, and `v7` is a tag name a forge would resolve. A
   dotted core is required; a pre-release or build suffix after it is allowed,
   because that is a version somebody chose.
5. **`strays` knows the side's stubs, not the pair's.** A `release-cut.yml` in
   an intent repo is a workflow with no reusable workflow behind it for that
   half, and a `known` set covering the whole pair would pass over it silently.

## Landmines

- **A stub added on a new side breaks every loop over `SHIPPED`.** Nine test
  loops and three call sites read that tuple as "the stubs this checkout
  carries". `install.shipped_for(side)` is what they want; `SHIPPED` is right
  only where the subject really is the whole table — the render-drift check over
  `adapters/github/`, and `owned.table()`.
- **The report must name no tag but the one it would mint.**
  `release-tag-is-minted-on-the-default-branch` asserts the *absence* of the
  used name, so a report that listed the repository's existing tags would fail
  it. `tags(repo, tag)` asks about one name with an exact glob for that reason.
- **`vellum release tag` writes its JSON under `$RUNNER_TEMP`, not the
  checkout.** The product repo's working tree is what gets tagged, and a file
  dropped into it would make `git status` in a later step report a change the
  job invented.
- **`git tag -a` at `$GITHUB_SHA`, and the message subject through `env`.** A
  commit message is attacker-supplied text and `${{ }}` pastes it into the
  script before the shell parses the line, on a runner holding `contents:
  write`. Same rule, same reason, as `on-spec-merge`'s tagging step.
- **Tag protection on `v*` is invisible to every checkout.** The push step reads
  the remote back before failing and says which of the two happened, because "a
  push was refused" and "somebody else got there first" look identical from
  inside the job.
