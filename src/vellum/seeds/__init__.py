"""Files ``vellum init`` seeds into a new installation, shipped as package data.

Package data rather than repository paths, for the reason ``vellum.install``'s
docstring gives about the caller stubs: an installed CLI is a wheel, and a
command that read ``harness/`` off disk would work from a development checkout
and fail from a pip install. Every file under ``seeds/harness/`` is a module of
an ordinary package, which is what makes a wheel carry it whatever
``[tool.setuptools.package-data]`` says — see ``seeds/harness/__init__.py``.
The files beside it that are *not* ``.py`` need that declaration and have it
(see below). ``tests/test_init_provision.py`` names the seeded set exactly and
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

What lives here, and what stays a template in ``vellum.provision``
------------------------------------------------------------------
Anything an **upgrade** may have to reproduce lives here as a *file*, and that
is a change this wave made for one reason: ``vellum upgrade`` obtains a
release's templates either from this package (the CLI's own) or from a
``waviisoft/vellum`` checkout with ``git show <ref>:src/vellum/seeds/…``
(:data:`PACKAGE_PATH`). A template that is a Python string constant can be read
back at a ref only by parsing the module that holds it, so the seeded config,
the release ledger and the product repo's memory map moved out of
``vellum.provision`` and into ``templates/`` — byte for byte what they were.
``.yaml`` and ``.md`` under a package are *not* shipped by setuptools' defaults
the way ``.py`` is, so ``pyproject.toml`` now declares them as package data;
``tests/test_upgrade.py`` reads them out of the installed package so a
declaration that stopped covering one cannot pass silently.

What stays in ``vellum.provision`` is what an upgrade must never rewrite: the
spec tree (the product's own words), ``.vellum/workspace.yaml`` and
``.vellum/product.yaml`` (installation data — the repo map and the pin), and
``harness/README.md`` (it names the product). ``vellum.manifest`` states the
whole ownership table and the reason for each row.
"""

from __future__ import annotations

from importlib import resources

#: The directory inside this package holding the harness skeleton.
HARNESS = "harness"

#: The directory inside this package holding the seeded templates that are not
#: the harness skeleton: the installation config, the release ledger and the
#: product repo's memory map. Files rather than string constants so a release's
#: copy of one is readable at a ref (see the module docstring).
TEMPLATES = "templates"

#: The installation-shape changelog: one entry per release, naming the
#: configuration keys it adds (always with a default), the files it adds, the
#: files it retires and the changes to what the caller stubs pass.
#: ``vellum upgrade --plan`` prints the entries for the range it would cross.
CHANGES = "CHANGES.yaml"

#: Where this package sits inside a ``waviisoft/vellum`` checkout. ``vellum
#: upgrade --from`` reads a release's templates with ``git show <ref>:<this>/…``
#: rather than importing another release's code, so the path has to be a stated
#: constant on both sides of that read.
PACKAGE_PATH = "src/vellum/seeds"


def source_path(*parts: str) -> str:
    """A path inside a ``waviisoft/vellum`` checkout, for ``git show <ref>:``."""
    return "/".join((PACKAGE_PATH, *parts))


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
    """This install does not carry a file it ships, so it cannot seed or upgrade it."""


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


def _read(*parts: str) -> str:
    """One shipped file, or :class:`SeedsMissing` when this install lacks it.

    Absent is a *refusal*, never an empty string. A wheel that stopped carrying
    the package data would otherwise seed an installation with an empty config
    and upgrade one against an empty template, and both of those look like a
    release that deleted the file rather than like a packaging fault.
    """
    node = resources.files(__package__)
    for part in parts:
        node = node / part
    try:
        return node.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError) as exc:
        raise SeedsMissing(
            f"this install carries no {source_path(*parts)}. It ships as package "
            f"data (pyproject.toml declares it); an install missing it cannot "
            f"seed or upgrade that file, so this refuses rather than guessing "
            f"its contents: {exc}"
        ) from exc


def template(name: str) -> str:
    """The seeded template *name*, verbatim, from this install's package data."""
    return _read(TEMPLATES, name)


def changes_text() -> str:
    """The installation-shape changelog, verbatim. Parsed by ``vellum.changes``."""
    return _read(CHANGES)


def read_source(path: str) -> str:
    """A file this install carries, named by its path in a ``waviisoft/vellum`` checkout.

    The other half of :func:`source_path`, and the reason both exist: ``vellum
    upgrade`` names a template once — ``src/vellum/seeds/templates/config.yaml``
    — and then reads it either out of a ``--from`` checkout at a ref (``git show
    <ref>:<that>``) or out of this install (here). One name, two readers; a
    second spelling of the path in either would be a template the two commands
    disagree about the location of.
    """
    prefix = PACKAGE_PATH + "/"
    if not path.startswith(prefix):
        raise SeedsMissing(
            f"{path!r} is not a path inside {PACKAGE_PATH}, so this package does "
            f"not carry it. Only the seeds ship with the CLI; everything else a "
            f"release holds is read from a checkout of it."
        )
    return _read(*path[len(prefix):].split("/"))
