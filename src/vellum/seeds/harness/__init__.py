"""Makes the harness skeleton a package, so setuptools ships it. Not seeded.

This file exists for exactly one reason and has no other job. ``vellum init``
writes ``harness/`` into a new installation out of package data, and package
data is shipped by *declaration*: without one, a wheel of this project carries
whichever files setuptools' defaults happen to include, which on the version
that built it was every ``.py`` under here and not ``harness/README.md``. An
installation seeded from such a wheel is missing a file, and nothing else in the
system would say so.

Declaring it in ``pyproject.toml``'s ``[tool.setuptools.package-data]`` would be
the direct route. It is not taken because ``pyproject.toml`` is outside the
implementer's write boundary (``.vellum/product.yaml``,
``spec/behaviors/write-boundaries.md``) and the seed is not worth a crossing:
``__init__.py`` here, and in ``support/`` and ``steps/``, makes every seeded
file an ordinary module of an ordinary package, which every builder ships
because it must.

Two consequences, both deliberate:

* **This file is not part of the seed.** ``vellum.seeds.harness_files`` skips
  it by name; a seeded ``harness/__init__.py`` would make an installation's
  harness a package, which it is not and does not want to be — ``run.py`` puts
  ``harness/`` itself on ``sys.path`` and imports ``support`` and ``steps`` as
  top-level.
* **The modules under here are shipped, not runnable in place.**
  ``support/world.py`` says ``from support.adapter import …``, which resolves in
  a seeded installation and not here. Nothing imports them from this package,
  and nothing should: they are text with a ``.py`` extension until ``init``
  writes them somewhere they mean something.

Anything that is not a file copied verbatim into a new installation belongs in
``vellum.provision``'s templates instead, with the rest of the seed.
"""
