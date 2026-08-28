"""``vellum ledger open|advance``: record shape, idempotence, cost accounting."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import yaml

from vellum.cli import main
from vellum.ledger import (
    ITEM_KEYS,
    LedgerError,
    RECORD_KEYS,
    advance,
    dump,
    load,
    load_plan,
    open_record,
    parse_version,
    record_path,
)


def run_cli(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return main(argv), buf.getvalue()


class TestVersionParsing(unittest.TestCase):
    def test_accepts_bare_integer_and_tag_form(self):
        self.assertEqual(parse_version("42"), 42)
        self.assertEqual(parse_version("spec-v42"), 42)
        self.assertEqual(parse_version(42), 42)

    def test_rejects_semantic_versions(self):
        # Versions are bare monotonic integers (decision D6).
        for bad in ("v1.2.3", "spec-v1.2", "latest", ""):
            with self.assertRaises(LedgerError):
                parse_version(bad)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "ledger"
        self.addCleanup(self.tmp.cleanup)

    def record(self, number=42):
        return load(record_path(self.dir, number))


class TestOpen(LedgerCase):
    def test_creates_a_record_in_state_approved(self):
        path, created = open_record(self.dir, 42, spec_pr=118, baseline=38, labels=["spec:feature"])
        self.assertTrue(created)
        record = load(path)
        self.assertEqual(record["spec_version"], "spec-v42")
        self.assertEqual(record["state"], "approved")
        self.assertEqual(record["spec_pr"], 118)
        self.assertEqual(record["baseline"], "spec-v38")
        self.assertEqual(record["labels"], ["spec:feature"])

    def test_record_carries_every_field_the_spec_names(self):
        path, _ = open_record(self.dir, 42)
        self.assertEqual(list(load(path)), list(RECORD_KEYS))

    def test_reserved_fields_are_present_with_defaults(self):
        # Deferred features keep their shape reserved: lines (D13), locks (G2).
        record = load(open_record(self.dir, 42)[0])
        self.assertEqual(record["line"], "main")
        self.assertEqual(record["locks"], [])

    def test_opening_twice_leaves_the_first_record_untouched(self):
        # The reconciler may replay an approval (decision D11).
        path, _ = open_record(self.dir, 42, spec_pr=118)
        advance(self.dir, 42, state="implementing")
        before = path.read_text()
        _, created = open_record(self.dir, 42, spec_pr=999)
        self.assertFalse(created)
        self.assertEqual(path.read_text(), before)

    def test_approval_time_is_recorded(self):
        record = load(open_record(self.dir, 42, approved="2026-08-27T14:02:00Z")[0])
        self.assertEqual(record["approved"], "2026-08-27T14:02:00Z")


class TestAdvance(LedgerCase):
    def setUp(self):
        super().setUp()
        open_record(self.dir, 42, spec_pr=118, baseline=38, labels=["spec:feature"])

    def test_record_state_advances(self):
        advance(self.dir, 42, state="implementing")
        self.assertEqual(self.record()["state"], "implementing")

    def test_unknown_record_state_is_rejected(self):
        with self.assertRaises(LedgerError):
            advance(self.dir, 42, state="finished")

    def test_unknown_item_state_is_rejected(self):
        advance(self.dir, 42, issue=121, title="t", repo="app")
        with self.assertRaises(LedgerError):
            advance(self.dir, 42, issue=121, item_state="done")

    def test_advancing_a_missing_record_fails(self):
        with self.assertRaises(LedgerError):
            advance(self.dir, 99, state="shipped")

    def test_work_item_is_added_then_updated_in_place(self):
        advance(self.dir, 42, issue=121, title="Session expiry", repo="app",
                satisfies=["features/auth.md#session-expiry/idle-session-expires"])
        advance(self.dir, 42, issue=121, item_state="merged", pr=124)
        items = self.record()["work_items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "merged")
        self.assertEqual(items[0]["pr"], 124)
        self.assertEqual(items[0]["satisfies"], ["features/auth.md#session-expiry/idle-session-expires"])

    def test_work_item_carries_every_field_the_spec_names(self):
        advance(self.dir, 42, issue=121, title="t", repo="app")
        self.assertEqual(list(self.record()["work_items"][0]), list(ITEM_KEYS))

    def test_adding_an_unknown_item_without_title_and_repo_fails(self):
        with self.assertRaises(LedgerError):
            advance(self.dir, 42, issue=404, item_state="merged")

    def test_work_item_options_require_an_item(self):
        with self.assertRaises(LedgerError):
            advance(self.dir, 42, pr=124)

    def test_briefing_is_stored_on_the_work_item(self):
        # "what the agent knew is a factual, reproducible question"
        advance(self.dir, 42, issue=121, title="t", repo="app", briefing="the briefing text")
        self.assertEqual(self.record()["work_items"][0]["briefing"], "the briefing text")

    def test_release_is_recorded(self):
        advance(self.dir, 42, release="r58")
        self.assertEqual(self.record()["release"], "r58")


class TestCostAccounting(LedgerCase):
    def setUp(self):
        super().setUp()
        open_record(self.dir, 42)
        advance(self.dir, 42, issue=121, title="t", repo="app")

    def cost(self):
        return self.record()["work_items"][0]["cost"]

    def test_a_new_item_starts_at_zero(self):
        self.assertEqual(self.cost(), {"attempts": 0, "tokens": 0, "usd": 0.0, "executor": None})

    def test_every_invocation_accumulates(self):
        # Each agent invocation records cost into its work item's entry.
        advance(self.dir, 42, issue=121, attempts=1, tokens=210000, usd=1.60, executor="claude-actions")
        advance(self.dir, 42, issue=121, attempts=1, tokens=202000, usd=1.50, executor="claude-actions")
        self.assertEqual(
            self.cost(),
            {"attempts": 2, "tokens": 412000, "usd": 3.10, "executor": "claude-actions"},
        )

    def test_executor_records_the_most_recent_one(self):
        advance(self.dir, 42, issue=121, executor="claude-local")
        advance(self.dir, 42, issue=121, executor="claude-actions")
        self.assertEqual(self.cost()["executor"], "claude-actions")


class TestWorkPlan(LedgerCase):
    def setUp(self):
        super().setUp()
        open_record(self.dir, 42)

    def plan_file(self, text):
        path = Path(self.tmp.name) / "workplan.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_plan_is_committed_to_the_record(self):
        path = self.plan_file(
            "work_items:\n"
            "  - issue: 121\n    title: Session expiry\n    repo: app\n"
            "    satisfies: [features/auth.md#session-expiry/idle-session-expires]\n"
            "  - issue: 122\n    title: Sign-out\n    repo: app\n"
        )
        advance(self.dir, 42, plan=load_plan(path))
        items = self.record()["work_items"]
        self.assertEqual([i["issue"] for i in items], [121, 122])
        self.assertEqual(items[0]["state"], "planned")

    def test_replaying_a_plan_does_not_duplicate_items(self):
        path = self.plan_file("work_items:\n  - issue: 121\n    title: t\n    repo: app\n")
        advance(self.dir, 42, plan=load_plan(path))
        advance(self.dir, 42, plan=load_plan(path))
        self.assertEqual(len(self.record()["work_items"]), 1)

    def test_a_bare_list_is_accepted(self):
        path = self.plan_file("- issue: 121\n  title: t\n  repo: app\n")
        self.assertEqual(load_plan(path)[0]["issue"], 121)

    def test_an_entry_without_an_issue_is_rejected(self):
        path = self.plan_file("work_items:\n  - title: t\n    repo: app\n")
        with self.assertRaises(LedgerError):
            advance(self.dir, 42, plan=load_plan(path))


class TestRoundTrip(LedgerCase):
    def test_a_record_reread_and_redumped_is_byte_identical(self):
        open_record(self.dir, 42, spec_pr=118, baseline=38, labels=["spec:feature"])
        advance(self.dir, 42, state="implementing")
        advance(self.dir, 42, issue=121, title="Session expiry", repo="app",
                satisfies=["features/auth.md#session-expiry/idle-session-expires"])
        advance(self.dir, 42, issue=121, item_state="merged", pr=124,
                attempts=2, tokens=412000, usd=3.10, executor="claude-actions")
        advance(self.dir, 42, state="shipped", release="r58")
        path = record_path(self.dir, 42)
        self.assertEqual(path.read_text(), dump(load(path)))

    def test_the_record_is_valid_yaml_with_the_expected_values(self):
        open_record(self.dir, 42, spec_pr=118)
        advance(self.dir, 42, issue=121, title="t", repo="app", attempts=1, usd=0.25)
        data = yaml.safe_load(record_path(self.dir, 42).read_text())
        self.assertEqual(data["work_items"][0]["cost"]["usd"], 0.25)


class TestCommandLine(LedgerCase):
    def test_open_then_advance_round_trips_through_the_cli(self):
        base = ["--ledger-dir", str(self.dir)]
        self.assertEqual(run_cli(["ledger", "open", "--version", "42", "--spec-pr", "118"] + base)[0], 0)
        self.assertEqual(run_cli(["ledger", "advance", "--version", "spec-v42",
                                  "--item", "121", "--title", "t", "--repo", "app",
                                  "--item-state", "merged", "--pr", "124",
                                  "--attempts", "2", "--tokens", "412000", "--usd", "3.10",
                                  "--executor", "claude-actions"] + base)[0], 0)
        item = self.record()["work_items"][0]
        self.assertEqual(item["pr"], 124)
        self.assertEqual(item["cost"]["tokens"], 412000)

    def test_open_is_reported_as_already_open_on_replay(self):
        base = ["--ledger-dir", str(self.dir)]
        run_cli(["ledger", "open", "--version", "42"] + base)
        _, output = run_cli(["ledger", "open", "--version", "42"] + base)
        self.assertIn("already open", output)

    def test_failures_exit_non_zero(self):
        code, output = run_cli(["ledger", "advance", "--version", "99", "--state", "shipped",
                                "--ledger-dir", str(self.dir)])
        self.assertEqual(code, 2)
        self.assertIn("open it first", output)


if __name__ == "__main__":
    unittest.main()
