# Wave: an installation names the files Vellum owns, and `vellum upgrade` rewrites only those

`spec/features/installation.md` (the three behaviors after the provisioning
ones) and `spec/decisions/2026-09-04-vellum-owned-files-and-upgrades.md`. Parts
1 and 2 could install an installation and stamp its stubs; nothing could bring
one to a newer release. An installation pinned Vellum in three places that drift
at different speeds, and only the cheapest of the three — the stubs' ref — had a
command.

Four scenarios: `@id:upgrade-rewrites-only-owned-files`,
`@id:upgrade-refuses-an-edited-owned-file`,
`@id:upgrade-plan-names-the-shape-changes`,
`@id:doctor-reports-the-local-cli-against-the-stubs`.

## What was built

| Path | What |
|---|---|
| `src/vellum/manifest.py` | `.vellum/install.yaml`: the format, its only reader and writer, and the validation of every path on the owned list. |
| `src/vellum/owned.py` | The ownership table — which seeded files are Vellum's, where each one's template lives, and (in the docstring) why every excluded file is excluded. |
| `src/vellum/changes.py` | Reads `seeds/CHANGES.yaml` and selects the entries in `(manifest release, --to]`. Refuses a configuration key added without a default. |
| `src/vellum/upgrade.py` | The command: the two template sources, the comparison, the refusal, the branch, the commit and the pull request. |
| `src/vellum/seeds/CHANGES.yaml` | The installation-shape changelog: v0.1.0, v0.2.0, and a `template:` entry to copy when a release is cut. |
| `src/vellum/seeds/templates/` | `config.yaml`, `releases.yaml`, `memory-map.md` — byte for byte what `provision.py`'s string constants held. |
| `src/vellum/install.py` | `stamp_manifest()`, `manifest_findings()`, `installed_shape()`; `inspect()` now also returns the `vellum-ref` a stub installs; doctor's two new lines. |
| `src/vellum/provision.py` | The seed writes a manifest on both sides; three templates became file reads; `git` and `default_branch` are public for `upgrade`. |
| `src/vellum/cli.py` | The `upgrade` subcommand and its five arguments. |
| `pyproject.toml` | `[tool.setuptools.package-data]` for the seeds that are not `.py`. |
| `tests/test_upgrade.py` | 54 tests driving the real command against a provisioned installation and a sandbox release built as a git clone of this repo. |
| `tests/test_install.py` | The manifest at stamp time, doctor's findings, the manifest format, the ownership table. |
| `tests/test_init_provision.py` | The seed writes a manifest on each side, and it owns what it should and nothing it should not. |

Suite: 936 → 1016 tests, green. `.vellum/product.yaml` was not touched — the
pin is the architect's, and `.vellum/install.yaml` for this repo's own
installation is outside the implementer's write boundary.

## The decisions, and why

**Ownership is data, everywhere, with no exception.** The manifest is read and
what is not on it is not touched. Nothing consults a file's history or its
contents to decide. The two commands that write ownership data write it in
exactly two places: `init` at provisioning (the whole default set for that side)
and `init` over an installation with no manifest at all (the caller stubs and
nothing else). `upgrade` never edits `owned:` — not to add a file a release
ships, not to drop one it has retired. Adding would silently re-take a file the
operator had removed, which is the one edit the refusal exists to invite.

**A file is ownable when the checkout alone can reproduce it.** That single rule
answered "which seeded files are owned" without a case-by-case argument. The
owned set is: the three caller stubs, `.vellum/config.yaml`,
`ledger/releases.yaml`, the harness *machinery* (`run.py`, `support/runner.py`,
`registry.py`, `report.py`, `world.py`, `support/__init__.py`) on the intent
side, and `.vellum/memory/map.md` on the product side. Everything else the seed
writes is not, and `owned.py`'s docstring says why one row at a time — the spec
tree is the product's own words, `.vellum/workspace.yaml` is the repo map an
installation edits, `.vellum/product.yaml` is the pin, `harness/README.md` names
the product, and `harness/steps/` and `harness/support/adapter.py` are what the
seeded `harness/README.md` itself calls "yours".

**The seed templates became files.** `upgrade` reads a release's templates with
`git show <ref>:<path>`, and a template that is a Python string constant is
readable at a ref only by parsing the module holding it — or by importing
another release's code, which is worse. So three constants moved to
`seeds/templates/` unchanged, `pyproject.toml` declares them as package data,
and `seeds` raises rather than returning an empty string when a wheel does not
carry them.

**Two refs, hence `--from`.** Proving a file is unedited needs the templates of
the release the manifest names; writing it needs `--to`'s. A checkout serves any
ref it carries and this CLI serves exactly one. Where either cannot be served
the command exits 2 naming `--from`, rather than skipping the check — the check
is the whole safety property, and an upgrade that quietly stopped making it
would overwrite the edits it exists to protect.

**The whole comparison runs before anything is written.** `compare()` returns
the complete list; the git half runs only when nothing is `edited` or
`unverifiable`. "Exit 1 and nothing is written" has to mean nothing.

## Judgment calls

1. **A missing owned file is skipped with a note, not recreated** — `--restore`
   asks for it back. waviisoft/vellum-intent carries no `harness-ci.yml` stub by
   design; recreating it would undo a decision nobody re-opened, inside a pull
   request about something else.
2. **`ledger/releases.yaml` is owned, and it is the row to re-open.** The brief
   named it and the seed ships its shape, so a release that changes that shape
   has no other way to deliver it. But the pipeline writes to that file, so the
   first upgrade of any installation that has cut a release refuses it by name.
   That is the designed mechanism — once, visibly, with two named ways out —
   and it is still a refusal every real installation meets. Worth a second look
   if it proves to be noise.
3. **Role files (`.vellum/agents/*.md`) are not in the owned set.** The decision
   names them, but the CLI ships no templates for them — the `vellum-initiate`
   skill copies them out of the intent repo — so no release of this CLI can
   compare or rewrite one. A manifest may still list one; `upgrade` reports that
   the release ships no template and touches it. They join the owned set the day
   the CLI seeds them.
4. **The stubs are re-stamped by this CLI's renderer, not copied out of the new
   release.** The brief said "reuse install.py", and a stub interpolates the
   host, the ref and the branch, none of which is a release's to choose. The
   limitation: a release that changed what a stub *contains* delivers that when
   a CLI at that release stamps it. Doctor's caller-half compare asks for that,
   and the new local-CLI-against-the-stubs line makes the gap visible.
5. **A release's new files are reported, not adopted.** `--plan` prints the
   crossed range's `files_added` and the operator adds the line to `owned:`.
   Auto-adding would re-take a file somebody had deliberately removed, and that
   loses more than one line of typing saves.
6. **`--plan` still exits 1 when the run would refuse.** A plan whose answer is
   "this would not run" should say so with the code that means it; 0 would make
   a plan and a refusal indistinguishable to a caller.
7. **`__version__` was left at `0.1.0`.** The tag `v0.2.0` exists and the version
   file does not follow it; bumping it re-renders `adapters/github/` and is a
   release decision, not this wave's. It does mean this repo's own doctor
   reports the local CLI as behind the stubs — which is exactly the case the new
   line exists to report, so it is dogfood rather than a defect.

## Landmines

- **Changing the manifest's release without restamping the stubs makes all
  three look edited.** The stub's "is it edited?" render is at the manifest's
  release. `vellum init --ref <ref> --force` is the one command that moves the
  stubs and the manifest together; the tests' `restamp()` helper exists for
  exactly this and the comment on it says why.
- **`git show` output has no trailing newline stripped for a reason.** A test
  helper that `.strip()`s file contents will report byte-identical files as
  different; `tests/test_upgrade.py`'s `files_at` reads without stripping and
  says so.
- **Cutting a release without adding its `CHANGES.yaml` entry reddens
  `EveryReleaseTagHasAShapeEntry`.** That is the intended alarm, the same shape
  as re-rendering `adapters/github/` when `__version__` changes.
