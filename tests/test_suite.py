"""``vellum suite extract``: scenario collection and version derivation."""

import json
import tempfile
import unittest
from pathlib import Path

from support import (
    FIXTURES,
    commit_area,
    commit_elsewhere,
    git,
    intent_checkout,
    intent_spec_tree,
    make_spec_repo,
    make_raw_tree,
    make_tree,
    pinned_commit,
    pinned_gherkin_file_count,
    pinned_scenario_count,
    run_cli,
    run_cli_streams,
    write_area,
)
from vellum.gherkin_blocks import Step, parse_block, split_documents
from vellum.lint import lint_tree
from vellum.specfile import find_fences
from vellum.suite import (
    DroppedScenarios,
    extract,
    fingerprint,
    scenarios_in,
    to_dict,
)

ONE = """Feature: Login
  @id:login-good-password
  Scenario: Good password
    Given a user
    When they sign in
    Then they see the dashboard"""

TWO = ONE + """

  @id:login-bad-password
  Scenario: Bad password
    Given a user
    When they mistype
    Then they see an error"""

TWO_CHANGED = TWO.replace("Then they see the dashboard", "Then they see the dashboard in 400ms")

#: A block the Cucumber parser refuses: the docstring is never closed.
BROKEN = """Feature: Unreadable
  @id:never-closed
  Scenario: The quote is never closed
    Given a payload
      \"\"\"
      {"still": "open"
    Then it is rejected"""

#: A block that parses perfectly and still hands back one scenario fewer than a
#: stock runner executes: the Rule's child is not admitted (GH010).
RULE_NESTED = """Feature: Deletion
  @id:rule-direct-child
  Scenario: A direct child
    Given a signed-in user
    Then the page renders

  Rule: Only admins may delete
    @id:rule-nested-child
    Scenario: A normal user may not delete
      Given a normal user
      Then the delete is refused"""

#: A Rule holding nothing. Lint faults it (GH010) and the suite loses nothing,
#: which is the line the refusal is drawn on.
RULE_EMPTY = """Feature: Retention
  Rule: Records older than a year are purged"""

#: A fence that parses and declares no scenarios — GH002, and a complete suite.
NO_SCENARIOS = """Feature: Nothing yet
  Some description prose and no scenarios at all."""

#: The same two scenarios as TWO, before ids existed — the spec-v1 shape.
TWO_WITHOUT_IDS = "\n".join(
    line for line in TWO.split("\n") if not line.strip().startswith("@id:")
)


class TestBlockSplitting(unittest.TestCase):
    """The spec bans two Features in one fence (spec-v4). The splitter stays: it
    reads the older tags the version chain walks, and it is what lets lint name
    the second Feature (GH009) rather than echo the parser's error."""

    def test_single_feature_is_one_document(self):
        self.assertEqual(len(split_documents(ONE)), 1)

    def test_two_features_split_into_two_documents(self):
        block = "Feature: A\n  Scenario: S\n    Given x\nFeature: B\n  Scenario: T\n    Given y"
        docs = split_documents(block)
        self.assertEqual([offset for offset, _ in docs], [0, 3])

    def test_tags_travel_with_the_feature_below_them(self):
        block = "Feature: A\n  Scenario: S\n    Given x\n@wip\nFeature: B\n  Scenario: T\n    Given y"
        self.assertEqual([offset for offset, _ in split_documents(block)], [0, 3])

    def test_scenarios_from_both_features_are_collected(self):
        block = "Feature: A\n  Scenario: S\n    Given x\nFeature: B\n  Scenario: T\n    Given y"
        self.assertEqual([s.feature for s in parse_block(block, 1).scenarios], ["A", "B"])


class TestScenarioIds(unittest.TestCase):
    """Identity is the @id: tag (spec/decisions/2026-08-28-scenario-identity.md)."""

    def parse(self, block):
        return parse_block(block, 1).scenarios[0]

    def test_id_is_read_from_the_tag(self):
        sc = self.parse("Feature: F\n  @id:the-slug\n  Scenario: S\n    Given x")
        self.assertEqual(sc.id, "the-slug")
        self.assertEqual(sc.id_tags, ["the-slug"])

    def test_other_tags_are_left_alone(self):
        sc = self.parse("Feature: F\n  @slow\n  @id:the-slug\n  Scenario: S\n    Given x")
        self.assertEqual(sc.id, "the-slug")
        self.assertEqual(sc.tags, ["@slow", "@id:the-slug"])

    def test_missing_id_leaves_it_unset(self):
        sc = self.parse("Feature: F\n  Scenario: S\n    Given x")
        self.assertIsNone(sc.id)
        self.assertEqual(sc.id_tags, [])

    def test_malformed_id_is_recorded_but_not_accepted(self):
        sc = self.parse("Feature: F\n  @id:Not_A_Slug\n  Scenario: S\n    Given x")
        self.assertIsNone(sc.id)
        self.assertEqual(sc.id_tags, ["Not_A_Slug"])

    def test_two_ids_are_recorded_but_neither_is_accepted(self):
        sc = self.parse("Feature: F\n  @id:a\n  @id:b\n  Scenario: S\n    Given x")
        self.assertIsNone(sc.id)
        self.assertEqual(sc.id_tags, ["a", "b"])

    def test_ledger_reference_form(self):
        from vellum.gherkin_blocks import scenario_ref

        self.assertEqual(scenario_ref("the-slug"), "scenario:the-slug")



class TestScenarioCollection(unittest.TestCase):
    def setUp(self):
        self.suite = to_dict(extract(FIXTURES / "good"))
        self.by_id = {s["id"]: s for s in self.suite["scenarios"]}

    def test_every_scenario_in_the_tree_is_collected(self):
        self.assertEqual(self.suite["scenario_count"], 4)

    def test_scenarios_carry_id_ref_file_and_line(self):
        sc = self.by_id["auth-idle-session-expires"]
        self.assertEqual(sc["file"], "features/auth.md")
        self.assertEqual(sc["ref"], "scenario:auth-idle-session-expires")
        self.assertGreater(sc["line"], 1)

    def test_two_features_in_one_block_are_both_present(self):
        self.assertIn("auth-sign-out-clears-session", self.by_id)

    def test_no_scenario_carries_background_steps(self):
        # Backgrounds are banned, so a conforming tree has none.
        self.assertTrue(all(not s["background_steps"] for s in self.suite["scenarios"]))

    def test_scenario_outline_examples_are_captured(self):
        sc = self.by_id["auth-idle-threshold"]
        self.assertEqual(sc["keyword"], "Scenario Outline")
        self.assertEqual(sc["examples"][0]["header"], ["minutes", "outcome"])
        self.assertEqual(len(sc["examples"][0]["rows"]), 2)

    def test_tags_are_captured(self):
        self.assertEqual(
            self.by_id["auth-sign-out-clears-session"]["tags"],
            ["@slow", "@id:auth-sign-out-clears-session"],
        )

    def test_output_is_ordered_for_stable_diffs(self):
        keys = [(s["file"], s["line"]) for s in self.suite["scenarios"]]
        self.assertEqual(keys, sorted(keys))


class TestUnparseableBlocksAreRefused(unittest.TestCase):
    """A fence that does not parse fails the extraction (waviisoft/vellum#7).

    It used to be skipped, with lint left as the only thing that faulted it.
    That made the two commands disagree about the same tree, and the direction
    of the disagreement was the dangerous one: ``extract`` exited 0 and emitted
    a suite short exactly the scenarios nobody could see were missing.
    """

    FIXTURE = FIXTURES / "bad-gherkin-mixed"

    def broken_fence(self):
        """The fixture's failing fence, located by reading the file.

        Not written out as a line number here: a hard-coded fact about a
        fixture fails on every edit to it, which is noise rather than a defect.
        """
        text = (self.FIXTURE / "features" / "mixed.md").read_text(encoding="utf-8")
        return next(
            f
            for f in find_fences(text.split("\n"))
            if f.language == "gherkin" and "Unreadable" in f.body
        )

    def test_extract_raises_naming_the_file_and_the_block(self):
        with self.assertRaises(DroppedScenarios) as caught:
            extract(self.FIXTURE)
        errors = caught.exception.errors
        self.assertEqual([e.relpath for e in errors], ["features/mixed.md"])
        self.assertEqual(errors[0].block_line, self.broken_fence().start_line)
        self.assertEqual(errors[0].code, "GH001")
        # The fault is located inside the block it names, not at its opening.
        self.assertGreater(errors[0].line, errors[0].block_line)

    def test_the_cli_exits_1_and_says_which_block(self):
        # 1 exactly, not merely non-zero: 2 is what SpecTreeError and
        # LedgerError mean ("the path you gave me is not a spec tree"), so a
        # drift that started answering 2 here would tell a caller the wrong
        # thing about its own invocation and still pass a non-zero assertion.
        code, output = run_cli(["suite", "extract", str(self.FIXTURE), "-o", "-"])
        self.assertEqual(code, 1)
        self.assertIn("features/mixed.md", output)
        self.assertIn(f"line {self.broken_fence().start_line}", output)

    def test_a_refusal_writes_nothing_to_stdout(self):
        # `-o -` puts the suite on stdout, so a diagnostic printed there would
        # be read as part of the JSON — `extract … -o - | jq` would fail on a
        # tree it should merely have refused, or worse, parse the wrong thing.
        # Every word of a refusal is on stderr; stdout stays empty.
        code, out, err = run_cli_streams(
            ["suite", "extract", str(self.FIXTURE), "-o", "-"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("features/mixed.md", err)

    def test_no_suite_is_written_when_the_tree_is_refused(self):
        # There is no partial extraction: the good block in this fixture reads
        # cleanly, and emitting it alone is precisely the quietly-wrong suite
        # the refusal exists to stop.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "suite.json"
            code, _ = run_cli(["suite", "extract", str(self.FIXTURE), "-o", str(out)])
            self.assertEqual(code, 1)
            self.assertFalse(out.exists())

    def test_an_existing_output_file_is_left_exactly_as_it_was(self):
        # `on-spec-merge.yml` extracts over `ledger/suite-<sha>.json` on a main
        # that may already carry one. Refusing must leave the last good suite
        # in place: truncating it, or writing a short one over it, would take
        # the tree from "no answer" to "a wrong answer already committed".
        previous = '{"scenario_count": 22, "the": "last good suite"}\n'
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "suite.json"
            out.write_text(previous, encoding="utf-8")
            code, _ = run_cli(["suite", "extract", str(self.FIXTURE), "-o", str(out)])
            self.assertEqual(code, 1)
            self.assertEqual(out.read_text(encoding="utf-8"), previous)

    def test_lint_and_extract_now_agree_about_the_same_tree(self):
        # The defect was the disagreement itself, so it is asserted as one.
        self.assertEqual(
            [f.code for f in lint_tree(self.FIXTURE) if f.code == "GH001"], ["GH001"]
        )
        self.assertEqual(run_cli(["lint", str(self.FIXTURE)])[0], 1)
        self.assertEqual(
            run_cli(["suite", "extract", str(self.FIXTURE), "-o", "-"])[0], 1
        )

    def test_every_failing_block_is_reported_not_just_the_first(self):
        # bad-gherkin holds two unparseable fences; one run names both, so a
        # repair does not have to be discovered one command at a time.
        with self.assertRaises(DroppedScenarios) as caught:
            extract(FIXTURES / "bad-gherkin")
        self.assertEqual(len(caught.exception.errors), 2)


class TestRuleNestedScenariosAreRefused(unittest.TestCase):
    """The other silent drop: a `Rule:` parses cleanly and still costs scenarios.

    ``parse_block`` does not descend into a Rule — the construct is banned
    (``spec/decisions/2026-08-28-no-rules.md``) — so a stock Cucumber runner
    executes its nested scenarios and ``suite.json`` does not describe them.
    That is the same harm as an unparseable fence, reached the other way, and
    it is the class the ban was raised for (waviisoft/vellum-intent#16). The
    refusal is keyed on the harm, not on ``GherkinParseError``.
    """

    FIXTURE = FIXTURES / "bad-rules"

    def errors(self):
        with self.assertRaises(DroppedScenarios) as caught:
            extract(self.FIXTURE)
        return caught.exception.errors

    def test_extract_refuses_a_tree_that_nests_scenarios_under_a_rule(self):
        errors = self.errors()
        self.assertEqual(
            sorted(e.relpath for e in errors),
            ["features/absorbs-what-follows.md", "features/nested.md"],
        )
        self.assertEqual({e.code for e in errors}, {"GH010"})

    def test_the_refusal_names_how_many_scenarios_would_be_omitted(self):
        # The count is what makes this a refusal rather than a style rule, and
        # it is the same count GH010 reports, from the same field.
        nested = next(e for e in self.errors() if e.relpath == "features/nested.md")
        self.assertIn("2 scenario(s)", nested.message)
        self.assertIn("Only admins may delete", nested.message)
        self.assertIn("omitted", nested.message)

    def test_the_refusal_points_at_the_rule_inside_the_block_it_names(self):
        nested = next(e for e in self.errors() if e.relpath == "features/nested.md")
        rule_line = next(
            f.line
            for f in lint_tree(self.FIXTURE)
            if f.code == "GH010" and f.file == "features/nested.md"
        )
        self.assertEqual(nested.line, rule_line)
        self.assertGreater(nested.line, nested.block_line)

    def test_the_cli_exits_1_and_sends_the_reader_to_gh010(self):
        code, output = run_cli(["suite", "extract", str(self.FIXTURE), "-o", "-"])
        self.assertEqual(code, 1)
        self.assertIn("features/nested.md", output)
        self.assertIn("GH010", output)

    def test_lint_and_extract_agree_about_this_tree_too(self):
        self.assertEqual(run_cli(["lint", str(self.FIXTURE)])[0], 1)
        self.assertEqual(
            run_cli(["suite", "extract", str(self.FIXTURE), "-o", "-"])[0], 1
        )

    def test_a_rule_holding_nothing_does_not_refuse(self):
        # Where the line is drawn. An empty Rule fails lint on its own account
        # (GH010, and GH002 besides) and costs the suite nothing, so extraction
        # has no business refusing it: the suite it would emit is complete.
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_tree(Path(tmp), {"empty-rule": RULE_EMPTY, "good": ONE})
            # Lint still rejects the tree; extraction still answers. That the
            # two disagree here is the point, not an oversight.
            self.assertEqual(
                sorted({f.code for f in lint_tree(tree)}), ["GH002", "GH010"]
            )
            suite = extract(tree)
            self.assertEqual([e.id for e in suite.entries], ["login-good-password"])

    def test_a_rule_written_in_prose_does_not_refuse(self):
        # The negative control GH007 and GH009 both paid for: detection reads
        # the parsed node, so a `Rule:` inside a docstring is literal text and
        # one inside a step's text is prose, and neither may cost a tree its
        # extraction. The block is read out of the fixture lint uses rather
        # than restated here, so the two controls cannot drift apart.
        source = FIXTURES / "bad-rules" / "features" / "mentions-a-rule.md"
        block = next(
            f.body
            for f in find_fences(source.read_text(encoding="utf-8").split("\n"))
            if f.language == "gherkin"
        )
        with tempfile.TemporaryDirectory() as tmp:
            ids = {e.id for e in extract(make_tree(Path(tmp), {"prose": block})).entries}
        self.assertEqual(
            ids,
            {"rules-in-a-docstring-is-not-a-rule", "rules-in-step-text-is-not-a-rule"},
        )


class TestFindingsThatDropNothingStillExtract(unittest.TestCase):
    """Extraction refuses a short suite, not a lint-rejected tree.

    Those are different sets, and conflating them would make ``extract`` a
    second ``lint`` — a tree whose only defect is a broken link or a missing
    ``@id:`` yields a suite describing every scenario in it, and refusing to
    hand that over stops the harness for a reason that costs the suite nothing.
    The rule is the drop, and only the drop.
    """

    def extracts(self, fixture):
        code, _ = run_cli(["suite", "extract", str(fixture), "-o", "-"])
        return code

    def test_a_tree_lint_rejects_for_its_links_still_extracts(self):
        self.assertEqual(run_cli(["lint", str(FIXTURES / "bad-links")])[0], 1)
        self.assertEqual(self.extracts(FIXTURES / "bad-links"), 0)

    def test_a_tree_lint_rejects_for_its_ids_still_extracts(self):
        # GH003/GH005/GH006. An id defect is a defect in how a scenario is
        # referred to, not in whether the suite carries it.
        self.assertEqual(run_cli(["lint", str(FIXTURES / "bad-ids")])[0], 1)
        self.assertEqual(self.extracts(FIXTURES / "bad-ids"), 0)
        self.assertEqual(len(extract(FIXTURES / "bad-ids").entries), 5)

    def test_a_fence_declaring_no_scenarios_still_extracts(self):
        # GH002. A block with nothing in it drops nothing: there is no scenario
        # a runner would execute that the suite fails to describe.
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_tree(Path(tmp), {"empty": NO_SCENARIOS, "good": ONE})
            self.assertEqual(
                [f.code for f in lint_tree(tree) if f.code == "GH002"], ["GH002"]
            )
            self.assertEqual([e.id for e in extract(tree).entries],
                             ["login-good-password"])


class TestRefusalAcrossFiles(unittest.TestCase):
    """Every dropping block in the tree is found, wherever in the walk it sits.

    ``extract`` scans the whole tree before it judges any of it, so one run
    names the entire repair list. The three shapes below are the ones a walk
    that stopped early, or lost a file's failures behind an empty scenario
    list, would each pass anyway.
    """

    def refuse(self, blocks):
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_tree(Path(tmp), blocks)
            with self.assertRaises(DroppedScenarios) as caught:
                extract(tree)
            return caught.exception.errors

    def test_bad_blocks_in_several_files_are_all_reported(self):
        errors = self.refuse({"a-bad": BROKEN, "b-good": ONE, "c-bad": RULE_NESTED})
        self.assertEqual(
            [e.relpath for e in errors],
            ["features/a-bad.md", "features/c-bad.md"],
        )
        # Both kinds land in the one list, and the summary names both codes.
        self.assertEqual([e.code for e in errors], ["GH001", "GH010"])

    def test_a_bad_block_in_the_last_file_scanned_is_not_lost(self):
        # `iter_spec_files` sorts by path, so `features/z-bad.md` is scanned
        # last. A loop that judged as it went and returned on the first clean
        # file would still be green on every other shape here.
        errors = self.refuse({"a-good": ONE, "z-bad": BROKEN})
        self.assertEqual([e.relpath for e in errors], ["features/z-bad.md"])

    def test_a_file_with_no_good_block_at_all_is_reported(self):
        # The file contributes zero scenarios, so its failures are the only
        # thing it hands back — and the good file's scenarios are still not
        # emitted, because there is no partial extraction.
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_tree(Path(tmp), {"a-good": ONE, "b-nothing-good": BROKEN})
            out = Path(tmp) / "suite.json"
            code, _ = run_cli(["suite", "extract", str(tree), "-o", str(out)])
            self.assertEqual(code, 1)
            self.assertFalse(out.exists())
            with self.assertRaises(DroppedScenarios) as caught:
                extract(tree)
        self.assertEqual(
            [e.relpath for e in caught.exception.errors], ["features/b-nothing-good.md"]
        )


class TestHistoryStillToleratesABrokenBlock(unittest.TestCase):
    """Dating re-parses old trees, and refusing there would be a different rule.

    ``version_history`` reads every spec-touching commit in the checkout's
    ancestry through ``scenarios_in``. A fence that failed to parse at some past
    commit is a tree nobody can go back and fix, so one bad commit would
    otherwise make every descendant of it permanently unextractable. Extraction
    refuses the tree it was pointed at; the walk behind it keeps skipping.
    """

    def test_a_broken_block_in_an_earlier_commit_does_not_fail_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_spec_repo(Path(tmp))
            commit_area(repo, BROKEN)
            good = commit_area(repo, ONE)
            suite = extract(repo / "spec")
            self.assertEqual([e.id for e in suite.entries], ["login-good-password"])
            self.assertEqual(suite.entries[0].version, good)
            self.assertFalse(suite.entries[0].pending)


class TestFenceInfoStrings(unittest.TestCase):
    """A fence's language is its info string's first word (waviisoft/vellum#10).

    The last "exit 0 while omitting a scenario" hole, and it was reached from
    the markdown side rather than the Gherkin side: a two-word info string was
    not recognised as opening a fence, so the block's own closing line opened a
    phantom one that ran to the *next* block's opener. Two gherkin blocks
    became one language-less fence, extraction found nothing to collect, and
    exited 0 — with lint silent for the same reason, since both read the one
    ``SpecFile.fences``.
    """

    REPRO = (
        "```gherkin title=demo\n"
        "Feature: First\n"
        "  @id:attributed-fence-first\n"
        "  Scenario: The first one\n"
        "    Given a thing\n"
        "    Then it worked\n"
        "```\n"
        "\n"
        "Prose between the blocks.\n"
        "\n"
        "```gherkin\n"
        "Feature: Second\n"
        "  @id:attributed-fence-second\n"
        "  Scenario: The second one\n"
        "    Given another thing\n"
        "    Then it worked\n"
        "```"
    )

    def ids_from(self, bodies):
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_raw_tree(Path(tmp), bodies)
            return [sc["id"] for sc in to_dict(extract(tree))["scenarios"]]

    def test_the_repro_extracts_both_scenarios(self):
        self.assertEqual(
            self.ids_from({"demo": self.REPRO}),
            ["attributed-fence-first", "attributed-fence-second"],
        )

    def test_a_non_gherkin_fence_with_attributes_is_skipped_without_desync(self):
        # `python foo=bar` contributes nothing and must cost nothing: the
        # gherkin block below it used to be swallowed by the phantom fence its
        # closing line opened.
        body = (
            "```python foo=bar\n"
            "print('not gherkin')\n"
            "```\n"
            "\n"
            "```gherkin\n"
            "Feature: Still visible\n"
            "  @id:after-an-attributed-python-fence\n"
            "  Scenario: Still extracted\n"
            "    Given a thing\n"
            "    Then it worked\n"
            "```"
        )
        self.assertEqual(
            self.ids_from({"demo": body}), ["after-an-attributed-python-fence"]
        )

    def test_a_dropping_construct_in_an_attributed_fence_still_refuses(self):
        # The reason the desync mattered, stated as the invariant it broke:
        # extraction refuses any scenario-dropping construct. A fence nobody
        # parsed could not be refused, so the strictest rule in the CLI had a
        # markdown-shaped way around it — write `title=` on the fence and a
        # `Rule:` inside it, and the suite comes back short at exit 0.
        body = (
            "```gherkin title=demo\n"
            "Feature: Smuggled\n"
            "  Rule: A rule holding scenarios\n"
            "    @id:nested-under-a-rule\n"
            "    Scenario: Nobody sees me\n"
            "      Given a thing\n"
            "      Then it worked\n"
            "```"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_raw_tree(Path(tmp), {"demo": body})
            with self.assertRaises(DroppedScenarios) as caught:
                extract(tree)
            self.assertEqual([e.code for e in caught.exception.errors], ["GH010"])
            code, out, err = run_cli_streams(["suite", "extract", str(tree), "-o", "-"])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("GH010", err)


class TestFingerprint(unittest.TestCase):
    """"Changed" is the fingerprint: normalized steps and example tables only."""

    def fp(self, block):
        return fingerprint(scenarios_in("a.md", wrap(block))[0])

    def test_moving_a_scenario_does_not_change_its_fingerprint(self):
        a = scenarios_in("a.md", wrap(ONE))[0]
        b = scenarios_in("a.md", "\n\nprose\n\n" + wrap(ONE))[0]
        self.assertNotEqual(a.line, b.line)
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_changing_a_step_changes_the_fingerprint(self):
        self.assertNotEqual(self.fp(ONE), self.fp(TWO_CHANGED))

    def test_renaming_a_scenario_does_not_change_it(self):
        # Titles are presentation.
        self.assertEqual(self.fp(ONE), self.fp(ONE.replace("Good password", "Correct password")))

    def test_adding_a_tag_does_not_change_it(self):
        # Tags are presentation — which is what lets spec-v2 add @id: tags to
        # all nineteen scenarios without bumping any of their versions.
        self.assertEqual(self.fp(TWO_WITHOUT_IDS), self.fp(TWO))

    def test_rewriting_and_as_the_keyword_above_it_does_not_change_it(self):
        conjunction = "Feature: F\n  @id:x\n  Scenario: S\n    Given a\n    And b"
        spelled_out = "Feature: F\n  @id:x\n  Scenario: S\n    Given a\n    Given b"
        self.assertEqual(self.fp(conjunction), self.fp(spelled_out))

    def test_changing_a_steps_kind_does_change_it(self):
        given = "Feature: F\n  @id:x\n  Scenario: S\n    Given a\n    Given b"
        when = "Feature: F\n  @id:x\n  Scenario: S\n    Given a\n    When b"
        self.assertNotEqual(self.fp(given), self.fp(when))

    def test_reindenting_a_step_does_not_change_it(self):
        tight = "Feature: F\n  @id:x\n  Scenario: S\n    Given a user"
        loose = "Feature: F\n  @id:x\n  Scenario: S\n    Given a    user"
        self.assertEqual(self.fp(tight), self.fp(loose))

    def test_background_steps_would_count_toward_the_fingerprint(self):
        # Backgrounds are banned, so this cannot arise in a conforming tree.
        # The decision that banned them records what must hold if the ban is
        # ever lifted — a Background edit bumps every scenario in the feature —
        # and rejects the opposite reading outright. Pinned so a later
        # relaxation cannot quietly implement the rejected one.
        from vellum.suite import fingerprint

        base = scenarios_in("a.md", wrap(ONE))[0]
        with_background = scenarios_in("a.md", wrap(ONE))[0]
        with_background.background_steps = [
            Step(keyword="Given", text="the reference environment", keyword_type="Context")
        ]
        self.assertNotEqual(fingerprint(base), fingerprint(with_background))

    def test_changing_an_example_row_does_change_it(self):
        base = ("Feature: F\n  @id:x\n  Scenario Outline: S\n    Given <n>\n\n"
                "    Examples:\n      | n |\n      | 1 |")
        self.assertNotEqual(self.fp(base), self.fp(base.replace("| 1 |", "| 2 |")))

def wrap(block):
    return f"---\nid: a\ntitle: A\nsince: spec-v1\n---\n\n```gherkin\n{block}\n```\n"


class TestVersionDerivation(unittest.TestCase):
    """Each scenario carries the commit that introduced or last changed it.

    A version is a main commit whose diff touches the spec tree
    (``spec/decisions/2026-08-28-versions-are-commits.md``), so these assert
    against the shas ``commit_area`` returns. Where a test names a ``spec-v*``
    tag it is checking the decoration itself; nothing here dates by one.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_spec_repo(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def versions(self):
        return {
            (e.scenario.id or e.scenario.name): (e.version, e.pending)
            for e in extract(self.repo / "spec").entries
        }

    def test_a_committed_tree_dates_from_its_commit(self):
        # No tag anywhere, and the scenario is still dated and not pending:
        # the commit is the version, so there is nothing left to be missing.
        first = commit_area(self.repo, ONE)
        self.assertEqual(self.versions(), {"login-good-password": (first, False)})

    def test_scenario_keeps_the_version_that_introduced_it(self):
        first = commit_area(self.repo, ONE)
        commit_area(self.repo, TWO)
        self.assertEqual(self.versions()["login-good-password"], (first, False))

    def test_scenario_added_later_carries_the_later_version(self):
        commit_area(self.repo, ONE)
        second = commit_area(self.repo, TWO)
        self.assertEqual(self.versions()["login-bad-password"], (second, False))

    def test_changed_scenario_advances_to_the_version_that_changed_it(self):
        commit_area(self.repo, ONE)
        second = commit_area(self.repo, TWO)
        third = commit_area(self.repo, TWO_CHANGED)
        found = self.versions()
        self.assertEqual(found["login-good-password"], (third, False))
        self.assertEqual(found["login-bad-password"], (second, False))

    def test_an_uncommitted_scenario_is_pending_with_no_version(self):
        # Spec CI on a working tree: the version this will belong to has no sha
        # yet, so there is nothing honest to report but None. Under tags this
        # said "latest + 1"; an integer could be predicted and a sha cannot.
        commit_area(self.repo, ONE)
        commit_area(self.repo, TWO)
        write_area(
            self.repo,
            TWO + "\n\n  @id:login-locked-out\n  Scenario: Locked out\n"
                  "    Given five failures\n    Then they are locked",
        )
        self.assertEqual(self.versions()["login-locked-out"], (None, True))

    def test_committing_that_scenario_dates_it_at_that_commit(self):
        # The other half of the pending case, and the reason pending shrank:
        # any committed spec change is itself a version, so it needs no tag to
        # become datable.
        commit_area(self.repo, ONE)
        third = commit_area(self.repo, TWO)
        self.assertEqual(self.versions()["login-bad-password"], (third, False))

    def test_a_commit_that_does_not_touch_the_spec_tree_is_not_a_version(self):
        first = commit_area(self.repo, ONE)
        commit_elsewhere(self.repo)
        # The scenario is still dated at the spec commit, not at the head.
        self.assertEqual(self.versions()["login-good-password"], (first, False))

    def test_dating_reads_the_checkout_ancestry_not_every_ref_in_the_repo(self):
        # The tag walker read "every spec-v* tag present", so a repo that had
        # moved ahead of the checkout dated scenarios by tags the checkout did
        # not contain — which is what failed the conformance check on main
        # (waviisoft/vellum#4). Ancestry cannot: a commit that is not an
        # ancestor of HEAD is not a version this tree has.
        first = commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO_CHANGED, "spec-v2")
        git(self.repo, "checkout", "-q", first)
        suite = extract(self.repo / "spec")
        self.assertEqual(suite.spec_version, first)
        self.assertEqual(suite.spec_head, first)
        self.assertEqual({e.version for e in suite.entries}, {first})
        self.assertFalse(any(e.pending for e in suite.entries))

    def test_a_version_is_the_same_read_from_any_descendant(self):
        # `@id:scenario-version-tagging` says the suite may be extracted "at
        # that version or any descendant" and the scenario still carries the
        # commit that introduced it. Asserted across every later version rather
        # than just the next one.
        first = commit_area(self.repo, ONE)
        later = [commit_area(self.repo, TWO), commit_area(self.repo, TWO_CHANGED)]
        later.append(commit_elsewhere(self.repo))
        for descendant in later:
            git(self.repo, "checkout", "-q", descendant)
            found = {
                (e.scenario.id): e.version
                for e in extract(self.repo / "spec").entries
            }
            self.assertEqual(
                found["login-bad-password"], later[0], f"read from {descendant[:8]}"
            )
        # And at the introducing version itself.
        git(self.repo, "checkout", "-q", first)
        self.assertEqual(self.versions()["login-good-password"], (first, False))

    def test_renaming_a_scenario_keeps_its_version(self):
        first = commit_area(self.repo, ONE)
        commit_area(self.repo, ONE.replace("Good password", "Correct password"))
        self.assertEqual(self.versions()["login-good-password"], (first, False))

    def test_giving_an_existing_scenario_an_id_keeps_its_version(self):
        # The spec-v1 -> spec-v2 migration: nineteen scenarios gained @id: tags
        # and none of them changed. Matching falls back to the fingerprint.
        first = commit_area(self.repo, TWO_WITHOUT_IDS)
        commit_area(self.repo, TWO)
        self.assertEqual(
            self.versions(),
            {"login-good-password": (first, False), "login-bad-password": (first, False)},
        )

    def test_identical_content_under_different_ids_does_not_cross_assign(self):
        # Two scenarios with the same steps; only one of them changes.
        both = ("Feature: F\n  @id:alpha\n  Scenario: A\n    Given a\n    Then b\n\n"
                "  @id:beta\n  Scenario: B\n    Given a\n    Then b")
        first = commit_area(self.repo, both)
        second = commit_area(
            self.repo, both.replace("Then b\n\n  @id:beta", "Then c\n\n  @id:beta")
        )
        self.assertEqual(self.versions(), {"alpha": (second, False), "beta": (first, False)})

    def test_renaming_an_id_over_unchanged_content_keeps_the_version(self):
        # Consequence of the spec's own rule: "changed" is the fingerprint, and
        # this content was already specified at the first commit, so nothing is
        # re-armed.
        one = "Feature: F\n  @id:one\n  Scenario: S\n    Given a\n    Then b"
        first = commit_area(self.repo, one)
        commit_area(self.repo, one.replace("@id:one", "@id:two"))
        self.assertEqual(self.versions(), {"two": (first, False)})

    def test_the_earliest_version_wins_among_identical_content(self):
        # Shas do not compare, so "earliest" is ancestry rank. Dating a
        # fallback match too early leaves it enforced; too late would arm a
        # scenario the product already satisfies.
        one = "Feature: F\n  @id:one\n  Scenario: S\n    Given a\n    Then b"
        first = commit_area(self.repo, one)
        commit_area(
            self.repo,
            one + "\n\n  @id:two\n  Scenario: T\n    Given a\n    Then b",
        )
        commit_area(self.repo, one.replace("@id:one", "@id:three"))
        self.assertEqual(self.versions()["three"], (first, False))

    def test_a_scenario_moving_between_files_keeps_its_version(self):
        # Identity is the id; the file is only its current home.
        first = commit_area(self.repo, TWO)
        second = self.repo / "spec" / "features" / "moved.md"
        from support import AREA_TEMPLATE

        block_one, _, block_two = TWO.partition("\n\n  @id:login-bad-password")
        write_area(self.repo, block_one)
        second.write_text(
            AREA_TEMPLATE.format(block="Feature: Login\n  @id:login-bad-password" + block_two),
            encoding="utf-8",
        )
        self.addCleanup(second.unlink, missing_ok=True)
        found = self.versions()
        self.assertEqual(found["login-bad-password"], (first, False))

    def test_the_suite_is_extracted_at_the_checkouts_commit(self):
        head = commit_area(self.repo, ONE)
        suite = extract(self.repo / "spec")
        self.assertEqual(suite.spec_version, head)
        self.assertRegex(suite.spec_version, r"^[0-9a-f]{40}$")
        self.assertFalse(suite.shallow)

    def test_spec_head_is_the_newest_version_not_the_checkout_head(self):
        spec_change = commit_area(self.repo, ONE)
        other = commit_elsewhere(self.repo)
        suite = extract(self.repo / "spec")
        self.assertEqual(suite.spec_version, other)
        self.assertEqual(suite.spec_head, spec_change)

    def test_tree_outside_git_dates_nothing(self):
        # There is no base version to fall back to any more: a version is a
        # commit, and a tree with no history has none. Saying so beats
        # inventing one.
        with tempfile.TemporaryDirectory() as plain:
            root = Path(plain) / "spec"
            (root / "features").mkdir(parents=True)
            (root / "index.md").write_text(
                "---\nid: index\ntitle: I\nsince: spec-v1\n---\n\nfeatures/auth.md\n"
            )
            (root / "features" / "auth.md").write_text(wrap(ONE))
            suite = extract(root)
            self.assertIsNone(suite.spec_version)
            self.assertEqual([(e.version, e.pending) for e in suite.entries], [(None, True)])


class TestDecorativeNames(unittest.TestCase):
    """Names are reported, never read. A missing one changes no version."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_spec_repo(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def test_a_name_is_reported_alongside_the_sha(self):
        first = commit_area(self.repo, ONE, "spec-v1")
        suite = to_dict(extract(self.repo / "spec"))
        self.assertEqual(suite["spec_version"], first)
        self.assertEqual(suite["spec_version_name"], "spec-v1")
        self.assertEqual(suite["scenarios"][0]["version_name"], "spec-v1")

    def test_dropping_every_name_changes_no_version(self):
        # The tag-era failure mode, asserted absent: a missing, late or wrong
        # name breaks nothing, because nothing reads one.
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        with_names = to_dict(extract(self.repo / "spec"))
        git(self.repo, "tag", "-d", "spec-v1")
        git(self.repo, "tag", "-d", "spec-v2")
        without = to_dict(extract(self.repo / "spec"))
        self.assertEqual(
            [s["version"] for s in with_names["scenarios"]],
            [s["version"] for s in without["scenarios"]],
        )
        self.assertEqual(without["spec_version_name"], None)
        self.assertEqual(with_names["spec_version_name"], "spec-v2")

    def test_a_wrong_name_changes_no_version(self):
        # Out-of-order naming re-dated everything under the tag walker; here it
        # only mislabels the report.
        first = commit_area(self.repo, ONE, "spec-v9")
        second = commit_area(self.repo, TWO, "spec-v2")
        suite = to_dict(extract(self.repo / "spec"))
        by_id = {s["id"]: s for s in suite["scenarios"]}
        self.assertEqual(by_id["login-good-password"]["version"], first)
        self.assertEqual(by_id["login-bad-password"]["version"], second)


class TestShallowHistory(unittest.TestCase):
    """Truncated history is the one way ancestry dating still goes wrong."""

    def test_a_shallow_clone_is_reported_as_such(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = make_spec_repo(Path(tmp) / "origin")
            commit_area(origin, ONE)
            newest = commit_area(origin, TWO_CHANGED)
            clone = Path(tmp) / "clone"
            git(Path(tmp), "clone", "-q", "--depth", "1", f"file://{origin}", str(clone))
            suite = extract(clone / "spec")
            # The dating is wrong in the dangerous direction — the older
            # scenario re-dates forward to the graft — and nothing is pending,
            # so the flag is the only signal there is.
            self.assertTrue(suite.shallow)
            self.assertEqual({e.version for e in suite.entries}, {newest})
            self.assertFalse(any(e.pending for e in suite.entries))
            self.assertFalse(extract(origin / "spec").shallow)


class TestPinnedSpecTree(unittest.TestCase):
    """The definition of done: every scenario in the pinned tree, correctly dated.

    Nothing here hard-codes a count. A count is a fact about the pinned tree,
    so it goes stale on the next wave whose spec adds a scenario, exactly the
    way a hard-coded pin goes stale on the next advance — and a test that fails
    on every advance is noise that trains people to ignore red. The oracles in
    ``support`` read the tree instead.

    The tree comes from ``VELLUM_INTENT_REPO`` (or a ``./spec`` mount): the pin
    of record is a file now and nothing checks the intent repo out for us.
    """

    def setUp(self):
        self.tree = intent_spec_tree()
        if self.tree is None:
            self.skipTest("no intent checkout (set VELLUM_INTENT_REPO)")
        self.suite = to_dict(extract(intent_checkout()))

    def test_every_scenario_in_the_tree_is_extracted(self):
        # Asserted non-zero first: an oracle that silently found nothing would
        # otherwise agree with an extractor that silently found nothing.
        self.assertGreater(pinned_scenario_count(self.tree), 0)
        self.assertEqual(
            self.suite["scenario_count"], pinned_scenario_count(self.tree)
        )

    def test_every_version_is_an_ancestor_of_the_pin(self):
        # "Dated past the pin" is an ancestry question now. A version that is
        # not an ancestor of the checkout could not have been read from it.
        pin = pinned_commit()
        for s in self.suite["scenarios"]:
            self.assertIsNotNone(s["version"], s["id"])
            self.assertEqual(
                0,
                __import__("subprocess").run(
                    ["git", "-C", str(intent_checkout()), "merge-base",
                     "--is-ancestor", s["version"], pin],
                ).returncode,
                f"{s['id']} is dated at {s['version'][:12]}, not an ancestor of the pin",
            )

    def test_the_oldest_scenarios_are_still_at_the_seed_version(self):
        # spec-v2 only added @id: tags and spec-v4 only re-fenced a block, both
        # presentation changes the fingerprint deliberately ignores. So the
        # seed commit must still be the version of something.
        versions = {s["version"] for s in self.suite["scenarios"]}
        seed = __import__("subprocess").run(
            ["git", "-C", str(intent_checkout()), "rev-list", "--first-parent",
             "--reverse", pinned_commit(), "--", "spec"],
            capture_output=True, text=True, check=True,
        ).stdout.split("\n")[0].strip()
        self.assertIn(seed, versions)

    def test_re_fencing_a_block_did_not_re_date_the_scenarios_in_it(self):
        # spec-v4 split certification-and-releases.md into two fences, and the
        # decision behind it claims that "splitting a fence moves no version
        # and re-dates no scenario". This is that claim, asserted. It is also
        # the guard on the splitter: reading those older *commits* is the only
        # reason the three scenarios here are still dated at the seed rather
        # than at the re-fencing, and the failure if it goes is silent — right
        # count, nothing pending.
        seed = __import__("subprocess").run(
            ["git", "-C", str(intent_checkout()), "rev-list", "--first-parent",
             "--reverse", pinned_commit(), "--", "spec"],
            capture_output=True, text=True, check=True,
        ).stdout.split("\n")[0].strip()
        refenced = {
            s["version"]
            for s in self.suite["scenarios"]
            if s["file"] == "features/certification-and-releases.md"
        }
        self.assertEqual(refenced, {seed})

    def test_the_suite_is_extracted_at_the_pin(self):
        self.assertEqual(self.suite["spec_version"], pinned_commit())
        self.assertFalse(any(s["pending"] for s in self.suite["scenarios"]))
        self.assertFalse(self.suite["shallow"])

    def test_every_scenario_has_a_unique_id_and_a_ledger_ref(self):
        ids = [s["id"] for s in self.suite["scenarios"]]
        self.assertTrue(all(ids))
        self.assertEqual(len(set(ids)), self.suite["scenario_count"])
        for s in self.suite["scenarios"]:
            self.assertEqual(s["ref"], f"scenario:{s['id']}")

    def test_every_spec_file_with_a_gherkin_block_is_represented(self):
        self.assertGreater(pinned_gherkin_file_count(self.tree), 0)
        self.assertEqual(
            len({s["file"] for s in self.suite["scenarios"]}),
            pinned_gherkin_file_count(self.tree),
        )

    def test_suite_is_json_serialisable(self):
        json.loads(json.dumps(self.suite))


if __name__ == "__main__":
    unittest.main()
