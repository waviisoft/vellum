"""Tests for the seeded harness's covsel test-boundary hook.

waviisoft/vellum#31 item 2: ``seeds/harness/support/runner.py`` gains
covsel#122's env-gated boundary protocol, as a reference implementation a
harness engineer reads and ports — Portolan's 49-module harness is not an
owned path, so ``vellum upgrade`` never re-stamps it there.

These tests target the seed's ``support/runner.py`` directly (not a
provisioned installation), the way ``harness/run.py`` itself runs it: with the
harness directory on ``sys.path`` so its own ``from support import ...``
imports resolve. Each test resets ``sys.path`` and ``sys.modules`` afterwards
so this file's import of the seed cannot leak into another test module that
happens to run in the same process.
"""

from __future__ import annotations

import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vellum import seeds

HARNESS = Path(seeds.__file__).resolve().parent / seeds.HARNESS


def scenario(id="scn-1", steps=None, examples=None):
    return {
        "id": id,
        "name": "a scenario",
        "feature": "a feature",
        "file": "spec/features/f.md",
        "line": 3,
        "version": "deadbeef",
        "pending": False,
        "steps": steps or [],
        "examples": examples or [],
    }


class CovselBoundaryCase(unittest.TestCase):
    def setUp(self):
        self._old_sys_path = list(sys.path)
        self._old_modules = {
            name: mod for name, mod in sys.modules.items()
            if name == "support" or name.startswith("support.")
        }
        for name in list(self._old_modules):
            del sys.modules[name]
        sys.path.insert(0, str(HARNESS))

        import support.adapter as adapter
        import support.runner as runner

        self.runner = runner
        self.deployment = adapter.no_deployment()

        self._old_env = os.environ.pop("COVSEL_BOUNDARY", None)

    def tearDown(self):
        sys.path[:] = self._old_sys_path
        for name in list(sys.modules):
            if name == "support" or name.startswith("support."):
                del sys.modules[name]
        sys.modules.update(self._old_modules)
        if self._old_env is not None:
            os.environ["COVSEL_BOUNDARY"] = self._old_env
        else:
            os.environ.pop("COVSEL_BOUNDARY", None)


class TestUnsetIsATrueNoOp(CovselBoundaryCase):
    """"Unset, it does nothing" — the property that matters most."""

    def test_dispatch_is_never_reached(self):
        called = []
        self.runner._covsel_dispatch = lambda boundary, message: called.append(message)

        results = self.runner.run_scenario(self.deployment, scenario())

        self.assertEqual([r.outcome for r in results], [self.runner.PASS])
        self.assertEqual(called, [])

    def test_no_output_reaches_stdout_or_stderr(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.runner.run_scenario(self.deployment, scenario())
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_a_scenario_with_no_stable_id_is_also_a_no_op(self):
        # Not in covsel's inventory either (waviisoft/covsel#123 keys entries
        # by id), so there is nothing to correlate a boundary with.
        called = []
        self.runner._covsel_dispatch = lambda boundary, message: called.append(message)
        os.environ["COVSEL_BOUNDARY"] = "http://example.invalid/covsel"

        results = self.runner.run_scenario(self.deployment, scenario(id=None))

        self.assertEqual([r.outcome for r in results], [self.runner.PASS])
        self.assertEqual(called, [])


class TestExamplesRows(CovselBoundaryCase):
    """Does the seed run an Examples row as a separate test? Yes — each row
    is its own call to ``_run_one`` (own ``World``, own steps, own
    ``ScenarioResult``), so it gets its own begin/end pair. But every row of
    one outline shares ``scenario["id"]``: the id names the scenario, not the
    row, so covsel sees the same id begin and end twice in a row rather than
    once. Safe only because rows run strictly serially — which covsel#122
    requires of the harness anyway ("one app process, one test at a time").
    """

    def _outline(self):
        seen = []
        self.runner.registry.given(r"a value of (\d+)")(
            lambda world, n: seen.append(n)
        )
        outline = scenario(
            id="outline-1",
            steps=[{"keyword": "Given", "text": "a value of <n>"}],
            examples=[{"header": ["n"], "rows": [["1"], ["2"]]}],
        )
        return outline, seen

    def test_two_rows_produce_two_results_sharing_one_id(self):
        outline, seen = self._outline()

        results = self.runner.run_scenario(self.deployment, outline)

        self.assertEqual(seen, ["1", "2"])
        self.assertEqual([r.id for r in results], ["outline-1", "outline-1"])
        self.assertEqual([r.example for r in results], [{"n": "1"}, {"n": "2"}])
        self.assertEqual([r.outcome for r in results], [self.runner.PASS, self.runner.PASS])

    def test_each_row_gets_its_own_begin_end_pair_naming_the_shared_id(self):
        outline, _ = self._outline()
        seen_messages = []
        self.runner._covsel_dispatch = (
            lambda boundary, message: seen_messages.append(dict(message))
        )
        os.environ["COVSEL_BOUNDARY"] = "http://example.invalid/covsel"

        self.runner.run_scenario(self.deployment, outline)

        self.assertEqual(seen_messages, [
            {"event": "begin", "id": "outline-1"},
            {"event": "end", "id": "outline-1", "outcome": "pass"},
            {"event": "begin", "id": "outline-1"},
            {"event": "end", "id": "outline-1", "outcome": "pass"},
        ])


class TestSetButUndispatchableRefusesRatherThanGuessing(CovselBoundaryCase):
    """covsel#122 is an open, uncommented, code-free "candidate shape" with
    no wire framing, no acknowledgement shape, and no lost-acknowledgement
    behavior defined. Setting COVSEL_BOUNDARY must fail loudly and
    immediately rather than hang, silently proceed, or guess — this is the
    reference implementation's substitute for a literal lost-acknowledgement
    test, since there is no real acknowledgement to lose yet.
    """

    def test_refuses_before_the_world_or_any_step_is_touched(self):
        class BoomIfConstructed:
            def __init__(self, *a, **k):
                raise AssertionError("World must not be constructed")

        self.runner.World = BoomIfConstructed
        os.environ["COVSEL_BOUNDARY"] = "http://example.invalid/covsel"

        step_ran = []
        self.runner.registry.given(r"a step that must not run")(
            lambda world: step_ran.append(True)
        )

        with self.assertRaises(NotImplementedError) as ctx:
            self.runner.run_scenario(
                self.deployment,
                scenario(steps=[{"keyword": "Given", "text": "a step that must not run"}]),
            )

        self.assertIn("covsel#122", str(ctx.exception))
        self.assertIn("COVSEL_BOUNDARY", str(ctx.exception))
        self.assertEqual(step_ran, [])

    def test_the_refusal_names_the_message_it_could_not_send(self):
        os.environ["COVSEL_BOUNDARY"] = "/tmp/does-not-exist.fifo"
        with self.assertRaises(NotImplementedError) as ctx:
            self.runner._covsel_notify("begin", "scn-1")
        self.assertIn("'event': 'begin'", str(ctx.exception))
        self.assertIn("'id': 'scn-1'", str(ctx.exception))


class TestOutcomeMapping(CovselBoundaryCase):
    """The mapping lives in one place: ``runner._COVSEL_OUTCOME``."""

    def test_every_harness_outcome_has_an_entry(self):
        self.assertEqual(set(self.runner._COVSEL_OUTCOME), set(self.runner.OUTCOMES))

    def test_an_end_message_carries_the_mapped_outcome(self):
        captured = []
        self.runner._covsel_dispatch = lambda boundary, message: captured.append(message)
        os.environ["COVSEL_BOUNDARY"] = "http://example.invalid/covsel"

        for outcome, mapped in self.runner._COVSEL_OUTCOME.items():
            captured.clear()
            self.runner._covsel_notify("end", "scn-1", outcome)
            self.assertEqual(
                captured, [{"event": "end", "id": "scn-1", "outcome": mapped}]
            )

    def test_a_begin_message_carries_no_outcome_key(self):
        captured = []
        self.runner._covsel_dispatch = lambda boundary, message: captured.append(message)
        os.environ["COVSEL_BOUNDARY"] = "http://example.invalid/covsel"

        self.runner._covsel_notify("begin", "scn-1")

        self.assertEqual(captured, [{"event": "begin", "id": "scn-1"}])


if __name__ == "__main__":
    unittest.main()
