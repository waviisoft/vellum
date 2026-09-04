"""Files ``vellum init`` seeds into a new installation, shipped as package data.

Package data rather than repository paths, for the reason ``vellum.install``'s
docstring gives about the caller stubs: an installed CLI is a wheel, and a
command that read ``harness/`` off disk would work from a development checkout
and fail from a pip install. Every file under ``seeds/harness/`` is a module of
an ordinary package, which is what makes a wheel carry it without a
``[tool.setuptools.package-data]`` declaration — see ``seeds/harness/
__init__.py``. ``tests/test_init_provision.py`` names the seeded set exactly and
imports it out of a provisioned checkout, so a packaging change that dropped a
file cannot pass silently.

The split against ``vellum.install``'s ``SHIPPED`` table is deliberate and is
the same distinction one level down. A caller stub is *generated* — it
interpolates a host, a ref and a branch, and doctor compares an installation's
against a fresh render, so it has to be a template. The harness skeleton is
*copied*: nothing in it varies by installation, nothing compares an
installation's copy against it afterwards, and it is ordinary Python a
harness engineer will edit on day one. A template engine over files nobody
re-renders would be surface with no reader.

Only files copied *verbatim* live here. Everything that interpolates the
installation — the spec tree, the config, the workspace, the ledger, and the
harness's own README, which names the product — is a template in
``vellum.provision``. The line is the same one ``vellum.install`` draws between
a rendered stub and a copied file, and it is also what keeps this package's
contents all ``.py``: see ``seeds/harness/__init__.py`` for why that matters to
whether a wheel carries them at all.
"""

from __future__ import annotations

from importlib import resources

#: The directory inside this package holding the harness skeleton.
HARNESS = "harness"

#: Files under :data:`HARNESS` that exist for the *packaging* and are not part
#: of the seed. ``harness/__init__.py`` is what makes the skeleton an ordinary
#: package so every builder ships it (its own docstring has the argument);
#: seeding it would make an installation's ``harness/`` a package, which it is
#: not — ``run.py`` puts that directory on ``sys.path`` and imports ``support``
#: and ``steps`` as top-level. The other two ``__init__.py`` files ARE seeded:
#: they are the real ones.
NOT_SEEDED = frozenset({f"{HARNESS}/__init__.py"})

#: The seed is Python source and nothing else, and the filter is not cosmetic:
#: installing this package byte-compiles it, so the *installed* directory holds
#: ``__pycache__/`` beside every module. Walking it read a ``.pyc`` as UTF-8 and
#: took the command down with a ``UnicodeDecodeError`` — from a wheel, and never
#: from the development checkout the tests run against. Both halves are
#: belt-and-braces on purpose: the directory is skipped, and a file that is not
#: a module is not read.
SOURCE = ".py"
BYTECODE = "__pycache__"


class SeedsMissing(Exception):
    """This install carries no harness skeleton, so it cannot seed one."""


def harness_files() -> dict[str, str]:
    """``{repo-relative path: text}`` for every file in the harness skeleton.

    Paths come back rooted at ``harness/`` — the tree's place in an intent repo
    — so a caller writes them straight into a checkout. Ordered by path, so a
    plan that lists them and a seed that writes them agree, and two runs of
    either produce the same bytes in the same order.
    """
    root = resources.files(__package__) / HARNESS
    found: dict[str, str] = {}

    def walk(node, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            relative = f"{prefix}/{child.name}"
            if child.is_dir():
                if child.name != BYTECODE:
                    walk(child, relative)
            elif child.name.endswith(SOURCE) and relative not in NOT_SEEDED:
                found[relative] = child.read_text(encoding="utf-8")

    walk(root, HARNESS)
    if not found:
        raise SeedsMissing(
            f"{root} holds no seed files. The harness skeleton ships as part of "
            f"this package; an install that does not carry it can provision an "
            f"installation with no harness at all, so this refuses rather than "
            f"seeding one."
        )
    return dict(sorted(found.items()))
