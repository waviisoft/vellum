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
    find_record,
    load,
    load_plan,
    open_record,
    parse_version,
    record_path,
)

#: A spec version is a commit (spec/decisions/2026-08-28-versions-are-commits.md),
#: so these are shas. They are real commits of the intent repo only so that they
#: read as shas rather than as `deadbeef` filler; nothing here resolves them.
VERSION = "9c8b70a71089fc8fa6f585ff5f287bb740eff141"
BASELINE = "1ce87cb5dd140bf2d9b125f9124d256fc4a19303"
#: A version no record has been opened for.
UNOPENED = "0123456789abcdef0123456789abcdef01234567"
#: Invented, unlike the two above, and for one purpose: it shares VERSION's
#: first seven characters — git's own abbreviation floor — so that
#: `VERSION[:7]` names both records and neither more than the other.
SIBLING = "9c8b70a9e3ff41c0d5b2a6748e0c1d93af5528b6"


def run_cli(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return main(argv), buf.getvalue()


class TestVersionParsing(unittest.TestCase):
    def test_accepts_a_commit_sha_full_or_abbreviated(self):
        self.assertEqual(parse_version(VERSION), VERSION)
        self.assertEqual(parse_version(VERSION[:7]), VERSION[:7])
        self.assertEqual(parse_version("  9C8B70A  "), "9c8b70a")

    def test_rejects_the_integer_forms_versions_used_to_take(self):
        # Versions are commits now; an integer or a `spec-vN` name is a
        # decorative label, and accepting one as a key would quietly resurrect
        # the second version system the decision removed.
        for bad in ("42", "spec-v42", "v1.2.3", "latest", "", "9c8b70", "zzzzzzz"):
            with self.assertRaises(LedgerError):
                parse_version(bad)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "ledger"
        self.addCleanup(self.tmp.cleanup)

    def record(self, sha=VERSION):
        return load(record_path(self.dir, sha))


class TestOpen(LedgerCase):
    def test_creates_a_record_in_state_approved(self):
        path, created = open_record(
            self.dir, VERSION, spec_pr=118, baseline=BASELINE,
            labels=["spec:feature"], name="spec-v42",
        )
        self.assertTrue(created)
        record = load(path)
        self.assertEqual(record["spec_version"], VERSION)
        self.assertEqual(record["name"], "spec-v42")
        self.assertEqual(record["state"], "approved")
        self.assertEqual(record["spec_pr"], 118)
        self.assertEqual(record["baseline"], BASELINE)
        self.assertEqual(record["labels"], ["spec:feature"])

    def test_the_record_is_keyed_by_the_sha_and_a_name_is_optional(self):
        path, _ = open_record(self.dir, VERSION)
        self.assertEqual(path.name, f"{VERSION}.yaml")
        self.assertIsNone(load(path)["name"])

    def test_record_carries_every_field_the_spec_names(self):
        path, _ = open_record(self.dir, VERSION)
        self.assertEqual(list(load(path)), list(RECORD_KEYS))

    def test_reserved_fields_are_present_with_defaults(self):
        # Deferred features keep their shape reserved: lines (D13), locks (G2).
        record = load(open_record(self.dir, VERSION)[0])
        self.assertEqual(record["line"], "main")
        self.assertEqual(record["locks"], [])

    def test_opening_twice_leaves_the_first_record_untouched(self):
        # The reconciler may replay an approval (decision D11).
        path, _ = open_record(self.dir, VERSION, spec_pr=118)
        advance(self.dir, VERSION, state="implementing")
        before = path.read_text()
        _, created = open_record(self.dir, VERSION, spec_pr=999)
        self.assertFalse(created)
        self.assertEqual(path.read_text(), before)

    def test_approval_time_is_recorded(self):
        record = load(open_record(self.dir, VERSION, approved="2026-08-27T14:02:00Z")[0])
        self.assertEqual(record["approved"], "2026-08-27T14:02:00Z")


class TestAdvance(LedgerCase):
    def setUp(self):
        super().setUp()
        open_record(self.dir, VERSION, spec_pr=118, baseline=BASELINE, labels=["spec:feature"])

    def test_record_state_advances(self):
        advance(self.dir, VERSION, state="implementing")
        self.assertEqual(self.record()["state"], "implementing")

    def test_unknown_record_state_is_rejected(self):
        with self.assertRaises(LedgerError):
            advance(self.dir, VERSION, state="finished")

    def test_unknown_item_state_is_rejected(self):
        advance(self.dir, VERSION, issue=121, title="t", repo="app")
        with self.assertRaises(LedgerError):
            advance(self.dir, VERSION, issue=121, item_state="done")

    def test_advancing_a_missing_record_fails(self):
        with self.assertRaises(LedgerError):
            advance(self.dir, UNOPENED, state="shipped")

    def test_work_item_is_added_then_updated_in_place(self):
        advance(self.dir, VERSION, issue=121, title="Session expiry", repo="app",
                satisfies=["scenario:auth-idle-session-expires"])
        advance(self.dir, VERSION, issue=121, item_state="merged", pr=124)
        items = self.record()["work_items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "merged")
        self.assertEqual(items[0]["pr"], 124)
        self.assertEqual(items[0]["satisfies"], ["scenario:auth-idle-session-expires"])

    def test_work_item_carries_every_field_the_spec_names(self):
        advance(self.dir, VERSION, issue=121, title="t", repo="app")
        self.assertEqual(list(self.record()["work_items"][0]), list(ITEM_KEYS))

    def test_adding_an_unknown_item_without_title_and_repo_fails(self):
        with self.assertRaises(LedgerError):
            advance(self.dir, VERSION, issue=404, item_state="merged")

    def test_work_item_options_require_an_item(self):
        with self.assertRaises(LedgerError):
            advance(self.dir, VERSION, pr=124)

    def test_briefing_is_stored_on_the_work_item(self):
        # "what the agent knew is a factual, reproducible question"
        advance(self.dir, VERSION, issue=121, title="t", repo="app", briefing="the briefing text")
        self.assertEqual(self.record()["work_items"][0]["briefing"], "the briefing text")

    def test_release_is_recorded(self):
        advance(self.dir, VERSION, release="r58")
        self.assertEqual(self.record()["release"], "r58")


class TestCostAccounting(LedgerCase):
    def setUp(self):
        super().setUp()
        open_record(self.dir, VERSION)
        advance(self.dir, VERSION, issue=121, title="t", repo="app")

    def cost(self):
        return self.record()["work_items"][0]["cost"]

    def test_a_new_item_starts_at_zero(self):
        self.assertEqual(self.cost(), {"attempts": 0, "tokens": 0, "usd": 0.0, "executor": None})

    def test_every_invocation_accumulates(self):
        # Each agent invocation records cost into its work item's entry.
        advance(self.dir, VERSION, issue=121, attempts=1, tokens=210000, usd=1.60, executor="claude-actions")
        advance(self.dir, VERSION, issue=121, attempts=1, tokens=202000, usd=1.50, executor="claude-actions")
        self.assertEqual(
            self.cost(),
            {"attempts": 2, "tokens": 412000, "usd": 3.10, "executor": "claude-actions"},
        )

    def test_executor_records_the_most_recent_one(self):
        advance(self.dir, VERSION, issue=121, executor="claude-local")
        advance(self.dir, VERSION, issue=121, executor="claude-actions")
        self.assertEqual(self.cost()["executor"], "claude-actions")


class TestWorkPlan(LedgerCase):
    def setUp(self):
        super().setUp()
        open_record(self.dir, VERSION)

    def plan_file(self, text):
        path = Path(self.tmp.name) / "workplan.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_plan_is_committed_to_the_record(self):
        path = self.plan_file(
            "work_items:\n"
            "  - issue: 121\n    title: Session expiry\n    repo: app\n"
            "    satisfies: [scenario:auth-idle-session-expires]\n"
            "  - issue: 122\n    title: Sign-out\n    repo: app\n"
        )
        advance(self.dir, VERSION, plan=load_plan(path))
        items = self.record()["work_items"]
        self.assertEqual([i["issue"] for i in items], [121, 122])
        self.assertEqual(items[0]["state"], "planned")

    def test_replaying_a_plan_does_not_duplicate_items(self):
        path = self.plan_file("work_items:\n  - issue: 121\n    title: t\n    repo: app\n")
        advance(self.dir, VERSION, plan=load_plan(path))
        advance(self.dir, VERSION, plan=load_plan(path))
        self.assertEqual(len(self.record()["work_items"]), 1)

    def test_a_bare_list_is_accepted(self):
        path = self.plan_file("- issue: 121\n  title: t\n  repo: app\n")
        self.assertEqual(load_plan(path)[0]["issue"], 121)

    def test_an_entry_without_an_issue_is_rejected(self):
        path = self.plan_file("work_items:\n  - title: t\n    repo: app\n")
        with self.assertRaises(LedgerError):
            advance(self.dir, VERSION, plan=load_plan(path))


class TestRoundTrip(LedgerCase):
    def test_a_record_reread_and_redumped_is_byte_identical(self):
        open_record(self.dir, VERSION, spec_pr=118, baseline=BASELINE, labels=["spec:feature"])
        advance(self.dir, VERSION, state="implementing")
        advance(self.dir, VERSION, issue=121, title="Session expiry", repo="app",
                satisfies=["scenario:auth-idle-session-expires"])
        advance(self.dir, VERSION, issue=121, item_state="merged", pr=124,
                attempts=2, tokens=412000, usd=3.10, executor="claude-actions")
        advance(self.dir, VERSION, state="shipped", release="r58")
        path = record_path(self.dir, VERSION)
        self.assertEqual(path.read_text(), dump(load(path)))

    def test_the_record_is_valid_yaml_with_the_expected_values(self):
        open_record(self.dir, VERSION, spec_pr=118)
        advance(self.dir, VERSION, issue=121, title="t", repo="app", attempts=1, usd=0.25)
        data = yaml.safe_load(record_path(self.dir, VERSION).read_text())
        self.assertEqual(data["work_items"][0]["cost"]["usd"], 0.25)


class TestCommandLine(LedgerCase):
    def test_open_then_advance_round_trips_through_the_cli(self):
        base = ["--ledger-dir", str(self.dir)]
        self.assertEqual(run_cli(["ledger", "open", "--version", VERSION, "--spec-pr", "118"] + base)[0], 0)
        self.assertEqual(run_cli(["ledger", "advance", "--version", VERSION[:8],
                                  "--item", "121", "--title", "t", "--repo", "app",
                                  "--item-state", "merged", "--pr", "124",
                                  "--attempts", "2", "--tokens", "412000", "--usd", "3.10",
                                  "--executor", "claude-actions"] + base)[0], 0)
        item = self.record()["work_items"][0]
        self.assertEqual(item["pr"], 124)
        self.assertEqual(item["cost"]["tokens"], 412000)

    def test_open_is_reported_as_already_open_on_replay(self):
        base = ["--ledger-dir", str(self.dir)]
        run_cli(["ledger", "open", "--version", VERSION] + base)
        _, output = run_cli(["ledger", "open", "--version", VERSION] + base)
        self.assertIn("already open", output)

    def test_failures_exit_non_zero(self):
        code, output = run_cli(["ledger", "advance", "--version", UNOPENED, "--state", "shipped",
                                "--ledger-dir", str(self.dir)])
        self.assertEqual(code, 2)
        self.assertIn("open it first", output)


class TestShaKeying(LedgerCase):
    """The key is the sha; the filename and the name are not."""

    def test_a_record_is_found_by_an_abbreviated_sha(self):
        open_record(self.dir, VERSION)
        self.assertEqual(find_record(self.dir, VERSION[:8]), record_path(self.dir, VERSION))

    def test_a_record_opened_abbreviated_is_found_by_the_full_sha(self):
        open_record(self.dir, VERSION[:10])
        self.assertEqual(find_record(self.dir, VERSION), record_path(self.dir, VERSION[:10]))

    def test_a_renamed_record_is_still_found(self):
        # The filename is where a record is written; `spec_version` is what
        # identifies it. A ledger carried over from the integer era can be
        # renamed without the CLI losing sight of it.
        path, _ = open_record(self.dir, VERSION)
        moved = path.with_name("spec-v42.yaml")
        path.rename(moved)
        self.assertEqual(find_record(self.dir, VERSION), moved)
        advance(self.dir, VERSION, state="shipped")
        self.assertEqual(load(moved)["state"], "shipped")

    def test_a_record_keyed_by_a_name_rather_than_a_sha_is_not_matched(self):
        # A legacy record whose spec_version is `spec-v6` is not a sha-keyed
        # record. Reaching it by name would be reading a decoration to decide
        # something, which is the thing that stopped being allowed.
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "spec-v6.yaml").write_text("spec_version: spec-v6\nstate: shipped\n")
        self.assertIsNone(find_record(self.dir, VERSION))

    def test_a_different_version_does_not_collide(self):
        open_record(self.dir, VERSION)
        self.assertIsNone(find_record(self.dir, UNOPENED))

    def test_an_ambiguous_abbreviation_is_refused_rather_than_guessed(self):
        # Two records whose shas share the seven characters the caller typed.
        # Returning the first in filename order would answer a question that
        # has no single answer, and the caller would never learn that the
        # record they reached was picked by `sorted()`.
        open_record(self.dir, VERSION)
        open_record(self.dir, SIBLING)
        with self.assertRaises(LedgerError) as caught:
            find_record(self.dir, VERSION[:7])
        message = str(caught.exception)
        # The candidates are named, because "be more specific" is not
        # actionable unless you can see what you have to be specific against.
        self.assertIn(VERSION, message)
        self.assertIn(SIBLING, message)

    def test_a_full_sha_still_resolves_when_a_sibling_shares_its_prefix(self):
        # Ambiguity is a property of the abbreviation, not of the ledger: a sha
        # that names its record exactly is unaffected by what sits beside it.
        open_record(self.dir, VERSION)
        open_record(self.dir, SIBLING)
        self.assertEqual(find_record(self.dir, VERSION), record_path(self.dir, VERSION))
        self.assertEqual(find_record(self.dir, SIBLING), record_path(self.dir, SIBLING))

    def test_an_ambiguous_abbreviation_advances_neither_record(self):
        # The CLI surfaces it as a failure, and — the point of raising rather
        # than returning — both records are left exactly as they were.
        open_record(self.dir, VERSION)
        open_record(self.dir, SIBLING)
        code, output = run_cli(["ledger", "advance", "--version", VERSION[:7],
                                "--state", "shipped", "--ledger-dir", str(self.dir)])
        self.assertEqual(code, 2)
        self.assertIn("ambiguous", output)
        self.assertEqual(load(record_path(self.dir, VERSION))["state"], "approved")
        self.assertEqual(load(record_path(self.dir, SIBLING))["state"], "approved")

    def test_the_replay_guard_is_that_the_record_exists(self):
        # The whole idempotence story the minting workflow now relies on: no
        # version arithmetic, no already-tagged check, just this.
        first, created = open_record(self.dir, VERSION, spec_pr=118)
        advance(self.dir, VERSION, state="implementing")
        again, created_again = open_record(self.dir, VERSION, spec_pr=999)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, again)
        self.assertEqual(load(again)["state"], "implementing")
        self.assertEqual(load(again)["spec_pr"], 118)


if __name__ == "__main__":
    unittest.main()
