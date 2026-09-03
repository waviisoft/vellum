"""``vellum tick``: the stateless reconciler, one behavior per scenario.

Each class names the acceptance scenario it exists for, because that is the only
reason any of this behavior is here. The sandboxes are real git repositories in
tempdirs with real ledger records written through ``vellum.ledger``, so a tick
reconciles the same shapes it will meet on a live intent repo — the reconciler's
inputs are files, and a fixture that stipulated them would be testing a mock.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from support import commit_files, git, make_intent_repo, write_releases, write_suite
from vellum.cli import main
from vellum.ledger import (
    dump,
    load,
    new_item,
    new_lease,
    new_record,
    record_path,
)
from vellum.reconcile import (
    DEFAULT_LEASE_MINUTES,
    TickError,
    corpus_answer,
    question_terms,
    reconcile,
)

#: The tick's clock in every test that has one. Fixed, because lease expiry and
#: the question timebox are both resolved against it and a test whose subject is
#: "expired" must not depend on how long the suite took to get here.
NOW = datetime.datetime(2026, 8, 31, 12, 0, 0, tzinfo=datetime.timezone.utc)


def at(hours: float) -> str:
    """An ISO 8601 moment *hours* from ``NOW``, as the ledger writes them."""
    return (NOW + datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_cli(argv):
    """Run the CLI, returning ``(exit_code, stdout, stderr)`` kept apart."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class TickCase(unittest.TestCase):
    """A sandbox intent repo with a real spec history and a real ledger."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_intent_repo(Path(self.tmp.name) / "intent")
        self.ledger = self.repo / "ledger"
        # Two spec-touching commits, so the ledger's two versions really do
        # order by ancestry rather than by anything this test asserts.
        self.older = commit_files(
            self.repo,
            {"spec/features/auth.md": "---\nid: auth\ntitle: Auth\nsince: spec-v1\n---\n\n# Auth\n"},
            "spec: sign-in",
        )
        self.newer = commit_files(
            self.repo,
            {"spec/features/auth.md": "---\nid: auth\ntitle: Auth\nsince: spec-v1\n---\n\n# Auth\n\nMore.\n"},
            "spec: sign-in again",
        )

    # ------------------------------------------------------------- fixtures

    def record(self, sha, state="approved", items=(), approved=None, name=None) -> Path:
        record = new_record(sha, name=name, approved=approved)
        record["state"] = state
        record["work_items"] = list(items)
        path = record_path(self.ledger, sha)
        path.write_text(dump(record), encoding="utf-8")
        return path

    def item(self, issue, satisfies=(), state="planned", lease=None, briefing=None, pr=None):
        entry = new_item(
            issue, f"item {issue}", "app", list(satisfies), state=state, briefing=briefing
        )
        entry["lease"] = lease
        entry["pr"] = pr
        return entry

    def observed(self, **payload) -> str:
        path = Path(self.tmp.name) / "observed.yaml"
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return str(path)

    def tick(self, **kwargs):
        kwargs.setdefault("now", NOW)
        return reconcile(self.repo, **kwargs)

    def items_of(self, sha) -> list[dict]:
        return load(record_path(self.ledger, sha))["work_items"]


# =====================================================  @id:reconcile-missed-webhook


class TestAMissedWebhookDoesNotStrandAWave(TickCase):
    """`@id:reconcile-missed-webhook` — a tick reconciles from durable state.

    "Given spec-v42 is approved and no issues exist for its work plan / And no
    webhook fired for the approval / When the next reconciler tick runs / Then
    the work plan issues for spec-v42 are filed."

    Nothing here delivers an event, and that is the point: the tick is handed a
    checkout and reads the record off disk. A missed webhook costs the latency
    of one tick and no correctness at all (decision D11).
    """

    def test_an_approved_version_with_no_plan_asks_for_one(self):
        self.record(self.newer, state="approved", items=[])
        tick = self.tick()
        plans = tick.of_kind("plan")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].version, self.newer)
        self.assertFalse(plans[0].taken, "a planner run is the caller's to perform")

    def test_the_plan_action_names_the_conformed_baseline(self):
        write_releases(self.ledger, spec_head=self.newer)
        data = yaml.safe_load((self.ledger / "releases.yaml").read_text(encoding="utf-8"))
        data["channels"]["production"]["spec_conformed"] = self.older
        (self.ledger / "releases.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        self.record(self.newer, state="approved", items=[])
        self.assertIn(self.older[:12], self.tick().of_kind("plan")[0].detail)

    def test_no_conformed_baseline_is_said_plainly_and_not_invented(self):
        write_releases(self.ledger)  # spec_conformed: null, as the real ledger has it
        self.record(self.newer, state="approved", items=[])
        detail = self.tick().of_kind("plan")[0].detail
        self.assertIn("none recorded", detail)
        self.assertIn("releases.yaml", detail)
        for sha in (self.older, self.newer):
            self.assertNotIn(sha[:12], detail, "a baseline nobody recorded is not guessed")

    def test_committing_a_plan_files_an_issue_for_every_item(self):
        self.record(self.newer, state="approved", items=[])
        plan = [
            {"issue": 11, "title": "sign-in", "repo": "app", "satisfies": ["scenario:a"]},
            {"issue": 12, "title": "sign-out", "repo": "app", "satisfies": ["scenario:b"]},
        ]
        tick = self.tick(plan=plan, version=self.newer)
        self.assertEqual([a.item for a in tick.of_kind("file-issue")], [11, 12])
        self.assertEqual([i["issue"] for i in self.items_of(self.newer)], [11, 12])
        self.assertEqual(load(record_path(self.ledger, self.newer))["state"], "planning")

    def test_an_issue_the_forge_already_has_is_not_filed_again(self):
        self.record(self.newer, state="planning", items=[self.item(11), self.item(12)])
        tick = self.tick(observed=self.observed(issues=[11]))
        self.assertEqual([a.item for a in tick.of_kind("file-issue")], [12])

    def test_without_observed_state_the_report_says_which_half_it_saw(self):
        self.record(self.newer, state="planning", items=[self.item(11)])
        tick = self.tick()
        self.assertFalse(tick.observed_supplied)
        self.assertIn("Observed state was not supplied", tick.report())
        self.assertIn("no observed issues were supplied", tick.of_kind("file-issue")[0].detail)

    def test_a_second_tick_over_an_unchanged_world_writes_nothing(self):
        """Idempotence, asserted on the bytes rather than on the action list.

        Decision D11 makes every orchestrator action idempotent; the way that
        fails in practice is a record rewritten identically on every tick, which
        is invisible in a report and very visible in a git history.
        """
        self.record(self.newer, state="planning", items=[self.item(11)])
        path = record_path(self.ledger, self.newer)
        first = self.tick(observed=self.observed(issues=[11]), executor="ex")
        self.assertEqual(first.written, [path.name])
        before = path.read_bytes()
        second = self.tick(observed=self.observed(issues=[11]), executor="ex")
        self.assertEqual(second.written, [])
        self.assertEqual(path.read_bytes(), before)

    def test_a_shipped_version_is_not_reconciled(self):
        self.record(self.newer, state="shipped", items=[self.item(11)])
        self.assertEqual(self.tick().actions, [])


# =======================================================  @id:coalescing-supersedes


class TestCoalescingSupersedesUnstartedOverlappingWork(TickCase):
    """`@id:coalescing-supersedes`.

    "Given spec-v42's wave has an unstarted item touching features/auth.md /
    When spec-v43 is approved changing the same behavior / Then the unstarted
    item is marked superseded / And the plan for spec-v43 targets the conformed
    baseline."
    """

    def test_an_unstarted_overlapping_item_is_marked_superseded(self):
        self.record(self.older, items=[self.item(1, ["scenario:auth"])])
        self.record(self.newer, items=[self.item(2, ["scenario:auth"])])
        tick = self.tick()
        self.assertEqual([(a.version, a.item) for a in tick.of_kind("supersede")],
                         [(self.older, 1)])
        self.assertEqual(self.items_of(self.older)[0]["state"], "superseded")
        self.assertEqual(self.items_of(self.newer)[0]["state"], "planned")

    def test_the_newer_wave_supersedes_the_older_and_never_the_reverse(self):
        """Order is ancestry, because shas do not compare.

        Both records claim the same criterion, so a rule that did not order them
        would supersede both — or, worse, whichever the filename sort reached
        first, which is a coin flip on the sha.
        """
        self.record(self.older, items=[self.item(1, ["scenario:auth"])])
        self.record(self.newer, items=[self.item(2, ["scenario:auth"])])
        self.tick()
        self.assertEqual(self.items_of(self.newer)[0]["state"], "planned")

    def test_an_item_claiming_something_else_is_left_alone(self):
        self.record(self.older, items=[self.item(1, ["scenario:billing"])])
        self.record(self.newer, items=[self.item(2, ["scenario:auth"])])
        self.assertEqual(self.tick().of_kind("supersede"), [])
        self.assertEqual(self.items_of(self.older)[0]["state"], "planned")

    def test_overlap_is_read_from_the_newer_versions_armed_scenarios_too(self):
        """A newer version with no plan yet still coalesces.

        The scenario's ``When`` is an *approval*, which happens before anyone
        plans against it, so overlap cannot depend on the newer wave already
        having items. ``ledger/suite-<sha>.json`` dates the criteria that
        version armed — the same reading ``vellum ledger verify`` gives the word
        — and that is a fact about the repository rather than a plan somebody
        has yet to write.
        """
        self.record(self.older, items=[self.item(1, ["scenario:auth"])])
        self.record(self.newer, items=[])
        write_suite(self.ledger, self.newer, ["auth"])
        self.assertEqual([a.item for a in self.tick().of_kind("supersede")], [1])

    def test_a_started_item_is_not_superseded_while_its_lease_is_live(self):
        """"Unstarted" is state *and* lease. "A superseded in-flight item stops"
        is a different sentence with a different remedy — the lease lapses —
        and marking a running item superseded here would take an executor's
        work out of the ledger while it was still being done."""
        lease = new_lease("claude-cloud", at(+1), taken=at(-1))
        self.record(self.older, items=[self.item(1, ["scenario:auth"], lease=lease)])
        self.record(self.newer, items=[self.item(2, ["scenario:auth"])])
        self.assertEqual(self.tick().of_kind("supersede"), [])
        self.assertEqual(self.items_of(self.older)[0]["state"], "planned")

    def test_an_item_already_implementing_is_not_unstarted(self):
        self.record(self.older, items=[self.item(1, ["scenario:auth"], state="implementing")])
        self.record(self.newer, items=[self.item(2, ["scenario:auth"])])
        self.assertEqual(self.tick().of_kind("supersede"), [])

    def test_superseding_is_written_and_not_merely_reported(self):
        self.record(self.older, items=[self.item(1, ["scenario:auth"])])
        self.record(self.newer, items=[self.item(2, ["scenario:auth"])])
        tick = self.tick()
        self.assertTrue(tick.of_kind("supersede")[0].taken)
        self.assertIn(record_path(self.ledger, self.older).name, tick.written)

    def test_a_superseded_item_is_not_filed_and_not_dispatched(self):
        self.record(self.older, items=[self.item(1, ["scenario:auth"])])
        self.record(self.newer, items=[self.item(2, ["scenario:auth"])])
        tick = self.tick(executor="ex")
        self.assertNotIn(1, [a.item for a in tick.of_kind("file-issue")])
        self.assertNotIn(1, [a.item for a in tick.of_kind("claim")])

    def test_ordering_falls_back_to_approval_times_where_git_cannot_answer(self):
        """A record for a commit this checkout does not have still orders.

        ``merge-base --is-ancestor`` cannot answer about a sha the repository
        has never seen — a shallow clone, a record copied in — and the fallback
        is the records' own ``approved`` times, the only other clock the ledger
        has. Weaker, and reported as such, but not silence.
        """
        absent_old = "0" * 40
        absent_new = "1" * 40
        self.record(absent_old, items=[self.item(1, ["scenario:auth"])], approved=at(-48))
        self.record(absent_new, items=[self.item(2, ["scenario:auth"])], approved=at(-1))
        self.assertEqual([a.item for a in self.tick().of_kind("supersede")], [1])

    def test_a_supersede_the_fallback_decided_says_so_in_the_report(self):
        """``_newer`` promises the fallback "is reported rather than silent".

        It is the promise that makes the fallback acceptable at all. A
        supersede is a *write* — the item leaves the queue — and approved times
        are the weaker of the two clocks: two versions approved in the same
        second do not order, and a skew between two approvals orders them
        wrongly. That is the case the docstring itself flags, so a reader has
        to be able to see which answer took their item.
        """
        absent_old = "0" * 40
        absent_new = "1" * 40
        self.record(absent_old, items=[self.item(1, ["scenario:auth"])], approved=at(-48))
        self.record(absent_new, items=[self.item(2, ["scenario:auth"])], approved=at(-1))
        tick = self.tick()
        self.assertEqual([a.item for a in tick.of_kind("supersede")], [1])
        self.assertTrue(
            any("Ancestry could not order" in n for n in tick.notes),
            f"the fallback left no note: {tick.notes}",
        )
        self.assertIn("Ancestry could not order", tick.report())
        self.assertIn("ordered by approved time", tick.of_kind("supersede")[0].detail)

    def test_a_supersede_ancestry_decided_carries_no_such_note(self):
        """The note has to mean something, so the strong ordering stays quiet."""
        self.record(self.older, items=[self.item(1, ["scenario:auth"])])
        self.record(self.newer, items=[self.item(2, ["scenario:auth"])])
        tick = self.tick()
        self.assertEqual([a.item for a in tick.of_kind("supersede")], [1])
        self.assertFalse(any("Ancestry could not order" in n for n in tick.notes))
        self.assertNotIn("ordered by approved time", tick.of_kind("supersede")[0].detail)


# ==========================================================  @id:fire-and-collect


class TestTheLeaseIsAMutex(TickCase):
    """`@id:fire-and-collect` — the reconciler never puts two runs on one item.

    "Given an executor mid-run on a claimed work item". ``spec/features/ledger.md``
    defines mid-run as holding an unexpired lease, so these are the tests that
    ``active_lease`` is asked before ``take_lease`` and that its answer is
    obeyed.
    """

    def claimed(self, expires_in_hours, executor="claude-cloud"):
        lease = new_lease(executor, at(expires_in_hours), taken=at(-1))
        self.record(self.newer, state="implementing",
                    items=[self.item(1, ["scenario:auth"], state="implementing", lease=lease)])

    def test_a_claimed_unexpired_item_is_not_re_dispatched(self):
        self.claimed(+1)
        tick = self.tick(executor="a-second-executor")
        self.assertEqual(tick.of_kind("claim"), [])
        self.assertEqual(tick.of_kind("dispatch"), [])
        self.assertEqual([a.item for a in tick.of_kind("hold")], [1])

    def test_the_holders_lease_is_not_overwritten_by_the_tick(self):
        """The double-claim this forbids is a write, not just a dispatch."""
        self.claimed(+1, executor="claude-cloud")
        before = record_path(self.ledger, self.newer).read_bytes()
        tick = self.tick(executor="a-second-executor")
        self.assertEqual(tick.written, [])
        self.assertEqual(record_path(self.ledger, self.newer).read_bytes(), before)
        self.assertEqual(self.items_of(self.newer)[0]["lease"]["executor"], "claude-cloud")

    def test_an_expired_lease_returns_the_item_to_the_queue(self):
        self.claimed(-1)
        tick = self.tick(executor="a-second-executor")
        self.assertEqual([a.item for a in tick.of_kind("claim")], [1])
        self.assertEqual([a.item for a in tick.of_kind("dispatch")], [1])
        self.assertEqual(self.items_of(self.newer)[0]["lease"]["executor"], "a-second-executor")

    def test_a_lease_expiring_exactly_now_is_not_held(self):
        """Expiry is exclusive — a lease is held *until* it expires — which is
        ``active_lease``'s reading, resolved there once rather than here."""
        self.claimed(0)
        self.assertEqual([a.item for a in self.tick(executor="ex").of_kind("claim")], [1])

    def test_a_lease_with_an_unreadable_expiry_is_no_lease(self):
        self.record(self.newer, state="implementing", items=[
            self.item(1, state="implementing",
                      lease={"executor": "ghost", "taken": at(-1), "expires": "whenever"})
        ])
        self.assertEqual([a.item for a in self.tick(executor="ex").of_kind("claim")], [1])

    def test_a_claim_is_stamped_with_the_ticks_own_moment(self):
        self.record(self.newer, items=[self.item(1)])
        self.tick(executor="ex", lease_minutes=30)
        lease = self.items_of(self.newer)[0]["lease"]
        self.assertEqual(lease["taken"], NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(lease["expires"], at(0.5))

    def test_without_an_executor_a_ready_item_is_reported_and_not_claimed(self):
        """A lease names its holder, so a tick with nobody to name takes none."""
        self.record(self.newer, items=[self.item(1)])
        tick = self.tick()
        self.assertEqual([a.item for a in tick.of_kind("dispatch")], [1])
        self.assertEqual(tick.of_kind("claim"), [])
        self.assertIsNone(self.items_of(self.newer)[0]["lease"])

    def test_a_claim_taken_with_no_observed_state_is_named_in_the_report(self):
        """A claim is the one action a tick *writes* on a fact it cannot check.

        With no ``--observed`` nothing says this item's issue was ever filed,
        and the lease goes on anyway. The claim is still taken — "absent
        observed = repository state alone" is this command's contract, and the
        mutex above is a property of repository state that holds with no forge
        in reach — but leasing an unfiled item is a foot-gun, so it is named
        rather than silent.
        """
        self.record(self.newer, items=[self.item(1)])
        tick = self.tick(executor="ex")
        self.assertEqual([a.item for a in tick.of_kind("claim")], [1])
        self.assertIn("A claim wants observed state", tick.report())
        self.assertIn("Work item(s) 1", tick.report())

    def test_a_claim_taken_with_observed_state_is_not(self):
        self.record(self.newer, items=[self.item(1)])
        tick = self.tick(observed=self.observed(issues=[1]), executor="ex")
        self.assertEqual([a.item for a in tick.of_kind("claim")], [1])
        self.assertNotIn("A claim wants observed state", tick.report())

    def test_an_item_held_by_someone_else_is_not_reported_as_leased_unconfirmed(self):
        """The caveat names leases that were really taken, not ones considered.

        `_claim` writes nothing for an item somebody already holds, so counting
        the item before the call would have the report announce a lease that
        does not exist — the mutex working correctly, described as a foot-gun.
        """
        self.claimed(+1, executor="claude-cloud")
        tick = self.tick(executor="a-second-executor")
        self.assertEqual(tick.of_kind("claim"), [])
        self.assertNotIn("A claim wants observed state", tick.report())

    def test_a_tick_with_no_executor_writes_no_lease_and_so_needs_no_caveat(self):
        """Nothing is claimed, so there is no unchecked write to warn about."""
        self.record(self.newer, items=[self.item(1)])
        tick = self.tick()
        self.assertEqual(tick.of_kind("claim"), [])
        self.assertNotIn("A claim wants observed state", tick.report())

    def test_a_reported_item_with_a_pr_is_not_dispatched_again(self):
        self.record(self.newer, state="implementing",
                    items=[self.item(1, state="implementing", pr=7)])
        self.assertEqual(self.tick(executor="ex").of_kind("dispatch"), [])


class TestSteeringIsAFreshRun(TickCase):
    """`@id:fire-and-collect`, the direction half.

    "Then the current run is ended or its lease is left to lapse / And a fresh
    run is spawned with a briefing carrying the direction." There is no mid-run
    channel (``spec/decisions/2026-08-28-fire-and-collect-executors.md``), so the
    reconciler records the direction and lets the lease lapse; the fresh run is
    the next tick's dispatch.
    """

    def setUp(self):
        super().setUp()
        self.direction = "use the new sign-in flow"

    def leased(self, hours):
        lease = new_lease("claude-cloud", at(hours), taken=at(-1))
        self.record(self.newer, state="implementing",
                    items=[self.item(1, state="implementing", lease=lease, briefing="old")])

    def observed_direction(self):
        return self.observed(
            issues=[1],
            directions=[{"version": self.newer, "item": 1, "briefing": self.direction}],
        )

    def test_direction_is_recorded_on_the_item_while_the_run_continues(self):
        self.leased(+1)
        tick = self.tick(observed=self.observed_direction(), executor="ex")
        self.assertEqual([a.item for a in tick.of_kind("record-direction")], [1])
        self.assertEqual(self.items_of(self.newer)[0]["briefing"], self.direction)

    def test_a_running_executor_is_not_steered_and_not_replaced(self):
        self.leased(+1)
        tick = self.tick(observed=self.observed_direction(), executor="ex")
        self.assertEqual(tick.of_kind("dispatch"), [])
        self.assertEqual(self.items_of(self.newer)[0]["lease"]["executor"], "claude-cloud")
        self.assertIn("left to lapse", " ".join(a.detail for a in tick.of_kind("hold")))

    def test_once_the_lease_lapses_a_fresh_run_carries_the_direction(self):
        self.leased(-1)
        tick = self.tick(observed=self.observed_direction(), executor="ex")
        dispatched = tick.of_kind("dispatch")
        self.assertEqual([a.item for a in dispatched], [1])
        self.assertIn(self.direction, dispatched[0].detail)

    def test_recording_the_same_direction_twice_writes_once(self):
        self.leased(-1)
        self.tick(observed=self.observed_direction(), executor="ex")
        before = record_path(self.ledger, self.newer).read_bytes()
        second = self.tick(observed=self.observed_direction(), executor="ex")
        self.assertEqual(second.of_kind("record-direction"), [])
        self.assertEqual(record_path(self.ledger, self.newer).read_bytes(), before)


# ======================================================  @id:corpus-answer-bounces


class TestCorpusAnswerableQuestionsBounce(TickCase):
    """`@id:corpus-answer-bounces`.

    "Given an agent question whose answer exists in spec/decisions / When the
    question is raised / Then the orchestrator replies with the decision
    reference / And no question issue is opened."
    """

    ANSWER = (
        "---\nid: decision-retries\ntitle: \"Retries\"\ndate: 2026-08-28\n---\n\n"
        "# Retries\n\nA flaky certification run is retried twice and then quarantined.\n"
    )

    def setUp(self):
        super().setUp()
        commit_files(
            self.repo,
            {"spec/decisions/2026-08-28-retries.md": self.ANSWER},
            "spec: record the decision that answers the question",
        )
        self.record(self.newer, items=[self.item(1)])

    def raise_question(self, asks, **kwargs):
        return self.tick(
            observed=self.observed(
                issues=[1], raised=[{"version": self.newer, "item": 1, "asks": asks}]
            ),
            **kwargs,
        )

    def test_a_question_the_corpus_answers_is_answered_and_files_nothing(self):
        tick = self.raise_question("How is a flaky certification run retried?")
        answered = tick.of_kind("answer-question")
        self.assertEqual(len(answered), 1)
        self.assertIn("spec/decisions/2026-08-28-retries.md", answered[0].detail)
        self.assertEqual(tick.of_kind("open-question"), [],
                         "no question issue is opened for a corpus-answerable question")

    def test_a_question_the_corpus_does_not_answer_escalates_and_parks_the_item(self):
        tick = self.raise_question("Should settlement use kafka or rabbitmq?", executor="ex")
        self.assertEqual(len(tick.of_kind("open-question")), 1)
        self.assertEqual(tick.of_kind("answer-question"), [])
        self.assertEqual(tick.of_kind("claim"), [], "a parked item is not dispatched")
        self.assertIn("parked behind an open question",
                      " ".join(a.detail for a in tick.of_kind("hold")))

    def test_an_unanswered_question_is_raised_to_the_architect_not_the_owner(self):
        """``@id:architect-answers-before-owner``: the ladder is corpus, then
        architect, then owner (spec/decisions/2026-09-03-architect-answers-first.md).
        The forge half a tick emits for a corpus miss names the architect as the
        issue's addressee; the owner is the next rung, reached only by the
        architect's judgment or the coding agent's appeal — never by the tick."""
        tick = self.raise_question("Should settlement use kafka or rabbitmq?")
        (opened,) = tick.of_kind("open-question")
        self.assertIn("mentioning the architect", opened.detail)
        self.assertNotIn("mentioning the owner", opened.detail)
        self.assertIn("appeal", opened.detail, "the appeal path is named, not implied")
        self.assertFalse(opened.taken, "opening the issue is the forge's half")

    def test_answering_writes_nothing_to_the_ledger(self):
        """The reply is a forge action; the ledger records no question at all."""
        tick = self.raise_question("How is a flaky certification run retried?")
        self.assertEqual(tick.written, [])
        self.assertFalse(tick.of_kind("answer-question")[0].taken)

    def test_a_question_with_one_significant_term_is_never_answered_mechanically(self):
        """At one term a match is a coincidence, so it escalates instead."""
        self.assertEqual(self.raise_question("retried?").of_kind("answer-question"), [])
        self.assertEqual(len(self.raise_question("retried?").of_kind("open-question")), 1)

    def test_the_threshold_is_a_knob_and_its_default_is_the_strict_end(self):
        loose = "How many minutes may a flaky certification run be retried for?"
        self.assertEqual(self.raise_question(loose).of_kind("answer-question"), [])
        self.assertEqual(
            len(self.raise_question(loose, corpus_match=0.6).of_kind("answer-question")), 1
        )

    def test_the_briefing_is_part_of_the_corpus(self):
        self.record(self.newer, items=[self.item(1, briefing="Settlement uses kafka, never rabbitmq.")])
        answered = self.raise_question("Should settlement use kafka or rabbitmq?")
        self.assertIn("briefing", answered.of_kind("answer-question")[0].detail)

    def test_the_search_is_a_function_of_the_tree_and_is_tested_directly(self):
        self.assertIsNotNone(
            corpus_answer(self.repo, "How is a flaky certification run retried?")
        )
        self.assertIsNone(corpus_answer(self.repo, "kafka rabbitmq settlement"))

    def test_question_terms_drop_the_words_that_discriminate_nothing(self):
        terms = question_terms("Which of these should the executor retry?")
        self.assertIn("executor", terms)
        self.assertIn("retry", terms)
        for noise in ("which", "these", "should"):
            self.assertNotIn(noise, terms)


# ========  @id:comment-becomes-clarify-pr, @id:clarify-merge-closes-question


class TestQuestionsResolveIntoFiles(TickCase):
    """`@id:comment-becomes-clarify-pr` and `@id:clarify-merge-closes-question`.

    Both scenarios have a forge-triggered half — a comment arriving, a PR
    merging — that stays a deployment property. What is asserted here is the
    reconciler's decision and bookkeeping half: given an open question with an
    owner comment, a clarify PR is what should be drafted; given one whose
    clarify PR has merged, the issue should be closed and the item resumes.
    """

    def setUp(self):
        super().setUp()
        self.record(self.newer, state="implementing", items=[self.item(1)])

    def question(self, **fields):
        entry = {"issue": 40, "version": self.newer, "item": 1, "opened": at(-1)}
        entry.update(fields)
        return self.observed(issues=[1], questions=[entry])

    def test_an_owner_comment_becomes_a_clarify_draft(self):
        tick = self.tick(observed=self.question(comments=[{"author": "owner", "body": "two"}]))
        drafts = tick.of_kind("draft-clarify")
        self.assertEqual(len(drafts), 1)
        self.assertIn("spec:clarify", drafts[0].detail)
        self.assertIn("40", drafts[0].detail)

    def test_an_open_question_with_no_comment_yet_drafts_nothing(self):
        self.assertEqual(self.tick(observed=self.question()).of_kind("draft-clarify"), [])

    def test_a_clarify_pr_that_already_exists_is_not_drafted_twice(self):
        tick = self.tick(observed=self.question(
            comments=[{"author": "owner", "body": "two"}], clarify_pr=55
        ))
        self.assertEqual(tick.of_kind("draft-clarify"), [])

    def test_an_open_question_parks_its_item_and_others_continue(self):
        self.record(self.newer, state="implementing", items=[self.item(1), self.item(2)])
        tick = self.tick(observed=self.observed(
            issues=[1, 2],
            questions=[{"issue": 40, "version": self.newer, "item": 1, "opened": at(-1)}],
        ), executor="ex")
        self.assertEqual([a.item for a in tick.of_kind("claim")], [2])
        self.assertIn(1, [a.item for a in tick.of_kind("hold")])

    def test_a_merged_clarify_pr_closes_the_question_and_resumes_the_item(self):
        tick = self.tick(
            observed=self.question(clarify_pr=55, clarify_merged=True), executor="ex"
        )
        closed = tick.of_kind("close-question")
        self.assertEqual(len(closed), 1)
        self.assertIn("#55", closed[0].detail)
        self.assertEqual([a.item for a in tick.of_kind("claim")], [1],
                         "the item is no longer parked once the question resolves")

    def test_an_issue_closed_by_hand_with_no_file_counts_as_withdrawn(self):
        tick = self.tick(observed=self.question(closed=True))
        self.assertEqual(tick.actions, [] if not tick.actions else tick.actions)
        self.assertIn("withdrawing the question", " ".join(tick.notes))

    def test_past_the_timebox_the_wave_parks(self):
        tick = self.tick(observed=self.question(opened=at(-25)))
        self.assertTrue(tick.blocked)
        self.assertEqual(tick.parked[0][1], 40)
        self.assertIn("PARKED", tick.report())

    def test_inside_the_timebox_the_wave_does_not_park(self):
        self.assertFalse(self.tick(observed=self.question(opened=at(-23))).blocked)

    def test_the_installations_timebox_is_read_from_its_config(self):
        config = self.repo / ".vellum" / "config.yaml"
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        data["questions"] = {"timebox_hours": 1}
        config.write_text(yaml.safe_dump(data), encoding="utf-8")
        self.assertTrue(self.tick(observed=self.question(opened=at(-2))).blocked)

    def test_a_config_with_no_timebox_uses_the_specs_default_and_says_so(self):
        tick = self.tick(observed=self.question(opened=at(-25)))
        self.assertIn("spec's default of 24h", " ".join(tick.notes))


# ================================================================  the command


class TestCommandLine(TickCase):
    def test_a_converged_tick_exits_zero(self):
        self.record(self.newer, items=[self.item(1)])
        code, out, _ = run_cli(["tick", str(self.repo)])
        self.assertEqual(code, 0)
        self.assertIn("OK: the tick converged", out)

    def test_a_parked_wave_is_the_only_thing_that_exits_one(self):
        """1 is "an answer you will not like", the code a caller blocks on.

        Everything else this command can go wrong at is 2, for the reason
        ``backpressure`` splits them: a caller that learns to read "non-zero"
        cannot tell an armed gate from a broken one.
        """
        self.record(self.newer, state="implementing", items=[self.item(1)])
        observed = self.observed(
            issues=[1],
            questions=[{"issue": 40, "version": self.newer, "item": 1, "opened": at(-25)}],
        )
        code, out, err = run_cli(
            ["tick", str(self.repo), "--observed", observed, "--now", at(0)]
        )
        self.assertEqual(code, 1)
        self.assertIn("PARKED", out)
        self.assertIn("past the timebox", err)

    def test_a_checkout_with_no_ledger_exits_two(self):
        code, _, err = run_cli(["tick", str(Path(self.tmp.name) / "nowhere")])
        self.assertEqual(code, 2)
        self.assertIn("no ledger directory", err)

    def test_an_unreadable_now_exits_two(self):
        code, _, err = run_cli(["tick", str(self.repo), "--now", "sometime"])
        self.assertEqual(code, 2)
        self.assertIn("ISO 8601", err)

    def test_a_plan_without_a_version_exits_two(self):
        plan = Path(self.tmp.name) / "workplan.yaml"
        plan.write_text("work_items: [{issue: 1, title: t, repo: app}]\n", encoding="utf-8")
        code, _, err = run_cli(["tick", str(self.repo), "--plan", str(plan)])
        self.assertEqual(code, 2)
        self.assertIn("--plan needs --version", err)

    def test_a_version_naming_no_record_exits_two(self):
        self.record(self.newer, items=[self.item(1)])
        code, _, err = run_cli(["tick", str(self.repo), "--version", "f" * 40])
        self.assertEqual(code, 2)
        self.assertIn("names 0 of the", err)

    def test_a_lease_that_expires_when_it_is_taken_is_refused(self):
        code, _, err = run_cli(["tick", str(self.repo), "--lease-minutes", "0"])
        self.assertEqual(code, 2)
        self.assertIn("must be positive", err)

    def test_dry_run_computes_everything_and_writes_nothing(self):
        self.record(self.older, items=[self.item(1, ["scenario:auth"])])
        self.record(self.newer, items=[self.item(2, ["scenario:auth"])])
        before = record_path(self.ledger, self.older).read_bytes()
        code, out, _ = run_cli(["tick", str(self.repo), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("[supersede]", out)
        self.assertIn("--dry-run: nothing was written", out)
        self.assertEqual(record_path(self.ledger, self.older).read_bytes(), before)

    def test_json_goes_to_stdout_and_every_diagnostic_to_stderr(self):
        """A parked tick still parses — the property ``budget --json`` has."""
        self.record(self.newer, state="implementing", items=[self.item(1)])
        observed = self.observed(
            issues=[1],
            questions=[{"issue": 40, "version": self.newer, "item": 1, "opened": at(-25)}],
        )
        code, out, err = run_cli(
            ["tick", str(self.repo), "--observed", observed, "--now", at(0), "--json"]
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertTrue(payload["parked_wave"])
        self.assertEqual(payload["parked"][0]["issue"], 40)
        self.assertTrue(err.strip(), "the denial is still reported, on stderr")

    def test_the_whole_loop_runs_end_to_end_through_the_cli(self):
        """Open, plan, file, claim — the sequence a live installation ticks."""
        run_cli(["ledger", "open", "--version", self.newer, "--ledger-dir", str(self.ledger)])
        plan = Path(self.tmp.name) / "workplan.yaml"
        plan.write_text(
            "work_items:\n- {issue: 9, title: sign-in, repo: app, satisfies: [scenario:auth]}\n",
            encoding="utf-8",
        )
        code, out, _ = run_cli(
            ["tick", str(self.repo), "--version", self.newer, "--plan", str(plan)]
        )
        self.assertEqual(code, 0)
        self.assertIn("[commit-plan]", out)
        self.assertIn("[file-issue]", out)
        observed = self.observed(issues=[9])
        code, out, _ = run_cli(
            ["tick", str(self.repo), "--observed", observed, "--executor", "claude-cloud"]
        )
        self.assertEqual(code, 0)
        self.assertIn("[claim]", out)
        self.assertEqual(self.items_of(self.newer)[0]["lease"]["executor"], "claude-cloud")


class TestObservedStateIsRefusedRatherThanMisread(TickCase):
    """A shape the tick cannot read is refused, never read as "nothing there".

    The two are opposite instructions. "Nothing is filed" makes a tick file
    every issue again; "nobody holds a lease" makes it dispatch an item somebody
    is already running. Both are the failures the observed/desired split exists
    to avoid, so a mistyped file is exit 2 and not a confident tick.
    """

    def bad(self, text):
        path = Path(self.tmp.name) / "observed.yaml"
        path.write_text(text, encoding="utf-8")
        return ["tick", str(self.repo), "--observed", str(path)]

    def test_a_scalar_where_a_list_belongs_is_refused(self):
        code, _, err = run_cli(self.bad("questions: 3\n"))
        self.assertEqual(code, 2)
        self.assertIn("expected a list of mappings", err)

    def test_an_issue_that_is_not_a_number_is_refused(self):
        code, _, err = run_cli(self.bad("issues: [seven]\n"))
        self.assertEqual(code, 2)
        self.assertIn("not an issue number", err)

    def test_a_question_naming_no_issue_is_refused(self):
        code, _, err = run_cli(self.bad("questions: [{item: 1}]\n"))
        self.assertEqual(code, 2)
        self.assertIn("names no issue number", err)

    def test_a_direction_naming_no_item_is_refused(self):
        code, _, err = run_cli(self.bad("directions: [{briefing: go}]\n"))
        self.assertEqual(code, 2)
        self.assertIn("names no work item", err)

    def test_a_falsy_scalar_where_the_issue_list_belongs_is_refused(self):
        """The four shapes ``issues: ... or []`` swallowed before the type check.

        Each one used to parse as "observed, and nothing is filed" — which is
        not a weaker answer than the truth, it is the opposite instruction: the
        tick emits ``file-issue`` for every planned item, telling the caller to
        re-file issues that already exist. The sibling ``_mappings`` helper
        reads its key off the raw value for exactly this reason, and this one
        does now too: only absent and ``[]`` are empty.
        """
        for text in ("issues: 0\n", "issues: false\n", "issues: ''\n", "issues: {}\n"):
            with self.subTest(shape=text.strip()):
                code, out, err = run_cli(self.bad(text))
                self.assertEqual(code, 2)
                self.assertIn("expected a list of issue numbers", err)
                self.assertNotIn("file-issue", out)

    def test_an_absent_or_empty_issue_list_is_the_one_thing_that_is_empty(self):
        """The refusal above must not swallow the shapes that really are empty."""
        for text in ("", "issues:\n", "issues: []\n"):
            with self.subTest(shape=text.strip() or "(empty file)"):
                code, _, err = run_cli(self.bad(text))
                self.assertEqual(code, 0, err)

    def test_a_malformed_comments_shape_is_refused(self):
        """A question's ``comments`` was the parser's one silent downgrade.

        Its failure direction is the safe one — a missed ``draft-clarify``
        heals on the next tick — but a refuse-not-misread rule with a quiet
        exception is not a rule a caller can lean on.
        """
        code, _, err = run_cli(self.bad("questions: [{issue: 9, comments: 'a comment'}]\n"))
        self.assertEqual(code, 2)
        self.assertIn("'comments' is str, expected a list", err)

    def test_an_unreadable_file_is_refused(self):
        code, _, err = run_cli(
            ["tick", str(self.repo), "--observed", str(self.repo / "nope.yaml")]
        )
        self.assertEqual(code, 2)
        self.assertIn("cannot read the observed state", err)

    def test_an_empty_file_is_supplied_observed_state_holding_nothing(self):
        path = Path(self.tmp.name) / "observed.yaml"
        path.write_text("", encoding="utf-8")
        tick = self.tick(observed=str(path))
        self.assertTrue(tick.observed_supplied)

    def test_a_ledger_file_that_is_not_a_record_is_reported_and_skipped(self):
        (self.ledger / "junk.yaml").write_text("[1, 2, 3]\n", encoding="utf-8")
        self.record(self.newer, items=[self.item(1)])
        tick = self.tick()
        self.assertEqual(tick.unreadable, ["junk.yaml"])
        self.assertIn("could not be read as records", tick.report())


class TestTheReportIsNotAWorkflowCommandChannel(TickCase):
    """Every string the report prints that came from outside is narrowed first.

    A caller may pipe this into a runner's step summary the way ``spec-ci.yml``
    pipes ``backpressure``'s, where a value carrying a newline starts a line of
    its own — and a line of its own is all ``::add-mask`` needs. The values here
    arrive from an observed-state file and from the intent repo's ``ledger/``,
    both of which are written by whoever can land a merge there.
    """

    def test_a_briefing_spanning_lines_reaches_the_report_as_one_line(self):
        self.record(self.newer, items=[self.item(1)])
        observed = self.observed(
            issues=[1],
            directions=[{
                "version": self.newer, "item": 1,
                "briefing": "ok\n::add-mask::secret\nmore",
            }],
        )
        report = self.tick(observed=observed, executor="ex").report()
        for line in report.splitlines():
            self.assertNotEqual(line.strip(), "::add-mask::secret")

    def test_a_question_spanning_lines_reaches_the_report_as_one_line(self):
        self.record(self.newer, items=[self.item(1)])
        observed = self.observed(
            raised=[{"version": self.newer, "item": 1, "asks": "why\n::error::forged\n"}]
        )
        report = self.tick(observed=observed).report()
        self.assertNotIn("\n::error::forged", report)

    def test_an_executor_name_spanning_lines_reaches_the_report_as_one_line(self):
        lease = new_lease("holder\n::error::forged", at(+1), taken=at(-1))
        self.record(self.newer, state="implementing",
                    items=[self.item(1, state="implementing", lease=lease)])
        report = self.tick(executor="ex").report()
        self.assertNotIn("\n::error::forged", report)

    def test_an_unreadable_ledger_filename_spanning_lines_is_narrowed(self):
        """A ledger filename is an outside string like any other.

        Anyone who can land a merge on the intent repo writes ``ledger/``, git
        permits a newline inside a filename, and the ``*.yaml`` glob matches
        across one — so a name printed verbatim is a free line in whatever step
        summary the caller pipes this into.
        """
        (self.ledger / "evil\n::error::pwned .yaml").write_text("[1, 2, 3]\n", encoding="utf-8")
        self.record(self.newer, items=[self.item(1)])
        report = self.tick().report()
        self.assertNotIn("\n::error::pwned", report)
        for line in report.splitlines():
            self.assertNotEqual(line.strip(), "::error::pwned .yaml")

    def test_a_written_ledger_filename_spanning_lines_is_narrowed(self):
        """The same for the *written* list, which is the reachable half too.

        A record is keyed by the ``spec_version`` it carries and not by what it
        is called, so a valid record under a crafted name is a real record —
        and the moment a tick writes it, its name reaches the report.
        """
        record = self.record(self.newer, items=[self.item(1, ["scenario:auth"])])
        crafted = self.ledger / "evil\n::error::pwned .yaml"
        crafted.write_text(record.read_text(encoding="utf-8"), encoding="utf-8")
        record.unlink()
        report = self.tick(executor="ex").report()
        self.assertIn("Ledger files written:", report)
        for line in report.splitlines():
            self.assertNotEqual(line.strip(), "::error::pwned .yaml")

    def test_the_briefing_is_written_to_yaml_by_the_serialiser_not_by_hand(self):
        """A briefing is interpolated into no string on the way to the ledger.

        ``pin.py`` learned this the hard way with a record's ``name``: a value
        f-strung into YAML wrote a second key. Everything this module writes
        goes through ``ledger.dump``, so the check is that a briefing designed
        to forge one comes back as a single scalar.
        """
        self.record(self.newer, items=[self.item(1)])
        forged = "ok\nstate: superseded\n"
        observed = self.observed(
            issues=[1],
            directions=[{"version": self.newer, "item": 1, "briefing": forged}],
        )
        self.tick(observed=observed)
        item = self.items_of(self.newer)[0]
        self.assertEqual(item["briefing"], forged)
        self.assertEqual(item["state"], "planned", "the forged key did not become a state")


class TestTheLibraryRefusesWhatTheCommandRefuses(TickCase):
    """The refusals live below the CLI, so a second caller cannot skip them."""

    def test_corpus_match_outside_its_range_is_refused(self):
        for bad in (0.0, -1.0, 1.5):
            with self.assertRaises(TickError):
                self.tick(corpus_match=bad)

    def test_a_plan_without_a_single_record_is_refused(self):
        self.record(self.older, items=[])
        self.record(self.newer, items=[])
        with self.assertRaises(TickError):
            self.tick(plan=[{"issue": 1, "title": "t", "repo": "app"}])

    def test_the_default_lease_is_the_commands_own_and_the_report_says_so(self):
        self.record(self.newer, items=[self.item(1)])
        tick = self.tick(executor="ex")
        self.assertIn(f"{DEFAULT_LEASE_MINUTES} minute(s)", tick.report())
        self.assertIn("not the installation's", tick.report())


class TestTheSandboxIsARealRepository(TickCase):
    """Guards on the fixtures themselves: a stipulated Given proves nothing."""

    def test_the_two_versions_really_do_order_by_ancestry(self):
        out = git(self.repo, "merge-base", "--is-ancestor", self.older, self.newer)
        self.assertEqual(out, "")

    def test_the_records_are_the_shape_the_ledger_writes(self):
        self.record(self.newer, items=[self.item(1)])
        data = load(record_path(self.ledger, self.newer))
        self.assertEqual(data["spec_version"], self.newer)
        self.assertEqual(data["work_items"][0]["lease"], None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
