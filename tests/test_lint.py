"""``vellum lint``: frontmatter schema, cross-references, gherkin parsing."""

import contextlib
import io
import unittest

from support import (
    FIXTURES,
    PINNED_SPEC,
    REPO_ROOT,
    pinned_scenario_count,
    pinned_spec_is_checked_out,
)
from vellum.cli import main
from vellum.gherkin_blocks import parse_block
from vellum.lint import lint_tree
from vellum.specfile import SpecTreeError, iter_spec_files, resolve_spec_root
from vellum.suite import scenarios_in


def run_cli(argv):
    """Run the CLI, swallowing its output so the test log stays quiet."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return main(argv), buf.getvalue()


def codes(name):
    return sorted(f.code for f in lint_tree(FIXTURES / name))


def scenarios_in_tree(spec_dir):
    """Every scenario extraction finds in a tree, defects and all."""
    root = resolve_spec_root(spec_dir)
    return [sc for sf in iter_spec_files(root) for sc in scenarios_in(sf.relpath, sf.text)]


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
        if not (REPO_ROOT / "spec" / "spec" / "index.md").is_file():
            self.skipTest("spec submodule is not checked out")
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


class TestBackgrounds(unittest.TestCase):
    """Backgrounds are banned (spec/decisions/2026-08-28-no-backgrounds.md)."""

    def setUp(self):
        self.findings = lint_tree(FIXTURES / "bad-backgrounds")

    def test_a_background_fails_the_run(self):
        found = [f for f in self.findings if f.code == "GH008"]
        self.assertEqual(len(found), 1)
        self.assertIn("Sign-in", found[0].message)
        self.assertEqual(run_cli(["lint", str(FIXTURES / "bad-backgrounds")])[0], 1)

    def test_the_finding_points_at_the_background_not_the_fence(self):
        background = next(f for f in self.findings if f.code == "GH008")
        self.assertEqual(background.line, 13)

    def test_the_scenarios_themselves_are_not_faulted(self):
        # The Background is the defect; the scenarios under it are well-formed.
        self.assertEqual({f.code for f in self.findings}, {"GH008"})


class TestUnrunnableScenarios(unittest.TestCase):
    """A scenario that parses and can never run fails lint
    (spec/decisions/2026-08-28-runnable-scenarios.md, spec-v5).

    The class is "declared but unrunnable", not one construct. Both members the
    decision names are here: an outline with no Examples section at all, and one
    whose Examples table has a header and no data rows.
    """

    def setUp(self):
        self.findings = lint_tree(FIXTURES / "bad-unrunnable")

    def test_both_kinds_of_empty_outline_fail_the_run(self):
        found = [f for f in self.findings if f.code == "GH007"]
        self.assertEqual(len(found), 2)
        for f in found:
            self.assertIn("never runs", f.message)
        self.assertEqual(run_cli(["lint", str(FIXTURES / "bad-unrunnable")])[0], 1)

    def test_an_examples_table_with_a_header_and_no_rows_is_not_coverage(self):
        # The header alone parses into an Examples node, so a truthiness check
        # on `examples` would pass this and the outline would still never run.
        found = [f for f in self.findings if "no data rows" in f.message]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].code, "GH007")

    def test_nothing_else_is_faulted(self):
        self.assertEqual({f.code for f in self.findings}, {"GH007"})


class TestMultiFeatureFences(unittest.TestCase):
    """One Feature per fence (spec/decisions/2026-08-28-one-feature-per-fence.md)."""

    def setUp(self):
        self.findings = lint_tree(FIXTURES / "bad-multi-feature")

    def test_a_second_feature_fails_the_run(self):
        found = [f for f in self.findings if f.code == "GH009"]
        self.assertEqual(len(found), 1)
        self.assertIn("Second concern", found[0].message)
        self.assertIn("First concern", found[0].message)
        self.assertEqual(run_cli(["lint", str(FIXTURES / "bad-multi-feature")])[0], 1)

    def test_the_finding_points_at_the_second_feature_not_the_fence(self):
        found = next(f for f in self.findings if f.code == "GH009")
        self.assertEqual(found.line, 21)

    def test_the_scenarios_themselves_are_not_faulted(self):
        # The extra Feature is the defect; the scenarios in both documents are
        # well-formed, and both still extract.
        self.assertEqual({f.code for f in self.findings}, {"GH009"})
        ids = {sc.id for sc in scenarios_in_tree(FIXTURES / "bad-multi-feature")}
        self.assertEqual(ids, {"two-in-one-first", "two-in-one-second"})

    def test_a_block_the_stock_parser_reads_whole_is_never_split(self):
        # Indentation is not significant to Gherkin, so a step line beginning
        # "Feature:" really is a second Feature and GH009 is right to fault it.
        # Inside a docstring it is not: the text is literal, one Feature, and
        # the stock parser reads the block. Cutting at column-zero "Feature:"
        # lines before parsing used to split this sound block down the middle
        # and report the unterminated docstring as GH001. The parser is asked
        # to read the block whole first, so nothing is cut.
        body = (
            "Feature: Real\n"
            "Scenario: Carries a docstring\n"
            "Given a payload\n"
            '"""\n'
            "Feature: quoted, not declared\n"
            '"""\n'
            "Then it is accepted\n"
        )
        block = parse_block(body, 1)
        self.assertEqual([f.name for f in block.features], ["Real"])
        self.assertEqual(len(block.scenarios), 1)
        self.assertEqual(len(block.scenarios[0].steps), 2)


class TestPinnedSpecTree(unittest.TestCase):
    """The definition of done: the tree at the pin lints clean."""

    def setUp(self):
        if not pinned_spec_is_checked_out():
            self.skipTest("spec submodule is not checked out")

    def test_pinned_spec_lints_clean(self):
        self.assertEqual(lint_tree(PINNED_SPEC), [])

    def test_every_scenario_in_the_pinned_spec_has_an_id(self):
        from vellum.suite import extract

        entries = extract(PINNED_SPEC).entries
        self.assertEqual(len(entries), pinned_scenario_count())
        self.assertTrue(all(e.scenario.id for e in entries))


if __name__ == "__main__":
    unittest.main()
