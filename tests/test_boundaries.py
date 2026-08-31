"""``vellum verify boundaries``: what a role may write, and what a diff touched.

The scenario is ``@id:implementer-cannot-touch-harness`` in
``spec/behaviors/write-boundaries.md``: an implementation PR containing changes
under ``harness/`` fails the write-boundary guard.
"""

import tempfile
import unittest
from pathlib import Path

from support import (
    UNSET, branch, commit_files, git, make_git_product_repo, run_cli, write_product,
)
from vellum.boundaries import BoundaryError, check
from vellum.product import ProductFileError, under, write_boundaries


class BoundaryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def repo(self, boundaries=UNSET, files=None):
        product = make_git_product_repo(self.root / "app", boundaries, files)
        self.base = git(product, "rev-parse", "HEAD").strip()
        return product

    def implementation(self, product, files, message="implementation"):
        branch(product, "implementation")
        return commit_files(product, files, message)


class TestTheScenario(BoundaryCase):
    def test_an_implementation_pr_touching_harness_fails(self):
        # @id:implementer-cannot-touch-harness, as written.
        product = self.repo()
        self.implementation(product, {"harness/steps.py": "# an implementer writing where it may not\n"})
        result = check(product, self.base, "HEAD", "implementer")
        self.assertTrue(result.crossed)
        self.assertEqual(result.offending, ["harness/steps.py"])

    def test_the_cli_exits_one_for_it(self):
        # 1, not 2: the guard answered, and the answer is that this diff crosses
        # a boundary. 2 stays "I could not answer", which is what a mistyped
        # --role gets, so a workflow blocking on 1 blocks on boundary crossings
        # and nothing else.
        product = self.repo()
        self.implementation(product, {"harness/steps.py": "# nope\n"})
        code, out = run_cli(
            ["verify", "boundaries", str(product), "--base", self.base,
             "--head", "HEAD", "--role", "implementer"]
        )
        self.assertEqual(code, 1)
        self.assertIn("harness/steps.py", out)

    def test_a_pr_staying_inside_its_trees_passes(self):
        product = self.repo()
        self.implementation(product, {"src/app.py": "def main():\n    return 1\n"})
        code, _ = run_cli(
            ["verify", "boundaries", str(product), "--base", self.base,
             "--head", "HEAD", "--role", "implementer"]
        )
        self.assertEqual(code, 0)


class TestWhatCounts(BoundaryCase):
    def test_every_declared_tree_is_in_bounds(self):
        product = self.repo()
        self.implementation(product, {
            "src/new.py": "x = 1\n",
            "tests/test_new.py": "assert True\n",
            ".vellum/memory/areas/app.md": "# App\n\nchanged\n",
        })
        self.assertFalse(check(product, self.base, "HEAD", "implementer").crossed)

    def test_a_file_named_as_a_boundary_is_in_bounds(self):
        # `write_boundaries.implementer` in the real product file lists
        # README.md, a file rather than a tree. `under` must admit the entry
        # itself, not only paths beneath it.
        product = self.repo(boundaries={"implementer": ["src", "README.md"]})
        self.implementation(product, {"README.md": "# product\n\nchanged\n"})
        self.assertFalse(check(product, self.base, "HEAD", "implementer").crossed)

    def test_a_boundary_does_not_admit_a_sibling_sharing_its_prefix(self):
        # The `startswith` spelling this is written to avoid: `src` admitting
        # `srcs/`. Compared as path components, it does not.
        product = self.repo(boundaries={"implementer": ["src"]})
        self.implementation(product, {"srcs/evil.py": "x = 1\n"})
        result = check(product, self.base, "HEAD", "implementer")
        self.assertEqual(result.offending, ["srcs/evil.py"])

    def test_deleting_a_file_out_of_bounds_is_a_write_to_it(self):
        product = self.repo()
        self.implementation(product, {"harness/steps.py": None})
        self.assertEqual(
            check(product, self.base, "HEAD", "implementer").offending,
            ["harness/steps.py"],
        )

    def test_moving_a_file_out_of_a_protected_tree_still_names_that_tree(self):
        # With rename detection on, git reports a move as one path — the new
        # one — and the write to the tree the file LEFT disappears from the
        # diff a boundary check reads. `changed_paths` passes --no-renames for
        # exactly this, so both halves are visible.
        product = self.repo()
        branch(product, "implementation")
        git(product, "mv", "harness/steps.py", "src/steps.py")
        git(product, "commit", "-qm", "smuggle the harness into src")
        offending = check(product, self.base, "HEAD", "implementer").offending
        self.assertEqual(offending, ["harness/steps.py"])

    def test_a_role_with_wider_trees_is_not_blocked_by_them(self):
        product = self.repo(boundaries={"harness-engineer": ["harness"],
                                        "implementer": ["src"]})
        self.implementation(product, {"harness/steps.py": "# the engineer's own tree\n"})
        self.assertFalse(check(product, self.base, "HEAD", "harness-engineer").crossed)
        self.assertTrue(check(product, self.base, "HEAD", "implementer").crossed)


class TestTheComparison(BoundaryCase):
    def test_a_commit_landing_on_base_is_not_charged_to_the_branch(self):
        # The reason the comparison goes through the merge base. `base` is the
        # ref name, and main moves while a PR is open: diffing the two refs
        # directly reports main's own harness commit, inverted, as a change this
        # branch made — and the guard faults an implementer for somebody else's
        # work.
        product = self.repo()
        self.implementation(product, {"src/app.py": "def main():\n    return 1\n"})
        git(product, "checkout", "-q", "main")
        commit_files(product, {"harness/extra.py": "# the harness engineer's own PR\n"},
                     "harness: land on main")
        git(product, "checkout", "-q", "implementation")
        result = check(product, "main", "HEAD", "implementer")
        self.assertEqual(result.basis, "merge-base")
        self.assertEqual(result.offending, [])
        self.assertEqual(result.changed, ["src/app.py"])

    def test_without_a_merge_base_the_wider_diff_is_read_and_said_so(self):
        # Two unrelated histories, which is also what a truncated CI clone can
        # look like. The direct diff is a superset of the branch's own changes,
        # so the guard can report a path the branch did not touch — never miss
        # one it did — and the report has to say which comparison it made.
        product = self.repo()
        git(product, "checkout", "-q", "--orphan", "unrelated")
        commit_files(product, {"harness/other.py": "# a different history\n"}, "unrelated")
        result = check(product, "main", "HEAD", "implementer")
        self.assertEqual(result.basis, "two-dot")
        self.assertIn("harness/other.py", result.offending)
        self.assertIn("no merge base", result.report())

    def test_an_unresolvable_ref_cannot_be_answered(self):
        product = self.repo()
        with self.assertRaises(BoundaryError):
            check(product, "no-such-ref", "HEAD", "implementer")

    def test_the_cli_exits_two_for_it(self):
        product = self.repo()
        code, out = run_cli(
            ["verify", "boundaries", str(product), "--base", "no-such-ref",
             "--head", "HEAD", "--role", "implementer"]
        )
        self.assertEqual(code, 2)
        self.assertIn("no-such-ref", out)


class TestBoundariesThatWouldTurnTheGuardOff(BoundaryCase):
    """Every one of these admits every path under a naive prefix test."""

    def refuses(self, boundaries, fragment):
        product = self.repo(boundaries=boundaries)
        with self.assertRaises(ProductFileError) as caught:
            write_boundaries(product, "implementer")
        self.assertIn(fragment, str(caught.exception))

    def test_an_empty_entry_is_refused(self):
        self.refuses({"implementer": ['""']}, "names no tree")

    def test_a_bare_dot_is_refused(self):
        self.refuses({"implementer": ["."]}, "names no tree")

    def test_a_slash_is_refused(self):
        self.refuses({"implementer": ["/"]}, "absolute")

    def test_an_absolute_path_is_refused(self):
        self.refuses({"implementer": ["/etc"]}, "absolute")

    def test_an_entry_escaping_the_repo_is_refused(self):
        self.refuses({"implementer": ["../.."]}, "escapes the repository")

    def test_a_leading_dot_slash_is_normalised_rather_than_refused(self):
        product = self.repo(boundaries={"implementer": ["./src"]})
        self.assertEqual(write_boundaries(product, "implementer"), ["src"])


class TestAnUndeclaredRole(BoundaryCase):
    def test_a_role_the_file_does_not_declare_is_refused_not_defaulted(self):
        # Neither default is safe, which is why there is none: an empty list
        # faults every honest PR, an unrestricted one passes every dishonest
        # one. The command says which roles the file does declare.
        product = self.repo(boundaries={"implementer": ["src"]})
        with self.assertRaises(BoundaryError) as caught:
            check(product, self.base, "HEAD", "verifier")
        self.assertIn("implementer", str(caught.exception))

    def test_a_product_file_with_no_boundaries_at_all_is_refused(self):
        product = self.repo(boundaries={})
        write_product(product, boundaries=None)
        commit_files(product, {}, "drop the boundaries")
        with self.assertRaises(BoundaryError) as caught:
            check(product, self.base, "HEAD", "implementer")
        self.assertIn("no write_boundaries", str(caught.exception))

    def test_the_cli_exits_two_for_an_undeclared_role(self):
        # 2, not 1. A mistyped --role reaching a caller as "this PR wrote
        # outside its trees" is a red nobody can find the cause of.
        product = self.repo(boundaries={"implementer": ["src"]})
        self.implementation(product, {"harness/steps.py": "# nope\n"})
        code, out = run_cli(
            ["verify", "boundaries", str(product), "--base", self.base,
             "--head", "HEAD", "--role", "implementor"]
        )
        self.assertEqual(code, 2)
        self.assertIn("implementor", out)

    def test_a_checkout_with_no_product_file_is_refused(self):
        with self.assertRaises(BoundaryError):
            check(self.root / "nowhere", "HEAD", "HEAD", "implementer")


class TestTheReport(BoundaryCase):
    def test_a_passing_run_still_names_the_trees_it_allowed(self):
        # The half that catches a mis-declared boundary: a green run that
        # printed nothing leaves a reviewer unable to see what was in bounds.
        product = self.repo(boundaries={"implementer": ["src"]})
        self.implementation(product, {"src/app.py": "x = 1\n"})
        code, out = run_cli(
            ["verify", "boundaries", str(product), "--base", self.base,
             "--head", "HEAD", "--role", "implementer"]
        )
        self.assertEqual(code, 0)
        self.assertIn("may write: src", out)

    def test_the_report_marks_which_paths_crossed(self):
        product = self.repo()
        self.implementation(product, {"src/app.py": "x = 1\n", "harness/steps.py": "# no\n"})
        report = check(product, self.base, "HEAD", "implementer").report()
        self.assertIn("CROSSES  harness/steps.py", report)
        self.assertIn("ok     src/app.py", report)


class TestUnder(unittest.TestCase):
    def test_a_tree_admits_itself_and_its_contents(self):
        self.assertTrue(under("src", "src"))
        self.assertTrue(under("src/a/b.py", "src"))

    def test_a_tree_does_not_admit_a_prefix_sibling(self):
        self.assertFalse(under("srcs/a.py", "src"))
        self.assertFalse(under("src.py", "src"))


if __name__ == "__main__":
    unittest.main()
