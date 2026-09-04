"""Files ``vellum init`` seeds into a new installation, shipped as package data.

Package data rather than repository paths, for the reason ``vellum.install``'s
docstring gives about the caller stubs: an installed CLI is a wheel, and a
command that read ``harness/`` off disk would work from a development checkout
and fail from a pip install. ``pyproject.toml`` carries the
``[tool.setuptools.package-data]`` entry that ships this directory, and
``tests/test_init_provision.py`` asserts the seeded tree is non-empty and
importable, so a packaging change that dropped it cannot pass silently.

The split against ``vellum.install``'s ``SHIPPED`` table is deliberate and is
the same distinction one level down. A caller stub is *generated* — it
interpolates a host, a ref and a branch, and doctor compares an installation's
against a fresh render, so it has to be a template. The harness skeleton is
*copied*: nothing in it varies by installation, nothing compares an
installation's copy against it afterwards, and it is ordinary Python a
harness engineer will edit on day one. A template engine over files nobody
re-renders would be surface with no reader.

Only the harness lives here. The seeded spec tree, config, workspace and
ledger are rendered from templates in ``vellum.provision``, because every one
of them interpolates the product, the org, the areas or the docs.
"""

from __future__ import annotations

from importlib import resources

#: The directory inside this package holding the harness skeleton.
HARNESS = "harness"


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
                walk(child, relative)
            else:
                found[relative] = child.read_text(encoding="utf-8")

    walk(root, HARNESS)
    return dict(sorted(found.items()))
