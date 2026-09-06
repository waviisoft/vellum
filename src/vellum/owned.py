"""Which files Vellum owns in an installation, and where each one's template is.

``spec/decisions/2026-09-04-vellum-owned-files-and-upgrades.md``: an installation
records "the repo-relative paths **Vellum owns**: the files an upgrade may
rewrite". This module is the *default* that ``vellum init`` writes into a fresh
manifest. After that the manifest is the authority and this table is not
consulted again for ownership — ``vellum upgrade`` reads the list out of
``.vellum/install.yaml`` — but it is still consulted for the other half of the
question: **where a release keeps that file's template**, which is what makes an
upgrade able to compare and rewrite it.

The line: a file is ownable when the checkout alone can reproduce it
--------------------------------------------------------------------
An upgrade has to answer two questions about an owned file — "is this still what
release X shipped?" and "what does release Y ship instead?" — from a checkout
and a release's templates, with no record of the conversation that provisioned
the installation. So the ownable files are exactly the ones whose text is
reproducible from what is still on disk:

* **verbatim package data** (the seeded config, the release ledger, the harness
  machinery) — nothing about them varies by installation; and
* **one-value templates** whose value the checkout still carries (the product
  repo's memory map names the intent repo, which ``.vellum/product.yaml`` names
  too), and the caller stubs, whose ref, host and branch are readable out of the
  installed stubs and which ``vellum.install`` re-stamps rather than copies.

Everything else a ``vellum init`` seeds is **not** owned, and each for its own
reason rather than by a blanket rule:

===================================  ============================================
``spec/**``                          The product's own words. The decision and
                                     the brief both exclude it: the spec is the
                                     one thing an installation exists to write.
``docs/**``                          Same, one repo over.
``.vellum/workspace.yaml``           Installation *data*: the intent slug, the
                                     product map, the forge. Rewriting it from a
                                     template would delete every product an
                                     installation has added since.
``.vellum/product.yaml``             It IS the pin (``spec/decisions/
                                     2026-08-28-pin-file.md``). An upgrade that
                                     rewrote it would reset the spec version the
                                     product answers to.
``harness/README.md``                It names the product, and once a workspace
                                     maps more than one product the checkout has
                                     no single title to re-render it from.
``harness/steps/**``                 The harness engineer's, and the seeded
                                     README says so: "Two files are yours:
                                     ``steps/`` … and ``support/adapter.py``".
``harness/support/adapter.py``       Same sentence. It is how *this* harness
                                     reaches *this* deployment.
``.vellum/agents/*.md``              The CLI ships no role-file templates — the
                                     ``vellum-initiate`` skill copies them from
                                     the intent repo — so no release of this CLI
                                     can compare or rewrite one. A manifest may
                                     still list them; ``upgrade`` will report
                                     that it has no template and touch nothing.
``.vellum/install.yaml``             Vellum's own bookkeeping, rewritten
                                     unconditionally (``vellum.manifest``).
``ledger/releases.yaml``             Pipeline-written state, not a template —
                                     see the paragraph below.
===================================  ============================================

``ledger/releases.yaml`` is seeded and is **not** owned, and the row is worth a
paragraph because it was owned once. The seed ships its *shape*, which reads
like a template Vellum should keep current — but the file is **pipeline-written
state**: ``vellum ledger open|advance`` and ``vellum certify record`` write to
it, so every installation that has done any work at all has edited it, and every
first upgrade would refuse by name on a file whose contents Vellum has no
business restoring. A shape change there is a *changelog migration* — a release
says what changed and the pipeline commands move the file — not a rewrite an
upgrade performs. The other rows are all files an installation is expected never
to write; this one is a file it is expected to write constantly, which is the
line.
"""

from __future__ import annotations

from dataclasses import dataclass

from vellum import install, seeds

#: The two sides of a pair. An intent repo carries three caller stubs, the
#: config, the release ledger and the harness; a product repo carries its memory
#: map and — since release tags — the ``release-cut`` caller stub.
#:
#: Re-exported from ``vellum.install`` rather than declared here: a shipped
#: workflow states which side it is stamped on, so the table that holds the
#: workflows is the one place the two names can live without either module
#: importing the other backwards.
INTENT = install.INTENT
PRODUCT = install.PRODUCT
SIDES = install.SIDES

#: How an owned file's text is produced. A ``SEED`` is read out of a release's
#: ``src/vellum/seeds/`` — verbatim, or formatted with values the checkout
#: carries. A ``STUB`` is *rendered* by ``vellum.install.render``, which is what
#: ``vellum init`` does, because a stub interpolates the ref, the host and the
#: branch and none of those is a release's to choose.
SEED, STUB = "seed", "stub"

#: The template file names under ``src/vellum/seeds/templates/``.
CONFIG_TEMPLATE = "config.yaml"
#: Seeded by ``vellum init`` and deliberately absent from :func:`table` — the
#: release ledger is pipeline-written state, not a file an upgrade rewrites (see
#: the module docstring). The name stays here because ``vellum.provision`` seeds
#: the file from it.
RELEASES_TEMPLATE = "releases.yaml"
MEMORY_MAP_TEMPLATE = "memory-map.md"


@dataclass(frozen=True)
class Owned:
    """One file Vellum ships into an installation, and where its template is."""

    #: Repo-relative path in the installation.
    path: str
    #: :data:`SEED` or :data:`STUB`.
    kind: str
    #: Which side of the pair it belongs to.
    side: str
    #: One line: why this one is Vellum's to rewrite. Printed by ``--plan``.
    why: str
    #: For a :data:`SEED`, the path inside a ``waviisoft/vellum`` checkout that
    #: holds its template; None for a :data:`STUB`.
    source: str | None = None
    #: Placeholders the template interpolates, which the caller must supply from
    #: the checkout. Empty for everything copied verbatim.
    placeholders: tuple[str, ...] = ()
    #: For a :data:`STUB`, the shipped workflow it stands for.
    shipped: install.Shipped | None = None


#: The harness files that are the *machinery* and the same in every
#: installation. The seeded ``harness/README.md`` draws this line itself — "The
#: machinery — `run.py`, `support/runner.py`, `support/registry.py`,
#: `support/report.py`, `support/world.py` — is generic and is the same in every
#: Vellum installation. Two files are yours" — so the ownership table is reading
#: the product's own documentation rather than inventing a second rule.
#: ``support/__init__.py`` joins them: it is the package marker, not a file
#: anybody writes.
HARNESS_MACHINERY = (
    "harness/run.py",
    "harness/support/__init__.py",
    "harness/support/registry.py",
    "harness/support/report.py",
    "harness/support/runner.py",
    "harness/support/world.py",
)


def _harness_rows() -> list[Owned]:
    return [
        Owned(
            path=path,
            kind=SEED,
            side=INTENT,
            source=seeds.source_path(*path.split("/")),
            why=(
                "the harness runner skeleton, which is generic and the same in "
                "every installation (harness/README.md names it as the machinery)"
            ),
        )
        for path in HARNESS_MACHINERY
    ]


def _stub_rows(forge: str) -> list[Owned]:
    """One row per shipped workflow, on the side that workflow's stub is stamped.

    ``shipped.side`` rather than a constant: the product side carries one now
    (``release-cut``), and a table that went on saying ``INTENT`` here would
    make ``vellum upgrade`` call it "a product-side file in an intent checkout"
    on the very half it belongs to.
    """
    directory = install.WORKFLOWS_DIR[forge]
    return [
        Owned(
            path=(directory / shipped.filename).as_posix(),
            kind=STUB,
            side=shipped.side,
            shipped=shipped,
            why=(
                "a caller stub, which holds no logic of its own — upgrading an "
                "installation is bumping the ref it pins"
            ),
        )
        for shipped in install.SHIPPED
    ]


def table(forge: str = "github") -> dict[str, Owned]:
    """Every file Vellum ships into an installation, by repo-relative path.

    Ordered by path so a manifest written from it, a plan that lists it and a
    report that walks it agree without three sorts being kept in step.
    """
    install.check_forge(forge)
    rows: list[Owned] = [
        Owned(
            path=".vellum/config.yaml",
            kind=SEED,
            side=INTENT,
            source=seeds.source_path(seeds.TEMPLATES, CONFIG_TEMPLATE),
            why=(
                "the installation config, seeded with the defaults. It is where a "
                "release's new keys arrive — always with a default — so a release "
                "that adds one has no way to deliver it if this is not Vellum's"
            ),
        ),
        Owned(
            path=".vellum/memory/map.md",
            kind=SEED,
            side=PRODUCT,
            source=seeds.source_path(seeds.TEMPLATES, MEMORY_MAP_TEMPLATE),
            placeholders=("intent_slug",),
            why=(
                "the product repo's memory map skeleton. It names the intent "
                "repo, which `.vellum/product.yaml` names too, so the checkout "
                "can still reproduce it"
            ),
        ),
        *_harness_rows(),
        *_stub_rows(forge),
    ]
    return {row.path: row for row in sorted(rows, key=lambda r: r.path)}


def for_side(side: str, forge: str = "github") -> tuple[str, ...]:
    """The default owned set for one side of a pair, sorted.

    What ``vellum init`` writes into a *fresh* manifest, and nothing else. An
    existing manifest's list is the operator's and no command recomputes it:
    a run that quietly re-added a path the operator had removed would undo the
    one edit the refusal exists to invite.
    """
    if side not in SIDES:
        raise ValueError(f"{side!r} is not one of {SIDES}")
    return tuple(path for path, row in table(forge).items() if row.side == side)


def stub_paths(forge: str = "github", side: str | None = None) -> tuple[str, ...]:
    """Just the caller stubs, sorted; one side's when *side* is given.

    What a *stamp* over an installation with no manifest records as owned. Not
    the whole seed: a stamp is run in a checkout whose repos already exist and
    cannot know whether the rest of that tree came from a Vellum seed or from
    the installation's own hand, and guessing would be exactly the inference the
    decision refused.

    *side* matters now that the stubs are not all on one half. A stamp writes
    the stubs of the side it ran on, so an owned set naming the whole pair's
    four would claim a file that side does not carry — and `vellum upgrade`
    would then report it as owned-but-missing on every run.
    """
    if side is not None and side not in SIDES:
        raise ValueError(f"{side!r} is not one of {SIDES}")
    return tuple(sorted(
        row.path for row in table(forge).values()
        if row.kind == STUB and (side is None or row.side == side)
    ))
