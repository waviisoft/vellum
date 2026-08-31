"""Reading an installation's ``.vellum/config.yaml``.

One installation config governs one intent repo and every product repo under
it. Only the keys a command actually reads are given accessors here; the file
carries a great deal more (executors, reviewers, budgets, labels) and this
module deliberately does not model it. A schema written ahead of a reader is a
second place for the shape to drift.

The config lives in the *intent* repo, beside ``spec/``, because the values in
it are installation policy rather than product code — ``divergence_cap`` is a
statement about how far intent may run ahead of any product, so it cannot live
in one of them.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class ConfigError(Exception):
    """The installation config is missing, unreadable, or lacks a needed key."""


#: Where the config sits inside an intent checkout.
CONFIG_RELPATH = Path(".vellum") / "config.yaml"

#: Names an intent-repo checkout for commands that need one but are pointed at
#: a product repo. Product CI already sets it for the test suite; the same
#: variable serves ``vellum pin advance``, deliberately, so an installation has
#: one answer to "where is the intent repo checked out" rather than two.
INTENT_ENV = "VELLUM_INTENT_REPO"


def config_path(checkout: str | Path) -> Path:
    return Path(checkout) / CONFIG_RELPATH


def load(checkout: str | Path) -> dict:
    """The parsed ``.vellum/config.yaml`` from an intent checkout."""
    path = config_path(checkout)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read the installation config: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: the installation config is not a YAML mapping")
    return data


def divergence_cap(checkout: str | Path) -> int:
    """``budgets.divergence_cap``: unshipped spec versions before backpressure.

    Missing is an error rather than a default. A default would make a
    typo'd key — or a config from an older installation — silently disable the
    gate, and a gate that can turn itself off is not one. The caller may still
    override the number on the command line, which is the supported way to ask
    "what would a cap of 5 say?" without editing installation policy.
    """
    path = config_path(checkout)
    budgets = load(checkout).get("budgets")
    if not isinstance(budgets, dict) or "divergence_cap" not in budgets:
        raise ConfigError(
            f"{path}: no budgets.divergence_cap. Backpressure has no cap to "
            f"measure against; set one, or pass --cap."
        )
    cap = budgets["divergence_cap"]
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        raise ConfigError(
            f"{path}: budgets.divergence_cap is {cap!r}; expected a non-negative integer"
        )
    return cap
