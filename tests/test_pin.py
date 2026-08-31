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

        # Every test in this file is checked for leaving os.environ as it
        # found it. `run_cli` calls `main()` in-process, so a test that unsets
        # VELLUM_INTENT_REPO for good silently disarms `test_suite`'s
        # pinned-tree assertions — which is what a bare `os.environ.pop` here
        # did, invisibly, in the CI job that exists to run them.
        import os

        self._environ = dict(os.environ)
        self.addCleanup(self._assert_environ_restored)

    def _assert_environ_restored(self):
        import os

        self.assertEqual(
            dict(os.environ), self._environ,
            "this test changed os.environ and did not put it back; "
            "later test modules read VELLUM_INTENT_REPO at call time",
        )

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
        # Same code as "this is not a pin file": both are "there is no pin
        # here", and a caller should not have to read the message to tell an
        # absent file from a malformed one.
        self.pin_file.unlink()
        with self.assertRaises(SpecTreeError):
            advance(self.product, self.sha, self.intent)
        code, _ = run_cli(
            ["pin", "advance", str(self.product), "--to", self.sha, "--intent", str(self.intent)]
        )
        self.assertEqual(code, 2)

    def test_a_second_top_level_pin_block_is_refused_rather_than_guessed(self):
        # Two `pin:` blocks is not a file to repair by picking one.
        self.pin_file.write_text(
            "pin:\n  commit: " + "c" * 40 + "\npin:\n  commit: " + "d" * 40 + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(PinError):
            advance(self.product, self.sha, self.intent)


class TestTheRewriteCannotReachNestedContent(PinCase):
    """The verification in `advance()` cannot see inside the `pin:` block.

    `drifted` skips `pin` entirely and the key-set check ignores values, so a
    rewrite that edited a line nested under `pin:` passed every check and was
    written. The guard is therefore in `_rewrite` itself: only the block's own
    indent level is a candidate.
    """

    def setUp(self):
        super().setUp()
        self.sha = commit_area(self.intent, BLOCK)

    def test_a_key_inside_a_block_scalar_is_not_rewritten(self):
        self.pin_file.write_text(
            "pin:\n"
            "  commit: " + "c" * 40 + "\n"
            "  name: null\n"
            "  note: |\n"
            "    how this pin is chosen\n"
            "    name: whatever the tag says\n",
            encoding="utf-8",
        )
        write_record(self.ledger, self.sha, name="spec-v9")

        advance(self.product, self.sha, self.intent)

        pin = self.pin()
        self.assertEqual(pin["commit"], self.sha)
        self.assertEqual(pin["name"], "spec-v9")
        self.assertEqual(
            pin["note"], "how this pin is chosen\nname: whatever the tag says\n"
        )

    def test_a_deeper_nested_commit_is_not_rewritten(self):
        self.pin_file.write_text(
            "pin:\n  commit: " + "c" * 40 + "\n  meta:\n    commit: keep-me\n",
            encoding="utf-8",
        )
        advance(self.product, self.sha, self.intent)

        pin = self.pin()
        self.assertEqual(pin["commit"], self.sha)
        self.assertEqual(pin["meta"], {"commit": "keep-me"})

    def test_another_value_under_pin_is_preserved_exactly(self):
        self.pin_file.write_text(
            "pin:\n  commit: " + "c" * 40 + "\n  line: main\n",
            encoding="utf-8",
        )
        advance(self.product, self.sha, self.intent)
        self.assertEqual(self.pin()["line"], "main")


class TestANameFromTheLedgerIsNotTrusted(PinCase):
    """`ledger/` is written by anyone who can land a merge on the intent repo.

    The name goes into a YAML file by string interpolation, and the pin is what
    the product's CI fetches the spec at, so this is a trust boundary rather
    than a formality.
    """

    def setUp(self):
        super().setUp()
        self.sha = commit_area(self.intent, BLOCK)
        self.pin_file.write_text(
            "pin:\n  commit: " + "c" * 40 + "\n  name: null\n  line: main\n",
            encoding="utf-8",
        )

    def write_name(self, name):
        import yaml as _yaml

        from vellum.ledger import record_path

        self.ledger.mkdir(parents=True, exist_ok=True)
        record_path(self.ledger, self.sha).write_text(
            _yaml.safe_dump(
                {"spec_version": self.sha, "name": name, "state": "approved"},
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def test_a_name_that_would_forge_another_key_is_dropped(self):
        self.write_name("spec-v9\n  line: forged")

        advance(self.product, self.sha, self.intent)

        pin = self.pin()
        self.assertIsNone(pin["name"])
        self.assertEqual(pin["line"], "main")
        self.assertEqual(pin["commit"], self.sha)

    def test_a_name_that_is_not_a_spec_vn_is_dropped(self):
        self.write_name("not a version name")
        advance(self.product, self.sha, self.intent)
        self.assertIsNone(self.pin()["name"])

    def test_a_well_formed_name_is_still_written(self):
        self.write_name("spec-v9")
        advance(self.product, self.sha, self.intent)
        self.assertEqual(self.pin()["name"], "spec-v9")

    def test_the_rewrite_refuses_a_multi_line_value_directly(self):
        # The second layer. `_decorative_name` filters upstream; this is the
        # guard at the point of writing, so a future caller reaching `_rewrite`
        # another way cannot forge a key either.
        from vellum.pin import _rewrite

        with self.assertRaises(PinError):
            _rewrite(
                "pin:\n  commit: b\n  name: null\n", "a" * 40, "spec-v9\n  ref: forged"
            )


class TestAnUnreadableLedgerRecord(PinCase):
    def test_it_raises_pin_error_rather_than_a_traceback(self):
        # `find_record` short-circuits on an exact filename hit *without*
        # parsing, so a corrupt `ledger/<sha>.yaml` reaches the read unparsed.
        sha = commit_area(self.intent, BLOCK)
        self.ledger.mkdir(parents=True, exist_ok=True)
        (self.ledger / f"{sha}.yaml").write_text("{ not: valid: yaml", encoding="utf-8")

        with self.assertRaises(PinError):
            advance(self.product, sha, self.intent)

    def test_the_cli_exits_one_for_it(self):
        sha = commit_area(self.intent, BLOCK)
        self.ledger.mkdir(parents=True, exist_ok=True)
        (self.ledger / f"{sha}.yaml").write_text("{ not: valid: yaml", encoding="utf-8")

        code, out = run_cli(
            ["pin", "advance", str(self.product), "--to", sha, "--intent", str(self.intent)]
        )

        self.assertEqual(code, 1)
        self.assertIn("ledger record", out)


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

        # `patch.dict` restores; `addCleanup(os.environ.pop, ...)` *deletes*,
        # which throws away a value the environment already had. Same leak as
        # the one below by a different route, and it only shows up in the shape
        # where the variable was already set — the conformance job.
        import unittest.mock

        sha = commit_area(self.intent, BLOCK)
        with unittest.mock.patch.dict(os.environ, {INTENT_ENV: str(self.intent)}):
            code, _ = run_cli(["pin", "advance", str(self.product), "--to", sha])

        self.assertEqual(code, 0)
        self.assertEqual(self.pin()["commit"], sha)

    def test_no_intent_checkout_at_all_exits_two(self):
        import os
        import unittest.mock
        from vellum.config import INTENT_ENV

        # `patch.dict`, not a bare `pop`. `run_cli` calls `main()` in-process,
        # so unsetting this for good leaks into every module discovered after
        # this one — and `test_suite`'s pinned-tree assertions read it at call
        # time. A bare pop here silently skipped eight of them, in the CI job
        # whose whole purpose is to stop that skip from being a hole.
        with unittest.mock.patch.dict(os.environ):
            os.environ.pop(INTENT_ENV, None)
            self._assert_no_intent_exits_two()

    def _assert_no_intent_exits_two(self):
        from vellum.config import INTENT_ENV

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
