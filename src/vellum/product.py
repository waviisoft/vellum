"""Reading a product checkout's ``.vellum/product.yaml``.

``.vellum/product.yaml`` **is** the pin (``spec/decisions/2026-08-28-pin-file.md``)
and it is also where a product repo declares which trees each role may write
(``write_boundaries.<role>``, ``spec/behaviors/write-boundaries.md``). Only the
keys a command actually reads have accessors here, the same call
``vellum.config`` makes about the installation config: a schema written ahead of
a reader is a second place for the shape to drift.

``vellum pin advance`` edits this file a line at a time and never parses it into
a structure it then writes back, because the comments in it are the
documentation. The guards here only *read* it, so they may parse it normally —
and they must not grow a writer. Anything that changes the file belongs in
``pin.py``, where the line-level rewrite and its verification live.

Boundary entries are repo-relative path prefixes and this module normalises them
before anybody compares a changed path against one. That normalisation is the
security-relevant half: an entry of ``../..``, ``/``, or the empty string would
each, read naively, admit every path in the diff, which is a write boundary that
has quietly stopped being one.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml

#: The pin of record, relative to a product checkout.
PRODUCT_RELPATH = Path(".vellum") / "product.yaml"


class ProductFileError(Exception):
    """``.vellum/product.yaml`` is missing, unreadable, or lacks a needed key."""


def product_path(checkout: str | Path) -> Path:
    return Path(checkout) / PRODUCT_RELPATH


def load(checkout: str | Path) -> dict:
    """The parsed ``.vellum/product.yaml`` from a product checkout."""
    path = product_path(checkout)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProductFileError(f"{path}: cannot read the product file: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ProductFileError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProductFileError(f"{path}: the product file is not a YAML mapping")
    return data


def normalise_tree(entry, *, path, where: str) -> str:
    """One declared tree, as a repo-relative POSIX prefix.

    Refuses everything that would widen the tree it names rather than narrow it.
    A boundary list is an allowlist, so a malformed entry is not a cosmetic
    problem: ``""``, ``"/"``, ``"."`` and ``"../.."`` all match every path in a
    diff under a naive prefix test, which turns the guard off while leaving it
    looking configured. Refusing is the only safe reading — there is no sensible
    default for "which tree did you mean".
    """
    if not isinstance(entry, str):
        raise ProductFileError(
            f"{path}: {where} contains {entry!r}; every entry must be a string path"
        )
    text = entry.strip()
    if text.startswith("/"):
        raise ProductFileError(
            f"{path}: {where} entry {entry!r} is absolute; entries are repo-relative"
        )
    pure = PurePosixPath(text)
    parts = [p for p in pure.parts if p not in (".",)]
    if any(p == ".." for p in parts):
        raise ProductFileError(
            f"{path}: {where} entry {entry!r} escapes the repository with '..'"
        )
    if not parts:
        raise ProductFileError(
            f"{path}: {where} entry {entry!r} names no tree; it would admit every path"
        )
    return "/".join(parts)


def write_boundaries(checkout: str | Path, role: str) -> list[str]:
    """``write_boundaries.<role>``: the trees *role* may write, normalised.

    Missing is an error rather than an empty list or an unrestricted one, for
    the reason ``config.divergence_cap`` gives about its own key: a default
    would make a typo'd role name — or a product file written before this guard
    existed — silently decide the answer, and a boundary that can turn itself
    off is not one. Which way it failed open would depend on which default was
    picked, and both are wrong: an empty list faults every honest PR, an
    unrestricted one passes every dishonest one.
    """
    path = product_path(checkout)
    declared = load(checkout).get("write_boundaries")
    if not isinstance(declared, dict):
        raise ProductFileError(
            f"{path}: no write_boundaries mapping. This checkout does not declare "
            f"what any role may write, so there is nothing to check {role!r} against "
            f"(spec/behaviors/write-boundaries.md)."
        )
    if role not in declared:
        raise ProductFileError(
            f"{path}: write_boundaries declares no {role!r}; it declares "
            f"{', '.join(sorted(map(str, declared))) or '(nothing)'}. A role with no "
            f"declared trees is not the same as a role that may write anywhere."
        )
    trees = declared[role]
    if not isinstance(trees, list):
        raise ProductFileError(
            f"{path}: write_boundaries.{role} is {trees!r}; expected a list of trees"
        )
    where = f"write_boundaries.{role}"
    return [normalise_tree(entry, path=path, where=where) for entry in trees]


def under(path: str, tree: str) -> bool:
    """True when *path* is *tree* itself or lies inside it.

    Compared component-wise, not as a string prefix: ``src`` must not admit
    ``srcs/evil.py``, and the ``startswith`` spelling that does is the one
    mistake this test exists to not make.
    """
    if path == tree:
        return True
    return path.startswith(tree + "/")
