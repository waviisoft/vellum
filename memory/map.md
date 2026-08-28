# Map

Where things are in this repo, and the standing decisions behind them. Area
notes live in `memory/areas/`; wave worklogs in `memory/waves/`.

## Layout

| Path | What |
|---|---|
| `src/vellum/` | The CLI. One module per concern; see `memory/areas/cli.md`. |
| `tests/` | `unittest` suite plus fixture spec trees under `tests/fixtures/`. |
| `adapters/github/` | Workflows written **for the intent repo**, ready to copy over. See `memory/areas/adapters-github.md`. |
| `.vellum/product.yaml` | Backref to the intent repo, and the pin of record — `pin.commit`. Nothing mounts the intent repo here; CI fetches it, and tests read `VELLUM_INTENT_REPO`. |

## Areas

- [`memory/areas/cli.md`](areas/cli.md) — `vellum lint`, `vellum suite extract`, `vellum ledger`.
- [`memory/areas/adapters-github.md`](areas/adapters-github.md) — the two GitHub Actions workflows.

## Waves

- [`memory/waves/spec-v1.md`](waves/spec-v1.md) — the hand-built loop.
- [`memory/waves/spec-v2.md`](waves/spec-v2.md) — scenarios carry explicit stable ids.
- [`memory/waves/spec-v3.md`](waves/spec-v3.md) — scenarios are self-contained; Backgrounds banned.
- [`memory/waves/spec-v6.md`](waves/spec-v6.md) — one Feature per fence; runnable scenarios; the PO/PA/CA hierarchy.
- [`memory/waves/versions-are-commits.md`](waves/versions-are-commits.md) — versions became commits, the pin became a file, minting shrank, `Rule:` banned.

Worklogs up to `spec-v6.md` are named for the version they landed at. This one
is named for what it did, because a version's name is decoration now
(`spec/decisions/2026-08-28-versions-are-commits.md`) and a filename that
presumes a `spec-vN` the architect has not yet attached would be a name doing
work. Later waves should follow this one.

## Technology choice, and why

**Python 3.10+, standard library, plus exactly two pure-data dependencies:
`PyYAML` and `gherkin-official`.** Pinned by range in `requirements.txt` and
`pyproject.toml`.

The instruction was boring and portable: no frameworks, no database, no
services, plain files as the substrate. The reasoning, so a later wave can
re-open it knowingly rather than by accident:

- **Python 3, not Go or Node.** Python is already on every GitHub Actions
  runner and every developer machine, so the workflows in `adapters/github/`
  need no toolchain install and no build step — `pip install -e .` and run.
  Go would produce a nicer single binary but needs a toolchain in CI and a YAML
  dependency regardless; Node needs `node_modules` in CI and a lockfile.
- **`gherkin-official`, not a hand-written parser.** Decision D5
  (`spec/decisions/2026-08-28-gherkin.md`) chose Gherkin *because* mature
  parsers exist. `gherkin-official` is the Cucumber parser itself, so `vellum
  lint` accepts exactly what the v0.2 harness runner will accept. A lookalike
  parser would drift from it, and the drift would surface as a scenario that
  lints clean and then fails to run.
- **`PyYAML`, not a hand-rolled subset.** Frontmatter and ledger records are
  YAML and no ecosystem has a YAML parser in its standard library. A
  hand-written subset is the classic quiet-rot choice: it works until someone
  writes a construct it silently mis-reads.
- **`argparse` and `unittest`, both stdlib.** No CLI framework, no pytest.
- **No database, no service, no daemon.** Every command reads and writes plain
  files, and git is the only state store — which is what decision D11
  (stateless reconciler) requires of anything the orchestrator drives.

Cost of the two dependencies: they are a supply-chain surface, which
`spec/behaviors/security.md` makes a verifier red-flag item. Both are widely
used, pure-Python and pinned by range.
