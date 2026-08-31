"""``vellum ledger verify``: does every link in the chain resolve.

The scenario is ``@id:chain-resolution-fails-release`` in
``spec/features/ledger.md``: a cut that includes a wave with a work item lacking
a merged PR fails before promotion.

The behavior beside it names three broken links, and they are not all the same
scope — two are wrong about a record however it is read, and one is a property
of a release. See the module docstring in ``vellum.chain``.
"""

import tempfile
import unittest
from pathlib import Path

from support import (
    make_intent_repo, run_cli, write_record, write_releases, write_suite,
)
import yaml

from vellum.chain import ChainError, verify
from vellum.ledger import advance, load

SHAS = [f"a{n:039x}" for n in range(1, 6)]


class ChainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_intent_repo(Path(self.tmp.name) / "intent")
        self.ledger = self.repo / "ledger"

    def wave(self, sha, state="approved", scenarios=("billing",)):
        write_record(self.ledger, sha, state=state)
        if scenarios is not None:
            write_suite(self.ledger, sha, scenarios)
        return sha

    def item(self, sha, issue=1, pr=None, satisfies=("scenario:billing",), **kwargs):
        advance(
            self.ledger, sha, issue=issue, title=f"Implement {issue}", repo="app",
            satisfies=list(satisfies), pr=pr, **kwargs
        )

    def kinds(self, chain):
        return [f.kind for f in chain.findings]


class TestTheScenario(ChainCase):
    def setUp(self):
        super().setUp()
        self.sha = self.wave(SHAS[0])
        self.item(self.sha)                       # no PR
        write_releases(self.ledger, cuts=[self.sha])

    def test_a_cut_including_a_work_item_with_no_pr_does_not_resolve(self):
        chain = verify(self.repo)
        self.assertTrue(chain.broken)
        self.assertIn("no-pr", self.kinds(chain))

    def test_the_first_broken_link_is_named(self):
        chain = verify(self.repo)
        self.assertEqual(chain.findings[0].kind, "no-pr")
        self.assertIn("no-pr", chain.report())
        self.assertIn("BLOCKED", chain.report())

    def test_the_cli_exits_one_for_it(self):
        # 1: the guard answered, and the chain does not resolve. 2 stays "I
        # could not answer" — no ledger directory, a --strict refusal.
        code, out = run_cli(["ledger", "verify", str(self.repo)])
        self.assertEqual(code, 1)
        self.assertIn("no-pr", out)

    def test_giving_the_work_item_its_pr_resolves_the_chain(self):
        self.item(self.sha, pr=42)
        advance(self.ledger, self.sha, state="verified")
        chain = verify(self.repo)
        self.assertFalse(chain.broken)


class TestWorkItemsResolve(ChainCase):
    def test_a_record_with_no_work_items_is_vacuously_sound(self):
        self.wave(SHAS[0])
        self.assertFalse(verify(self.repo).broken)

    def test_a_work_item_with_no_pr_is_broken_even_with_no_cut(self):
        # This link is wrong the moment it is written, not only at a cut.
        self.wave(SHAS[0])
        self.item(SHAS[0])
        self.assertEqual(self.kinds(verify(self.repo)), ["no-pr"])

    def test_the_finding_names_the_item(self):
        self.wave(SHAS[0])
        self.item(SHAS[0], issue=7)
        self.assertIn("work item 7", str(verify(self.repo).findings[0]))

    def test_a_work_item_that_is_not_a_mapping_is_reported_not_skipped(self):
        # Written past `ledger.dump`, which refuses to emit it — the point is
        # what the guard does with a record a hand-edit left in that shape.
        self.wave(SHAS[0])
        path = self.ledger / f"{SHAS[0]}.yaml"
        path.write_text(
            yaml.safe_dump({**load(path), "work_items": ["not a mapping"]},
                           sort_keys=False),
            encoding="utf-8",
        )
        self.assertEqual(self.kinds(verify(self.repo)), ["no-pr"])


class TestSatisfiedScenariosResolve(ChainCase):
    def test_a_claimed_scenario_in_the_suite_resolves(self):
        self.wave(SHAS[0], scenarios=["billing"])
        self.item(SHAS[0], pr=1, satisfies=["scenario:billing"])
        self.assertFalse(verify(self.repo).broken)

    def test_a_claimed_scenario_the_suite_does_not_have_is_broken(self):
        self.wave(SHAS[0], scenarios=["billing"])
        self.item(SHAS[0], pr=1, satisfies=["scenario:invoicing"])
        chain = verify(self.repo)
        self.assertEqual(self.kinds(chain), ["unknown-scenario"])
        self.assertIn("scenario:invoicing", str(chain.findings[0]))

    def test_a_prose_slice_is_not_a_scenario_id_and_is_not_faulted(self):
        # `spec/features/ledger.md`: acceptance criteria are referenced by
        # scenario id, prose slices by file path and heading anchor. Resolving
        # the second against the suite would fault every correctly written one.
        self.wave(SHAS[0], scenarios=["billing"])
        self.item(SHAS[0], pr=1, satisfies=["features/ledger.md#behavior"])
        self.assertFalse(verify(self.repo).broken)

    def test_the_suite_consulted_is_the_one_at_that_version(self):
        # Two waves, two suites. A scenario present in the other version's
        # suite does not resolve a claim made at this one.
        self.wave(SHAS[0], scenarios=["billing"])
        self.wave(SHAS[1], scenarios=["shipping"])
        self.item(SHAS[0], pr=1, satisfies=["scenario:shipping"])
        self.assertEqual(self.kinds(verify(self.repo)), ["unknown-scenario"])


class TestAnAbsentSuiteIsUncheckedNotPassed(ChainCase):
    def setUp(self):
        super().setUp()
        self.wave(SHAS[0], scenarios=None)
        self.item(SHAS[0], pr=1, satisfies=["scenario:nowhere"])

    def test_it_is_reported_rather_than_resolved(self):
        chain = verify(self.repo)
        self.assertFalse(chain.broken)
        self.assertEqual(len(chain.unchecked), 1)
        self.assertIn("Unchecked is not passed", chain.report())

    def test_strict_refuses_to_pronounce_the_chain_sound(self):
        with self.assertRaises(ChainError):
            verify(self.repo, strict=True)

    def test_the_cli_exits_two_under_strict_not_one(self):
        # The same split `backpressure --strict` keeps: "I could not resolve
        # three records" must never arrive as "the chain is broken", nor as
        # "the chain is sound".
        code, out = run_cli(["ledger", "verify", str(self.repo), "--strict"])
        self.assertEqual(code, 2)
        self.assertIn("--strict", out)

    def test_an_unreadable_suite_file_is_also_unchecked(self):
        (self.ledger / f"suite-{SHAS[0]}.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(len(verify(self.repo).unchecked), 1)


class TestCuts(ChainCase):
    def test_a_cut_naming_a_wave_with_no_record_is_broken(self):
        self.wave(SHAS[0], state="verified")
        write_releases(self.ledger, cuts=[SHAS[1]])
        chain = verify(self.repo)
        self.assertEqual(self.kinds(chain), ["unknown-wave"])
        self.assertIn(SHAS[1][:12], str(chain.findings[0]))

    def test_a_cut_naming_a_wave_that_has_not_reached_verified_is_broken(self):
        # `scenarios=[]` so the coverage check has nothing to say and this test
        # is about the certification proxy alone.
        self.wave(SHAS[0], state="implementing", scenarios=[])
        write_releases(self.ledger, cuts=[SHAS[0]])
        self.assertEqual(self.kinds(verify(self.repo)), ["uncertified-wave"])

    def test_a_verified_or_shipped_wave_passes_that_check(self):
        for state in ("verified", "shipped"):
            with self.subTest(state=state):
                self.wave(SHAS[0], state=state)
                write_releases(self.ledger, cuts=[SHAS[0]])
                self.assertNotIn("uncertified-wave", self.kinds(verify(self.repo)))

    def test_the_report_says_the_certification_check_is_a_proxy(self):
        # There is no certification field in the record schema, so this reads
        # `state` instead. The report has to say so, or a reader takes a green
        # run as evidence a certification run happened.
        self.wave(SHAS[0], state="implementing", scenarios=[])
        write_releases(self.ledger, cuts=[SHAS[0]])
        self.assertIn("is a proxy", verify(self.repo).report())

    def test_a_cut_naming_no_wave_is_broken(self):
        self.wave(SHAS[0], state="verified")
        write_releases(self.ledger, cuts=[{"channel": "production"}])
        self.assertEqual(self.kinds(verify(self.repo)), ["unknown-wave"])

    def test_a_cut_naming_something_that_is_not_a_version_is_broken(self):
        self.wave(SHAS[0], state="verified")
        write_releases(self.ledger, cuts=["spec-v1"])
        chain = verify(self.repo)
        self.assertEqual(self.kinds(chain), ["unknown-wave"])
        self.assertIn("spec-v1", str(chain.findings[0]))

    def test_a_cut_pinning_several_waves_names_all_of_them(self):
        # `spec/features/ledger.md` says a cut records "pinned waves" — plural.
        # Reading only a scalar `wave:` would skip the rest, and skipping a cut
        # wave is a guard failing open, so both spellings are read.
        self.wave(SHAS[0], state="verified", scenarios=[])
        self.wave(SHAS[1], state="implementing", scenarios=[])
        write_releases(self.ledger, cuts=[{"wave": [SHAS[0], SHAS[1]]}])
        chain = verify(self.repo)
        self.assertEqual(chain.cuts, [SHAS[0], SHAS[1]])
        self.assertEqual(self.kinds(chain), ["uncertified-wave"])

    def test_a_cut_using_the_plural_key_is_read_too(self):
        self.wave(SHAS[0], state="implementing", scenarios=[])
        write_releases(self.ledger, cuts=[{"waves": [SHAS[0]]}])
        self.assertEqual(self.kinds(verify(self.repo)), ["uncertified-wave"])

    def test_no_releases_file_is_no_cuts_rather_than_an_error(self):
        self.wave(SHAS[0])
        self.assertEqual(verify(self.repo).cuts, [])


class TestCoverageIsAskedAtTheCut(ChainCase):
    def test_a_criterion_a_cut_wave_arms_and_nothing_claims_is_broken(self):
        self.wave(SHAS[0], state="verified", scenarios=["billing", "shipping"])
        self.item(SHAS[0], pr=1, satisfies=["scenario:billing"])
        write_releases(self.ledger, cuts=[SHAS[0]])
        chain = verify(self.repo)
        self.assertEqual(self.kinds(chain), ["unclaimed-criterion"])
        self.assertIn("scenario:shipping", str(chain.findings[0]))

    def test_an_open_wave_no_cut_names_is_not_faulted_for_coverage(self):
        # An unplanned wave legitimately has criteria nothing claims yet; that
        # is what an unplanned wave IS. Asking this of every record would fault
        # every version between approval and its work plan.
        self.wave(SHAS[0], scenarios=["billing", "shipping"])
        self.item(SHAS[0], pr=1, satisfies=["scenario:billing"])
        self.assertFalse(verify(self.repo).broken)

    def test_only_the_criteria_this_version_armed_are_asked_about(self):
        # The rest of the suite belongs to earlier waves and was claimed — or
        # not — there. Re-faulting it at every cut makes the guard noisier the
        # longer an installation runs.
        self.wave(SHAS[0], state="verified",
                  scenarios=[("billing", SHAS[0]), ("legacy", SHAS[1])])
        self.item(SHAS[0], pr=1, satisfies=["scenario:billing"])
        write_releases(self.ledger, cuts=[SHAS[0]])
        self.assertFalse(verify(self.repo).broken)


class TestWhatIsNotARecord(ChainCase):
    def test_releases_yaml_is_not_counted_as_a_record(self):
        self.wave(SHAS[0])
        write_releases(self.ledger, cuts=[])
        self.assertEqual(verify(self.repo).records, 1)
        self.assertEqual(verify(self.repo).unreadable, [])

    def test_a_name_keyed_legacy_record_is_reported_not_read(self):
        # `ledger/spec-v1.yaml` style records predate versions being commits.
        (self.ledger / "spec-v1.yaml").write_text(
            "spec_version: spec-v1\nstate: approved\nwork_items: []\n", encoding="utf-8"
        )
        self.wave(SHAS[0])
        chain = verify(self.repo)
        self.assertEqual(chain.records, 1)
        self.assertEqual(chain.unreadable, ["spec-v1.yaml"])

    def test_an_unparseable_record_is_reported(self):
        (self.ledger / "broken.yaml").write_text("{[", encoding="utf-8")
        self.assertEqual(verify(self.repo).unreadable, ["broken.yaml"])

    def test_no_ledger_directory_cannot_be_answered(self):
        with self.assertRaises(ChainError):
            verify(Path(self.tmp.name) / "nowhere")

    def test_the_cli_exits_two_for_it(self):
        code, out = run_cli(["ledger", "verify", str(Path(self.tmp.name) / "nowhere")])
        self.assertEqual(code, 2)
        self.assertIn("no ledger directory", out)


class TestTheReport(ChainCase):
    def test_a_sound_chain_says_so(self):
        self.wave(SHAS[0])
        self.item(SHAS[0], pr=1)
        report = verify(self.repo).report()
        self.assertIn("OK: every link", report)

    def test_it_counts_records_and_cuts(self):
        self.wave(SHAS[0], state="verified")
        write_releases(self.ledger, cuts=[SHAS[0]])
        self.assertIn("1 record(s), 1 cut(s)", verify(self.repo).report())


if __name__ == "__main__":
    unittest.main()
