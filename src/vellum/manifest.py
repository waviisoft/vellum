"""``.vellum/install.yaml`` — the installation manifest, and its only reader.

``spec/features/installation.md``: "An installation names the files Vellum
owns. ``.vellum/install.yaml`` on each side of the pair — written at
provisioning and by every stamp and upgrade — records the Vellum release the
installation was last brought to and the repo-relative paths Vellum may rewrite
on upgrade. A path not listed is the product's; no upgrade touches it, and
ownership is never inferred from a file's contents or history."

Two keys, and no more
---------------------
``vellum:`` is the release the installation was last *brought to* — not the ref
its stubs happen to pin, which ``doctor`` reads out of the stubs themselves, and
not the CLI that wrote it. ``owned:`` is the set of repo-relative paths an
upgrade may rewrite. Nothing else lives here: every other fact about an
installation already has a file that owns it (``.vellum/workspace.yaml`` the
repo map, ``.vellum/product.yaml`` the pin, the stubs the ref and the branch),
and a manifest that restated one would be a second place for it to drift.

Ownership is data, and that is the whole point
----------------------------------------------
The decision (``spec/decisions/2026-09-04-vellum-owned-files-and-upgrades.md``)
rejected inferring ownership from history — "a product that edited a seeded file
once and reverted it would silently flip ownership; data beats a heuristic". So
nothing in this module or in ``vellum.upgrade`` ever looks at a file's contents
or its log to decide whether Vellum owns it. The list is read, and what is not
on it is not touched. :func:`vellum.owned.for_side` computes the list once, at
provisioning; after that it is the operator's file to edit, and the two ways out
of a refused upgrade are both edits to it.

Why the paths are validated on the way in
-----------------------------------------
``upgrade`` *writes* every path on this list, and the list is a file in a
repository that anyone who can land a pull request can edit. So a path is held
to being repo-relative and inside the checkout — no absolute path, no ``..``
component, no backslash — and a manifest carrying one is **malformed**, not a
list with one bad entry skipped. Skipping would let a manifest that half-works
go on half-working; refusing puts it in front of the operator once, which is
also what ``doctor`` reports it as.

The manifest is not itself owned. It is rewritten by every stamp and every
upgrade unconditionally, so listing it would compare a file against a template
of itself; :data:`MANIFEST_RELPATH` in ``owned:`` is refused for that reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from vellum.text import one_line

#: Where the manifest lives, on both sides of a pair.
MANIFEST_RELPATH = Path(".vellum") / "install.yaml"

#: The release key, and the owned key. Named rather than spelled at four call
#: sites: this file is read by ``doctor``, written by ``init`` and rewritten by
#: ``upgrade``, and a typo in one of the three is a manifest the other two
#: cannot see.
RELEASE_KEY = "vellum"
OWNED_KEY = "owned"

#: A release the manifest may name. Deliberately wider than
#: ``install.RELEASE_RE`` — a manifest written by a pre-release install may name
#: ``main`` or a sha, and refusing to *read* such a file would leave an
#: installation with no way to upgrade off it. It is narrow enough to be a
#: single-line YAML scalar and a git ref: what it must not be is a value that
#: reshapes the file it is written back into.
RELEASE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}$")

HEADER = """# The installation manifest. Written by `vellum init` and rewritten by every
# stamp and every `vellum upgrade` (spec/features/installation.md).
#
# `{release_key}` is the Vellum release this installation was last brought to. It is
# what `vellum upgrade` compares owned files against, so it is a claim about
# the FILES, not about the ref the caller stubs pin — `vellum doctor` reads that
# out of the stubs.
#
# `{owned_key}` is the repo-relative paths Vellum may rewrite on upgrade. A path that
# is not listed is this installation's own and no upgrade touches it. Ownership
# is DATA: nothing infers it from a file's contents or its history, so this list
# is the only thing that decides. Two edits are the two ways out of a refused
# upgrade — put a file back as Vellum shipped it, or take its line out of here
# and keep it for good.
{release_key}: {release}
"""

#: What an empty ``owned:`` list is written as. A manifest that owns nothing is
#: a legal installation — every seeded file has been taken over — and YAML's
#: ``[]`` says so where a bare key followed by nothing reads back as ``None``
#: and would have to be told apart from a truncated file.
EMPTY_OWNED = "[]"


class ManifestError(Exception):
    """The manifest could not be read: it is absent where one is required, or malformed."""


@dataclass(frozen=True)
class Manifest:
    """One installation's ``.vellum/install.yaml``."""

    #: The checkout it was read from.
    checkout: Path
    #: The release the installation was last brought to.
    release: str
    #: Repo-relative paths Vellum may rewrite, sorted and deduplicated.
    owned: tuple[str, ...]

    @property
    def path(self) -> Path:
        return self.checkout / MANIFEST_RELPATH


def path_for(checkout: str | Path) -> Path:
    return Path(checkout) / MANIFEST_RELPATH


def check_owned_path(value: str) -> str:
    """*value* as a repo-relative path, or raise :class:`ManifestError`.

    Every rule here is about the fact that ``upgrade`` **writes** this path into
    the checkout, and the list comes out of a file in the repository. An
    absolute path or one climbing out through ``..`` is a write outside the
    installation, which is not an ownership claim anybody can make in a file
    that lives inside it.
    """
    text = str(value)
    if not text.strip():
        raise ManifestError(
            f"`{OWNED_KEY}:` carries an empty path. Every entry names one file "
            f"`vellum upgrade` may rewrite; an empty one names the checkout."
        )
    if "\\" in text:
        raise ManifestError(
            f"`{OWNED_KEY}:` carries {one_line(text)!r}, which uses a backslash. "
            f"Paths here are repo-relative and POSIX-separated, the way git "
            f"spells them, so one installation's manifest reads the same on "
            f"every platform."
        )
    pure = PurePosixPath(text)
    if pure.is_absolute():
        raise ManifestError(
            f"`{OWNED_KEY}:` carries the absolute path {one_line(text)!r}. "
            f"`vellum upgrade` writes every path on this list, and it writes "
            f"them inside the checkout; an absolute path is a write somewhere "
            f"else entirely."
        )
    if ".." in pure.parts:
        raise ManifestError(
            f"`{OWNED_KEY}:` carries {one_line(text)!r}, which has a `..` "
            f"component. `vellum upgrade` writes every path on this list; one "
            f"that climbs out of the checkout is a write outside the "
            f"installation, and this file lives inside it."
        )
    # `.` and `./` are normalised away by PurePosixPath rather than refused —
    # they name the same file — but a path that normalises to NOTHING names the
    # checkout itself, and "Vellum owns this repository" is not an ownership
    # claim this file can make.
    if not pure.parts:
        raise ManifestError(
            f"`{OWNED_KEY}:` carries {one_line(text)!r}, which names the "
            f"checkout rather than a file in it. Every entry is one file "
            f"`vellum upgrade` may rewrite."
        )
    if pure == PurePosixPath(MANIFEST_RELPATH.as_posix()):
        raise ManifestError(
            f"`{OWNED_KEY}:` carries {MANIFEST_RELPATH.as_posix()}, which is this "
            f"file. The manifest is Vellum's own bookkeeping and is rewritten by "
            f"every stamp and every upgrade unconditionally, so owning it would "
            f"compare it against a template of itself."
        )
    return pure.as_posix()


def parse(text: str, checkout: str | Path) -> Manifest:
    """Read manifest *text*. Raises :class:`ManifestError` on anything malformed."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"is not valid YAML: {one_line(str(exc))}") from exc
    if not isinstance(data, dict):
        raise ManifestError(
            "is not a YAML mapping, so it declares neither the release this "
            f"installation was brought to nor the files Vellum owns."
        )
    release = data.get(RELEASE_KEY)
    if release is None or not str(release).strip():
        raise ManifestError(
            f"declares no `{RELEASE_KEY}:`. That key is the release this "
            f"installation was last brought to, and `vellum upgrade` compares "
            f"every owned file against ITS templates — without it there is "
            f"nothing to compare against and no upgrade can be safe."
        )
    # Read back as a STRING, and said out loud when it did not arrive as one:
    # YAML 1.1 hands `vellum: 1.10` back as the float `1.1` and `vellum: 010` as
    # the int `10`, which is the same round-trip the stubs' `vellum-ref:` was
    # quoted to survive (`vellum.install`'s docstring). A release that reads
    # back as a number would be compared against a tag nothing carries.
    if not isinstance(release, str):
        raise ManifestError(
            f"declares `{RELEASE_KEY}: {one_line(str(release))}`, which reads back "
            f"as {type(release).__name__} rather than a string. Quote it: a "
            f"release is a git ref and only accidentally not a number."
        )
    if not RELEASE_RE.match(release):
        raise ManifestError(
            f"declares `{RELEASE_KEY}: {one_line(release)!r}`, which is not a "
            f"usable ref. It is handed to git as a ref to read a release's "
            f"templates at, so it must be a plain tag, branch or sha."
        )
    if OWNED_KEY not in data:
        raise ManifestError(
            f"declares no `{OWNED_KEY}:`. An installation that owns nothing "
            f"writes `{OWNED_KEY}: {EMPTY_OWNED}` and says so; an absent key is "
            f"indistinguishable from a file that was truncated, and the "
            f"difference decides whether an upgrade rewrites anything."
        )
    listed = data[OWNED_KEY]
    if listed is None:
        raise ManifestError(
            f"declares `{OWNED_KEY}:` with nothing under it, which YAML reads as "
            f"null rather than as an empty list. Write `{OWNED_KEY}: "
            f"{EMPTY_OWNED}` for an installation that owns nothing."
        )
    if not isinstance(listed, list):
        raise ManifestError(
            f"declares `{OWNED_KEY}:` as {one_line(str(listed))!r}, which is not a "
            f"list of repo-relative paths."
        )
    owned = tuple(sorted({check_owned_path(entry) for entry in listed}))
    return Manifest(checkout=Path(checkout), release=release, owned=owned)


def read(checkout: str | Path) -> Manifest | None:
    """The manifest, or None when there is not one. Raises when there is a bad one.

    Absent and malformed are different answers and both callers need the
    difference: ``init`` writes a manifest over an absence and refuses to
    silently replace a broken one, and ``doctor`` reports the two with different
    detail.
    """
    path = path_for(checkout)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise ManifestError(f"{path}: is not UTF-8 text: {one_line(str(exc))}") from exc
    except OSError as exc:
        raise ManifestError(f"{path}: cannot be read: {exc}") from exc
    try:
        return parse(text, checkout)
    except ManifestError as exc:
        raise ManifestError(f"{path}: {exc}") from exc


def load(checkout: str | Path) -> Manifest:
    """The manifest, or :class:`ManifestError` when there is not one."""
    found = read(checkout)
    if found is None:
        raise ManifestError(
            f"{path_for(checkout)}: this installation carries no manifest, so "
            f"nothing here says which files Vellum owns. `vellum init` writes "
            f"one; ownership is data and no command infers it from a file's "
            f"contents or history (spec/features/installation.md)."
        )
    return found


def dump(release: str, owned) -> str:
    """The manifest's text: deterministic, comment-bearing, sorted.

    Written as text rather than dumped from a dict for the reason every other
    comment-bearing file in this system is: ``safe_dump`` would strip the header
    that tells an operator what the two keys mean and what editing ``owned:``
    does, and that header is the only documentation the file carries to the
    installation it lands in.
    """
    if not RELEASE_RE.match(str(release)):
        raise ManifestError(
            f"{one_line(str(release))!r} is not a usable release to record. It is "
            f"written into the manifest and handed to git as a ref afterwards."
        )
    paths = sorted({check_owned_path(entry) for entry in owned})
    head = HEADER.format(
        release_key=RELEASE_KEY, owned_key=OWNED_KEY,
        # Quoted for the round trip the reader refuses: `vellum: 1.10` comes
        # back as a float. `RELEASE_RE` forbids quotes, so nothing a caller
        # passes can escape them.
        release=f'"{release}"',
    )
    if not paths:
        return f"{head}{OWNED_KEY}: {EMPTY_OWNED}\n"
    return head + f"{OWNED_KEY}:\n" + "".join(f"  - {path}\n" for path in paths)


def write(checkout: str | Path, release: str, owned) -> Path:
    """Write the manifest into *checkout*. Returns the path written."""
    path = path_for(checkout)
    text = dump(release, owned)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"{path}: cannot write the manifest: {exc}") from exc
    return path
