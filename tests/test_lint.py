"""``vellum lint``: frontmatter schema, cross-references, gherkin parsing."""

import contextlib
import io
import unittest

from support import FIXTURES, REPO_ROOT
from vellum.cli import main
from vellum.lint import lint_tree
from vellum.specfile import SpecTreeError, resolve_spec_root


def run_cli(argv):
    """Run the CLI, swallowing its output so the test log stays quiet."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return main(argv), buf.getvalue()


def codes(name):
    return sorted(f.code for f in lint_tree(FIXTURES / name))


class TestCleanTree(unittest.TestCase):
    def test_good_fixture_has_no_findings(self):
        self.assertEqual(lint_tree(FIXTURES / "good"), [])

    def test_exit_code_is_zero(self):
        code, output = run_cli(["lint", str(FIXTURES / "good")])
        self.assertEqual(code, 0)
        self.assertEqual(output, "")


class TestSpecRootDetection(unittest.TestCase):
    def test_tree_given_directly(self):
        self.assertEqual(
            resolve_spec_root(FIXTURES / "good"), (FIXTURES / "good").resolve()
        )

    def test_intent_repo_root_resolves_to_its_spec_subdirectory(self):
        # A product repo mounts the whole intent repo at ./spec, so the tree is
        # one level down; `vellum lint spec/` must work either way.
        root = resolve_spec_root(REPO_ROOT / "spec")
        self.assertEqual(root.name, "spec")
        self.assertTrue((root / "index.md").is_file())

    def test_directory_that_is_not_a_spec_tree_is_rejected(self):
        with self.assertRaises(SpecTreeError):
            resolve_spec_root(FIXTURES)


class TestFrontmatter(unittest.TestCase):
    def test_reports_each_kind_of_frontmatter_defect(self):
        self.assertEqual(
            codes("bad-frontmatter"),
            ["FM001", "FM002", "FM002", "FM002", "FM003", "FM003", "FM004", "FM004"],
        )

    def test_missing_frontmatter_block(self):
        found = [
            f
            for f in lint_tree(FIXTURES / "bad-frontmatter")
            if f.file == "features/no-frontmatter.md"
        ]
        self.assertEqual([f.code for f in found], ["FM001"])

    def test_decisions_require_date_and_reject_since(self):
        found = [
            f
            for f in lint_tree(FIXTURES / "bad-frontmatter")
            if f.file.startswith("decisions/")
        ]
        self.assertEqual(sorted(f.code for f in found), ["FM002", "FM003"])
        self.assertTrue(any("date" in f.message for f in found))

    def test_yaml_dates_are_accepted_unquoted(self):
        # PyYAML turns an unquoted 2026-08-27 into a datetime.date, not a string.
        self.assertEqual(
            [f for f in lint_tree(FIXTURES / "good") if f.file.startswith("decisions/")],
            [],
        )

    def test_since_must_be_an_integer_version(self):
        found = [
            f for f in lint_tree(FIXTURES / "bad-frontmatter") if "since" in f.message
        ]
        self.assertTrue(any(f.code == "FM004" for f in found))


class TestLinks(unittest.TestCase):
    def setUp(self):
        self.findings = lint_tree(FIXTURES / "bad-links")

    def test_unresolvable_markdown_link_and_bare_path_both_reported(self):
        targets = sorted(f.message for f in self.findings if f.code == "LN001")
        self.assertEqual(len(targets), 2)
        self.assertTrue(any("features/absent.md" in m for m in targets))
        self.assertTrue(any("features/also-absent.md" in m for m in targets))

    def test_unknown_fragment_reported(self):
        self.assertEqual(
            [f.code for f in self.findings if f.code == "LN002"], ["LN002"]
        )

    def test_paths_inside_fences_and_code_spans_are_not_references(self):
        # features/imaginary.md appears inside a gherkin block; spec/** in a code span.
        self.assertFalse(any("imaginary" in f.message for f in self.findings))
        self.assertFalse(any("spec/**" in f.message for f in self.findings))

    def test_external_links_are_ignored(self):
        self.assertFalse(any("example.com" in f.message for f in self.findings))

    def test_trailing_sentence_punctuation_is_not_part_of_a_fragment(self):
        self.assertFalse(any("acceptance." in f.message for f in self.findings))

    def test_reference_resolves_against_the_spec_roots_parent(self):
        # docs/design.md lives above the tree, as it does in the intent repo.
        self.assertEqual(
            [f for f in lint_tree(FIXTURES / "good") if f.code == "LN001"], []
        )


class TestGherkin(unittest.TestCase):
    def setUp(self):
        self.findings = lint_tree(FIXTURES / "bad-gherkin")

    def test_unparseable_blocks_fail_the_run(self):
        self.assertEqual([f.code for f in self.findings if f.code == "GH001"], ["GH001"] * 2)
        self.assertEqual(run_cli(["lint", str(FIXTURES / "bad-gherkin")])[0], 1)

    def test_findings_point_inside_the_offending_block(self):
        for f in (f for f in self.findings if f.code == "GH001"):
            self.assertGreater(f.line, 1)

    def test_block_with_no_scenarios_is_reported(self):
        self.assertEqual([f.code for f in self.findings if f.code == "GH002"], ["GH002"])


class TestScenarioIds(unittest.TestCase):
    """Every scenario carries exactly one well-formed, repo-unique @id: tag."""

    def setUp(self):
        self.findings = lint_tree(FIXTURES / "bad-ids")

    def by_code(self, code):
        return [f for f in self.findings if f.code == code]

    def test_missing_id_is_reported(self):
        found = self.by_code("GH005")
        self.assertEqual(len(found), 1)
        self.assertIn("Declares no id at all", found[0].message)

    def test_malformed_id_is_reported(self):
        self.assertTrue(any("Not_A_Slug" in f.message for f in self.by_code("GH006")))

    def test_a_scenario_with_two_ids_is_reported(self):
        self.assertTrue(any("2 id tags" in f.message for f in self.by_code("GH006")))

    def test_duplicate_ids_are_caught_across_files_not_just_within_one(self):
        # Ids are unique across the intent repo, not per file.
        found = self.by_code("GH003")
        self.assertEqual({f.file for f in found}, {"features/one.md", "features/two.md"})
        for f in found:
            self.assertIn("shared-id", f.message)

    def test_each_home_of_a_duplicate_id_names_the_others(self):
        one = next(f for f in self.by_code("GH003") if f.file == "features/one.md")
        self.assertIn("features/two.md", one.message)

    def test_the_run_fails(self):
        self.assertEqual(run_cli(["lint", str(FIXTURES / "bad-ids")])[0], 1)

    def test_a_tree_with_well_formed_ids_is_clean(self):
        self.assertEqual(lint_tree(FIXTURES / "good"), [])


class TestPinnedSpecTree(unittest.TestCase):
    """The definition of done: the pinned spec-v1 tree lints clean."""

    def setUp(self):
        if not (REPO_ROOT / "spec" / "spec" / "index.md").is_file():
            self.skipTest("spec submodule is not checked out")

    def test_pinned_spec_lints_clean(self):
        self.assertEqual(lint_tree(REPO_ROOT / "spec"), [])

    def test_every_scenario_in_the_pinned_spec_has_an_id(self):
        from vellum.suite import extract

        entries = extract(REPO_ROOT / "spec").entries
        self.assertEqual(len(entries), 19)
        self.assertTrue(all(e.scenario.id for e in entries))


if __name__ == "__main__":
    unittest.main()
