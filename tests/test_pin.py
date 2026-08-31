"""``vellum pin advance``: what it accepts as a version, and what it leaves alone.

Two subjects. First, validation — a pin naming a commit that is not a spec
version is the failure this command exists to prevent. Second, the rewrite: the
pin file is mostly load-bearing comments, so the test that matters is the one
asserting what did *not* change.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from support import (
    commit_area,
    commit_elsewhere,
    make_intent_repo,
    make_product_repo,
    run_cli,
    write_record,
)
from vellum.pin import PinError, advance, product_path
from vellum.specfile import SpecTreeError

BLOCK = """Feature: Auth
  @id:auth-one
  Scenario: One
    Given a user
    When they log in
    Then they are in
"""

CHANGED = BLOCK.replace("When they log in", "When they log in twice")


class PinCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.intent = make_intent_repo(root / "intent")
        self.ledger = self.intent / "ledger"
        self.product = make_product_repo(root / "product")
        self.pin_file = product_path(self.product)

    def pin(self) -> dict:
        return yaml.safe_load(self.pin_file.read_text(encoding="utf-8"))["pin"]


class TestValidation(PinCase):
    def test_a_sha_with_a_ledger_record_is_a_version(self):
        sha = commit_area(self.intent, BLOCK)
        write_record(self.ledger, sha, name="spec-v1")

        result = advance(self.product, sha, self.intent)

        self.assertEqual(result.now, sha)
        self.assertIn("ledger record", result.evidence)

    def test_a_spec_commit_with_no_record_yet_is_still_a_version(self):
        # Under paired landing the pin advances to a commit whose ledger record
        # may still be a minute away. Refusing that would refuse the exact case
        # this command exists to serve.
        sha = commit_area(self.intent, BLOCK)

        result = advance(self.product, sha, self.intent)

        self.assertEqual(result.now, sha)
        self.assertIn("ancestry", result.evidence)

    def test_a_commit_that_does_not_touch_the_spec_tree_is_refused(self):
        commit_area(self.intent, BLOCK)
        stray = commit_elsewhere(self.intent, "ledger: open spec-v1")

        with self.assertRaises(PinError) as caught:
            advance(self.product, stray, self.intent)

        self.assertIn("not a spec version", str(caught.exception))

    def test_a_sha_the_intent_repo_has_never_seen_is_refused(self):
        commit_area(self.intent, BLOCK)
        with self.assertRaises(PinError):
            advance(self.product, "b" * 40, self.intent)

    def test_a_commit_on_another_line_is_refused(self):
        # A pin names a version on the line the checkout is on. A spec commit
        # on an abandoned branch is not one.
        first = commit_area(self.intent, BLOCK)
        subprocess.run(
            ["git", "-C", str(self.intent), "checkout", "-q", "-b", "side"],
            check=True, capture_output=True,
        )
        stray = commit_area(self.intent, CHANGED)
        subprocess.run(
            ["git", "-C", str(self.intent), "checkout", "-q", "main"],
            check=True, capture_output=True,
        )

        with self.assertRaises(PinError) as caught:
            advance(self.product, stray, self.intent)
        self.assertIn("not an ancestor", str(caught.exception))
        # The one on main still advances, so the refusal is about the line and
        # not about the walk being broken.
        self.assertEqual(advance(self.product, first, self.intent).now, first)

    def test_a_shallow_intent_checkout_is_refused_when_no_record_vouches(self):
        commit_area(self.intent, BLOCK)
        sha = commit_area(self.intent, CHANGED)
        clone = Path(self.tmp.name) / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{self.intent}", str(clone)],
            check=True, capture_output=True,
        )

        with self.assertRaises(PinError) as caught:
            advance(self.product, sha, clone)
        self.assertIn("shallow", str(caught.exception))

    def test_a_record_vouches_even_in_a_shallow_checkout(self):
        # The ledger and the ancestry are two sufficient answers, not one
        # answer checked twice: a record is automation's own statement that the
        # version exists, and it does not need the history to be readable.
        commit_area(self.intent, BLOCK)
        sha = commit_area(self.intent, CHANGED)
        clone = Path(self.tmp.name) / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{self.intent}", str(clone)],
            check=True, capture_output=True,
        )
        write_record(clone / "ledger", sha, name="spec-v2")

        self.assertEqual(advance(self.product, sha, clone).now, sha)

    def test_an_abbreviated_sha_is_expanded_to_the_full_forty(self):
        # A pin is read by CI to fetch a tree; an abbreviation there is a
        # collision waiting for the repo to grow.
        sha = commit_area(self.intent, BLOCK)
        self.assertEqual(advance(self.product, sha[:8], self.intent).now, sha)
        self.assertEqual(self.pin()["commit"], sha)

    def test_a_refusal_leaves_the_file_exactly_as_it_was(self):
        commit_area(self.intent, BLOCK)
        stray = commit_elsewhere(self.intent, "ledger: open spec-v1")
        before = self.pin_file.read_text(encoding="utf-8")

        with self.assertRaises(PinError):
            advance(self.product, stray, self.intent)

        self.assertEqual(self.pin_file.read_text(encoding="utf-8"), before)


class TestTheRewrite(PinCase):
    def setUp(self):
        super().setUp()
        self.sha = commit_area(self.intent, BLOCK)

    def test_it_replaces_pin_commit(self):
        advance(self.product, self.sha, self.intent)
        self.assertEqual(self.pin()["commit"], self.sha)

    def test_every_comment_survives(self):
        # The comments in this file are documentation — why the submodule is
        # gone, why `name` is decoration. A safe_load/safe_dump round-trip
        # would delete all of them and reflow what it kept.
        before = self.pin_file.read_text(encoding="utf-8")
        advance(self.product, self.sha, self.intent)
        after = self.pin_file.read_text(encoding="utf-8")

        comments = [ln for ln in before.splitlines() if ln.lstrip().startswith("#")]
        self.assertTrue(comments)
        for line in comments:
            self.assertIn(line, after)

    def test_only_the_pin_lines_differ(self):
        before = self.pin_file.read_text(encoding="utf-8").splitlines()
        advance(self.product, self.sha, self.intent)
        after = self.pin_file.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(before), len(after))
        differing = [b for b, a in zip(before, after) if b != a]
        self.assertEqual([ln.strip().split(":")[0] for ln in differing], ["commit"])

    def test_every_other_field_is_preserved_exactly(self):
        before = yaml.safe_load(self.pin_file.read_text(encoding="utf-8"))
        advance(self.product, self.sha, self.intent)
        after = yaml.safe_load(self.pin_file.read_text(encoding="utf-8"))

        self.assertEqual(
            {k: v for k, v in before.items() if k != "pin"},
            {k: v for k, v in after.items() if k != "pin"},
        )

    def test_the_indent_of_the_file_is_kept(self):
        advance(self.product, self.sha, self.intent)
        line = next(
            ln for ln in self.pin_file.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("commit:")
        )
        self.assertEqual(line, f"  commit: {self.sha}")

    def test_a_trailing_comment_on_the_pin_line_survives(self):
        # The comments in this file are the documentation. Replacing a value
        # while silently dropping the comment beside it is the loss the
        # line-level edit exists to avoid, so it is asserted rather than
        # assumed.
        self.pin_file.write_text(
            "pin:\n  commit: " + "c" * 40 + "  # the pin of record\n"
            "  name: null  # decoration\n",
            encoding="utf-8",
        )
        advance(self.product, self.sha, self.intent)
        text = self.pin_file.read_text(encoding="utf-8")

        self.assertIn(f"commit: {self.sha}  # the pin of record", text)
        self.assertIn("name: null  # decoration", text)

    def test_advancing_to_the_pin_already_set_rewrites_nothing(self):
        advance(self.product, self.sha, self.intent)
        before = self.pin_file.read_text(encoding="utf-8")

        result = advance(self.product, self.sha, self.intent)

        self.assertFalse(result.changed)
        self.assertEqual(self.pin_file.read_text(encoding="utf-8"), before)


class TestPinName(PinCase):
    """Decoration that follows the commit rather than being left behind."""

    def test_the_records_name_is_written_beside_the_commit(self):
        sha = commit_area(self.intent, BLOCK)
        write_record(self.ledger, sha, name="spec-v1")

        advance(self.product, sha, self.intent)

        self.assertEqual(self.pin()["name"], "spec-v1")

    def test_a_stale_name_is_cleared_when_the_new_version_has_none(self):
        # A `name` reading spec-v1 beside a commit that is spec-v2 is
        # decoration that has become a lie, and the reader it misleads is
        # exactly the reader it was for.
        first = commit_area(self.intent, BLOCK)
        write_record(self.ledger, first, name="spec-v1")
        advance(self.product, first, self.intent)
        self.assertEqual(self.pin()["name"], "spec-v1")

        second = commit_area(self.intent, CHANGED)
        advance(self.product, second, self.intent)

        self.assertIsNone(self.pin()["name"])

    def test_a_pin_file_without_a_name_key_is_not_given_one(self):
        # Only lines already in the pin block are rewritten; nothing is added.
        self.pin_file.write_text(
            "intent:\n  repo: a/b\n\npin:\n  commit: " + "c" * 40 + "\n",
            encoding="utf-8",
        )
        sha = commit_area(self.intent, BLOCK)
        write_record(self.ledger, sha, name="spec-v1")

        advance(self.product, sha, self.intent)

        self.assertEqual(self.pin(), {"commit": sha})


class TestMalformedPinFiles(PinCase):
    def setUp(self):
        super().setUp()
        self.sha = commit_area(self.intent, BLOCK)

    def test_a_file_with_no_pin_mapping_is_refused(self):
        self.pin_file.write_text("intent:\n  repo: a/b\n", encoding="utf-8")
        with self.assertRaises(SpecTreeError):
            advance(self.product, self.sha, self.intent)

    def test_a_pin_without_a_commit_is_refused(self):
        self.pin_file.write_text("pin:\n  name: spec-v1\n", encoding="utf-8")
        with self.assertRaises(SpecTreeError):
            advance(self.product, self.sha, self.intent)

    def test_a_missing_file_is_refused(self):
        self.pin_file.unlink()
        with self.assertRaises(PinError):
            advance(self.product, self.sha, self.intent)

    def test_a_second_top_level_pin_block_is_refused_rather_than_guessed(self):
        # Two `pin:` blocks is not a file to repair by picking one.
        self.pin_file.write_text(
            "pin:\n  commit: " + "c" * 40 + "\npin:\n  commit: " + "d" * 40 + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(PinError):
            advance(self.product, self.sha, self.intent)


class TestTheCommandLine(PinCase):
    def test_it_reports_what_it_did(self):
        sha = commit_area(self.intent, BLOCK)
        code, out = run_cli(
            ["pin", "advance", str(self.product), "--to", sha, "--intent", str(self.intent)]
        )
        self.assertEqual(code, 0)
        self.assertIn(sha, out)
        self.assertIn("pin.commit", out)

    def test_the_intent_env_var_stands_in_for_the_flag(self):
        import os
        from vellum.config import INTENT_ENV

        sha = commit_area(self.intent, BLOCK)
        os.environ[INTENT_ENV] = str(self.intent)
        self.addCleanup(os.environ.pop, INTENT_ENV, None)

        code, _ = run_cli(["pin", "advance", str(self.product), "--to", sha])

        self.assertEqual(code, 0)
        self.assertEqual(self.pin()["commit"], sha)

    def test_no_intent_checkout_at_all_exits_two(self):
        import os
        from vellum.config import INTENT_ENV

        os.environ.pop(INTENT_ENV, None)
        sha = commit_area(self.intent, BLOCK)
        code, out = run_cli(["pin", "advance", str(self.product), "--to", sha])
        self.assertEqual(code, 2)
        self.assertIn(INTENT_ENV, out)

    def test_a_name_rather_than_a_sha_exits_two(self):
        # Versions stopped being integers when they became commits; `spec-v1`
        # is a bad command line, not a bad repository.
        code, out = run_cli(
            ["pin", "advance", str(self.product), "--to", "spec-v1", "--intent", str(self.intent)]
        )
        self.assertEqual(code, 2)
        self.assertIn("not a spec version", out)

    def test_a_commit_that_is_not_a_version_exits_one(self):
        commit_area(self.intent, BLOCK)
        stray = commit_elsewhere(self.intent, "ledger: open spec-v1")
        code, _ = run_cli(
            ["pin", "advance", str(self.product), "--to", stray, "--intent", str(self.intent)]
        )
        self.assertEqual(code, 1)

    def test_an_intent_path_that_is_not_an_intent_checkout_exits_two(self):
        sha = commit_area(self.intent, BLOCK)
        code, out = run_cli(
            ["pin", "advance", str(self.product), "--to", sha, "--intent", str(self.product)]
        )
        self.assertEqual(code, 2)
        self.assertIn("intent", out)


if __name__ == "__main__":
    unittest.main()
