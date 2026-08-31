"""``vellum backpressure``: what counts, where the gate closes, what it says.

Replaces the ``backpressure`` job in ``spec-ci.yml``, which echoed a stub and
exited 0. The scenario it answers to is ``@id:backpressure-blocks-merge`` in
``spec/features/spec-pipeline.md``: a cap of 3 with 3 approved-and-unshipped
versions blocks the next merge.
"""

import tempfile
import unittest
from pathlib import Path

from support import CONFIG, make_intent_repo, run_cli, write_record
from vellum.backpressure import BackpressureError, measure

#: Distinct, well-formed shas. Nothing resolves them — the window is counted
#: from records, not from history — so they only have to differ. The leading
#: `a` is not decoration: an all-digit sha written unquoted is a *number* to
#: YAML (`0000…01` loads as `1`), and a fixture written that way tests the
#: quoting of the fixture rather than the counting of the records. `dump()`
#: quotes such a sha correctly, which is why real records never hit it.
SHAS = [f"a{n:039x}" for n in range(1, 9)]


class BackpressureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_intent_repo(Path(self.tmp.name) / "intent", cap=3)
        self.ledger = self.repo / "ledger"

    def records(self, *states):
        for sha, state in zip(SHAS, states):
            write_record(self.ledger, sha, state=state)


class TestWhatCounts(BackpressureCase):
    def test_an_empty_ledger_is_an_empty_window(self):
        window = measure(self.repo)
        self.assertEqual(window.count, 0)
        self.assertFalse(window.blocked)

    def test_every_state_short_of_shipped_counts_as_unshipped(self):
        # "approved-but-unshipped" is the whole window, not just state
        # `approved`: a version being implemented is intent the product has
        # still not delivered.
        self.records("approved", "planning", "implementing", "verified")
        self.assertEqual(measure(self.repo).count, 4)

    def test_shipped_versions_leave_the_window(self):
        self.records("shipped", "shipped", "approved")
        self.assertEqual(measure(self.repo).count, 1)

    def test_superseded_versions_leave_the_window(self):
        # A superseded version will never ship, so holding the gate closed on
        # it would be backpressure that nothing can ever relieve.
        self.records("superseded", "approved")
        self.assertEqual(measure(self.repo).count, 1)

    def test_releases_yaml_is_not_a_record(self):
        (self.ledger / "releases.yaml").write_text(
            "spec_head: null\nchannels: {}\ncuts: []\n", encoding="utf-8"
        )
        self.records("approved")
        window = measure(self.repo)
        self.assertEqual(window.count, 1)
        self.assertEqual(window.unreadable, [])

    def test_a_suite_json_beside_the_records_is_ignored(self):
        # `on-spec-merge` writes `ledger/suite-<sha>.json` into the same
        # directory. Only `*.yaml` is read.
        (self.ledger / f"suite-{SHAS[0]}.json").write_text("{}", encoding="utf-8")
        self.records("approved")
        self.assertEqual(measure(self.repo).count, 1)

    def test_a_name_keyed_legacy_record_is_not_counted_and_is_reported(self):
        # The intent repo carried `ledger/spec-v1.yaml`..`spec-v11.yaml` keyed
        # by name. Counting one would let a leftover from the pre-commit
        # version system hold the gate closed, and skipping it silently would
        # hide a ledger that needs migrating.
        (self.ledger / "spec-v1.yaml").write_text(
            "spec_version: spec-v1\nstate: approved\n", encoding="utf-8"
        )
        self.records("approved")

        window = measure(self.repo)

        self.assertEqual(window.count, 1)
        self.assertEqual(window.unreadable, ["spec-v1.yaml"])
        self.assertIn("spec-v1.yaml", window.report())

    def test_an_unparseable_record_is_reported_rather_than_counted(self):
        (self.ledger / "broken.yaml").write_text("{ not: valid: yaml", encoding="utf-8")
        window = measure(self.repo)
        self.assertEqual(window.unreadable, ["broken.yaml"])

    def test_a_record_with_no_state_still_counts(self):
        # It has certainly not shipped. Dropping it would let a malformed
        # record widen the window silently.
        (self.ledger / f"{SHAS[0]}.yaml").write_text(
            f"spec_version: {SHAS[0]}\n", encoding="utf-8"
        )
        self.assertEqual(measure(self.repo).count, 1)


class TestWhereTheGateCloses(BackpressureCase):
    """``@id:backpressure-blocks-merge``: cap 3, three unshipped, blocked."""

    def test_below_the_cap_is_not_blocked(self):
        self.records("approved", "approved")
        self.assertFalse(measure(self.repo).blocked)

    def test_at_the_cap_is_blocked(self):
        # The question is "may another version land", not "how many are
        # there": landing one more would put the window past the cap, so the
        # gate closes at the cap rather than after it.
        self.records("approved", "approved", "approved")
        self.assertTrue(measure(self.repo).blocked)

    def test_past_the_cap_is_blocked(self):
        self.records("approved", "approved", "approved", "approved")
        self.assertTrue(measure(self.repo).blocked)

    def test_the_scenario_as_written(self):
        self.records("approved", "approved", "approved")
        code, out = run_cli(["backpressure", str(self.repo)])
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", out)
        self.assertIn("cap of 3", out)

    def test_shipping_one_reopens_the_gate(self):
        self.records("approved", "approved", "shipped")
        code, _ = run_cli(["backpressure", str(self.repo)])
        self.assertEqual(code, 0)


class TestTheCap(BackpressureCase):
    def test_it_is_read_from_the_installation_config(self):
        self.assertEqual(measure(self.repo).cap, 3)

    def test_cap_overrides_it_without_editing_policy(self):
        self.records("approved", "approved", "approved")
        self.assertFalse(measure(self.repo, cap=5).blocked)
        code, _ = run_cli(["backpressure", str(self.repo), "--cap", "5"])
        self.assertEqual(code, 0)

    def test_a_cap_of_zero_blocks_everything(self):
        # Not a missing value: a deliberate freeze. `0` must not read as unset.
        self.assertTrue(measure(self.repo, cap=0).blocked)

    def test_a_missing_divergence_cap_is_an_error_not_a_default(self):
        # A default would let a typo'd key silently disable the gate, and a
        # gate that can turn itself off is not one.
        (self.repo / ".vellum" / "config.yaml").write_text(
            "version_prefix: spec-v\nbudgets:\n  per_item_usd: 10\n", encoding="utf-8"
        )
        with self.assertRaises(BackpressureError):
            measure(self.repo)
        code, out = run_cli(["backpressure", str(self.repo)])
        self.assertEqual(code, 1)
        self.assertIn("divergence_cap", out)

    def test_a_non_integer_cap_is_an_error(self):
        (self.repo / ".vellum" / "config.yaml").write_text(
            CONFIG.format(cap="lots"), encoding="utf-8"
        )
        with self.assertRaises(BackpressureError):
            measure(self.repo)

    def test_a_missing_config_is_an_error(self):
        (self.repo / ".vellum" / "config.yaml").unlink()
        with self.assertRaises(BackpressureError):
            measure(self.repo)


class TestPending(BackpressureCase):
    """The half a checkout cannot see.

    ``spec/features/spec-pipeline.md`` counts approved-but-unlanded spec PRs
    alongside landed-but-unshipped versions. An open PR is forge state, so a
    caller that can see the forge passes the count.
    """

    def test_pending_prs_count_toward_the_window(self):
        self.records("approved", "approved")
        self.assertTrue(measure(self.repo, pending=1).blocked)

    def test_the_report_says_when_only_the_ledger_half_was_measured(self):
        self.records("approved")
        self.assertIn("forge state", measure(self.repo).report())

    def test_it_does_not_say_so_when_the_caller_supplied_the_other_half(self):
        self.records("approved")
        self.assertNotIn("forge state", measure(self.repo, pending=2).report())

    def test_a_negative_count_is_refused(self):
        # Otherwise a caller could talk the window back under its cap.
        with self.assertRaises(BackpressureError):
            measure(self.repo, pending=-5)


class TestTheReport(BackpressureCase):
    """Report-style output either way: the margin is the useful half."""

    def test_a_passing_run_says_how_much_room_is_left(self):
        self.records("approved")
        code, out = run_cli(["backpressure", str(self.repo)])
        self.assertEqual(code, 0)
        self.assertIn("OK", out)
        self.assertIn("room for 2", out)

    def test_it_lists_the_versions_holding_the_window_open(self):
        write_record(self.ledger, SHAS[0], state="approved", name="spec-v9")
        out = measure(self.repo).report()
        self.assertIn(SHAS[0][:12], out)
        self.assertIn("spec-v9", out)

    def test_an_empty_window_says_so_rather_than_printing_nothing(self):
        self.assertIn("nothing unshipped", measure(self.repo).report())


class TestInvocationErrors(BackpressureCase):
    def test_a_checkout_with_no_ledger_is_an_error_not_an_empty_window(self):
        # An empty window is the answer that lets every merge through, so
        # "I could not find the ledger" must never be mistaken for it.
        for child in self.ledger.iterdir():
            child.unlink()
        self.ledger.rmdir()
        with self.assertRaises(BackpressureError):
            measure(self.repo)

    def test_the_cli_exits_one_for_it(self):
        code, out = run_cli(["backpressure", str(Path(self.tmp.name) / "nowhere")])
        self.assertEqual(code, 1)
        self.assertIn("config", out)

    def test_ledger_dir_points_it_elsewhere(self):
        other = Path(self.tmp.name) / "elsewhere"
        write_record(other, SHAS[0], state="approved")
        self.assertEqual(measure(self.repo, ledger_dir=other).count, 1)


if __name__ == "__main__":
    unittest.main()
