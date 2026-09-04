# Wave: go-live prep — a licence, and a token that is optional

`waviisoft/vellum` is being made public so an installation can read the CLI with
no credential of its own (waviisoft/vellum-intent#74). Three things had to
happen in this repo first, and a fourth turned out to be the interesting one.

## 1. MIT, and the packaging that carries it

`LICENSE` (MIT, "Copyright (c) 2026 WAVIISoft") plus `license = "MIT"` and
`license-files = ["LICENSE"]` in `pyproject.toml`.

**The build-system floor moved from `setuptools>=61` to `>=77`, and that is not
tidying.** `license = "MIT"` as a plain string is the PEP 639 SPDX expression;
the PEP 621 shape a setuptools of 61 understands is `license = {text = "MIT"}`,
and it *errors* on the string. `license-files` is PEP 639 too. So the floor has
to say what the metadata needs, or a build isolated onto an older setuptools
fails at metadata generation — later and less legibly than at resolution.

**No `License :: OSI Approved :: MIT License` classifier**, and not because the
table happens to declare no classifiers: setuptools>=77 *refuses* a licence
classifier beside an SPDX expression. Adding one "for completeness" breaks the
build.

Measured, not assumed: `pip install -e .` still works; `python3 -m build
--sdist` puts `LICENSE` in the tarball with `License-Expression: MIT` and
`License-File: LICENSE` in `PKG-INFO`; `pip wheel .` puts it at
`vellum-0.1.0.dist-info/licenses/LICENSE`.

`tests/test_deps.py::test_on_this_repos_own_pyproject` holds two readers of this
file to the same answer, and `license-files` is an array in `[project]` — it
passes because `_DEP_TABLES` reads only `dependencies` there. A future key that
is an array in `[project]` and *is* a dependency list would not be so lucky.

## 2. `VELLUM_TOKEN` became optional

The mechanics, the reason the `||` was chosen over two `if:`-gated checkouts, and
what is still unverified are all in
`.vellum/memory/areas/adapters-github.md` — the paragraph beginning
"**`VELLUM_TOKEN` IS OPTIONAL**". `doctor`'s side is in `areas/cli.md`
("**`no-secret` is GONE**").

The one thing worth repeating here because it is a *landing* fact rather than a
design one: **until `waviisoft/vellum` is actually public, a caller that passes
no token still fails at the CLI checkout.** This lands safely ahead of the flip
because every stub `vellum init` renders still passes the secret by name; what
changed is that the stub is no longer *required* to, and the shipped workflow no
longer refuses the run of a caller that omits it.

The stub templates in `adapters/github/` did not change, and that is the correct
outcome rather than an omission: a caller may keep passing the secret, so
`render()` still emits it and the byte-identity assertion in
`tests/test_install.py` holds untouched.

## 3. A stranger's read

Every file a person outside WAVIISoft would open, read as one: `README.md`,
`adapters/github/README.md`, the four workflows' comments, and this memory. The
edits are listed in the PR body. The shape of all of them is the same and worth
stating once, because the next wave will meet it again:

**Nothing here was secret. What was wrong was the unstated "we".** "This
organisation schedules Actions on Blacksmith" is a true sentence that silently
assumes the reader is inside the organisation it names — it reads to an outsider
as a property of Vellum. The fix is never to delete the fact: it is to say
*whose* choice it is and what an installation elsewhere would change. Every
Blacksmith paragraph in this repo now names WAVIISoft as the hosting
organisation and says what a fork edits.

The second shape: **"private" was written as a permanent property.** Half a
dozen comments justified a decision with "waviisoft/vellum is private", and that
sentence is about to stop being true. Each of them now either names the
condition (`while it is private`) or records the reason as spent — see the
checkout paragraph in `areas/adapters-github.md`, which now says outright that
both of its original reasons are gone and what is left is smaller.

## 4. Open, and named rather than hidden

- **`.vellum/product.yaml` was edited to widen
  `write_boundaries.implementer`** with `pyproject.toml` and `LICENSE`, and that
  file is itself outside the implementer's trees. So `vellum verify boundaries .
  --role implementer` reports exactly one path: the file that declares the
  boundary. This is a bootstrap, not a breach — the wave was asked for a licence
  and the packaging that carries it, and neither file could be in the diff
  without the block naming them.

  The installer wave met the same wall for `.github/workflows/` and recorded it
  as "the architect widens `write_boundaries.implementer` at landing". The
  widening is *in* this PR instead, for the reason this file has now recorded
  three times over: a "someone should do this" note outlives the doing of it.
  The reviewer accepts one edit or asks for it to be split out; either way it is
  visible in the diff rather than owed after the merge.

- **The repo is not public yet.** Everything in item 2 is correct and inert
  until it is. The first installation run against a public `waviisoft/vellum` is
  what settles whether a caller's own `github.token` reads it cross-organisation
  — documented behaviour, no forge available here to run it.

- **`test_no_job_asks_for_a_github_hosted_runner` asserts a WAVIISoft fact.**
  Now that the workflows say so in prose, the test that pins the labels is
  visibly an assertion about this organisation's runners rather than about
  correctness. A fork changes the test with the workflows. Not fixed here: the
  fix is the `runs-on` input nobody has asked for.
