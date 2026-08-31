"""``vellum verify boundaries``: what a role may write, and what a diff touched.

The scenario is ``@id:implementer-cannot-touch-harness`` in
``spec/behaviors/write-boundaries.md``: an implementation PR containing changes
under ``harness/`` fails the write-boundary guard.

The same behavior's "CI enforces the same boundaries as a backstop" sentence is
not about product repos only, and the second half of this file is the intent
repo: it declares its boundaries in ``.vellum/config.yaml`` because it has no
product file, and the breach that motivated reading them is the mirror image of
the scenario — a harness session writing ``.vellum/memory/``, which is the
librarian's tree, not the harness engineer's.
"""

import tempfile
import unittest
from pathlib import Path

from support import (
    UNSET, branch, commit_files, git, make_git_intent_repo, make_git_product_repo,
    run_cli, write_intent_config, write_product,
)
from vellum.boundaries import BoundaryError, check, resolve_source
from vellum.config import ConfigError
from vellum.config import write_boundaries as config_boundaries
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


class IntentCase(BoundaryCase):
    """A sandbox *intent* repo: boundaries in `.vellum/config.yaml`, no product file."""

    def repo(self, boundaries=UNSET, files=None):
        intent = make_git_intent_repo(self.root / "intent", boundaries, files)
        self.base = git(intent, "rev-parse", "HEAD").strip()
        return intent

    def harness_pr(self, intent, files):
        branch(intent, "harness")
        return commit_files(intent, files, "harness change")


class TestTheIntentRepoIsGuardedToo(IntentCase):
    def test_a_harness_pr_touching_the_memory_is_refused(self):
        # The recurring real breach: harness sessions editing `.vellum/memory/`,
        # which the librarian owns. `harness-engineer` may write `harness/` and
        # nothing else, so this crosses.
        intent = self.repo()
        self.harness_pr(intent, {
            "harness/run.py": "# a real harness change\n",
            ".vellum/memory/areas/pipeline.md": "# Pipeline\n\nedited by the harness\n",
        })
        result = check(intent, self.base, "HEAD", "harness-engineer")
        self.assertTrue(result.crossed)
        self.assertEqual(result.offending, [".vellum/memory/areas/pipeline.md"])

    def test_the_cli_exits_one_for_it(self):
        intent = self.repo()
        self.harness_pr(intent, {".vellum/memory/map.md": "# Map\n\nedited\n"})
        code, out = run_cli(
            ["verify", "boundaries", str(intent), "--base", self.base,
             "--head", "HEAD", "--role", "harness-engineer"]
        )
        self.assertEqual(code, 1)
        self.assertIn(".vellum/memory/map.md", out)

    def test_a_harness_pr_staying_in_harness_passes(self):
        intent = self.repo()
        self.harness_pr(intent, {
            "harness/run.py": "# a real harness change\n",
            "harness/steps/pipeline.py": "# a new step definition\n",
        })
        code, out = run_cli(
            ["verify", "boundaries", str(intent), "--base", self.base,
             "--head", "HEAD", "--role", "harness-engineer"]
        )
        self.assertEqual(code, 0)
        self.assertIn("may write: harness", out)

    def test_the_report_names_the_file_the_boundaries_came_out_of(self):
        # Which trees were considered is half the answer; which file declared
        # them is the other half, and on a repo that could have two it is the
        # half that catches the guard reading the wrong one.
        intent = self.repo()
        self.harness_pr(intent, {"harness/run.py": "# fine\n"})
        report = check(intent, self.base, "HEAD", "harness-engineer").report()
        self.assertIn("config.yaml (config)", report)

    def test_the_ledger_is_not_the_harness_engineers_either(self):
        intent = self.repo()
        self.harness_pr(intent, {"ledger/notes.md": "# rewritten by the harness\n"})
        self.assertEqual(
            check(intent, self.base, "HEAD", "harness-engineer").offending,
            ["ledger/notes.md"],
        )

    def test_the_librarian_may_write_the_memory_the_harness_may_not(self):
        # One diff, two roles, opposite answers — which is the whole point of
        # the block being per-role data rather than one list of protected trees.
        intent = self.repo()
        self.harness_pr(intent, {".vellum/memory/map.md": "# Map\n\nrewritten\n"})
        self.assertFalse(check(intent, self.base, "HEAD", "librarian").crossed)
        self.assertTrue(check(intent, self.base, "HEAD", "harness-engineer").crossed)


class TestTheConfigIsReadByTheSameRules(IntentCase):
    """One block shape, one reader: `product.role_trees` answers for both files."""

    def test_a_role_the_config_does_not_declare_is_refused(self):
        intent = self.repo(boundaries={"harness-engineer": ["harness"]})
        with self.assertRaises(BoundaryError) as caught:
            check(intent, self.base, "HEAD", "librarian")
        self.assertIn("harness-engineer", str(caught.exception))

    def test_the_cli_exits_two_for_it(self):
        intent = self.repo(boundaries={"harness-engineer": ["harness"]})
        code, out = run_cli(
            ["verify", "boundaries", str(intent), "--base", self.base,
             "--head", "HEAD", "--role", "librarian"]
        )
        self.assertEqual(code, 2)
        self.assertIn("librarian", out)

    def test_an_entry_that_would_turn_the_guard_off_is_refused_here_too(self):
        intent = self.repo(boundaries={"harness-engineer": ["../.."]})
        with self.assertRaises(ConfigError) as caught:
            config_boundaries(intent, "harness-engineer")
        self.assertIn("escapes the repository", str(caught.exception))

    def test_a_config_with_no_boundaries_block_is_refused(self):
        intent = self.repo(boundaries=None)
        with self.assertRaises(BoundaryError) as caught:
            check(intent, self.base, "HEAD", "harness-engineer")
        self.assertIn("no write_boundaries", str(caught.exception))

    def test_the_refusal_names_the_config_rather_than_a_product_file(self):
        # A message naming `.vellum/product.yaml` would send a harness engineer
        # looking for a file the intent repo does not have.
        intent = self.repo(boundaries=None)
        with self.assertRaises(BoundaryError) as caught:
            check(intent, self.base, "HEAD", "harness-engineer")
        self.assertIn("config.yaml", str(caught.exception))
        self.assertNotIn("product.yaml", str(caught.exception))


class TestWhichFileDeclaresTheBoundaries(IntentCase):
    def test_a_product_file_wins_when_both_exist(self):
        # A repo carrying a product file is a product repo, whatever else it
        # also carries; the installation config is the source only where there
        # is no product file to be the source.
        intent = self.repo()
        write_product(intent, boundaries={"harness-engineer": ["harness", "src"]})
        commit_files(intent, {}, "colocated: a product file too")
        kind, path = resolve_source(intent)
        self.assertEqual(kind, "product")
        self.assertEqual(path.name, "product.yaml")

    def test_naming_the_config_reads_the_config_even_then(self):
        intent = self.repo()
        write_product(intent, boundaries={"harness-engineer": ["harness", "src"]})
        self.base = commit_files(intent, {}, "colocated: a product file too")
        self.harness_pr(intent, {"src/app.py": "x = 1\n"})
        # The product file would allow `src`; the config does not, and this is
        # the run that says which one was asked.
        self.assertFalse(
            check(intent, self.base, "HEAD", "harness-engineer", "product").crossed
        )
        self.assertEqual(
            check(intent, self.base, "HEAD", "harness-engineer", "config").offending,
            ["src/app.py"],
        )

    def test_a_named_source_that_is_absent_is_an_error_not_a_fallback(self):
        # The dangerous shape this refuses: a CI job that says `config` and is
        # quietly answered by some other file's allowlist.
        intent = self.repo()
        with self.assertRaises(BoundaryError) as caught:
            check(intent, self.base, "HEAD", "harness-engineer", "product")
        self.assertIn("product.yaml", str(caught.exception))
        self.assertIn("--boundaries-from product", str(caught.exception))

    def test_the_cli_exits_two_for_a_named_source_that_is_absent(self):
        intent = self.repo()
        code, out = run_cli(
            ["verify", "boundaries", str(intent), "--base", self.base, "--head", "HEAD",
             "--role", "harness-engineer", "--boundaries-from", "product"]
        )
        self.assertEqual(code, 2)
        self.assertIn("product.yaml", out)

    def test_a_checkout_declaring_neither_is_refused_naming_both(self):
        bare = self.root / "bare"
        bare.mkdir()
        with self.assertRaises(BoundaryError) as caught:
            resolve_source(bare)
        self.assertIn("product.yaml", str(caught.exception))
        self.assertIn("config.yaml", str(caught.exception))

    def test_there_is_no_cascade_from_one_file_to_the_other(self):
        # A product repo whose `write_boundaries` block was deleted must not
        # start being judged against installation policy — a different repo's
        # allowlist, silently applied to this one's diff. It is refused instead,
        # even though a config sitting beside it could have answered.
        product = make_git_product_repo(self.root / "app", boundaries=None)
        base = git(product, "rev-parse", "HEAD").strip()
        write_intent_config(product, boundaries={"implementer": ["harness"]})
        branch(product, "implementation")
        commit_files(product, {"harness/steps.py": "# nope\n"}, "reach outside")
        with self.assertRaises(BoundaryError) as caught:
            check(product, base, "HEAD", "implementer")
        self.assertIn("no write_boundaries", str(caught.exception))
        self.assertIn("product.yaml", str(caught.exception))

    def test_an_unknown_source_is_refused_rather_than_defaulted(self):
        # Not reachable through argparse, which has `choices`. Reachable by any
        # other caller of `check`, and defaulting one to `auto` would answer a
        # question nobody asked.
        with self.assertRaises(BoundaryError):
            resolve_source(self.root, "somewhere-else")
