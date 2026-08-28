"""``vellum suite extract``: scenario collection and version derivation."""

import json
import tempfile
import unittest
from pathlib import Path

from support import (
    FIXTURES,
    REPO_ROOT,
    commit_area,
    make_spec_repo,
    pinned_version,
    write_area,
)
from vellum.gherkin_blocks import Step, parse_block, split_documents
from vellum.suite import extract, fingerprint, scenarios_in, to_dict

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

#: The same two scenarios as TWO, before ids existed — the spec-v1 shape.
TWO_WITHOUT_IDS = "\n".join(
    line for line in TWO.split("\n") if not line.strip().startswith("@id:")
)


class TestBlockSplitting(unittest.TestCase):
    """The spec bans two Features in one fence (spec-v4); the splitter stays so
    lint reports the banned shape (GH009) and extraction survives it."""

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

    def test_unparseable_blocks_are_skipped_not_raised(self):
        # lint is where a broken block fails a run; extraction still describes
        # the rest of the tree.
        suite = extract(FIXTURES / "bad-gherkin")
        self.assertEqual(suite.entries, [])


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
    """Each scenario carries the version that introduced or last changed it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_spec_repo(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def versions(self):
        return {
            (e.scenario.id or e.scenario.name): (e.version, e.pending)
            for e in extract(self.repo / "spec").entries
        }

    def test_untagged_tree_is_version_one(self):
        commit_area(self.repo, ONE)
        self.assertEqual(self.versions(), {"login-good-password": (1, True)})

    def test_scenario_keeps_the_version_that_introduced_it(self):
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        self.assertEqual(self.versions()["login-good-password"], (1, False))

    def test_scenario_added_later_carries_the_later_version(self):
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        self.assertEqual(self.versions()["login-bad-password"], (2, False))

    def test_changed_scenario_advances_to_the_version_that_changed_it(self):
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        commit_area(self.repo, TWO_CHANGED, "spec-v3")
        found = self.versions()
        self.assertEqual(found["login-good-password"], (3, False))
        self.assertEqual(found["login-bad-password"], (2, False))

    def test_untagged_working_tree_scenario_takes_the_version_it_would_mint(self):
        # This is spec CI on a PR: the tag does not exist yet.
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        write_area(
            self.repo,
            TWO + "\n\n  @id:login-locked-out\n  Scenario: Locked out\n"
                  "    Given five failures\n    Then they are locked",
        )
        self.assertEqual(self.versions()["login-locked-out"], (3, True))

    def test_renaming_a_scenario_keeps_its_version(self):
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, ONE.replace("Good password", "Correct password"), "spec-v2")
        self.assertEqual(self.versions()["login-good-password"], (1, False))

    def test_giving_an_existing_scenario_an_id_keeps_its_version(self):
        # The spec-v1 -> spec-v2 migration: nineteen scenarios gained @id: tags
        # and none of them changed. Matching falls back to the fingerprint.
        commit_area(self.repo, TWO_WITHOUT_IDS, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        self.assertEqual(
            self.versions(),
            {"login-good-password": (1, False), "login-bad-password": (1, False)},
        )

    def test_identical_content_under_different_ids_does_not_cross_assign(self):
        # Two scenarios with the same steps; only one of them changes.
        both = ("Feature: F\n  @id:alpha\n  Scenario: A\n    Given a\n    Then b\n\n"
                "  @id:beta\n  Scenario: B\n    Given a\n    Then b")
        commit_area(self.repo, both, "spec-v1")
        commit_area(self.repo, both.replace("Then b\n\n  @id:beta", "Then c\n\n  @id:beta"), "spec-v2")
        self.assertEqual(self.versions(), {"alpha": (2, False), "beta": (1, False)})

    def test_renaming_an_id_over_unchanged_content_keeps_the_version(self):
        # Consequence of the spec's own rule: "changed" is the fingerprint, and
        # this content was already specified at spec-v1, so nothing is re-armed.
        one = "Feature: F\n  @id:one\n  Scenario: S\n    Given a\n    Then b"
        commit_area(self.repo, one, "spec-v1")
        commit_area(self.repo, one.replace("@id:one", "@id:two"), "spec-v2")
        self.assertEqual(self.versions(), {"two": (1, False)})

    def test_a_tree_that_did_not_exist_at_an_older_tag_is_dated_correctly(self):
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        self.assertEqual(self.versions()["login-bad-password"], (2, False))

    def test_a_scenario_moving_between_files_keeps_its_version(self):
        # Identity is the id; the file is only its current home.
        commit_area(self.repo, TWO, "spec-v1")
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
        self.assertEqual(found["login-bad-password"], (1, False))

    def test_suite_records_the_commit_it_was_extracted_from(self):
        commit_area(self.repo, ONE, "spec-v1")
        suite = extract(self.repo / "spec")
        self.assertTrue(suite.tagged)
        self.assertRegex(suite.source_commit, r"^[0-9a-f]{40}$")

    def test_tree_outside_git_falls_back_to_the_base_version(self):
        with tempfile.TemporaryDirectory() as plain:
            root = Path(plain) / "spec"
            (root / "features").mkdir(parents=True)
            (root / "index.md").write_text(
                "---\nid: index\ntitle: I\nsince: spec-v1\n---\n\nfeatures/auth.md\n"
            )
            (root / "features" / "auth.md").write_text(wrap(ONE))
            self.assertEqual([e.version for e in extract(root).entries], [1])


class TestPinnedSpecTree(unittest.TestCase):
    """The definition of done: every scenario in the pinned tree, at version 1."""

    def setUp(self):
        if not (REPO_ROOT / "spec" / "spec" / "index.md").is_file():
            self.skipTest("spec submodule is not checked out")
        self.suite = to_dict(extract(REPO_ROOT / "spec"))

    def test_all_nineteen_scenarios_are_extracted(self):
        self.assertEqual(self.suite["scenario_count"], 19)

    def test_every_scenario_is_version_one(self):
        # spec-v2 only added @id: tags, a presentation change.
        self.assertEqual({s["version"] for s in self.suite["scenarios"]}, {1})

    def test_the_suite_is_extracted_at_the_pinned_version(self):
        self.assertEqual(self.suite["spec_version"], pinned_version())
        self.assertFalse(any(s["pending"] for s in self.suite["scenarios"]))

    def test_every_scenario_has_a_unique_id_and_a_ledger_ref(self):
        ids = [s["id"] for s in self.suite["scenarios"]]
        self.assertTrue(all(ids))
        self.assertEqual(len(set(ids)), 19)
        for s in self.suite["scenarios"]:
            self.assertEqual(s["ref"], f"scenario:{s['id']}")

    def test_every_spec_file_with_a_gherkin_block_is_represented(self):
        self.assertEqual(len({s["file"] for s in self.suite["scenarios"]}), 11)

    def test_suite_is_json_serialisable(self):
        json.loads(json.dumps(self.suite))


if __name__ == "__main__":
    unittest.main()
