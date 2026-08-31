"""``vellum verify exit-duty``: source moved, and did a note move with it.

The scenario is ``@id:exit-duty-required`` in
``spec/features/memory-and-briefings.md``: an implementation PR that touched
``src/billing/`` with no diff under ``.vellum/memory/areas/`` is incomplete.
"""

import tempfile
import unittest
from pathlib import Path

from support import branch, commit_files, git, make_git_product_repo, run_cli
from vellum.exitduty import AREAS_TREE, ExitDutyError, check


class ExitDutyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.product = make_git_product_repo(self.root / "app")
        self.base = git(self.product, "rev-parse", "HEAD").strip()

    def implementation(self, files, message="implementation"):
        branch(self.product, "implementation")
        return commit_files(self.product, files, message)


class TestTheScenario(ExitDutyCase):
    def test_source_changed_and_no_area_note_did(self):
        # @id:exit-duty-required, as written: the branch touches src/billing/
        # and nothing under .vellum/memory/areas/.
        self.implementation({"src/billing/invoices.py": "def issue():\n    return None\n"})
        result = check(self.product, self.base, "HEAD")
        self.assertTrue(result.owed)
        self.assertEqual(result.source_changed, ["src/billing/invoices.py"])
        self.assertEqual(result.memory_changed, [])

    def test_the_cli_exits_one_for_it(self):
        self.implementation({"src/billing/invoices.py": "def issue():\n    return None\n"})
        code, out = run_cli(
            ["verify", "exit-duty", str(self.product), "--base", self.base, "--head", "HEAD"]
        )
        self.assertEqual(code, 1)
        self.assertIn(AREAS_TREE, out)

    def test_the_same_diff_with_its_memory_update_passes(self):
        self.implementation({
            "src/billing/invoices.py": "def issue():\n    return None\n",
            ".vellum/memory/areas/billing.md": "# Billing\n\nInvoices are issued here.\n",
        })
        code, _ = run_cli(
            ["verify", "exit-duty", str(self.product), "--base", self.base, "--head", "HEAD"]
        )
        self.assertEqual(code, 0)


class TestWhenNoDutyIsOwed(ExitDutyCase):
    def test_a_diff_touching_no_source_owes_nothing(self):
        self.implementation({"README.md": "# product\n\nprose only\n"})
        result = check(self.product, self.base, "HEAD")
        self.assertFalse(result.owed)
        self.assertIn("no source changed", result.report())

    def test_an_empty_diff_owes_nothing(self):
        branch(self.product, "implementation")
        self.assertFalse(check(self.product, self.base, "HEAD").owed)

    def test_a_memory_only_diff_owes_nothing(self):
        self.implementation({".vellum/memory/areas/app.md": "# App\n\nrewritten\n"})
        self.assertFalse(check(self.product, self.base, "HEAD").owed)

    def test_deleting_an_area_note_counts_as_a_memory_diff(self):
        # A note removed because its area was removed is still the memory diff
        # riding in the PR. `--name-only` lists a deletion, so this needs no
        # special case; the test is here so a later "only count added or
        # modified notes" cannot land unnoticed.
        self.implementation({
            "src/app.py": "def main():\n    return 1\n",
            ".vellum/memory/areas/app.md": None,
        })
        self.assertFalse(check(self.product, self.base, "HEAD").owed)


class TestWhichTreesCount(ExitDutyCase):
    def test_a_note_is_memory_first_and_never_also_source(self):
        # An installation whose source tree is `.` — or one that lists
        # `.vellum/memory` in product.trees, as this very repo does — must not
        # be able to let the memory diff satisfy itself.
        self.implementation({".vellum/memory/areas/app.md": "# App\n\nrewritten\n"})
        result = check(self.product, self.base, "HEAD", source_trees=["."])
        self.assertEqual(result.source_changed, [])
        self.assertEqual(result.memory_changed, [".vellum/memory/areas/app.md"])
        self.assertFalse(result.owed)

    def test_a_repo_laid_out_differently_names_its_own_source_tree(self):
        self.implementation({"lib/thing.py": "x = 1\n"})
        self.assertFalse(check(self.product, self.base, "HEAD").owed)
        self.assertTrue(check(self.product, self.base, "HEAD", source_trees=["lib"]).owed)

    def test_the_cli_passes_repeated_src_flags_through(self):
        self.implementation({"lib/thing.py": "x = 1\n"})
        code, out = run_cli(
            ["verify", "exit-duty", str(self.product), "--base", self.base,
             "--head", "HEAD", "--src", "lib", "--src", "src"]
        )
        self.assertEqual(code, 1)
        self.assertIn("lib/thing.py", out)


class TestWhatItDoesNotClaim(ExitDutyCase):
    def test_any_note_satisfies_the_duty_and_the_report_says_so(self):
        # The editorial half is deliberately not enforced: an area's name is not
        # derivable from a source path (this product's own src/vellum/ is
        # documented by areas/cli.md), so a guess would fault correct PRs and
        # pass incorrect ones. Whether the note matches is the verifier's read.
        self.implementation({
            "src/billing/invoices.py": "def issue():\n    return None\n",
            ".vellum/memory/areas/app.md": "# App\n\nan unrelated note\n",
        })
        result = check(self.product, self.base, "HEAD")
        self.assertFalse(result.owed)
        self.assertIn("not that it is the right one", result.report())


class TestTheComparison(ExitDutyCase):
    def test_a_note_landing_on_base_does_not_discharge_the_branch_s_duty(self):
        # The merge-base half, in the direction that matters here: a memory
        # commit somebody else landed on main must not read as this branch's
        # exit duty. Diffing the refs directly would show it.
        self.implementation({"src/billing/invoices.py": "def issue():\n    return None\n"})
        git(self.product, "checkout", "-q", "main")
        commit_files(self.product, {".vellum/memory/areas/other.md": "# Other\n"},
                     "memory: somebody else's note")
        git(self.product, "checkout", "-q", "implementation")
        result = check(self.product, "main", "HEAD")
        self.assertEqual(result.basis, "merge-base")
        self.assertEqual(result.memory_changed, [])
        self.assertTrue(result.owed)

    def test_an_unresolvable_ref_cannot_be_answered(self):
        with self.assertRaises(ExitDutyError):
            check(self.product, "no-such-ref", "HEAD")

    def test_the_cli_exits_two_for_it(self):
        code, _ = run_cli(
            ["verify", "exit-duty", str(self.product), "--base", "no-such-ref",
             "--head", "HEAD"]
        )
        self.assertEqual(code, 2)

    def test_a_checkout_that_is_not_a_directory_is_refused(self):
        with self.assertRaises(ExitDutyError):
            check(self.root / "nowhere", "HEAD", "HEAD")


if __name__ == "__main__":
    unittest.main()
