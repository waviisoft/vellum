"""``vellum release cut`` and ``vellum suite partition``.

Two properties are asserted structurally rather than stipulated, because they
are the two this wave could most easily fake:

* **the partition is decided by real git ancestry.** Every fixture here builds
  an actual repository with ancestry-ordered spec commits and lets
  ``vellum suite extract`` date the scenarios; nothing writes a version into a
  suite by hand except the tests whose subject is a suite file the command was
  *handed*. A test that stipulated "this scenario is newer" would pass against
  an implementation comparing shas as strings.
* **a promoted cut relieves the divergence window.** That is the whole reason
  ``waviisoft/vellum-intent#41`` schedules arming the backpressure gate behind
  this wave, so it is asserted by running ``vellum backpressure`` before and
  after a cut rather than by reading the record's ``state`` field.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import yaml

from support import (
    commit_area,
    commit_elsewhere,
    git,
    make_intent_repo,
    run_cli,
    run_cli_streams,
    write_record,
    write_releases,
    write_suite,
)

FULL = "0" * 40
OTHER_FULL = "1" * 40


def block(*ids: str) -> str:
    """One Feature declaring a scenario per id, each with its own step text.

    The step text varies by id deliberately. ``fingerprint()`` hashes the steps
    and nothing else, and ``version_history()`` falls back to matching an
    id-less scenario against an unclaimed one with the same fingerprint — so
    scenarios sharing a step line are *interchangeable* to the dater, and a
    fixture built that way silently dates a brand-new scenario to an old commit.
    Measured here: with every scenario reading ``Given a sandbox``, an uncommitted
    third scenario came back enforced at the first commit instead of pending.
    """
    lines = ["Feature: Auth"]
    for ident in ids:
        lines += [
            f"  @id:{ident}",
            f"  Scenario: {ident}",
            f"    Given a sandbox holding {ident}",
        ]
    return "\n".join(lines) + "\n"


class ReleaseCase(unittest.TestCase):
    """A sandbox intent repo with ancestry-ordered spec commits.

    ``os.environ`` is asserted unchanged after every test, the guard
    ``PinCase`` and ``DepsCase`` already carry: ``run_cli`` calls ``main()``
    in-process, so a test that leaks ``VELLUM_INTENT_REPO`` disarms the
    conformance job for every module discovered after it.
    """

    def setUp(self):
        self._environ = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "intent"
        self.repo.mkdir()
        make_intent_repo(self.repo)
        self.ledger = self.repo / "ledger"
        self.addCleanup(self._assert_environ_unchanged)

    def _assert_environ_unchanged(self):
        self.assertEqual(dict(os.environ), self._environ)

    def commits(self, *specs: tuple[str, ...]) -> list[str]:
        """One spec commit per entry, oldest first. Returns their shas."""
        return [commit_area(self.repo, block(*ids)) for ids in specs]

    def records(self, *shas: str, state: str = "verified") -> None:
        for index, sha in enumerate(shas, start=1):
            write_record(self.ledger, sha, state=state, name=f"spec-v{index}")

    def releases(self, **kwargs) -> Path:
        return write_releases(self.ledger, **kwargs)

    def cut(self, *args: str, at: str = "2026-08-31T00:00:00Z", channel: str = "production"):
        return run_cli(
            ["release", "cut", str(self.repo), "--channel", channel, "--at", at, *args]
        )

    def released(self) -> dict:
        return yaml.safe_load((self.ledger / "releases.yaml").read_text(encoding="utf-8"))

    def pointer(self, channel: str = "production") -> str | None:
        return self.released()["channels"][channel]["spec_conformed"]

    def partition(self, *args: str, channel: str = "production"):
        return run_cli(["suite", "partition", str(self.repo), "--channel", channel, *args])


# ===================================================================== cut

class TestTheBookkeepingHalf(ReleaseCase):
    """A cut with no suite result: recorded, and nothing promoted."""

    def setUp(self):
        super().setUp()
        self.first, self.second = self.commits(("one",), ("one", "two"))
        self.records(self.first, self.second)
        self.releases()

    def test_a_cut_with_no_suite_result_is_recorded_and_promotes_nothing(self):
        code, _ = self.cut("--wave", self.first, "--versions", f"core={FULL}")
        self.assertEqual(code, 0)
        data = self.released()
        self.assertEqual(len(data["cuts"]), 1)
        cut = data["cuts"][0]
        self.assertEqual(cut["channel"], "production")
        self.assertEqual(cut["waves"], [self.first])
        self.assertEqual(cut["versions"], {"core": FULL})
        self.assertIs(cut["promoted"], False)
        self.assertIsNone(cut["suite_result"])
        # The two things promotion would have done, and neither happened.
        self.assertIsNone(self.pointer())
        record = yaml.safe_load((self.ledger / f"{self.first}.yaml").read_text())
        self.assertEqual(record["state"], "verified")
        self.assertIsNone(record["release"])

    def test_the_report_says_the_suite_result_was_not_supplied(self):
        _, out = self.cut("--wave", self.first, "--versions", f"core={FULL}")
        self.assertIn("NOT PROMOTED", out)
        self.assertIn("--suite-result", out)

    def test_the_recorded_shape_is_the_one_the_ledger_guard_reads(self):
        """`chain.py` finds a cut's waves; a shape it cannot read is a cut nothing checks."""
        self.cut("--wave", self.first, "--versions", f"core={FULL}")
        code, out = run_cli(["ledger", "verify", str(self.repo)])
        self.assertEqual(code, 0)
        self.assertIn("1 cut(s)", out)

    def test_a_cut_keeps_the_files_other_keys_and_their_order(self):
        """`releases.yaml` is the intent repo's, and may carry keys this does not model."""
        path = self.ledger / "releases.yaml"
        data = yaml.safe_load(path.read_text())
        data["policy"] = "batched"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        self.cut("--wave", self.first, "--versions", f"core={FULL}")
        after = yaml.safe_load(path.read_text())
        self.assertEqual(after["policy"], "batched")
        self.assertEqual(
            list(after), ["spec_head", "channels", "cuts", "stamps", "policy"]
        )


class TestPromotion(ReleaseCase):
    def setUp(self):
        super().setUp()
        self.first, self.second, self.third = self.commits(
            ("one",), ("one", "two"), ("one", "two", "three")
        )
        self.records(self.first, self.second, self.third)
        self.releases()

    def test_green_advances_the_pointer_and_ships_every_wave_it_names(self):
        code, out = self.cut(
            "--wave", self.first, "--wave", self.second,
            "--versions", f"core={FULL}", "--suite-result", "green",
        )
        self.assertEqual(code, 0)
        self.assertIn("PROMOTED", out)
        self.assertEqual(self.pointer(), self.second)
        for sha in (self.first, self.second):
            record = yaml.safe_load((self.ledger / f"{sha}.yaml").read_text())
            self.assertEqual(record["state"], "shipped")
            self.assertEqual(record["release"], "production@2026-08-31T00:00:00Z")
        # The wave the cut did not name is untouched.
        record = yaml.safe_load((self.ledger / f"{self.third}.yaml").read_text())
        self.assertEqual(record["state"], "verified")

    def test_the_pointer_is_the_newest_wave_by_ancestry_not_by_argument_order(self):
        """Shas do not compare, so 'newest' cannot be the last one typed."""
        self.cut(
            "--wave", self.second, "--wave", self.first,
            "--versions", f"core={FULL}", "--suite-result", "green",
        )
        self.assertEqual(self.pointer(), self.second)

    def test_waves_that_do_not_lie_on_one_line_of_ancestry_are_refused(self):
        git(self.repo, "checkout", "-q", "-b", "side", self.first)
        sideways = commit_area(self.repo, block("one", "sideways"))
        git(self.repo, "checkout", "-q", "main")
        # `commit_area` stages the whole tree, so the branch switch took
        # `ledger/` with it. Write it back rather than reaching into git: what
        # this test is about is two waves off one line, not checkout mechanics.
        self.records(self.first, self.second)
        write_record(self.ledger, sideways, state="verified")
        self.releases()
        code, out = self.cut(
            "--wave", self.second, "--wave", sideways,
            "--versions", f"core={FULL}", "--suite-result", "green",
        )
        self.assertEqual(code, 1)
        self.assertIn("one line of ancestry", out)
        self.assertIsNone(self.pointer())

    def test_red_records_the_cut_and_refuses_to_promote(self):
        code, out = self.cut(
            "--wave", self.first, "--versions", f"core={FULL}", "--suite-result", "red",
        )
        # An answer the caller acts on: "promotion occurs only if it passes".
        self.assertEqual(code, 1)
        self.assertIn("NOT PROMOTED", out)
        self.assertEqual(len(self.released()["cuts"]), 1)
        self.assertIsNone(self.pointer())
        record = yaml.safe_load((self.ledger / f"{self.first}.yaml").read_text())
        self.assertEqual(record["state"], "verified")

    def test_the_pointer_never_moves_backwards(self):
        self.cut(
            "--wave", self.second, "--versions", f"core={FULL}", "--suite-result", "green",
        )
        self.assertEqual(self.pointer(), self.second)
        code, out = self.cut(
            "--wave", self.first, "--versions", f"core={FULL}",
            "--suite-result", "green", at="2026-08-31T01:00:00Z",
        )
        self.assertEqual(code, 1)
        self.assertIn("re-arm", out)
        self.assertEqual(self.pointer(), self.second)

    def test_a_wave_that_has_not_reached_verified_is_not_promoted(self):
        """Otherwise a cut passes `ledger verify`'s uncertified-wave check by existing.

        Promotion writes ``state: shipped``, and ``shipped`` is one of
        ``chain.CERTIFIABLE_STATES`` — so without this refusal the guard's own
        input is written by the thing it is meant to judge.
        """
        write_record(self.ledger, self.first, state="approved")
        code, out = self.cut(
            "--wave", self.first, "--versions", f"core={FULL}", "--suite-result", "green",
        )
        self.assertEqual(code, 1)
        self.assertIn("uncertified-wave", out)
        self.assertEqual(self.released()["cuts"], [])
        self.assertIsNone(self.pointer())
        record = yaml.safe_load((self.ledger / f"{self.first}.yaml").read_text())
        self.assertEqual(record["state"], "approved")

    def test_a_promoted_cut_relieves_the_divergence_window(self):
        """The property waviisoft/vellum-intent#41 is waiting on, measured.

        Asserted through `vellum backpressure` rather than by reading `state`:
        what the arming decision turns on is the gate's answer, not the field
        the gate happens to read today.
        """
        blocked, before = run_cli(["backpressure", str(self.repo), "--cap", "2"])
        self.assertEqual(blocked, 1)
        self.assertIn("BLOCKED", before)
        self.cut(
            "--wave", self.first, "--wave", self.second, "--wave", self.third,
            "--versions", f"core={FULL}", "--suite-result", "green",
        )
        clear, after = run_cli(["backpressure", str(self.repo), "--cap", "2"])
        self.assertEqual(clear, 0)
        self.assertIn("Divergence window: 0 of 2", after)


class TestCutsThatAreRefused(ReleaseCase):
    def setUp(self):
        super().setUp()
        (self.first,) = self.commits(("one",))
        self.records(self.first)
        self.releases()
        self.before = (self.ledger / "releases.yaml").read_bytes()

    def assert_wrote_nothing(self):
        self.assertEqual((self.ledger / "releases.yaml").read_bytes(), self.before)

    def test_a_wave_with_no_ledger_record_is_refused(self):
        code, out = self.cut("--wave", "b" * 40, "--versions", f"core={FULL}")
        self.assertEqual(code, 1)
        self.assertIn("no ledger record", out)
        self.assert_wrote_nothing()

    def test_a_record_that_disagrees_about_which_version_it_is_is_refused(self):
        """The check `vellum pin advance` grew: this field becomes a pointer."""
        path = self.ledger / f"{self.first}.yaml"
        record = yaml.safe_load(path.read_text())
        record["spec_version"] = "c" * 40
        path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
        code, out = self.cut("--wave", self.first, "--versions", f"core={FULL}")
        self.assertEqual(code, 1)
        self.assertIn("does not agree", out)
        self.assert_wrote_nothing()

    def test_a_channel_the_file_does_not_declare_is_refused_not_created(self):
        code, out = self.cut(
            "--wave", self.first, "--versions", f"core={FULL}", channel="canary"
        )
        self.assertEqual(code, 2)
        self.assertIn("no channel 'canary'", out)
        self.assertIn("production", out)
        self.assert_wrote_nothing()

    def test_a_product_the_workspace_does_not_declare_is_refused(self):
        code, out = self.cut("--wave", self.first, "--versions", f"web={FULL}")
        self.assertEqual(code, 2)
        self.assertIn("does not declare", out)
        self.assert_wrote_nothing()

    def test_a_product_may_be_named_by_its_repo_slug(self):
        code, _ = self.cut(
            "--wave", self.first, "--versions", f"waviisoft/vellum={FULL}"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.released()["cuts"][0]["versions"], {"core": FULL})

    def test_an_abbreviated_product_version_is_refused_not_resolved(self):
        code, out = self.cut("--wave", self.first, "--versions", "core=abc1234")
        self.assertEqual(code, 2)
        self.assertIn("full 40-character commit sha", out)

    def test_a_versions_entry_that_is_not_product_equals_sha_is_refused(self):
        code, out = self.cut("--wave", self.first, "--versions", "core")
        self.assertEqual(code, 2)
        self.assertIn("<product>=<sha>", out)

    def test_a_cut_pinning_no_versions_is_refused(self):
        code, out = self.cut("--wave", self.first)
        self.assertEqual(code, 2)
        self.assertIn("--versions is required", out)

    def test_comma_separated_versions_are_the_same_as_repeating_the_flag(self):
        code, _ = self.cut(
            "--wave", self.first, "--versions", f"core={FULL},waviisoft/vellum={FULL}"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.released()["cuts"][0]["versions"], {"core": FULL})

    def test_two_different_commits_for_one_product_are_refused(self):
        code, out = self.cut(
            "--wave", self.first, "--versions", f"core={FULL},core={OTHER_FULL}"
        )
        self.assertEqual(code, 2)
        self.assertIn("two different commits", out)

    def test_a_wave_that_is_not_a_sha_is_a_bad_invocation(self):
        code, out = self.cut("--wave", "spec-v1", "--versions", f"core={FULL}")
        self.assertEqual(code, 2)
        self.assertIn("not a spec version", out)

    def test_an_unreadable_at_is_a_bad_invocation(self):
        code, out = self.cut(
            "--wave", self.first, "--versions", f"core={FULL}", at="yesterday"
        )
        self.assertEqual(code, 2)
        self.assertIn("ISO 8601", out)

    def test_a_missing_releases_file_is_refused_rather_than_created(self):
        (self.ledger / "releases.yaml").unlink()
        code, out = self.cut("--wave", self.first, "--versions", f"core={FULL}")
        self.assertEqual(code, 2)
        self.assertIn("cannot read the release pointers", out)
        self.assertFalse((self.ledger / "releases.yaml").exists())

    def test_a_missing_workspace_is_refused_rather_than_defaulted(self):
        (self.repo / ".vellum" / "workspace.yaml").unlink()
        code, out = self.cut("--wave", self.first, "--versions", f"core={FULL}")
        self.assertEqual(code, 2)
        self.assertIn("cannot read the workspace", out)
        self.assert_wrote_nothing()

    def test_a_checkout_with_no_ledger_is_refused(self):
        code, out = run_cli([
            "release", "cut", str(self.root / "nowhere"), "--channel", "production",
            "--wave", self.first, "--versions", f"core={FULL}",
        ])
        self.assertEqual(code, 2)
        self.assertIn("no ledger directory", out)

    def test_promoting_from_a_shallow_clone_is_refused(self):
        """Below the graft, ancestry answers 'no' for commits that are ancestors."""
        shallow = self.root / "shallow"
        git(self.root, "clone", "--quiet", "--depth", "1",
            f"file://{self.repo}", str(shallow))
        self.assertEqual(
            git(shallow, "rev-parse", "--is-shallow-repository").strip(), "true"
        )
        write_record(shallow / "ledger", self.first, state="verified")
        write_releases(shallow / "ledger")
        code, out = run_cli([
            "release", "cut", str(shallow), "--channel", "production",
            "--wave", self.first, "--versions", f"core={FULL}", "--suite-result", "green",
        ])
        self.assertEqual(code, 1)
        self.assertIn("shallow clone", out)


class TestReplay(ReleaseCase):
    """A cut's id is ``<channel>@<at>``, so the same cut can be re-run."""

    def setUp(self):
        super().setUp()
        self.first, self.second = self.commits(("one",), ("one", "two"))
        self.records(self.first, self.second)
        self.releases()

    def test_recording_the_same_cut_twice_writes_nothing_the_second_time(self):
        self.cut("--wave", self.first, "--versions", f"core={FULL}")
        before = (self.ledger / "releases.yaml").read_bytes()
        code, out = self.cut("--wave", self.first, "--versions", f"core={FULL}")
        self.assertEqual(code, 0)
        self.assertIn("already recorded", out)
        self.assertEqual((self.ledger / "releases.yaml").read_bytes(), before)

    def test_a_recorded_cut_can_be_promoted_when_its_suite_result_arrives(self):
        """The suite runs elsewhere, so its result legitimately arrives later."""
        self.cut("--wave", self.first, "--versions", f"core={FULL}")
        self.assertIsNone(self.pointer())
        code, _ = self.cut(
            "--wave", self.first, "--versions", f"core={FULL}", "--suite-result", "green"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.pointer(), self.first)
        self.assertEqual(len(self.released()["cuts"]), 1)

    def test_a_promotion_that_happened_is_not_un_recorded_by_re_running_the_cut(self):
        self.cut("--wave", self.first, "--versions", f"core={FULL}", "--suite-result", "green")
        code, out = self.cut("--wave", self.first, "--versions", f"core={FULL}")
        self.assertEqual(code, 1)
        self.assertIn("already recorded as promoted", out)
        self.assertEqual(self.pointer(), self.first)

    def test_a_different_cut_under_the_same_id_is_refused(self):
        self.cut("--wave", self.first, "--versions", f"core={FULL}")
        code, out = self.cut("--wave", self.second, "--versions", f"core={FULL}")
        self.assertEqual(code, 1)
        self.assertIn("already recorded", out)
        self.assertEqual(self.released()["cuts"][0]["waves"], [self.first])


# =============================================================== partition

class TestThePartitionIsDecidedByAncestry(ReleaseCase):
    """Every version here is dated by `vellum suite extract` off real history."""

    def setUp(self):
        super().setUp()
        self.first, self.second = self.commits(("one",), ("one", "two"))
        self.records(self.first, self.second)

    def test_a_scenario_above_the_pointer_is_armed_and_one_below_is_enforced(self):
        self.releases(spec_conformed=self.first)
        code, out = self.partition("--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([s["id"] for s in payload["enforced"]], ["one"])
        self.assertEqual([s["id"] for s in payload["armed"]], ["two"])
        # And the versions came from extraction, not from the fixture.
        self.assertEqual(payload["enforced"][0]["version"], self.first)
        self.assertEqual(payload["armed"][0]["version"], self.second)

    def test_a_scenario_introduced_at_the_pointer_is_enforced(self):
        """"At or below" is inclusive; the boundary is the easy one to get wrong."""
        self.releases(spec_conformed=self.second)
        code, out = self.partition("--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(sorted(s["id"] for s in payload["enforced"]), ["one", "two"])
        self.assertEqual(payload["armed"], [])

    def test_a_commit_that_touches_nothing_in_the_spec_tree_moves_no_scenario(self):
        """The pointer may name any commit; enforcement is still ancestry."""
        self.releases(spec_conformed=commit_elsewhere(self.repo))
        payload = json.loads(self.partition("--json")[1])
        self.assertEqual(sorted(s["id"] for s in payload["enforced"]), ["one", "two"])

    def test_a_channel_that_has_conformed_to_nothing_enforces_nothing(self):
        self.releases()
        code, out = self.partition()
        self.assertEqual(code, 0)
        self.assertIn("Every scenario is armed", out)
        payload = json.loads(self.partition("--json")[1])
        self.assertEqual(payload["enforced"], [])
        self.assertEqual(len(payload["armed"]), 2)

    def test_a_pending_scenario_is_armed(self):
        """It is in nobody's history, so it is above every pointer there is."""
        self.releases(spec_conformed=self.second)
        (self.repo / "spec" / "features" / "auth.md").write_text(
            (self.repo / "spec" / "features" / "auth.md").read_text().replace(
                "```\n", "  @id:three\n  Scenario: three\n    Given a sandbox\n```\n", 1
            ),
            encoding="utf-8",
        )
        payload = json.loads(self.partition("--json")[1])
        armed = [s["id"] for s in payload["armed"]]
        self.assertEqual(armed, ["three"])
        self.assertIsNone(payload["armed"][0]["version"])

    def test_the_partition_follows_the_channel_it_was_asked_about(self):
        self.releases(channels={
            "production": {"spec_conformed": self.first},
            "staging": {"spec_conformed": self.second},
        })
        production = json.loads(self.partition("--json")[1])
        staging = json.loads(self.partition("--json", channel="staging")[1])
        self.assertEqual(len(production["enforced"]), 1)
        self.assertEqual(len(staging["enforced"]), 2)

    def test_a_cut_and_a_partition_agree_about_what_the_cut_enforced(self):
        """The two commands' whole reason for sharing a module, asserted."""
        self.releases()
        self.assertEqual(
            self.cut("--wave", self.first, "--versions", f"core={FULL}",
                     "--suite-result", "green")[0],
            0,
        )
        payload = json.loads(self.partition("--json")[1])
        self.assertEqual([s["id"] for s in payload["enforced"]], ["one"])
        self.assertEqual([s["id"] for s in payload["armed"]], ["two"])


class TestPartitionsThatAreRefused(ReleaseCase):
    def setUp(self):
        super().setUp()
        self.first, self.second = self.commits(("one",), ("one", "two"))
        self.records(self.first, self.second)

    def test_a_shallow_suite_is_refused_rather_than_partitioned(self):
        self.releases(spec_conformed=self.first)
        write_suite(self.ledger, self.second, ["one"])
        path = self.ledger / f"suite-{self.second}.json"
        payload = json.loads(path.read_text())
        payload["shallow"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        code, out = self.partition("--suite", str(path))
        self.assertEqual(code, 1)
        self.assertIn("shallow", out)

    def test_a_version_the_checkout_cannot_place_is_refused_not_guessed(self):
        """Guessing 'armed' takes a regression check out of conformance monitoring."""
        self.releases(spec_conformed=self.first)
        path = write_suite(self.ledger, self.second, [("one", "d" * 40)])
        code, out = self.partition("--suite", str(path))
        self.assertEqual(code, 1)
        self.assertIn("cannot tell whether", out)

    def test_a_channel_the_file_does_not_declare_is_a_bad_invocation(self):
        self.releases()
        code, out = self.partition(channel="canary")
        self.assertEqual(code, 2)
        self.assertIn("no channel 'canary'", out)

    def test_a_pointer_that_is_not_a_sha_is_refused(self):
        """A name is decoration and nothing may be decided on one."""
        self.releases(spec_conformed="spec-v1")
        code, out = self.partition()
        self.assertEqual(code, 2)
        self.assertIn("not a commit sha", out)

    def test_a_tree_that_would_extract_short_is_refused(self):
        """No whole suite means no honest partition of one."""
        self.releases(spec_conformed=self.first)
        commit_area(
            self.repo,
            'Feature: Unterminated docstring\n'
            '  Scenario: The quote is never closed\n'
            '    Given a payload\n'
            '      """\n'
            '      {"still": "open"\n'
            "    Then it is rejected\n",
        )
        code, out = self.partition()
        self.assertEqual(code, 1)
        self.assertIn("no whole suite to partition", out)

    def test_an_unreadable_supplied_suite_is_a_bad_invocation(self):
        self.releases(spec_conformed=self.first)
        path = self.root / "not.json"
        path.write_text("{", encoding="utf-8")
        code, out = self.partition("--suite", str(path))
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", out)


class TestThePartitionOutput(ReleaseCase):
    def setUp(self):
        super().setUp()
        self.first, self.second = self.commits(("one",), ("one", "two"))
        self.records(self.first, self.second)
        self.releases(spec_conformed=self.first)

    def test_json_puts_the_payload_on_stdout_and_nothing_else(self):
        """`| jq` must parse it, the property `suite extract -o -` already has."""
        code, out, err = run_cli_streams(
            ["suite", "partition", str(self.repo), "--channel", "production", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        json.loads(out)

    def test_a_recorded_suite_can_be_partitioned_instead_of_the_working_tree(self):
        """What a cut was judged against is a different question from the tree."""
        path = write_suite(
            self.ledger, self.second, [("one", self.first), ("two", self.second)]
        )
        payload = json.loads(self.partition("--json", "--suite", str(path))[1])
        self.assertEqual(payload["source"], str(path))
        self.assertEqual([s["id"] for s in payload["enforced"]], ["one"])

    def test_the_report_is_not_a_workflow_command_channel(self):
        """Ids and paths come out of a spec tree anyone who lands a merge writes.

        The report is printed, and a caller may pipe it into a step summary the
        way `spec-ci.yml` pipes `backpressure`'s — where a value carrying a
        newline starts a line of its own, and a line of its own is all
        `::add-mask` needs.
        """
        path = write_suite(self.ledger, self.second, ["one"])
        payload = json.loads(path.read_text())
        payload["scenarios"][0]["id"] = "one\n::error::pwned"
        payload["scenarios"][0]["file"] = "features/a.md\n::add-mask::secret"
        path.write_text(json.dumps(payload), encoding="utf-8")
        code, out = self.partition("--suite", str(path))
        self.assertEqual(code, 0)
        self.assertFalse(
            [line for line in out.splitlines() if line.lstrip().startswith("::")],
            out,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
