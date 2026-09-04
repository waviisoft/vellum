"""Per-scenario state, and the one place a step says "this deployment cannot".

A `World` is created fresh for each scenario, holds a scratch directory that is
removed afterwards, and is the only thing passed between the steps of a
scenario. Nothing is shared between scenarios, so scenario order never changes
an outcome.

It is also where the harness's own write boundary is enforced. Step arguments
are captured out of sentences in the spec tree, and a spec PR is reviewed as
prose rather than as code — so a sentence must never be able to make the
harness write outside the scratch directory it owns. Every path built from a
captured argument goes through `contained`.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from support.adapter import Deployment, MissingCapability


class PathEscape(Exception):
    """A path built from scenario text would leave the directory that owns it.

    Never caught into an outcome that could be mistaken for a product result:
    the runner reports the scenario as a harness-level error, loudly, rather
    than letting the write happen and then reporting the ordinary FAIL that a
    diff-based assertion would produce for a file it cannot see.
    """


def contained(root: Path, *parts: str) -> Path:
    """*root* joined with *parts*, proven to still be inside *root*.

    Leading slashes are stripped so an absolute-looking argument is treated as
    relative to *root*, and the join is resolved before the check so ``..``
    cannot climb out of it. Nothing is created here — callers ``mkdir`` the
    result — and the message names no host path, because it reaches the report.
    """
    base = root.resolve()
    target = base.joinpath(*(part.strip("/") for part in parts)).resolve()
    if not target.is_relative_to(base):
        raise PathEscape(
            f"refusing to build {'/'.join(parts)!r} under {root.name!r}: it "
            f"resolves outside the directory that owns it. A path taken from a "
            f"scenario sentence may not leave the harness sandbox."
        )
    return target


def _slug(scenario_id: str) -> str:
    """A scenario id made safe to embed in one directory name.

    An ``@id`` is spec text. A ``/`` in one used to abort the whole run with a
    traceback out of ``mkdtemp``; anything that is not a plain identifier
    character becomes a dash, so the prefix stays readable and can only ever
    name a single path segment.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", scenario_id).strip("-.")
    return cleaned[:60] or "scenario"


class World:
    def __init__(self, deployment: Deployment, scenario_id: str):
        self.deployment = deployment
        self.scenario_id = scenario_id
        self._scratch = Path(
            tempfile.mkdtemp(prefix=f"vellum-harness-{_slug(scenario_id)}-")
        )
        self.state: dict = {}
        #: Work a step completed before it hit a missing capability. The report
        #: prints these, so a blocked scenario still says what it managed to
        #: prove — "the ledger drive worked; the reaction to it does not exist"
        #: is a more useful line than "blocked".
        self.progress: list[str] = []

    @property
    def scratch(self) -> Path:
        return self._scratch

    def dir(self, name: str) -> Path:
        path = contained(self._scratch, name)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def note(self, message: str) -> None:
        self.progress.append(message)

    def require(self, *capabilities: str, detail: str = "") -> None:
        """Assert the deployment provides every capability, or stop the scenario.

        This is the honesty valve. A step that cannot be driven against this
        deployment names everything it would need, and the scenario is reported
        CANNOT RUN YET listing all of them. There is deliberately no way to turn
        the exception into a pass or a skip.
        """
        missing = [c for c in capabilities if not self.deployment.provides(c)]
        if missing:
            raise MissingCapability(missing, detail)

    def unimplemented(self, *capabilities: str, detail: str = "") -> None:
        """A step that only a richer deployment could drive, with no body yet.

        Every step in the suite has a definition — that is the wave's whole
        point — but the steps *after* a blocked one describe behavior no
        deployment here can reach, so there is nothing honest to write in them.
        They declare the same capabilities their scenario blocks on, so the
        report's answer stays consistent. If a deployment ever does provide
        those capabilities, this raises instead of quietly passing: an
        unimplemented step is a gap in `harness/`, and it should read as one.
        """
        self.require(*capabilities, detail=detail)
        raise NotImplementedError(
            f"this step has no implementation for the {self.deployment.name} "
            f"deployment, which now provides {sorted(capabilities)}; "
            f"harness/ has to catch up"
        )

    def cleanup(self) -> None:
        shutil.rmtree(self._scratch, ignore_errors=True)
