"""The product adapter: how the harness reaches the product under test.

This file is the **one** file a new installation has to write before its
acceptance suite can do anything but report honestly. Everything else under
`harness/` is generic machinery seeded by `vellum init` and is the same in
every installation.

Two jobs, deliberately separated — and separating them is the whole difference
between this skeleton and the harness of the repository that seeded it:

**Extracting the suite.** The suite of record is whatever `vellum suite
extract` reports for the intent repo. That needs the `vellum` CLI, which is
*tooling*, not the product under test. `vellum_cli()` finds it.

**Reaching the product.** A deployment provides *capabilities*. A step that
needs one this deployment does not have says so by calling `World.require`,
which raises `MissingCapability`; the runner turns that into a BLOCKED step and
a CANNOT RUN YET scenario. That is the mechanism behind the report's CANNOT RUN
YET column: a scenario is never silently skipped and never fakes a pass — it
names the thing that is not there.

A fresh installation has **no deployment at all**, and this file says so rather
than pretending: `no_deployment()` returns a deployment providing nothing, so
every scenario in the seeded spec tree reports CANNOT RUN YET naming
``deployment``. Replace `no_deployment()` with a real one — a running instance
of the product, a CLI, a preview environment the product adapter conjures — and
declare the capabilities it actually provides. Nothing else here changes.

Capability kinds separate two very different absences:

``product-gap``
    Nothing in the product implements this yet, in any deployment. Building it
    is a wave, not a harness change.

``deployment-gap``
    The behavior may well exist, but only inside infrastructure this harness
    has no adapter for. A new adapter, not new product code, unblocks these.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: The capability every scenario blocks on until this file names a real
#: deployment. Seeded by `vellum init`; the wording is the report's wording,
#: so edit the sentence when the situation changes rather than leaving a
#: description that is no longer true.
DEPLOYMENT = "deployment"

#: ``id -> (kind, what it would be)``. The report prints these verbatim.
CAPABILITIES: dict[str, tuple[str, str]] = {
    DEPLOYMENT: (
        "deployment-gap",
        "a deployment of this product the suite can drive strictly from the "
        "outside — an API to call, a UI to automate, a command line to invoke. "
        "`vellum init` seeded this harness with none, because a checkout cannot "
        "know how the product is run: write one in "
        "harness/support/adapter.py, declare what it provides, and the "
        "scenarios waiting on this stop waiting",
    ),
}


class MissingCapability(Exception):
    """A step needs something this deployment does not provide.

    Raised by `World.require`, which takes every capability the step needs and
    reports *all* of the missing ones. Reporting only the first would hide the
    deeper gap behind the shallow one.

    The runner turns this into a BLOCKED step and a CANNOT RUN YET scenario. It
    is never caught and turned into a pass.
    """

    def __init__(self, capabilities: list[str], detail: str = ""):
        self.capabilities = list(capabilities)
        self.detail = detail
        super().__init__("; ".join(
            f"{c}: {CAPABILITIES[c][1]}" for c in self.capabilities
        ))


class AdapterError(Exception):
    """The product could not be reached at all, or the harness could not start."""


class ProductFailed(Exception):
    """The product ran and exited non-zero.

    Distinct from `AdapterError` on purpose: the runner maps this to FAIL — an
    honest red about the product — and `AdapterError` to ERROR, which the
    report headlines as a defect in `harness/`. A product regression must not
    read as a bug in this repository.
    """


@dataclass(frozen=True)
class Deployment:
    """A product deployment the suite can run against."""

    name: str
    capabilities: frozenset[str]

    def provides(self, capability: str) -> bool:
        return capability in self.capabilities


def no_deployment() -> Deployment:
    """The deployment a freshly provisioned installation has: none.

    Named `none` in the report so a later real deployment is distinguishable
    from this one at a glance. Replace this function; do not make it lie.
    """
    return Deployment(name="none", capabilities=frozenset())


def vellum_cli() -> Path:
    """The `vellum` command line — tooling, not the product under test.

    ``VELLUM_BIN`` names it explicitly; otherwise it is looked up on ``PATH``.
    """
    named = os.environ.get("VELLUM_BIN")
    if named:
        path = Path(named)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AdapterError(f"VELLUM_BIN={named} is not an executable file")
        return path
    found = shutil.which("vellum")
    if found:
        return Path(found)
    raise AdapterError(
        "no vellum CLI found. Set VELLUM_BIN to the executable, or put "
        "`vellum` on PATH. It is what extracts the suite from the intent repo."
    )


def run_vellum(cli: Path, *args: str, cwd: Path | None = None):
    """Invoke the `vellum` CLI. List arguments, no shell."""
    return subprocess.run(
        [str(cli), *args], capture_output=True, text=True,
        cwd=str(cwd) if cwd else None,
    )
