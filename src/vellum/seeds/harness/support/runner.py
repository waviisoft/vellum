"""Running the extracted suite.

The suite of record is whatever ``vellum suite extract`` reports — this runner
consumes ``suite.json`` and never re-reads the spec markdown. That is deliberate:
a second extractor is a second opinion about what the suite contains, and
`spec/decisions/2026-08-28-one-feature-per-fence.md` and
`spec/decisions/2026-08-28-no-rules.md` both exist because a suite that silently
under-reports is the failure mode that matters here. There is one extractor, it
lives in the product, and the harness runs exactly what it emits.

Outcomes, and what each one is allowed to mean:

``PASS``
    Every step ran against the product and every assertion held.

``FAIL``
    Every step up to the failure ran against the product, and an assertion about
    observable behavior did not hold — or a command the product provides,
    invoked correctly, exited non-zero. An honest red, about the product.

``CANNOT RUN YET``
    A step needs a capability this deployment does not provide. The report names
    it. This is never a skip and never a pass.

``ERROR``
    The harness itself broke — a defect in ``harness/``. Deliberately narrow: a
    product command that fails is a product result (FAIL), not a harness one,
    and a step that tries to build a path outside its sandbox stops here rather
    than writing and then reporting the FAIL its own diff assertion would give.

``UNDEFINED``
    No step definition matched. The suite is not fully executable and the run
    fails on it.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from support import registry
from support.adapter import AdapterError, Deployment, MissingCapability, ProductFailed
from support.world import World

PASS = "PASS"
FAIL = "FAIL"
CANNOT_RUN = "CANNOT RUN YET"
ERROR = "ERROR"
UNDEFINED = "UNDEFINED"

#: Report order: what the product does, then what it does not, then what the
#: harness could not ask, then the harness's own defects.
OUTCOMES = (PASS, FAIL, CANNOT_RUN, ERROR, UNDEFINED)


def _elsewhere() -> list[tuple[str, str]]:
    """Host locations that must never reach the report, longest first.

    ``conformance.md`` is committed, so a detail carrying an absolute path
    commits the operator's home directory (and their scratch layout) into the
    intent repo, and makes the report vary between machines — which the report
    is built not to do. Longest first so a scratch directory inside the temp
    directory is replaced as ``<scratch>``, not as ``<tmp>/...``.
    """
    places = [
        (tempfile.gettempdir(), "<tmp>"),
        (os.path.expanduser("~"), "~"),
        (os.getcwd(), "<cwd>"),
    ]
    return sorted(
        ((str(Path(where)), name) for where, name in places if where),
        key=lambda pair: len(pair[0]), reverse=True,
    )


def _redact_paths(detail: str, scratch: Path) -> str:
    """*detail* with host locations replaced by stable names."""
    names: dict[str, str] = {}
    for where, name in [(str(scratch.resolve()), "<scratch>"),
                        (str(scratch), "<scratch>"), *_elsewhere()]:
        names.setdefault(where, name)
    ordered = sorted(names, key=len, reverse=True)
    pattern = re.compile(
        # Only where a path actually starts. A step argument like
        # `../../../tmp/x` names no host location, and rewriting the `/tmp`
        # inside it would corrupt the very text the reader needs to see.
        r"(?<![\w.~/-])(" + "|".join(re.escape(where) for where in ordered) + r")"
    )
    return pattern.sub(lambda found: names[found.group(1)], detail)


@dataclass
class StepResult:
    keyword: str
    text: str
    status: str          # passed | failed | blocked | error | undefined | not run
    detail: str = ""
    where: str = ""


@dataclass
class ScenarioResult:
    id: str | None
    name: str
    feature: str
    file: str
    line: int
    version: str | None
    outcome: str
    steps: list[StepResult] = field(default_factory=list)
    blocked_on: list[str] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    example: dict | None = None

    @property
    def key(self) -> str:
        return self.id or f"{self.file}:{self.line}"


def _expand(scenario: dict) -> list[tuple[dict | None, list[dict]]]:
    """One entry per run: ``(example row, steps)``.

    A plain Scenario runs once. A Scenario Outline runs once per Examples row,
    with ``<placeholder>`` substituted — lint guarantees an outline in the tree
    has at least one row (GH007), so an outline never expands to nothing.
    """
    if not scenario["examples"]:
        return [(None, scenario["steps"])]
    runs: list[tuple[dict | None, list[dict]]] = []
    for table in scenario["examples"]:
        for row in table["rows"]:
            values = dict(zip(table["header"], row))
            runs.append((
                values,
                [
                    {
                        "keyword": step["keyword"],
                        "text": _substitute(step["text"], values),
                    }
                    for step in scenario["steps"]
                ],
            ))
    return runs


def _substitute(text: str, values: dict[str, str]) -> str:
    for name, value in values.items():
        text = text.replace(f"<{name}>", value)
    return text


def run_scenario(deployment: Deployment, scenario: dict) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for example, steps in _expand(scenario):
        results.append(_run_one(deployment, scenario, example, steps))
    return results


def _run_one(deployment: Deployment, scenario: dict, example: dict | None,
             steps: list[dict]) -> ScenarioResult:
    result = ScenarioResult(
        id=scenario["id"],
        name=scenario["name"],
        feature=scenario["feature"],
        file=scenario["file"],
        line=scenario["line"],
        version=scenario["version"],
        outcome=PASS,
        example=example,
    )
    world = World(deployment, scenario["id"] or "unnamed")
    try:
        for keyword, text in registry.normalize_keywords(steps):
            if result.outcome != PASS:
                result.steps.append(StepResult(keyword, text, "not run"))
                continue
            result.steps.append(_run_step(world, result, keyword, text))
        result.progress = list(world.progress)
    finally:
        world.cleanup()
    return result


def _step(world: World, keyword: str, text: str, status: str,
          detail: str = "", where: str = "") -> StepResult:
    """One step's result, with host paths stripped out of *detail*."""
    return StepResult(keyword, text, status, _redact_paths(detail, world.scratch), where)


def _run_step(world: World, result: ScenarioResult, keyword: str, text: str) -> StepResult:
    try:
        found = registry.find(keyword, text)
    except registry.AmbiguousStep as exc:
        result.outcome = ERROR
        return _step(world, keyword, text, "error", str(exc))

    if found is None:
        result.outcome = UNDEFINED
        return _step(world, keyword, text, "undefined",
                     "no step definition matches this sentence")

    definition, match = found
    try:
        definition.fn(world, *match.groups())
    except MissingCapability as exc:
        result.outcome = CANNOT_RUN
        result.blocked_on = exc.capabilities
        return _step(world, keyword, text, "blocked",
                     exc.detail or str(exc), definition.where)
    except (AssertionError, ProductFailed) as exc:
        # An assertion that did not hold, or a product command that exited
        # non-zero: both are results about the product, not about harness/.
        result.outcome = FAIL
        return _step(world, keyword, text, "failed", str(exc), definition.where)
    except AdapterError as exc:
        result.outcome = ERROR
        return _step(world, keyword, text, "error", str(exc), definition.where)
    except Exception as exc:  # a defect in harness/
        result.outcome = ERROR
        return _step(world, keyword, text, "error",
                     f"{type(exc).__name__}: {exc}", definition.where)
    return _step(world, keyword, text, "passed", "", definition.where)


def run_suite(deployment: Deployment, suite: dict) -> list[ScenarioResult]:
    """Every scenario in the extracted suite, in the suite's own order.

    ``vellum suite extract`` sorts by file then line, so the order — and so the
    report — is a function of the spec tree alone.
    """
    results: list[ScenarioResult] = []
    for scenario in suite["scenarios"]:
        results.extend(run_scenario(deployment, scenario))
    return results
