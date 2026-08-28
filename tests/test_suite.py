"""``vellum suite extract``: scenario collection and version derivation."""

import json
import tempfile
import unittest
from pathlib import Path

from support import FIXTURES, REPO_ROOT, commit_area, make_spec_repo, write_area
from vellum.gherkin_blocks import parse_block, split_documents
from vellum.suite import extract, fingerprint, scenarios_in, to_dict

ONE = """Feature: Login
  Scenario: Good password
    Given a user
    When they sign in
    Then they see the dashboard"""

TWO = ONE + """

  Scenario: Bad password
    Given a user
    When they mistype
    Then they see an error"""

TWO_CHANGED = TWO.replace("Then they see the dashboard", "Then they see the dashboard in 400ms")


class TestBlockSplitting(unittest.TestCase):
    """The spec tree puts two Features in one fence; Gherkin allows one per document."""

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
        self.assertEqual([s.feature for s in parse_block(block, 1)], ["A", "B"])



class TestScenarioCollection(unittest.TestCase):
    def setUp(self):
        self.suite = to_dict(extract(FIXTURES / "good"))
        self.by_anchor = {s["anchor"]: s for s in self.suite["scenarios"]}

    def test_every_scenario_in_the_tree_is_collected(self):
        self.assertEqual(self.suite["scenario_count"], 4)

    def test_scenarios_carry_file_anchor_and_line(self):
        sc = self.by_anchor["session-expiry/idle-session-expires"]
        self.assertEqual(sc["file"], "features/auth.md")
        self.assertEqual(sc["id"], "features/auth.md#session-expiry/idle-session-expires")
        self.assertGreater(sc["line"], 1)

    def test_two_features_in_one_block_are_both_present(self):
        self.assertIn("sign-out/sign-out-clears-the-session", self.by_anchor)

    def test_background_steps_ride_with_each_scenario(self):
        sc = self.by_anchor["session-expiry/idle-session-expires"]
        self.assertEqual([s["text"] for s in sc["background_steps"]], ["the reference environment"])

    def test_scenario_outline_examples_are_captured(self):
        sc = self.by_anchor["session-expiry/idle-threshold"]
        self.assertEqual(sc["keyword"], "Scenario Outline")
        self.assertEqual(sc["examples"][0]["header"], ["minutes", "outcome"])
        self.assertEqual(len(sc["examples"][0]["rows"]), 2)

    def test_tags_are_captured(self):
        self.assertEqual(self.by_anchor["sign-out/sign-out-clears-the-session"]["tags"], ["@slow"])

    def test_output_is_ordered_for_stable_diffs(self):
        keys = [(s["file"], s["line"]) for s in self.suite["scenarios"]]
        self.assertEqual(keys, sorted(keys))

    def test_unparseable_blocks_are_skipped_not_raised(self):
        # lint is where a broken block fails a run; extraction still describes
        # the rest of the tree.
        suite = extract(FIXTURES / "bad-gherkin")
        self.assertEqual(suite.entries, [])


class TestFingerprint(unittest.TestCase):
    def test_moving_a_scenario_does_not_change_its_fingerprint(self):
        a = scenarios_in("a.md", wrap(ONE))[0]
        b = scenarios_in("a.md", "\n\nprose\n\n" + wrap(ONE))[0]
        self.assertNotEqual(a.line, b.line)
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_changing_a_step_changes_the_fingerprint(self):
        a = scenarios_in("a.md", wrap(ONE))[0]
        b = scenarios_in("a.md", wrap(TWO_CHANGED))[0]
        self.assertNotEqual(fingerprint(a), fingerprint(b))


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
            e.scenario.anchor: (e.version, e.pending)
            for e in extract(self.repo / "spec").entries
        }

    def test_untagged_tree_is_version_one(self):
        commit_area(self.repo, ONE)
        self.assertEqual(self.versions(), {"login/good-password": (1, True)})

    def test_scenario_keeps_the_version_that_introduced_it(self):
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        self.assertEqual(self.versions()["login/good-password"], (1, False))

    def test_scenario_added_later_carries_the_later_version(self):
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        self.assertEqual(self.versions()["login/bad-password"], (2, False))

    def test_changed_scenario_advances_to_the_version_that_changed_it(self):
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        commit_area(self.repo, TWO_CHANGED, "spec-v3")
        found = self.versions()
        self.assertEqual(found["login/good-password"], (3, False))
        self.assertEqual(found["login/bad-password"], (2, False))

    def test_untagged_working_tree_scenario_takes_the_version_it_would_mint(self):
        # This is spec CI on a PR: the tag does not exist yet.
        commit_area(self.repo, ONE, "spec-v1")
        commit_area(self.repo, TWO, "spec-v2")
        write_area(self.repo, TWO + "\n\n  Scenario: Locked out\n    Given five failures\n    Then they are locked")
        self.assertEqual(self.versions()["login/locked-out"], (3, True))

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
        self.assertEqual({s["version"] for s in self.suite["scenarios"]}, {1})

    def test_every_spec_file_with_a_gherkin_block_is_represented(self):
        self.assertEqual(len({s["file"] for s in self.suite["scenarios"]}), 11)

    def test_suite_is_json_serialisable(self):
        json.loads(json.dumps(self.suite))


if __name__ == "__main__":
    unittest.main()
