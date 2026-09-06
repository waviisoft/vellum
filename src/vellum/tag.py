"""``vellum release tag`` — the name a release would carry, computed and never applied.

``spec/features/release-tags.md``: "**`vellum release tag <product-checkout>`**
computes the tag and never applies it, the division `mint` keeps
(spec/features/spec-pipeline.md): it reads the declared version, reports the
name it would mint and the commit it would name, and exits 0. `--plan` is the
same answer, stated as one. The forge workflow applies it, with the forge's own
credential, and pushes the tag; the command pushes nothing."

The division, and why it is the same one twice
----------------------------------------------
``vellum mint`` computes ``spec-v<N>`` for a spec merge and ``on-spec-merge.yml``
is what runs ``git tag -a`` and pushes it. This module is that shape one repo
over: it answers "what name, and on what commit", and
``.github/workflows/release-cut.yml`` is what writes the ref. Nothing here
shells out to ``git tag``, nothing here pushes, and nothing here writes a byte
into the checkout — the acceptance scenario
``@id:release-tag-leaves-a-used-name-alone`` reads every file, every ref and
``HEAD`` back after a run and compares them, so "computes" is a checked property
rather than a promise in a docstring.

The declaration is data
-----------------------
"A product repo declares where its version lives ... The declaration is data:
nothing infers a version from tags, commits or file contents the block does not
name." So there is no fallback here at all. A checkout with no ``release:``
block is not a checkout whose version this guesses from ``pyproject.toml``
because one happens to be lying there; it is a checkout this cannot answer
about, which is exit 2.

The three sources are the spec's three, and only the third needs no parser:

* ``pyproject.toml`` — ``[project] version``, read with ``tomllib`` (3.11+) or
  ``tomli`` below it. The floor in ``pyproject.toml`` is 3.10, so the fallback
  is a real dependency and not a courtesy.
* ``package.json`` — the top-level ``version``.
* anything else — the trimmed contents of that file, which is the spec's own
  reading and the one the acceptance fixtures use.

Which of the three is decided by the file's **name**, not by its contents: a
declaration naming ``pyproject.toml`` and getting the trimmed-contents reader
because the parse failed would report a version that is most of a TOML file.

What a declaration may name, and why the checks are where they are
------------------------------------------------------------------
The ``release:`` block is a file in a repository anybody who can land a pull
request writes, and this command opens what it names and prints what it read —
in CI, on a runner holding ``contents: write`` for the repository it is about.
So a declared path is held three times, each in the place that can answer:

* **as a string** (:func:`_relative`): repo-relative, POSIX, printable, no
  ``..``, no first component ``.git``.
* **as a path on a disk** (``vellum.paths.unsafe_read``): no component a
  symlink, resolving inside the checkout, and a regular file — a FIFO never
  ends and ``/dev/zero`` never stops.
* **as bytes** (:data:`VERSION_SOURCE_LIMIT`): the one reader with no parser in
  front of it reads at most 4 KiB, because "a version file is one line".

And what comes back is held twice: :data:`VERSION_RE` says what a version may
be, and ``git check-ref-format`` says what a ref may be, which is the rule that
actually decides whether the workflow's ``git tag`` can succeed. No message on
any of these paths quotes what was read — the file's contents are the pull
request author's text, and this command's report is a CI log.

The exit codes, and why a missing changelog entry is 1 rather than 2
--------------------------------------------------------------------
"Exit codes follow the guards' contract" — 0 is an answer, 1 is the answer you
will not like, 2 is no answer. The changelog refusal is 1 because the command
*did* answer: it read the declaration, computed the name, and found that the
release nobody described is not one to name yet. A source file that cannot be
read is 2 because there is no answer to give. Getting these the wrong way round
would make the ``release-cut`` workflow treat "I could not read the repository"
as "this version is undescribed", and vice versa.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from vellum import paths, product
from vellum.gitver import GitUnavailable, ref_format_ok, resolve, tags
from vellum.text import one_line

#: The block a product repo declares, and the two keys under it. Only these two
#: are read; a third key is the installation's own and is passed over rather
#: than refused, the posture ``vellum.config`` takes to the config file.
RELEASE_KEY = "release"
VERSION_SOURCE_KEY = "version_source"
CHANGELOG_KEY = "changelog"

#: The two keys a YAML changelog is read through: the top-level list of entries,
#: and the key inside an entry that names the release. Both are the shape
#: ``src/vellum/seeds/CHANGES.yaml`` ships and ``vellum.changes`` reads, named
#: here rather than imported so that a module about tags does not depend on the
#: upgrade machinery to ask one question about a file.
RELEASES_KEY = "releases"
RELEASE_ENTRY_KEY = "release"

#: The two version sources that need a parser, by file name. Matched on the
#: name and never on the contents: a ``pyproject.toml`` this failed to parse
#: must be a refusal, not a fall back to "the trimmed contents of that file",
#: which would report most of a TOML file as a version.
PYPROJECT = "pyproject.toml"
PACKAGE_JSON = "package.json"

#: A version this is willing to mint a name from. Dotted, because ``v<version>``
#: is a tag name a forge resolves and an operator reads: a `version_source`
#: pointing at a README would otherwise turn a paragraph into a ref name. A
#: pre-release or build suffix is allowed after the dotted core — those are
#: versions somebody chose — and a bare integer is not, because that is what an
#: unrelated one-line file looks like.
#:
#: ``[0-9]`` and not ``\d``, and the three look-aheads, are all one point: this
#: value becomes a REF NAME, so what it may be is what git will accept. ``\d``
#: matches the fullwidth digits and every other decimal digit Unicode has, and
#: ``０.４.０`` is a version no forge resolves and no operator can type back.
#: ``..``, a trailing ``.`` and a ``.lock`` suffix are three names git itself
#: refuses — the same look-aheads ``install.REF_RE`` carries, for the same
#: reason. :func:`plan` asks ``git check-ref-format`` as the last word;
#: this is the first, so a value that never reaches git is refused with a
#: message about versions rather than one about refs.
VERSION_RE = re.compile(
    r"^(?!.*\.\.)(?!.*\.lock$)"
    r"[0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z][0-9A-Za-z.\-]*)?(?<!\.)$"
)

#: How much of a plain-text version source this reads before refusing it. A
#: version file is one line; a `version_source` naming a log, an archive or a
#: device is a file this would otherwise read to the end of, in a workflow, into
#: a string it then puts in a report. 4 KiB is far past every real one and far
#: short of anything that hurts.
VERSION_SOURCE_LIMIT = 4096

#: How wide a commit is printed in the PROSE report. Long enough to be
#: unambiguous in any repository anybody will run this in, short enough to read.
#: The full sha is what the workflow tags, and `--json` carries it — that is the
#: answer a machine reads, and abbreviating it there would make the workflow
#: resolve a name this had already resolved. Only the prose is abbreviated.
ABBREV = 12


class TagError(Exception):
    """The command could not answer: no declaration, an unreadable source."""


class TagRefused(Exception):
    """The command answered, and the answer is that nothing is tagged yet."""


# `tomli` is the 3.10 half of one reader. Imported here rather than inside the
# function so that an environment missing both says so once, at the point the
# version is read, with the install line in the message.
try:  # pragma: no cover - which branch runs is the interpreter's choice
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    try:
        import tomli as _toml
    except ModuleNotFoundError:
        _toml = None


@dataclass(frozen=True)
class Declaration:
    """The ``release:`` block, read. Paths are repo-relative and validated."""

    version_source: str
    changelog: str | None = None


@dataclass
class Plan:
    """What ``vellum release tag`` would have the forge do. Never does it."""

    checkout: Path
    declaration: Declaration
    version: str
    commit: str
    #: True when a ref by that name already exists in the checkout.
    used: bool

    @property
    def tag(self) -> str:
        return f"v{self.version}"

    @property
    def short(self) -> str:
        return self.commit[:ABBREV]

    def report(self) -> str:
        lines = [
            f"vellum release tag — {self.checkout}",
            f"  version:    {self.version} (from {self.declaration.version_source})",
        ]
        if self.declaration.changelog is not None:
            lines.append(
                f"  changelog:  {self.declaration.changelog} "
                f"(an entry for {self.tag} is there)"
            )
        lines.append(f"  commit:     {self.short} (HEAD of this checkout)")
        lines.append("")
        if self.used:
            # The word "used" is the spec's own — "the command reports the name
            # is used and exits 0" — and it is the word an operator's eye finds
            # in a workflow log full of green runs that did nothing.
            lines.append(
                f"The name {self.tag} is already USED: a ref by that name exists "
                f"in this checkout. It is not moved and it is not an error, so a "
                f"merge that does not bump the version — and a re-run over one "
                f"that did — is a no-op. Nothing was written."
            )
        else:
            lines.append(
                f"Would mint {self.tag} at {self.short}. That name is unused in "
                f"this checkout."
            )
        lines.append("")
        lines += [
            "This command computes and never applies: no tag was created, nothing "
            "was pushed, and not a byte was written into the checkout. The "
            "`release-cut` workflow is what creates the tag and pushes it, with "
            "the forge's own credential (spec/features/release-tags.md).",
            "",
            "A release tag is decoration. Versions are commits "
            "(spec/decisions/2026-08-28-versions-are-commits.md) and the cut "
            "itself is `vellum release cut`, which records commits in "
            "ledger/releases.yaml; this name is the friendly stamp such a cut "
            "may carry, and nothing reads it to decide anything.",
        ]
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "checkout": str(self.checkout),
            "version": self.version,
            "tag": self.tag,
            "commit": self.commit,
            "used": self.used,
            "version_source": self.declaration.version_source,
            "changelog": self.declaration.changelog,
        }


def _relative(value, *, where: str, path: Path) -> str:
    """One declared path, held to being repo-relative and inside the checkout.

    The block is a file in a repository anyone who can land a pull request can
    edit, and this command opens what it names. The reads are the only thing
    that happens to these paths — nothing here writes — but a `version_source`
    of `../../.ssh/id_rsa` reaching `open()` would make a report that prints its
    first line, and this runs in CI. Refused rather than normalised: there is no
    sensible reading of "the version lives outside this repository".
    """
    if not isinstance(value, str) or not value.strip():
        raise TagError(
            f"{one_line(path)}: {where} is {one_line(value)!r}; it must be a "
            f"repo-relative path to the file the version is read from."
        )
    text = value
    # Printable, and already stripped — the rule `vellum.manifest` states for an
    # `owned:` entry, for the same reason one repo over. This value is printed
    # into a report, and that report is piped into `$GITHUB_STEP_SUMMARY` and cut
    # down to one line for a `::error` annotation by the `release-cut` workflow.
    # A carriage return followed by `::error title=…` is a workflow command at
    # column 0, written by whoever could land a line in `.vellum/product.yaml`;
    # `one_line` flattens what reaches a message, and this refuses the value
    # outright, because a path with a newline in it names no file anyway.
    if not text.isprintable() or text != text.strip():
        raise TagError(
            f"{one_line(path)}: {where} is {one_line(text)!r}, which is not a "
            f"printable path with no surrounding whitespace. A control character "
            f"reaches a CI log as itself, where a line of its own is all a "
            f"workflow command needs — so it is refused rather than trimmed."
        )
    if text.startswith("/") or "\\" in text:
        raise TagError(
            f"{one_line(path)}: {where} is {one_line(text)!r}; entries are "
            f"repo-relative POSIX paths, so an absolute path or a backslash is "
            f"refused."
        )
    parts = [p for p in PurePosixPath(text).parts if p != "."]
    if any(p == ".." for p in parts):
        raise TagError(
            f"{one_line(path)}: {where} is {one_line(text)!r}, which escapes the "
            f"repository with '..'. The version lives in the repo whose version "
            f"it is."
        )
    if not parts:
        raise TagError(
            f"{one_line(path)}: {where} is {one_line(text)!r}, which names no file."
        )
    # Lexical, and kept beside the filesystem walk that also refuses it
    # (`vellum.paths.unsafe_read`) rather than instead of it: this one is about
    # the string an operator wrote and can be answered without a disk, and the
    # other is about where the components actually lead.
    if parts[0] == paths.GIT_DIR:
        raise TagError(
            f"{one_line(path)}: {where} is {one_line(text)!r}, which reads out of "
            f"`{paths.GIT_DIR}/` — git's own directory, not this repository's "
            f"content. It carries the remotes and, on a runner, the credential "
            f"`actions/checkout` persisted there. A version lives in a file the "
            f"repository tracks."
        )
    return "/".join(parts)


def declaration(checkout: str | Path) -> Declaration:
    """The ``release:`` block out of a product checkout. Never inferred.

    A checkout with no ``.vellum/product.yaml`` is not a product checkout, and a
    product checkout with no block "has not declared" — both are exit 2, which
    is the spec's own reading: "A repo that has not declared is not answered."
    """
    root = Path(checkout)
    if not root.is_dir():
        raise TagError(f"{root}: not a directory; is this a product checkout?")
    path = product.product_path(root)
    if not path.is_file():
        raise TagError(
            f"{root} carries no {product.PRODUCT_RELPATH.as_posix()}, so it is not "
            f"a product checkout. `vellum release tag` reads the `{RELEASE_KEY}:` "
            f"block a product repo declares (spec/features/release-tags.md)."
        )
    try:
        data = product.load(root)
    except product.ProductFileError as exc:
        raise TagError(str(exc)) from exc
    block = data.get(RELEASE_KEY)
    if block is None:
        raise TagError(
            f"{path} declares no `{RELEASE_KEY}:` block, so nothing says where "
            f"this repo's version lives and there is no name to compute. The "
            f"declaration is data and is never inferred: add\n"
            f"\n"
            f"    {RELEASE_KEY}:\n"
            f"      {VERSION_SOURCE_KEY}: pyproject.toml   # or package.json, or any path\n"
            f"      {CHANGELOG_KEY}: CHANGELOG.md          # optional\n"
            f"\n"
            f"(spec/features/release-tags.md)."
        )
    if not isinstance(block, dict):
        raise TagError(
            f"{path}: `{RELEASE_KEY}:` is {one_line(str(block))!r}, not a mapping "
            f"of `{VERSION_SOURCE_KEY}:` and an optional `{CHANGELOG_KEY}:`."
        )
    if VERSION_SOURCE_KEY not in block:
        raise TagError(
            f"{path}: `{RELEASE_KEY}:` declares no `{VERSION_SOURCE_KEY}:`; it "
            f"declares {', '.join(sorted(map(str, block))) or '(nothing)'}. The "
            f"block's whole job is naming the file the version is read from."
        )
    source = _relative(
        block[VERSION_SOURCE_KEY],
        where=f"{RELEASE_KEY}.{VERSION_SOURCE_KEY}", path=path,
    )
    changelog = block.get(CHANGELOG_KEY)
    if changelog is not None:
        changelog = _relative(
            changelog, where=f"{RELEASE_KEY}.{CHANGELOG_KEY}", path=path,
        )
    return Declaration(version_source=source, changelog=changelog)


def _read(root: Path, relative: str, *, what: str, limit: int | None = None) -> str:
    """The declared file's text, after the walk that says it is a file to read.

    :func:`vellum.paths.unsafe_read` comes first and the ``open()`` second, and
    the order is the whole point: `_relative` held the *string* to being
    repo-relative, and a string that is repo-relative still reaches
    ``/etc/shadow`` when ``VERSION`` is a symlink to it, or reads forever when it
    is ``/dev/zero``. What this command does with what it read is print it, in
    CI, so the check has to be about where the path leads rather than about how
    it is spelled.

    *limit*, when given, is the most this will read — see
    :data:`VERSION_SOURCE_LIMIT`. A file longer than it is refused by LENGTH and
    never by content: "a version file is one line", and a message that quoted
    what it found would be this command printing the file it just declined to
    read.
    """
    where = one_line(root / relative)
    refusal = paths.unsafe_read(root, relative)
    if refusal is not None:
        raise TagError(f"{where}: this is not a {what} to read — {refusal}")
    path = root / relative
    if limit is not None:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise TagError(
                f"{where}: cannot read the {what}: {one_line(str(exc))}"
            ) from exc
        if size > limit:
            raise TagError(
                f"{where}: the {what} is {size} bytes and this reads at most "
                f"{limit}. A version file is one line; a declaration naming a "
                f"log, an archive or a device is one this will not read to the "
                f"end of. Nothing of what is in it is quoted here, because a "
                f"file this refused to read is not one to print."
            )
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TagError(
            f"{where}: cannot read the {what}: {one_line(str(exc))}"
        ) from exc
    except UnicodeDecodeError as exc:
        # A ValueError, not an OSError. Uncaught it left this exiting 1 with a
        # traceback, and 1 is the code that must mean the changelog refusal. The
        # position is named and the byte is not: `str(exc)` carries the value it
        # choked on, which is one byte of the file's contents.
        raise TagError(
            f"{where}: the {what} is not UTF-8 text (it stops being text at "
            f"byte {exc.start})."
        ) from exc


def _from_pyproject(text: str, where: str) -> str:
    if _toml is None:  # pragma: no cover - both readers are present in CI
        raise TagError(
            f"{where} is a TOML version source and this Python has neither "
            f"`tomllib` (3.11+) nor `tomli`. Install `tomli`, or name a "
            f"`{VERSION_SOURCE_KEY}` this can read without a parser."
        )
    try:
        data = _toml.loads(text)
    except Exception as exc:  # the reader's own error type varies by backport
        raise TagError(f"{where}: not valid TOML: {one_line(str(exc))}") from exc
    project = data.get("project")
    # `isinstance`, not `or {}`: a `project = "0.4.0"` line — or a `[project]`
    # somebody wrote as an array of tables — parses fine and is not a mapping,
    # and `.get` on it was an AttributeError that left this exiting 1 with a
    # traceback. 1 is the code that must mean the changelog refusal, so a
    # malformed pyproject reaching it would have the `release-cut` workflow tell
    # an operator to write a changelog entry for a version it never read.
    if project is not None and not isinstance(project, dict):
        raise TagError(
            f"{where}: `project` is not a table, so it declares no `[project] "
            f"version`. The version source a `{RELEASE_KEY}:` block names has to "
            f"be a pyproject this can read a version out of."
        )
    version = (project or {}).get("version")
    if version is None:
        raise TagError(
            f"{where} declares no `[project] version`, so the file this "
            f"installation named as its version source does not yield one."
        )
    return str(version).strip()


def _from_package_json(text: str, where: str) -> str:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise TagError(f"{where}: not valid JSON: {one_line(str(exc))}") from exc
    # A top-level array, string or number is JSON this parsed and cannot read a
    # `version` out of; `isinstance` is what keeps it a refusal rather than a
    # `TypeError` — the same guard `_from_pyproject` needs one file over.
    if not isinstance(data, dict) or "version" not in data:
        raise TagError(
            f"{where} declares no top-level `version`, so the file this "
            f"installation named as its version source does not yield one."
        )
    return str(data["version"]).strip()


def version_from(root: Path, declared: Declaration) -> str:
    """The declared version, by the reader the source's NAME selects.

    "``pyproject.toml`` (its project version), ``package.json`` (its version),
    or any other path, read as the trimmed contents of that file."
    """
    relative = declared.version_source
    where = one_line(root / relative)
    name = PurePosixPath(relative).name
    if name == PYPROJECT:
        version = _from_pyproject(_read(root, relative, what="version source"), where)
    elif name == PACKAGE_JSON:
        version = _from_package_json(
            _read(root, relative, what="version source"), where
        )
    else:
        # The one reader with no parser in front of it, and so the one that is
        # capped: a `pyproject.toml` or a `package.json` this cannot parse is
        # already a refusal, and "the trimmed contents of that file" is the
        # branch where a declaration naming something enormous would otherwise be
        # read to the end.
        version = _read(
            root, relative, what="version source", limit=VERSION_SOURCE_LIMIT,
        ).strip()
    if not VERSION_RE.match(version):
        # The LENGTH, never the contents. This message is printed in CI and cut
        # to one line for a `::error` annotation by the `release-cut` workflow,
        # and the file it is about is one anybody who can land a pull request
        # writes — so quoting what was found would put their text in the log
        # under this command's name. What an operator needs is which file, and
        # that what came out of it is not a version.
        raise TagError(
            f"{where} yields {len(version)} characters that are not a version "
            f"this can name a tag from. A tag is `v<version>` and a version is "
            f"dotted, in ASCII digits — `0.4.0`, `1.10.2`, `2.0.0-rc.1` — and it "
            f"may not carry `..`, end with `.`, or end with `.lock`, three names "
            f"git itself refuses. Nothing is inferred from tags or commits, so "
            f"there is no second place to look."
        )
    return version


def _yaml_releases(text: str) -> list | None:
    """The ``releases:`` list of a changelog that is YAML, or None.

    This project's own ``CHANGES.yaml`` is one, and so is every installation's:
    the seeded changelog IS a YAML document with a ``releases:`` list whose
    entries carry ``release: v0.4.0``. When a changelog is that, "does it carry
    an entry for this version" is a question with an exact answer, and the
    substring test that used to stand here was answering a different one.

    None for anything that is not that shape — a Markdown changelog, a table, a
    text file — which is the common case and falls to :func:`_names_at_boundary`
    below. A Markdown file whose lines all begin ``#`` parses as YAML to
    ``None``; that is not a refusal, it is "this is not the YAML shape".
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    releases = data.get(RELEASES_KEY)
    return releases if isinstance(releases, list) else None


def _names_at_boundary(text: str, version: str) -> bool:
    """Whether *version* appears in *text* as a name rather than inside one.

    The old test was ``version in text``, and it said yes to three things that
    are not an entry for this version: ``10.4.0`` contains ``0.4.0``,
    ``0.4.0-rc1`` contains ``0.4.0``, and a sentence saying the number does not
    describe a release. What is asked for here is a *heading or a line*, so the
    version has to start one or follow the punctuation a changelog heads an entry
    with — ``## 0.4.0``, ``## [0.4.0] - 2026-09-06``, ``* v0.4.0``, ``(0.4.0)``
    — and nothing may follow it that would make it a longer name.

    What this deliberately does NOT do is judge prose: a line reading "we shipped
    0.4.0 last week" satisfies it, because a reader that refused that would have
    to understand the changelog's shape, and the shape is whatever its project
    keeps. The YAML branch above is where an exact answer is possible, and it is
    taken when it is available.
    """
    return bool(re.search(
        rf"(^|[\s#\[(])v?{re.escape(version)}(?![0-9A-Za-z\-])(?!\.[0-9A-Za-z])",
        text, re.M,
    ))


def changelog_names(text: str, version: str) -> bool:
    """Whether a changelog carries an entry for *version*, in either shape.

    Two branches, because a changelog is either a document this can read or prose
    in whatever shape its project keeps:

    * **YAML with a ``releases:`` list** — this project's own ``CHANGES.yaml``,
      and every installation's. An entry whose ``release`` is ``v<version>`` or
      ``<version>``, and nothing else will do: a version named only in a comment
      or in another entry's summary is a version nobody wrote an entry for, and
      the whole refusal exists to say so.
    * **anything else** — the version at a line or heading boundary, either
      spelling, because an entry may be headed ``v0.5.0`` or ``0.5.0`` and
      refusing the second would fail a project whose changelog has always been
      written the other way.
    """
    releases = _yaml_releases(text)
    if releases is not None:
        wanted = {version, f"v{version}"}
        return any(
            isinstance(entry, dict) and str(entry.get(RELEASE_ENTRY_KEY)) in wanted
            for entry in releases
        )
    return _names_at_boundary(text, version)


def plan(checkout: str | Path) -> Plan:
    """The name, the commit, and whether the name is used. Writes nothing.

    Every read is a read: the product file, the version source, the changelog,
    ``git rev-parse`` and ``git for-each-ref``. There is no branch of this
    function that creates a ref, and the acceptance suite asserts it by
    comparing the whole ref table across a run.
    """
    root = Path(checkout)
    declared = declaration(root)
    version = version_from(root, declared)
    tag = f"v{version}"
    # git's own answer, as the last word after `VERSION_RE`'s first one. The
    # regex is what this module can say about a *version*; `check-ref-format` is
    # the complete statement of what a REF may be, it is the forge's rule too,
    # and it is a dozen clauses that a regex kept beside it would drift from. A
    # name git refuses here is one `git tag` would refuse in the middle of the
    # `release-cut` workflow, with `contents: write` in hand and half a job done.
    if not ref_format_ok(root, f"refs/tags/{tag}"):
        raise TagError(
            f"{one_line(root / declared.version_source)} yields a version whose "
            f"tag name `{one_line(tag)}` is one `git check-ref-format` refuses, "
            f"so no tag by that name can be created and no forge would resolve "
            f"it. Declare a version git will take: dotted ASCII digits, with no "
            f"`..`, no trailing `.` and no `.lock`."
        )

    # The changelog is checked BEFORE the name is looked up, and the ordering
    # is a reading of two spec sentences that only ever meet in one odd case: a
    # tag that exists and a changelog entry that has since been deleted. "A
    # missing entry is a refusal" is written without an exception, so it gets
    # none here. Idempotency is not what pays for it — a name that was minted
    # was minted over a changelog that described it, so the re-run the workflow
    # makes finds the entry and reaches the used-name answer below.
    if declared.changelog is not None:
        text = _read(root, declared.changelog, what="changelog")
        if not changelog_names(text, version):
            # 1, not 2: this answered. "A version with no changelog entry is a
            # release nobody described; the tag can wait for the line that
            # describes it." Both nouns are named because a refusal that named
            # neither leaves an operator with a red and no next step.
            # Worded for the shape the file has: a YAML changelog is read as
            # entries, so "names neither" would be false of a file whose
            # entries use another key or describe other versions by name.
            if _yaml_releases(text) is not None:
                missing = (
                    f"no entry in `{RELEASES_KEY}:` has `{RELEASE_ENTRY_KEY}: "
                    f"{tag}` or `{RELEASE_ENTRY_KEY}: {version}`"
                )
            else:
                missing = (
                    f"no line or heading names `{tag}` or `{version}`"
                )
            raise TagRefused(
                f"{one_line(root / declared.changelog)} carries no entry for "
                f"{tag}: {missing}. A version its changelog does not describe "
                f"is not tagged — write the {tag} entry in "
                f"{declared.changelog} and run this again. Nothing was tagged "
                f"(spec/features/release-tags.md)."
            )

    try:
        commit = resolve(root, "HEAD")
    except GitUnavailable as exc:
        raise TagError(
            f"{one_line(root)}: cannot read HEAD ({one_line(str(exc))}). The tag "
            f"names the commit the default branch is at, so this needs a git "
            f"checkout."
        ) from exc
    try:
        # An exact-name glob, so this asks about ONE name. Listing every tag and
        # searching it would put the other names in front of a report that is
        # about this one — and `release-tag-is-minted-on-the-default-branch`
        # asserts that the report names no tag but the one it would mint.
        used = bool(tags(root, tag))
    except GitUnavailable as exc:
        raise TagError(
            f"{one_line(root)}: cannot read the tags ({one_line(str(exc))}), so "
            f"this cannot say whether {tag} is already used."
        ) from exc

    return Plan(
        checkout=root, declaration=declared, version=version,
        commit=commit, used=used,
    )


def run_tag(checkout: str, as_json: bool = False, out=None) -> int:
    """Report the name and the commit. Exit 0 whether or not the name is used."""
    stream = out if out is not None else sys.stdout
    result = plan(checkout)
    if as_json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True), file=stream)
    else:
        print(result.report(), file=stream)
    return 0
