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


# --- covsel test-boundary protocol (waviisoft/covsel#122) -------------------
#
# PROTOCOL VERSION: none exists to record. covsel#122 ("External harness
# adapter"), read at covsel@8d96eb66d51f002e56005ffd0b3a4e1407c4176e
# (2026-09-23), is open, carries zero comments, and frames its own protocol as
# a "Candidate shape ... For discussion" — not a contract yet. Nothing in
# covsel's tree implements it: no `adapter-harness` package, no
# `COVSEL_BOUNDARY` reference anywhere, no open or merged PR. A porter should
# replace this comment with the real version the day covsel#122 (or whatever
# supersedes it) publishes one — that is the coupling risk
# waviisoft/vellum#31's architect review flagged.
#
# What the issue DOES fix, and what this implements:
#   - the env var: COVSEL_BOUNDARY, unset by default;
#   - unset is a true no-op — the property that matters most, because it is
#     what lets the full run CI already does also be covsel's recording, at
#     no extra cost. Proven in tests/test_harness_covsel_boundary.py: the
#     dispatch step below is never reached, and the run's stdout/stderr and
#     results are unchanged;
#   - the message shape: `{"event": "begin", "id": ...}` before a scenario
#     (or one Examples row — see below) runs, `{"event": "end", "id": ...,
#     "outcome": ...}` after.
#
# What it does NOT fix, and so what this refuses rather than guesses:
#   - transport. The issue's own words are "COVSEL_BOUNDARY=<url or fifo
#     path>", with no rule for telling the two apart and no wire framing for
#     either (HTTP? a raw socket? newline-delimited JSON over the fifo? a
#     paired ack fifo?);
#   - the acknowledgement's shape. The issue says the runner "waits for the
#     acknowledgement before carrying on" but never says what an
#     acknowledgement looks like;
#   - what to do on a missing acknowledgement or a closed channel. Not
#     mentioned at all.
# Encoding a guess for any of those as if it were covsel's contract is the
# failure mode waviisoft/vellum#31 warns against by name: "a protocol
# implemented from a paraphrase is the thing most likely to be silently
# wrong." So `_covsel_dispatch` below refuses loudly, synchronously, and
# before any step of the scenario runs — never a hang, never a silent guess.
#
# Outcome mapping (asked for even though covsel#122 publishes no outcome
# vocabulary to map onto — its sketch shows only `"outcome": ...`). Recorded
# here, in the one place it belongs, as a proposal for whoever finalizes the
# other side of the contract, not as a confirmed mapping:
#   PASS          -> "pass"     an honest green
#   FAIL          -> "fail"     an honest red about the product
#   CANNOT_RUN    -> "blocked"  the deployment lacks a capability, not a
#                                verdict about the product — closest to
#                                covsel's own "fail-open" framing for a test
#                                the run never reported
#   UNDEFINED     -> "fail"     the suite is not fully executable; covsel has
#                                no "not a verdict" outcome to route this to,
#                                so it reads as red rather than as silently
#                                skipped
#   ERROR         -> "error"    a defect in harness/, not a product result
_COVSEL_BOUNDARY_ENV = "COVSEL_BOUNDARY"
_COVSEL_OUTCOME = {
    PASS: "pass",
    FAIL: "fail",
    CANNOT_RUN: "blocked",
    UNDEFINED: "fail",
    ERROR: "error",
}


def _covsel_dispatch(boundary: str, message: dict) -> None:
    """Send *message* to *boundary* and wait for covsel's acknowledgement.

    Not implemented on purpose — see the module-level note above. Reached
    only when ``COVSEL_BOUNDARY`` is set, which nothing does today, so this
    changes no default behavior; it exists so a harness engineer who lands
    with a real spec has exactly one place to fill in, instead of a silent
    wrong guess already sitting there.
    """
    raise NotImplementedError(
        f"COVSEL_BOUNDARY={boundary!r} is set, but waviisoft/covsel#122 does "
        f"not yet specify how to reach it (transport, message framing, the "
        f"acknowledgement's shape, or what to do on a missing acknowledgement "
        f"or a closed channel). Refusing to guess rather than silently "
        f"emitting a boundary protocol nobody has agreed to. Message that "
        f"could not be sent: {message!r}"
    )


def _covsel_notify(event: str, scenario_id: str | None, outcome: str | None = None) -> None:
    """Tell covsel a scenario (or one Examples row) is beginning or ending.

    A true no-op when ``COVSEL_BOUNDARY`` is unset: one dict lookup, nothing
    else. *scenario_id* is ``None`` for a scenario with no ``@id`` tag; such a
    scenario is outside covsel's inventory too (waviisoft/covsel#123 keys
    entries by id), so there is nothing to correlate a boundary with and this
    is skipped rather than sent with a null id — a call this seed makes, not
    a guess about covsel's wire format.
    """
    boundary = os.environ.get(_COVSEL_BOUNDARY_ENV)
    if not boundary or scenario_id is None:
        return
    message: dict = {"event": event, "id": scenario_id}
    if event == "end":
        message["outcome"] = _COVSEL_OUTCOME[outcome]
    _covsel_dispatch(boundary, message)


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
    # One `begin`/`end` pair per call to `_run_one` — which is one pair per
    # Examples row for a Scenario Outline, since `run_scenario` calls this
    # once per row `_expand` produces. All rows of one outline share
    # `scenario["id"]`: the id disambiguates scenarios, not rows within one,
    # so covsel sees the same id begin and end several times in a row rather
    # than once. That is safe only because rows run strictly serially, which
    # covsel#122 requires of the harness anyway ("one app process, one test
    # at a time").
    _covsel_notify("begin", scenario["id"])
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
    _covsel_notify("end", scenario["id"], result.outcome)
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
