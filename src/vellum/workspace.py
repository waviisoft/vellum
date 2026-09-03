"""Reading an intent repo's ``.vellum/workspace.yaml``.

``spec/features/repo-topology.md``: "One intent repo governs one or more product
repos; each product repo answers to exactly one intent repo.
``.vellum/workspace.yaml`` maps the products." That file is the installation's
statement of what it is — which intent repo, which product repos, and (since the
installer) which forge its adapters are stamped for.

One reader, because the shape has one meaning. ``vellum release cut`` reads the
products as the allowlist a cut may pin; ``vellum init`` reads them, the intent
slug and the forge to stamp the caller stubs; ``vellum doctor`` reads the forge
to know which stubs to look for. Three readers of one shape is how the three
come to disagree about what ``products: {}`` means — the same argument
``vellum.product.role_trees`` makes about the one ``write_boundaries`` block
read from two files. ``release.products`` calls in here and re-raises as its own
error, so a caller still learns which *command* refused.

Only the keys a command actually reads have accessors, the call
``vellum.config`` makes about the installation config: a schema written ahead of
a reader is a second place for the shape to drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

#: Where the workspace sits inside an intent checkout.
WORKSPACE_RELPATH = Path(".vellum") / "workspace.yaml"

#: The forge an installation's adapters are stamped for when the file does not
#: say. GitHub is the only forge v1 adapts to, and every workspace file written
#: before the installer existed — this installation's own included — carries no
#: ``forge`` key. Defaulting is safe *because* an unrecognised value is refused
#: rather than defaulted: the failure a silent default would buy is stamping
#: GitHub stubs into a GitLab installation, and that needs the key to be
#: present and wrong, which is exactly the case that raises.
DEFAULT_FORGE = "github"

#: A repo slug, ``owner/name``. Narrow deliberately, and shared: ``install.render``
#: holds ``--from`` to this shape because that value IS pasted into a `uses:`
#: line a forge then executes, where a newline and two spaces of indent open a
#: second job. The ``intent`` slug read here reaches only a report, so this is
#: the weaker of the two uses — but one definition of "a repo slug" is the point,
#: and the strong use is the one that sets the bar.
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

#: A forge name, as it appears in a workspace file and in `--forge`.
FORGE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class WorkspaceError(Exception):
    """The workspace file is missing, unreadable, or does not declare something."""


def workspace_path(checkout: str | Path) -> Path:
    return Path(checkout) / WORKSPACE_RELPATH


def load(checkout: str | Path) -> dict:
    """The parsed ``.vellum/workspace.yaml`` from an intent checkout."""
    path = workspace_path(checkout)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkspaceError(
            f"{path}: cannot read the workspace: {exc}. It maps this installation's "
            f"intent repo and product repos (spec/features/repo-topology.md); is "
            f"{checkout} an intent checkout?"
        ) from exc
    except yaml.YAMLError as exc:
        raise WorkspaceError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceError(f"{path}: the workspace is not a YAML mapping")
    return data


def products(checkout: str | Path) -> dict[str, str]:
    """``{product name: repo}`` from ``.vellum/workspace.yaml``.

    Missing is an error, not an empty allowlist: a caller composing a release
    cut would otherwise pin ``cor=<sha>`` and be told nothing, and a caller
    installing stubs would report an installation governing no product as
    though that were a shape.
    """
    path = workspace_path(checkout)
    entries = load(checkout).get("products")
    if not isinstance(entries, dict) or not entries:
        raise WorkspaceError(f"{path}: declares no products, so a cut can pin nothing")
    found: dict[str, str] = {}
    for name, value in entries.items():
        repo = value.get("repo") if isinstance(value, dict) else value
        found[str(name)] = str(repo) if repo is not None else ""
    return found


def intent(checkout: str | Path) -> str:
    """The ``intent:`` slug: the repo this installation's spec lives in.

    Refused rather than defaulted to the checkout's own remote. A workspace that
    does not say which intent repo it belongs to is not a workspace, and reading
    the answer out of `git remote` would make the stamped installation depend on
    how somebody cloned it.
    """
    path = workspace_path(checkout)
    slug = load(checkout).get("intent")
    if slug is None:
        raise WorkspaceError(
            f"{path}: declares no `intent:`. The workspace names the intent repo "
            f"that governs this installation (spec/features/repo-topology.md)."
        )
    text = str(slug).strip()
    if not SLUG_RE.match(text):
        raise WorkspaceError(
            f"{path}: intent {slug!r} is not an `owner/name` repo slug"
        )
    return text


def forge(checkout: str | Path) -> str:
    """The forge this installation's adapters are stamped for.

    ``spec/features/installation.md``: "`vellum init` ... stamps the caller stubs
    for the forge `.vellum/workspace.yaml` names". Absent means
    :data:`DEFAULT_FORGE`; present and unusable as a name is refused here, and
    present-but-unadapted is refused by the caller, which is the half that knows
    which forges it has stubs for.
    """
    path = workspace_path(checkout)
    value = load(checkout).get("forge")
    if value is None:
        return DEFAULT_FORGE
    text = str(value).strip().lower()
    if not FORGE_RE.match(text):
        raise WorkspaceError(
            f"{path}: forge {value!r} is not a forge name"
        )
    return text
