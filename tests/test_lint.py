"""``vellum lint``: frontmatter schema, cross-references, gherkin parsing."""

import tempfile
import unittest
from pathlib import Path

from support import (
    FIXTURES,
    intent_checkout,
    intent_spec_tree,
    make_raw_tree,
    pinned_scenario_count,
    run_cli,
)
from vellum.gherkin_blocks import parse_block
from vellum.lint import lint_tree
from vellum.specfile import SpecTreeError, iter_spec_files, resolve_spec_root
from vellum.suite import scenarios_in


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
        # `<spec-dir>` is two things: the spec tree, or the intent repo that
        # holds it one level down at `spec/`. Both must resolve, and this is
        # built here rather than read off a checkout — it was a submodule test
        # once, and it went quiet the moment the submodule went away
        # (spec/decisions/2026-08-28-pin-file.md), which is exactly the kind of
        # hole an environment-dependent test leaves behind.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "intent"
            (checkout / "spec").mkdir(parents=True)
            (checkout / "spec" / "index.md").write_text(
                "---\nid: index\ntitle: I\nsince: spec-v1\n---\n\n# Index\n"
            )
            root = resolve_spec_root(checkout)
            self.assertEqual(root, (checkout / "spec").resolve())
            # And the tree itself, given directly, resolves to itself.
            self.assertEqual(resolve_spec_root(root), root)

    def test_a_real_intent_checkout_resolves_the_same_way(self):
        # The live case, when one is available: whatever supplied the checkout,
        # `vellum lint <checkout>` finds the tree inside it.
        checkout = intent_checkout()
        if checkout is None:
            self.skipTest("no intent checkout (set VELLUM_INTENT_REPO)")
        root = resolve_spec_root(checkout)
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

    The class is "declared but unrunnable", defined by construct — so it is
    neither one construct nor one keyword. Both members the decision names are
    here (an outline with no Examples section at all, and one whose Examples
    table has a header and no data rows), each in one of the two spellings
    Gherkin's English dialect gives the construct, plus a template with a row,
    which runs and must be left alone.
    """

    def setUp(self):
        self.findings = lint_tree(FIXTURES / "bad-unrunnable")

    def test_every_outline_that_cannot_run_fails_the_run(self):
        found = [f for f in self.findings if f.code == "GH007"]
        self.assertEqual(len(found), 3)
        for f in found:
            self.assertIn("never runs", f.message)
        self.assertEqual(run_cli(["lint", str(FIXTURES / "bad-unrunnable")])[0], 1)

    def test_an_examples_table_with_a_header_and_no_rows_is_not_coverage(self):
        # The header alone parses into an Examples node, so a truthiness check
        # on `examples` would pass this and the outline would still never run.
        found = [f for f in self.findings if f.line == 26]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].code, "GH007")

    def test_a_scenario_template_is_an_outline_under_its_other_keyword(self):
        # `Scenario Template` is a synonym for `Scenario Outline`, not a second
        # construct. Matching the literal keyword missed it, so an unrunnable
        # template drew zero findings and extracted as coverage. The finding
        # names the keyword actually written.
        found = [f for f in self.findings if f.line == 37]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].code, "GH007")
        self.assertIn("scenario template '", found[0].message)

    def test_a_template_with_a_row_runs_and_is_not_faulted(self):
        # The rule fires on unrunnability, not on the keyword; a template that
        # can execute is sound. Guards the synonym against over-reach.
        self.assertEqual([f for f in self.findings if f.line == 48], [])

    def test_nothing_else_is_faulted(self):
        self.assertEqual({f.code for f in self.findings}, {"GH007"})


class TestRuleBlocks(unittest.TestCase):
    """`Rule:` blocks are banned (spec/decisions/2026-08-28-no-rules.md).

    The defect the ban was raised for is a silent drop, not a keyword: a stock
    runner executes a Rule's nested scenarios and neither lint nor extraction
    ever saw them (waviisoft/vellum-intent#16). So these check the drop as well
    as the finding, and keep a tree that merely *says* "Rule:" as the negative
    control — the lesson GH007 and GH009 both paid for is that a rule keyed on
    a token faults prose and misses constructs.
    """

    def setUp(self):
        self.findings = lint_tree(FIXTURES / "bad-rules")

    def gh010(self):
        return [f for f in self.findings if f.code == "GH010"]

    def test_a_rule_fails_the_run(self):
        found = next(f for f in self.gh010() if f.file == "features/nested.md")
        self.assertIn("Deletion", found.message)
        self.assertIn("Only admins may delete", found.message)
        self.assertEqual(run_cli(["lint", str(FIXTURES / "bad-rules")])[0], 1)

    def test_the_finding_points_at_the_rule_not_the_fence(self):
        found = next(f for f in self.gh010() if f.file == "features/nested.md")
        self.assertEqual(found.line, 23)

    def test_the_finding_names_how_many_scenarios_are_dropped(self):
        # The count is the defect: without it the finding reports a style rule,
        # and with it the finding reports missing coverage.
        found = next(f for f in self.gh010() if f.file == "features/nested.md")
        self.assertIn("2 scenario(s)", found.message)

    def test_the_nested_scenarios_really_are_dropped(self):
        # The oracle for the count above, taken from extraction rather than
        # from the same code path: the Feature's direct child is described and
        # the two under the Rule are not.
        found = scenarios_in_tree(FIXTURES / "bad-rules")
        ids = {sc.id for sc in found}
        self.assertIn("rules-direct-child", ids)
        self.assertNotIn("rules-nested-example", ids)
        self.assertNotIn("rules-nested-scenario", ids)

    def test_an_empty_rule_is_faulted_before_its_emptiness_matters(self):
        # The decision calls a Rule with no scenarios moot as an unrunnable
        # class member, "because the construct fails lint before its emptiness
        # matters". GH010 fires; GH007 has nothing to say about it.
        empty = [f for f in self.findings if f.file == "features/empty-rule.md"]
        self.assertEqual({f.code for f in empty}, {"GH010", "GH002"})
        self.assertIn("holding no scenarios", next(f for f in empty if f.code == "GH010").message)

    def test_a_rule_absorbs_every_scenario_after_it_not_the_indented_block(self):
        # Gherkin is not indentation-sensitive: a Rule holds every scenario
        # until the next Rule or the end of the Feature. So one stray `Rule:`
        # line above a Feature's existing scenarios takes all of them out of
        # the suite at once — which is why the finding counts them, and why
        # this is worse than it looks in a diff. Measured on the real tree:
        # one inserted line emptied features/repo-topology.md.
        found = next(f for f in self.gh010() if f.file == "features/absorbs-what-follows.md")
        self.assertIn("1 scenario(s)", found.message)
        ids = {sc.id for sc in scenarios_in_tree(FIXTURES / "bad-rules")}
        self.assertNotIn("rules-absorbed-by-a-rule-above", ids)

    def test_a_rule_written_in_prose_is_not_a_rule(self):
        # Detected from the parsed node, so a `Rule:` at column zero inside a
        # docstring — literal text to the parser — and one inside a step's text
        # are both left alone. A rule matching the token would fault both.
        self.assertEqual(
            [f for f in self.findings if f.file == "features/mentions-a-rule.md"], []
        )

    def test_the_clean_tree_draws_no_rule_finding(self):
        self.assertEqual([f for f in lint_tree(FIXTURES / "good") if f.code == "GH010"], [])


class TestMultiFeatureFences(unittest.TestCase):
    """One Feature per fence (spec/decisions/2026-08-28-one-feature-per-fence.md)."""

    def setUp(self):
        self.findings = lint_tree(FIXTURES / "bad-multi-feature")

    def gh009(self):
        return [f for f in self.findings if f.code == "GH009"]

    def test_a_second_feature_fails_the_run(self):
        found = next(f for f in self.gh009() if f.file == "features/two-in-one.md")
        self.assertIn("Second concern", found.message)
        self.assertIn("First concern", found.message)
        self.assertEqual(run_cli(["lint", str(FIXTURES / "bad-multi-feature")])[0], 1)

    def test_the_finding_points_at_the_second_feature_not_the_fence(self):
        found = next(f for f in self.gh009() if f.file == "features/two-in-one.md")
        self.assertEqual(found.line, 21)

    def test_a_second_feature_the_parser_absorbs_as_prose_is_still_found(self):
        # The parser refuses a second Feature: only where it reaches one as a
        # declaration. Reached where free text is legal it swallows the line —
        # into a Feature's description, or a Scenario's — and the block parses
        # one Feature short with no error at all. Trusting a clean whole-body
        # parse on its own therefore misses the rule entirely here.
        found = [f for f in self.gh009() if f.file == "features/absorbed.md"]
        self.assertEqual([f.line for f in found], [22, 37])
        self.assertIn("Swallowed by a description", found[0].message)
        self.assertIn("Swallowed by a scenario description", found[1].message)

    def test_an_absorbed_feature_does_not_steal_the_next_features_scenarios(self):
        # Left absorbed, the second Feature's scenarios re-parent onto the
        # first and suite.json reports the wrong feature for each of them.
        under = {
            sc.id: sc.feature
            for sc in scenarios_in_tree(FIXTURES / "bad-multi-feature")
        }
        self.assertEqual(
            under["absorbed-into-feature-description"], "Swallowed by a description"
        )
        self.assertEqual(
            under["absorbed-into-scenario-description"],
            "Swallowed by a scenario description",
        )

    def test_the_scenarios_themselves_are_not_faulted(self):
        # The extra Feature is the defect; the scenarios in both documents are
        # well-formed and both still extract. The one GH004 is intrinsic to the
        # fixture: a Scenario description exists only on a step-less scenario,
        # which is the only way to build the absorbed-into-a-scenario case.
        self.assertEqual({f.code for f in self.findings}, {"GH009", "GH004"})
        self.assertEqual(
            {f.code for f in self.findings if f.file == "features/two-in-one.md"},
            {"GH009"},
        )
        ids = {sc.id for sc in scenarios_in_tree(FIXTURES / "bad-multi-feature")}
        self.assertEqual(
            ids,
            {
                "two-in-one-first",
                "two-in-one-second",
                "absorbed-host-scenario",
                "absorbed-into-feature-description",
                "absorbed-into-scenario-description",
            },
        )

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


class TestFenceInfoStrings(unittest.TestCase):
    """A fence's language is its info string's first word (waviisoft/vellum#10).

    Lint's stake in this is the whole of the issue's severity. Lint and
    extraction read the same ``SpecFile.fences``, so a fence the markdown
    parser did not see was invisible to *both*: lint said nothing and the
    suite came back short, which is the "exit 0 while omitting a scenario"
    hole spec/features/spec-pipeline.md exists to close. These assert lint's
    half; ``tests/test_suite.py`` asserts extraction's, against the same
    shapes.
    """

    def lint_bodies(self, bodies):
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_raw_tree(Path(tmp), bodies)
            return lint_tree(tree)

    def test_an_attributed_gherkin_fence_is_linted_like_any_other(self):
        # A block that does not parse, in a fence whose info string carries an
        # attribute. Before the fix this drew no finding at all — the fence
        # was not a fence, so GH001 never looked inside it.
        findings = self.lint_bodies(
            {
                "demo": (
                    "```gherkin title=demo\n"
                    "Feature: X\n"
                    "  Scenario: An unclosed docstring runs off the end\n"
                    "    Given a payload\n"
                    '      """\n'
                    '      {"still": "open"\n'
                    "    Then it is rejected\n"
                    "```"
                )
            }
        )
        self.assertEqual([f.code for f in findings], ["GH001"])

    def test_the_block_after_an_attributed_one_is_still_linted(self):
        # The desync, as lint sees it: the first block's closing line used to
        # open a phantom fence that ran past the second block's opener, so a
        # defect in *either* block went unreported. One run finds both.
        findings = self.lint_bodies(
            {
                "demo": (
                    "```gherkin title=demo\n"
                    "Feature: First\n"
                    "  Scenario: No id here\n"
                    "    Given a thing\n"
                    "```\n"
                    "\n"
                    "Prose between the blocks.\n"
                    "\n"
                    "```gherkin\n"
                    "Feature: Second\n"
                    "  Scenario: Nor here\n"
                    "    Given a thing\n"
                    "```"
                )
            }
        )
        self.assertEqual([f.code for f in findings], ["GH005", "GH005"])
        self.assertEqual([f.line for f in findings], [11, 19])

    def test_a_non_gherkin_fence_with_attributes_is_skipped_and_desyncs_nothing(self):
        # `python foo=bar` is not gherkin and draws nothing on its own account.
        # What matters is that it no longer takes the gherkin block below it
        # down with it.
        findings = self.lint_bodies(
            {
                "demo": (
                    "```python foo=bar\n"
                    "print('not gherkin')\n"
                    "```\n"
                    "\n"
                    "```gherkin\n"
                    "Feature: Still visible\n"
                    "  Scenario: No id here\n"
                    "    Given a thing\n"
                    "```"
                )
            }
        )
        self.assertEqual([f.code for f in findings], ["GH005"])

    def test_a_clean_attributed_block_draws_nothing(self):
        # The negative control: attributes are ignored, not faulted. A rule
        # that fired on the attribute itself would pass every test above.
        self.assertEqual(
            self.lint_bodies(
                {
                    "demo": (
                        "```gherkin title=demo hl_lines='2 3'\n"
                        "Feature: Fine\n"
                        "  @id:attributed-fence-is-fine\n"
                        "  Scenario: Fine\n"
                        "    Given a thing\n"
                        "    Then it works\n"
                        "```"
                    )
                }
            ),
            [],
        )


class TestPinnedSpecTree(unittest.TestCase):
    """The definition of done: the tree at the pin lints clean."""

    def setUp(self):
        self.tree = intent_spec_tree()
        if self.tree is None:
            self.skipTest("no intent checkout (set VELLUM_INTENT_REPO)")

    def test_pinned_spec_lints_clean(self):
        self.assertEqual(lint_tree(intent_checkout()), [])

    def test_every_scenario_in_the_pinned_spec_has_an_id(self):
        from vellum.suite import extract

        entries = extract(intent_checkout()).entries
        self.assertGreater(pinned_scenario_count(self.tree), 0)
        self.assertEqual(len(entries), pinned_scenario_count(self.tree))
        self.assertTrue(all(e.scenario.id for e in entries))


if __name__ == "__main__":
    unittest.main()
