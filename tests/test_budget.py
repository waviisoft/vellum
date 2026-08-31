"""``vellum budget``: recorded spend against the installation's caps.

The scenario is ``@id:global-cap-parks-queue`` in
``spec/behaviors/budgets-and-costs.md``: a period cap of $100 with $99 recorded,
and a next work item whose certification would exceed the cap, parks the queue
and files a spend report.

Certification does not exist yet, so "would exceed" is an input rather than
something a checkout can compute — ``--projected``, the same shape
``backpressure --pending`` uses for the half of its window a repository cannot
see. Everything else in the scenario is real: the cap is a value in
``.vellum/config.yaml`` and the spend is accumulated by ``vellum ledger advance
--usd``, exactly as the intent repo's harness builds it.
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from support import make_intent_repo, run_cli, run_cli_streams, write_record
from vellum.budget import PARK_MARKER, BudgetError, measure, parse_time, window_for
from vellum.ledger import advance

SHAS = [f"a{n:039x}" for n in range(1, 6)]

CONFIG = """version_prefix: spec-v
budgets:
  divergence_cap: 3
  period: {period}
  period_usd: {period_usd}
{per_item}"""


class BudgetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_intent_repo(Path(self.tmp.name) / "intent")
        self.ledger = self.repo / "ledger"
        self.config(period_usd=100)

    def config(self, period_usd=100, per_item_usd=None, period="monthly", extra=None):
        text = CONFIG.format(
            period=period,
            period_usd=period_usd,
            per_item="" if per_item_usd is None else f"  per_item_usd: {per_item_usd}\n",
        )
        (self.repo / ".vellum" / "config.yaml").write_text(extra or text, encoding="utf-8")

    def spend(self, usd, sha=SHAS[0], issue=1, approved=None, **kwargs):
        """Record *usd* against a work item, through the product's own CLI path."""
        from vellum.ledger import new_record, record_path, write

        path = record_path(self.ledger, sha)
        if not path.exists():
            self.ledger.mkdir(parents=True, exist_ok=True)
            write(path, new_record(sha, approved=approved))
        advance(
            self.ledger, sha, issue=issue, title=f"Implement {issue}", repo="app",
            usd=usd, attempts=1, executor="claude-actions", **kwargs
        )
        return path


class TestTheScenario(BudgetCase):
    def setUp(self):
        super().setUp()
        self.config(period_usd=100)
        self.spend(99.0)

    def test_recorded_spend_is_what_the_ledger_says(self):
        self.assertEqual(measure(self.repo).spent, 99.0)

    def test_ninety_nine_of_a_hundred_does_not_park_on_its_own(self):
        self.assertFalse(measure(self.repo).parked)

    def test_a_next_item_that_would_exceed_the_cap_parks_the_queue(self):
        # @id:global-cap-parks-queue, as written.
        spend = measure(self.repo, projected=5.0)
        self.assertTrue(spend.queue_parked)
        self.assertTrue(spend.parked)

    def test_the_cli_exits_one_and_emits_the_park_marker(self):
        code, out = run_cli(["budget", str(self.repo), "--projected", "5"])
        self.assertEqual(code, 1)
        self.assertIn("PARKED [queue]", out)
        self.assertIn(PARK_MARKER, out)

    def test_the_report_names_the_window_and_the_spend(self):
        report = measure(self.repo, projected=5.0).report()
        self.assertIn("(monthly)", report)
        self.assertIn("$104.00 of $100.00", report)
        self.assertIn("spend report", report)


class TestWhereTheCapBites(BudgetCase):
    def test_hitting_the_cap_exactly_parks_the_queue(self):
        # "hitting the global cap parks the queue" — the question is whether the
        # next work item may run, so the comparison is >=, the same reading
        # `backpressure` gives its own cap.
        self.spend(100.0)
        self.assertTrue(measure(self.repo).queue_parked)

    def test_below_the_cap_is_not_parked(self):
        self.spend(50.0)
        self.assertFalse(measure(self.repo).parked)

    def test_a_cap_override_does_not_need_the_config_edited(self):
        self.spend(50.0)
        self.assertTrue(measure(self.repo, period_cap=50).queue_parked)

    def test_a_negative_projection_is_refused(self):
        with self.assertRaises(BudgetError):
            measure(self.repo, projected=-1)

    def test_the_report_says_when_no_projection_was_supplied(self):
        self.spend(1.0)
        self.assertIn("--projected", measure(self.repo).report())

    def test_it_does_not_say_so_when_the_caller_supplied_one(self):
        self.spend(1.0)
        self.assertNotIn(
            "not something a checkout can know", measure(self.repo, projected=2).report()
        )


class TestThePerItemCap(BudgetCase):
    def test_an_item_past_its_cap_is_parked_as_needs_human(self):
        self.config(period_usd=1000, per_item_usd=10)
        self.spend(12.0, issue=1)
        spend = measure(self.repo)
        self.assertFalse(spend.queue_parked)
        self.assertTrue(spend.parked)
        self.assertEqual([i.issue for i in spend.over_items], [1])
        self.assertIn(f"PARKED [{PARK_MARKER}]", spend.report())

    def test_an_item_at_its_cap_is_not_past_it(self):
        # The per-item rule is "exceeding", where the queue rule is "hitting".
        # Two different words in one behavior; both are read as written.
        self.config(period_usd=1000, per_item_usd=10)
        self.spend(10.0)
        self.assertFalse(measure(self.repo).parked)

    def test_it_is_not_windowed(self):
        # A per-item cap is a lifetime cap on one item, so a record from an
        # earlier period still parks its own item.
        self.config(period_usd=1000, per_item_usd=10)
        self.spend(12.0, approved="2020-01-01T00:00:00Z")
        spend = measure(self.repo)
        self.assertEqual(spend.spent, 0.0)
        self.assertEqual([i.issue for i in spend.over_items], [1])

    def test_no_per_item_cap_leaves_it_unchecked_and_says_so(self):
        # A missing cap is not a cap of zero and not a cap of infinity. The
        # period half still runs, and the report states plainly that the
        # per-item half did not — the harness's own sandbox config declares no
        # per_item_usd, so this is the ordinary shape, not an exotic one.
        self.config(period_usd=1000)
        self.spend(500.0)
        spend = measure(self.repo)
        self.assertIsNone(spend.item_cap)
        self.assertEqual(spend.over_items, [])
        self.assertIn("no per-item cap was checked", spend.report())

    def test_item_cap_supplies_one_without_editing_policy(self):
        self.config(period_usd=1000)
        self.spend(500.0)
        self.assertTrue(measure(self.repo, item_cap=100).parked)

    def test_the_cli_exits_one_for_a_parked_item(self):
        self.config(period_usd=1000, per_item_usd=10)
        self.spend(12.0)
        code, out = run_cli(["budget", str(self.repo)])
        self.assertEqual(code, 1)
        self.assertIn("item(s) parked", out)


class TestTheWindow(BudgetCase):
    def test_a_monthly_window_is_the_calendar_month_containing_the_moment(self):
        start, end = window_for("monthly", datetime.datetime(2026, 8, 31, 12,
                                tzinfo=datetime.timezone.utc))
        self.assertEqual(start.date().isoformat(), "2026-08-01")
        self.assertEqual(end.date().isoformat(), "2026-09-01")

    def test_a_december_window_rolls_the_year(self):
        _, end = window_for("monthly", datetime.datetime(2026, 12, 5,
                            tzinfo=datetime.timezone.utc))
        self.assertEqual(end.date().isoformat(), "2027-01-01")

    def test_a_weekly_window_starts_on_monday(self):
        start, end = window_for("weekly", datetime.datetime(2026, 8, 31, 12,
                                tzinfo=datetime.timezone.utc))
        self.assertEqual(start.weekday(), 0)
        self.assertEqual((end - start).days, 7)

    def test_spend_from_an_earlier_period_is_outside_the_window(self):
        self.spend(500.0, approved="2020-01-01T00:00:00Z")
        spend = measure(self.repo)
        self.assertEqual(spend.spent, 0.0)
        self.assertFalse(spend.parked)

    def test_as_of_measures_the_period_containing_that_moment(self):
        self.spend(500.0, approved="2020-01-15T00:00:00Z")
        spend = measure(
            self.repo,
            as_of=datetime.datetime(2020, 1, 20, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(spend.spent, 500.0)

    def test_a_record_at_the_first_instant_of_the_window_is_inside_it(self):
        self.spend(10.0, approved="2020-01-01T00:00:00Z")
        spend = measure(
            self.repo, as_of=datetime.datetime(2020, 1, 31, tzinfo=datetime.timezone.utc)
        )
        self.assertEqual(spend.spent, 10.0)

    def test_an_unreadable_approved_time_is_counted_inside_the_window(self):
        # Fail closed: a cost this cannot prove belongs to an earlier period is
        # one the cap must not let through. The report names every record it
        # had to treat that way, so the assumption is visible rather than
        # absorbed.
        self.spend(500.0, approved="not a date")
        spend = measure(self.repo)
        self.assertEqual(spend.spent, 500.0)
        self.assertEqual(len(spend.undated), 1)
        self.assertIn("no readable `approved`", spend.report())

    def test_an_unquoted_yaml_timestamp_is_read_as_one(self):
        # PyYAML turns an unquoted timestamp into a datetime before this sees
        # it — the same trap `_is_iso_date` in lint.py exists for.
        self.spend(10.0)
        path = self.ledger / f"{SHAS[0]}.yaml"
        path.write_text(
            path.read_text().replace("approved: '", "approved: ").replace("Z'", "Z"),
            encoding="utf-8",
        )
        spend = measure(self.repo)
        self.assertEqual(spend.undated, [])
        self.assertEqual(spend.spent, 10.0)

    def test_a_naive_timestamp_is_read_as_utc(self):
        self.assertEqual(parse_time("2026-08-31T00:00:00").tzinfo, datetime.timezone.utc)

    def test_a_bare_date_is_a_moment(self):
        self.assertEqual(parse_time(datetime.date(2026, 8, 31)).day, 31)


class TestConfiguration(BudgetCase):
    def test_a_missing_period_usd_is_an_error_not_a_default(self):
        self.config(extra="budgets:\n  period: monthly\n")
        with self.assertRaises(BudgetError) as caught:
            measure(self.repo)
        self.assertIn("period_usd", str(caught.exception))

    def test_a_missing_period_is_an_error_not_a_default(self):
        # A spend cap with no period is not a period cap, and guessing one
        # would measure the wrong window under a heading that says otherwise.
        self.config(extra="budgets:\n  period_usd: 100\n")
        with self.assertRaises(BudgetError) as caught:
            measure(self.repo)
        self.assertIn("no budgets.period", str(caught.exception))

    def test_an_unrecognised_period_is_refused(self):
        self.config(period="fortnightly")
        with self.assertRaises(BudgetError):
            measure(self.repo)

    def test_a_non_numeric_cap_is_refused(self):
        self.config(extra="budgets:\n  period: monthly\n  period_usd: lots\n")
        with self.assertRaises(BudgetError):
            measure(self.repo)

    def test_a_missing_config_cannot_be_answered(self):
        (self.repo / ".vellum" / "config.yaml").unlink()
        with self.assertRaises(BudgetError):
            measure(self.repo)

    def test_the_cli_exits_two_for_a_configuration_problem(self):
        # 2, not 1. A renamed config reaching a caller as "the queue is parked"
        # is a stop nobody can find the cause of — the same line
        # `backpressure` draws between "blocked" and "no answer".
        (self.repo / ".vellum" / "config.yaml").unlink()
        code, _ = run_cli(["budget", str(self.repo)])
        self.assertEqual(code, 2)

    def test_no_ledger_directory_cannot_be_answered(self):
        for entry in self.ledger.iterdir():
            entry.unlink()
        self.ledger.rmdir()
        with self.assertRaises(BudgetError):
            measure(self.repo)

    def test_an_unreadable_as_of_is_an_invocation_error(self):
        code, out = run_cli(["budget", str(self.repo), "--as-of", "yesterday"])
        self.assertEqual(code, 2)
        self.assertIn("ISO 8601", out)


class TestWhatIsCounted(BudgetCase):
    def test_records_with_no_work_items_contribute_nothing(self):
        write_record(self.ledger, SHAS[1])
        self.assertEqual(measure(self.repo).spent, 0.0)

    def test_spend_accumulates_across_records_and_items(self):
        self.spend(10.0, sha=SHAS[0], issue=1)
        self.spend(20.0, sha=SHAS[0], issue=2)
        self.spend(30.0, sha=SHAS[1], issue=3)
        self.assertEqual(measure(self.repo).spent, 60.0)

    def test_cost_accumulates_the_way_ledger_advance_writes_it(self):
        # `advance` adds rather than replaces, so two invocations of one agent
        # against one item is one item that has spent the sum.
        self.spend(40.0, issue=1)
        self.spend(40.0, issue=1)
        self.assertEqual(measure(self.repo).spent, 80.0)

    def test_a_junk_cost_field_is_listed_rather_than_taking_the_run_down(self):
        self.spend(10.0, issue=1)
        path = self.ledger / f"{SHAS[0]}.yaml"
        path.write_text(path.read_text().replace("usd: 10.0", "usd: lots"), encoding="utf-8")
        spend = measure(self.repo)
        self.assertEqual(spend.spent, 0.0)
        self.assertEqual(len(spend.windowed), 1)

    def test_releases_yaml_is_not_a_record(self):
        (self.ledger / "releases.yaml").write_text("cuts: []\n", encoding="utf-8")
        self.spend(10.0)
        self.assertEqual(measure(self.repo).unreadable, [])

    def test_an_unparseable_record_is_reported(self):
        (self.ledger / "broken.yaml").write_text("{[", encoding="utf-8")
        self.assertEqual(measure(self.repo).unreadable, ["broken.yaml"])


class TestJson(BudgetCase):
    def test_it_carries_the_park_state_a_caller_acts_on(self):
        # Read from stdout alone. A parked run also writes a diagnostic, and it
        # goes to stderr precisely so `vellum budget --json | jq` still parses —
        # the same property `suite extract -o -` keeps.
        self.spend(99.0)
        code, out, err = run_cli_streams(
            ["budget", str(self.repo), "--projected", "5", "--json"]
        )
        self.assertEqual(code, 1)
        self.assertIn(PARK_MARKER, err)
        payload = json.loads(out)
        self.assertTrue(payload["parked"])
        self.assertEqual(payload["state"], "parked")
        self.assertEqual(payload["marker"], PARK_MARKER)
        self.assertEqual(payload["committed_usd"], 104.0)

    def test_a_parked_run_writes_nothing_but_json_to_stdout(self):
        self.spend(99.0)
        _, out, _ = run_cli_streams(["budget", str(self.repo), "--projected", "5", "--json"])
        self.assertEqual(out.strip()[0], "{")
        self.assertEqual(out.strip()[-1], "}")

    def test_an_unparked_run_carries_no_marker(self):
        self.spend(1.0)
        code, out, _ = run_cli_streams(["budget", str(self.repo), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["parked"])
        self.assertEqual(payload["state"], "ok")
        self.assertIsNone(payload["marker"])

    def test_it_names_the_window_it_measured(self):
        _, out, _ = run_cli_streams(["budget", str(self.repo), "--json"])
        payload = json.loads(out)
        self.assertTrue(payload["window_start"].endswith("Z"))
        self.assertEqual(payload["period"], "monthly")


if __name__ == "__main__":
    unittest.main()
