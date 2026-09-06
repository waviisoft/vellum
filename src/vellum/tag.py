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

from vellum import product
from vellum.gitver import GitUnavailable, resolve, tags
from vellum.text import one_line

#: The block a product repo declares, and the two keys under it. Only these two
#: are read; a third key is the installation's own and is passed over rather
#: than refused, the posture ``vellum.config`` takes to the config file.
RELEASE_KEY = "release"
VERSION_SOURCE_KEY = "version_source"
CHANGELOG_KEY = "changelog"

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
VERSION_RE = re.compile(r"^\d+(?:\.\d+)+(?:[-+][0-9A-Za-z][0-9A-Za-z.\-]*)?$")

#: How wide a commit is printed. Long enough to be unambiguous in any repository
#: anybody will run this in, short enough to read; the full sha is what the
#: workflow tags, and it never travels through this report.
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
            f"{path}: {where} is {value!r}; it must be a repo-relative path to "
            f"the file the version is read from."
        )
    text = value.strip()
    if text.startswith("/") or "\\" in text:
        raise TagError(
            f"{path}: {where} is {one_line(text)!r}; entries are repo-relative "
            f"POSIX paths, so an absolute path or a backslash is refused."
        )
    parts = [p for p in PurePosixPath(text).parts if p != "."]
    if any(p == ".." for p in parts):
        raise TagError(
            f"{path}: {where} is {one_line(text)!r}, which escapes the repository "
            f"with '..'. The version lives in the repo whose version it is."
        )
    if not parts:
        raise TagError(
            f"{path}: {where} is {one_line(text)!r}, which names no file."
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


def _read(root: Path, relative: str, *, what: str) -> str:
    try:
        return (root / relative).read_text(encoding="utf-8")
    except OSError as exc:
        raise TagError(
            f"{root / relative}: cannot read the {what}: {one_line(str(exc))}"
        ) from exc
    except UnicodeDecodeError as exc:
        # A ValueError, not an OSError. Uncaught it left this exiting 1 with a
        # traceback, and 1 is the code that must mean the changelog refusal.
        raise TagError(
            f"{root / relative}: the {what} is not UTF-8 text "
            f"({one_line(str(exc))})."
        ) from exc


def _from_pyproject(text: str, where: Path) -> str:
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
    version = (data.get("project") or {}).get("version")
    if version is None:
        raise TagError(
            f"{where} declares no `[project] version`, so the file this "
            f"installation named as its version source does not yield one."
        )
    return str(version).strip()


def _from_package_json(text: str, where: Path) -> str:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise TagError(f"{where}: not valid JSON: {one_line(str(exc))}") from exc
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
    where = root / relative
    text = _read(root, relative, what="version source")
    name = PurePosixPath(relative).name
    if name == PYPROJECT:
        version = _from_pyproject(text, where)
    elif name == PACKAGE_JSON:
        version = _from_package_json(text, where)
    else:
        version = text.strip()
    if not VERSION_RE.match(version):
        raise TagError(
            f"{where} yields {one_line(version)!r}, which is not a version this "
            f"can name a tag from. A tag is `v<version>` and a version is dotted "
            f"— `0.4.0`, `1.10.2`, `2.0.0-rc.1`. Nothing is inferred from tags or "
            f"commits, so there is no second place to look."
        )
    return version


def changelog_names(text: str, version: str) -> bool:
    """Whether a changelog carries an entry for *version*, either spelling.

    A substring test, deliberately: a changelog is prose in whatever shape its
    project keeps — a Markdown heading, a YAML key under ``releases:``, a line
    in a table — and a reader that understood one of those would refuse the
    other two. What the spec asks is whether the version is *described*, and the
    honest checkable half of that is whether the file mentions it at all.

    Both spellings, because an entry may be headed ``v0.5.0`` or ``0.5.0`` and
    refusing the second would fail a project whose changelog has always been
    written the other way.
    """
    return f"v{version}" in text or version in text


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
            raise TagRefused(
                f"{root / declared.changelog} carries no entry for {tag}: it "
                f"names neither `{tag}` nor `{version}`. A version its changelog "
                f"does not describe is not tagged — write the {tag} entry in "
                f"{declared.changelog} and run this again. Nothing was tagged "
                f"(spec/features/release-tags.md)."
            )

    try:
        commit = resolve(root, "HEAD")
    except GitUnavailable as exc:
        raise TagError(
            f"{root}: cannot read HEAD ({one_line(str(exc))}). The tag names the "
            f"commit the default branch is at, so this needs a git checkout."
        ) from exc
    try:
        # An exact-name glob, so this asks about ONE name. Listing every tag and
        # searching it would put the other names in front of a report that is
        # about this one — and `release-tag-is-minted-on-the-default-branch`
        # asserts that the report names no tag but the one it would mint.
        used = bool(tags(root, tag))
    except GitUnavailable as exc:
        raise TagError(
            f"{root}: cannot read the tags ({one_line(str(exc))}), so this cannot "
            f"say whether {tag} is already used."
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
