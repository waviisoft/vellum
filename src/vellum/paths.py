"""Where a declared path may lead, for every command that opens or writes one.

Two commands now take a repo-relative path out of a file **inside the
repository** and act on it: ``vellum upgrade`` writes every entry of
``.vellum/install.yaml``'s ``owned:`` list, and ``vellum release tag`` reads the
``version_source:`` and ``changelog:`` its ``.vellum/product.yaml`` declares.
Both lists are lines anybody who can land a pull request can write, and both
commands run in CI with a credential.

The lexical half of the question — no absolute path, no ``..``, nothing under
``.git/`` — is each caller's own (``vellum.manifest.check_owned_path``,
``vellum.tag._relative``): it is about a *string*, and each has its own message
for the operator who wrote it. This module holds the half that needs a
filesystem, because that half is identical for both and two spellings of "does
this path stay inside the checkout" is how the two come to disagree — the same
argument ``vellum.text`` makes about flattening a string.

The walk is one walk
--------------------
:func:`unsafe_write` grew first, in ``vellum.upgrade``, against the three ways a
``mkdir(parents=True)`` plus ``write_text`` becomes a write somewhere else. A
read is the same walk with a different last step: a symlink among the components
redirects a *read* just as well as a write, and a ``version_source`` of
``VERSION`` where ``VERSION`` is a link to ``/etc/shadow`` is a report that
prints somebody else's file. So both functions walk the components here and
differ only in what they then require of the leaf — a directory that is inside
the checkout for a write, a regular file that is inside it for a read.

A reason, never a boolean. The report names the path and says which of the
refusals it is: an operator has to be able to look at the tree and see the same
thing this saw.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from vellum import manifest
from vellum.text import one_line

#: The directory no declared path may reach into, on either side of the pair.
#: A read there is `.git/config`, which carries an installation's remotes and,
#: on a runner, the credential `actions/checkout` persisted into it; a write
#: there is `.git/hooks/`, which the next `git commit` in the same run executes.
GIT_DIR = ".git"


def components(relative: str) -> tuple[str, ...]:
    """*relative*'s path components, with ``.`` segments dropped."""
    return tuple(p for p in PurePosixPath(relative).parts if p != ".")


def symlink_component(root: Path, parts: tuple[str, ...]) -> str | None:
    """The first component of *parts* under *root* that is a symlink, or None.

    Every component up to and including the leaf: a symlink anywhere along the
    way redirects everything after it, so the leaf being an honest file says
    nothing about where it was reached through. Returned as the path an operator
    sees in `git status` — the components joined, not the absolute path — because
    each caller words its own refusal around it.
    """
    walked = root
    for index, part in enumerate(parts):
        walked = walked / part
        if walked.is_symlink():
            return "/".join(parts[:index + 1])
    return None


def unsafe_write(root: Path, relative: str) -> str | None:
    """Why *relative* is not a path this may write into *root*, or None.

    ``vellum.manifest.check_owned_path`` holds the *lexical* half of this — no
    absolute path, no ``..``, nothing under ``.git/`` — and cannot hold any of
    the rest, because the rest is about a filesystem it never looks at. A
    manifest entry is a line in a repository that anybody who can land a pull
    request can write, and ``upgrade`` writes every path on that list after a
    ``mkdir(parents=True)``. Three ways that becomes a write somewhere else:

    * **a symlink among the components.** ``.github/workflows`` a symlink to
      ``../.git/hooks``, or the file itself a dangling symlink pointing there,
      and an owned path becomes a hook — one this command's own ``git commit``
      then executes, in the operator's shell, in the same run.
    * **a parent that is a regular file.** ``mkdir(parents=True)`` fails
      halfway, which is a traceback out of a half-written tree rather than a
      refusal before one exists (see ``vellum.upgrade._apply``).
    * **a parent that resolves outside the checkout.** The backstop for the
      first: whatever the components are, the directory written into has to be
      inside ``root``.
    """
    settled_root = root.resolve()
    parts = components(relative)
    linked = symlink_component(root, parts)
    if linked is not None:
        return (
            f"{linked} is a symlink, and this writes through no symlink: an "
            f"owned path whose components can be redirected is a write wherever "
            f"the link points — `.git/hooks/` among the reachable places, where "
            f"it would run during this upgrade's own commit. Nothing was "
            f"written. Replace the link with the real path, or take the line out "
            f"of `{manifest.OWNED_KEY}:`."
        )
    walked = root
    for index, part in enumerate(parts):
        walked = walked / part
        if index < len(parts) - 1 and walked.exists() and not walked.is_dir():
            return (
                f"{'/'.join(parts[:index + 1])} is a file, and this path needs "
                f"it to be a directory. Writing would have to create a directory "
                f"where a file already is, which fails part way through a run "
                f"that has already written other files — so it is refused here, "
                f"before anything is written."
            )
    try:
        settled = (root / relative).parent.resolve()
    except OSError as exc:  # a symlink loop, or a component that cannot be read
        return (
            f"its directory could not be resolved ({one_line(str(exc))}), so "
            f"nothing can say that writing it writes inside this checkout."
        )
    if settled != settled_root and settled_root not in settled.parents:
        return (
            f"its directory resolves to {settled}, which is outside {settled_root}. "
            f"Vellum owns files in the installation, and an `{manifest.OWNED_KEY}:` "
            f"line cannot claim one anywhere else."
        )
    return None


def unsafe_read(root: Path, relative: str) -> str | None:
    """Why *relative* is not a file this may read out of *root*, or None.

    The read half of :func:`unsafe_write`, and the last step is what differs.
    Four things are asked of the leaf, and each is a way a declaration reaches
    something that is not a file in this repository:

    * **no component is a symlink**, the walk above — a ``VERSION`` linked to
      ``/etc/shadow``, or a directory component linked out of the tree.
    * **it resolves inside the checkout.** The backstop for the first, and for
      whatever a future component type does that a symlink test does not see.
    * **it is a regular file.** A FIFO blocks the command forever; a character
      device (``/dev/zero``, ``/dev/urandom``) reads until the process dies.
      Neither is a version source, and neither refuses itself.
    * **it is not under ``.git/``.** Held lexically by each caller too, and
      kept here because this is the module a second reader will import.
    """
    parts = components(relative)
    if not parts:
        return "it names no file: a declared path names one file in this repository."
    if parts[0] == GIT_DIR:
        return (
            f"{GIT_DIR}/ is git's own directory, not this repository's content. "
            f"Nothing declared may be read out of it: it carries the remotes, "
            f"and on a runner the credential `actions/checkout` persisted there."
        )
    linked = symlink_component(root, parts)
    if linked is not None:
        return (
            f"{linked} is a symlink, and this reads through no symlink: a "
            f"declared path whose components can be redirected leads wherever "
            f"the link points, and this command prints what it read. Replace the "
            f"link with the real path, or declare a path that is one."
        )
    path = root / relative
    settled_root = root.resolve()
    try:
        settled = path.resolve()
    except OSError as exc:  # a symlink loop, or a component that cannot be read
        return (
            f"it could not be resolved ({one_line(str(exc))}), so nothing can "
            f"say that reading it reads inside this checkout."
        )
    if settled != settled_root and settled_root not in settled.parents:
        return (
            f"it resolves to {settled}, which is outside {settled_root}. A "
            f"declaration names a file in the repository whose file it is."
        )
    if not path.exists():
        return "there is no such file in this checkout."
    if not path.is_file():
        return (
            "it is not a regular file. A device or a FIFO is not something to "
            "read a declaration's answer out of — one of them never ends."
        )
    return None


__all__ = ["GIT_DIR", "components", "symlink_component",
           "unsafe_read", "unsafe_write"]
