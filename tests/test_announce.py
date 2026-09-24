"""``vellum announce``: the addressed event, and the dispatch its arrival causes.

One class per acceptance scenario of ``spec/features/continuous-engineering.md``,
because that is the only reason any of this behavior exists. The sandboxes are
real git repositories with a real ``.vellum/config.yaml`` and real ledger records
written through ``vellum ledger``, for ``test_reconcile.py``'s reason: the
addressee is read out of an installation's own ``write_boundaries`` block, and a
fixture that stipulated the addressee would be testing the stipulation.

The decision under all of it is
``spec/decisions/2026-09-12-collection-is-caused-not-scheduled.md``: the defect
is not an absent channel but an absent **cause**. So the assertions here are
mostly about *what causes what* — an announcement recorded by the run at its own
boundary, a dispatch that is a function of that record and of nothing else, and
a delivery that happens once.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path

import yaml

from support import commit_files, git, make_git_intent_repo, run_cli, run_cli_streams
from vellum.announce import AnnounceError, deliver, read_handoff
from vellum.ledger import find_item, load, record_path, write

#: The installation `vellum init` seeds, spelled here because these tests are
#: about reading an addressee out of it. `librarian: [ledger, ...]` is what makes
#: `ledger/` a tree with exactly one declared holder, and `harness-engineer:
#: [harness]` is the other edge of the same two-role block — the pair the
#: decision's own evidence (waviisoft/portolan#33) is about.
SEEDED_BOUNDARIES = {
    "harness-engineer": ["harness"],
    "librarian": ["ledger", ".vellum/memory"],
}

#: A version sha for the record every fixture opens. Forty hex characters
#: because `ledger` refuses anything shorter for a record it writes.
VERSION = "a" * 40

#: The item each test is about, and a control item in the same record that
#: differs in exactly the fact under test — `steps/orchestration.py`'s pair, and
#: the same reason: a product that did the same thing to every item it read
#: could not pass.
SUBJECT = 1
CONTROL = 2

#: The tick's clock in every test that has one, and a moment after any lease
#: taken at it has lapsed.
NOW = "2026-01-17T01:00:00Z"
AFTER_LEASE = "2026-01-17T03:00:00Z"

#: What a blocked run tried, observed and proved, in its own words — the three
#: the slice asks a handoff to carry so "the receiver's job is to verify, not to
#: rediscover" is true of the record rather than of the sender's intentions.
TRIED = "ran the scenario against the checkout and watched it go red"
OBSERVED = "the step reads a field the record does not carry"
PROVED = "the five-line fix turns it green, twice, on this checkout"
ASKS = "apply the proven fix under harness/ so work item 1 can go green"

OWNER_REVIEW = "Owner review: rework sign-in against the new rules"


def _intent(root: Path, boundaries=None) -> Path:
    """A real intent checkout with a two-item wave in it, built by the product."""
    repo = make_git_intent_repo(
        root, boundaries=SEEDED_BOUNDARIES if boundaries is None else boundaries
    )
    ledger = repo / "ledger"
    code, said = run_cli(["ledger", "open", "--version", VERSION,
                          "--ledger-dir", str(ledger), "--approved", NOW])
    assert code == 0, said
    for issue, title in ((SUBJECT, "Implement the auth slice"),
                         (CONTROL, "Implement the billing slice")):
        code, said = run_cli([
            "ledger", "advance", "--version", VERSION, "--ledger-dir", str(ledger),
            "--item", str(issue), "--title", title, "--repo", "app",
            "--item-state", "planned",
        ])
        assert code == 0, said
    code, said = run_cli(["ledger", "advance", "--version", VERSION,
                          "--ledger-dir", str(ledger), "--state", "approved"])
    assert code == 0, said
    return repo


def _item(repo: Path, issue: int = SUBJECT) -> dict:
    return find_item(load(record_path(repo / "ledger", VERSION)), issue)


def _tick(repo: Path, *extra, now: str = AFTER_LEASE, executor: str = "librarian"):
    """``vellum tick --json`` over *repo*, as the harness drives it."""
    argv = ["tick", str(repo), "--ledger-dir", str(repo / "ledger"), "--now", now,
            "--json"]
    if executor is not None:
        argv += ["--executor", executor]
    argv += list(extra)
    code, out, err = run_cli_streams(argv)
    assert code in (0, 1), f"tick exited {code}: {out}{err}"
    return json.loads(out)


def _dispatches(payload: dict, item: int | None = None) -> list[dict]:
    return [a for a in payload["actions"]
            if a["kind"] == "dispatch" and (item is None or a["item"] == item)]


def _addressed(payload: dict, item: int | None = None) -> list[dict]:
    """Every dispatch this payload addressed to a role.

    The shape the harness reads (`_addressed_dispatches`): a `dispatch` action
    carrying the role it is addressed to. Spelled here as a truthy `role` rather
    than as "the key is present", because an unaddressed dispatch must not be
    read as addressed to the empty string.
    """
    return [a for a in _dispatches(payload, item) if a.get("role")]


def _handoffs(repo: Path) -> dict[str, str]:
    tree = repo / "ledger" / "handoffs"
    if not tree.is_dir():
        return {}
    return {p.name: p.read_text(encoding="utf-8")
            for p in sorted(tree.iterdir()) if p.is_file()}


def _finish(repo: Path, *extra, item: int = SUBJECT, pr: int = 7):
    """``vellum ledger advance --pr``, against *repo*'s own installation.

    ``--checkout`` is required now (S4): the addressee comes from *repo*'s own
    ``write_boundaries``, never guessed from ``--ledger-dir``'s parent.
    """
    argv = ["ledger", "advance", "--version", VERSION, "--ledger-dir",
            str(repo / "ledger"), "--checkout", str(repo), "--item", str(item),
            "--pr", str(pr)]
    argv += list(extra)
    return run_cli(argv)


def _answer(repo: Path, name: str, *extra, by: str = "harness-engineer"):
    argv = ["announce", "answer", str(repo), "--ledger-dir", str(repo / "ledger"),
            "--handoff", name, "--by", by]
    argv += list(extra)
    return run_cli(argv)


def _record_handoff(repo: Path, *extra, item: int = SUBJECT):
    argv = ["announce", "handoff", str(repo), "--version", VERSION,
            "--ledger-dir", str(repo / "ledger"), "--item", str(item),
            "--from", "librarian", "--asks", ASKS,
            "--tried", TRIED, "--observed", OBSERVED, "--proved", PROVED,
            "--path", "harness/steps.py", "--now", NOW, "--json"]
    argv += list(extra)
    return run_cli_streams(argv)


# --------------------------------------------- a-blocked-unit-hands-off

class HandoffIsARecord(unittest.TestCase):
    """A run that cannot proceed records a durable, addressed handoff."""

    def test_one_file_per_handoff_under_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo)
            self.assertEqual(code, 0, out + err)
            recorded = _handoffs(repo)
            self.assertEqual(len(recorded), 1, recorded)

    def test_the_record_names_the_role_that_may_make_the_change(self):
        """The addressee is computed from the installation's own declaration."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            text = next(iter(_handoffs(repo).values()))
            self.assertIn("to: harness-engineer", text)
            self.assertIn("harness/steps.py", text)

    def test_the_record_carries_the_evidence_and_its_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            text = next(iter(_handoffs(repo).values()))
            for evidence in (TRIED, OBSERVED, PROVED):
                self.assertIn(evidence, text)
            self.assertIn(str(SUBJECT), text)

    def test_a_handoff_is_never_addressed_back_to_its_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--to", "librarian")
            self.assertEqual(code, 2, out)
            self.assertIn("librarian", out + err)

    def test_an_undeclared_addressee_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--to", "nobody-declared")
            self.assertEqual(code, 2, out)
            self.assertIn("nobody-declared", out + err)

    def test_a_tree_with_no_declared_holder_is_refused_rather_than_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp), boundaries={"librarian": ["ledger"]})
            code, out, err = _record_handoff(repo)
            self.assertEqual(code, 2, out)
            self.assertIn("harness", out + err)

    def test_a_tree_with_two_declared_holders_is_refused_rather_than_picked(self):
        """`write_boundaries` is a map, and nothing makes a tree's holder unique."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp), boundaries={
                "harness-engineer": ["harness"],
                "second-engineer": ["harness"],
                "librarian": ["ledger"],
            })
            code, out, err = _record_handoff(repo)
            self.assertEqual(code, 2, out)
            self.assertIn("harness-engineer", out + err)
            self.assertIn("second-engineer", out + err)

    def test_a_handoff_can_be_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            code, out, err = run_cli_streams(
                ["announce", "list", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--json"])
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            self.assertEqual(len(payload["handoffs"]), 1, payload)
            entry = payload["handoffs"][0]
            self.assertEqual(entry["to"], "harness-engineer")
            self.assertEqual(entry["item"], SUBJECT)
            self.assertEqual(entry["answered"], "")


# ------------------------------------ a-handoff-does-not-widen-a-boundary

class HandoffDoesNotWidenABoundary(unittest.TestCase):
    """The sender proposes; the role that holds the tree writes."""

    def test_recording_a_handoff_writes_nothing_in_the_proposed_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            before = git(repo, "status", "--porcelain")
            self.assertNotIn("harness/steps.py", before)
            _record_handoff(repo)
            changed = git(repo, "status", "--porcelain")
            self.assertNotIn("harness/steps.py", changed)
            self.assertFalse((repo / "harness" / "steps.py").exists())

    def test_recording_a_handoff_grants_its_sender_no_reach(self):
        """`write_boundaries:` is entry for entry what it was."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            config = repo / ".vellum" / "config.yaml"
            before = config.read_text(encoding="utf-8")
            _record_handoff(repo)
            self.assertEqual(config.read_text(encoding="utf-8"), before)

    def test_the_guard_still_refuses_the_sender_after_a_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            base = git(repo, "rev-parse", "HEAD").strip()
            _record_handoff(repo)
            commit_files(repo, {"harness/steps.py": f"# {PROVED}\n"},
                         "harness: the change this run proved")
            code, out = run_cli(["verify", "boundaries", str(repo), "--base", base,
                                 "--head", "HEAD", "--role", "librarian"])
            self.assertEqual(code, 1, out)
            self.assertIn("harness/steps.py", out)


# --------------------------- a-finished-run-dispatches-who-commissioned-it

class FinishingAnnounces(unittest.TestCase):
    """A run's last act is to say so, and the announcement is what dispatches."""

    def test_reporting_a_pull_request_records_an_addressed_announcement(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, said = _finish(repo)
            self.assertEqual(code, 0, said)
            announced = _item(repo).get("announced")
            self.assertIsNotNone(announced, _item(repo))
            self.assertEqual(announced["kind"], "finished")
            self.assertEqual(announced["to"], "librarian")
            # S3: `ledger advance --pr` delivers in the same act `announce
            # finished` does — no tick or transport needed to reach `to`.
            self.assertIs(announced["dispatched"], True)
            self.assertIn("7", announced["asks"])

    def test_reporting_a_pull_request_says_it_dispatched_in_its_note(self):
        """S3: the printed note is accurate about what actually happened."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, said = _finish(repo)
            self.assertEqual(code, 0, said)
            self.assertIn("dispatched", said)
            self.assertIn("librarian", said)

    def test_an_item_that_did_not_finish_announces_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _finish(repo)
            self.assertIsNone(_item(repo, CONTROL).get("announced"))

    def test_a_pending_announcement_is_delivered_as_an_addressed_dispatch(self):
        """The fallback path: a push that recorded without delivering (a
        transport that will carry the delivery itself) still reaches its
        addressee through `vellum tick`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = run_cli_streams(
                ["announce", "finished", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--pr", "7", "--no-dispatch"])
            self.assertEqual(code, 0, out + err)
            self.assertIs(_item(repo)["announced"]["dispatched"], False)
            payload = _tick(repo)
            addressed = _addressed(payload, SUBJECT)
            self.assertEqual(len(addressed), 1, payload["actions"])
            self.assertEqual(addressed[0]["role"], "librarian")

    def test_an_ordinary_work_dispatch_is_addressed_to_nobody(self):
        """The control item is dispatched for work and carries no addressee."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _finish(repo)
            payload = _tick(repo)
            self.assertTrue(_dispatches(payload, CONTROL))
            self.assertEqual(_addressed(payload, CONTROL), [])

    def test_a_pass_over_a_world_that_announced_nothing_addresses_nobody(self):
        """The discriminator: no announcement, no addressed dispatch, ever."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            payload = _tick(repo)
            self.assertEqual(_addressed(payload), [], payload["actions"])

    def test_the_dispatch_is_delivered_with_no_reconciler_pass_at_all(self):
        """The push path: the announcement's own command emits the dispatch."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = run_cli_streams(
                ["announce", "finished", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--pr", "7", "--json"])
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            addressed = _addressed(payload, SUBJECT)
            self.assertEqual(len(addressed), 1, payload)
            self.assertEqual(addressed[0]["role"], "librarian")
            self.assertIs(_item(repo)["announced"]["dispatched"], True)


# ------------------ an-owner-review-dispatches-the-role-that-must-act

class DirectionAnnounces(unittest.TestCase):
    """The owner's review is direction, and its arrival dispatches."""

    def _observed(self, root: Path, **extra) -> Path:
        path = root / "observed.yaml"
        data = {"issues": [SUBJECT, CONTROL]}
        data.update(extra)
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_recorded_direction_announces_and_dispatches_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _intent(root)
            code, said = _finish(repo)
            self.assertEqual(code, 0, said)
            observed = self._observed(root, directions=[
                {"version": VERSION, "item": SUBJECT, "briefing": OWNER_REVIEW}])
            payload = _tick(repo, "--observed", str(observed))
            addressed = _addressed(payload, SUBJECT)
            self.assertEqual(len(addressed), 1, payload["actions"])
            self.assertEqual(_addressed(payload, CONTROL), [])

    def test_the_dispatch_carries_the_owners_own_words(self):
        """Not a pointer to the review: the receiver is not sent looking."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _intent(root)
            observed = self._observed(root, directions=[
                {"version": VERSION, "item": SUBJECT, "briefing": OWNER_REVIEW}])
            payload = _tick(repo, "--observed", str(observed))
            addressed = _addressed(payload, SUBJECT)
            self.assertEqual(len(addressed), 1, payload["actions"])
            self.assertIn(OWNER_REVIEW, addressed[0]["detail"])

    def test_direction_supersedes_a_pending_finish_rather_than_adding_to_it(self):
        """One pending announcement per item: the newest event is what is asked."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _intent(root)
            code, said = _finish(repo)
            self.assertEqual(code, 0, said)
            self.assertEqual(_item(repo)["announced"]["kind"], "finished")
            self.assertIs(_item(repo)["announced"]["dispatched"], True)
            observed = self._observed(root, directions=[
                {"version": VERSION, "item": SUBJECT, "briefing": OWNER_REVIEW}])
            payload = _tick(repo, "--observed", str(observed))
            self.assertEqual(len(_addressed(payload, SUBJECT)), 1, payload["actions"])
            self.assertIn(OWNER_REVIEW, _addressed(payload, SUBJECT)[0]["detail"])

    def test_direction_already_on_the_briefing_announces_nothing(self):
        """Idempotence: a re-reported direction is not new direction."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _intent(root)
            observed = self._observed(root, directions=[
                {"version": VERSION, "item": SUBJECT, "briefing": OWNER_REVIEW}])
            _tick(repo, "--observed", str(observed))
            again = _tick(repo, "--observed", str(observed))
            self.assertEqual(_addressed(again, SUBJECT), [], again["actions"])


# --------------------------- an-answered-handoff-dispatches-nobody

class DispatchIsIdempotentAndTerminates(unittest.TestCase):
    """A handoff already acted on dispatches nobody."""

    def test_a_delivered_announcement_is_not_delivered_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli(["announce", "finished", str(repo), "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                     "--pr", "7", "--no-dispatch"])
            first = _tick(repo)
            self.assertEqual(len(_addressed(first, SUBJECT)), 1, first["actions"])
            again = _tick(repo)
            self.assertEqual(_addressed(again, SUBJECT), [], again["actions"])

    def test_announce_finished_pr_run_twice_dispatches_once(self):
        """B2: `set_announcement` excludes `dispatched` from its comparison, so
        replaying the same `announce finished --pr 7` does not rewrite the
        record back to undispatched and redeliver it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            argv = ["announce", "finished", str(repo), "--version", VERSION,
                    "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                    "--pr", "7", "--json"]
            code1, out1, err1 = run_cli_streams(argv)
            self.assertEqual(code1, 0, out1 + err1)
            self.assertEqual(len(_addressed(json.loads(out1), SUBJECT)), 1, out1)
            code2, out2, err2 = run_cli_streams(argv)
            self.assertEqual(code2, 0, out2 + err2)
            self.assertEqual(_addressed(json.loads(out2), SUBJECT), [], out2)
            self.assertIs(_item(repo)["announced"]["dispatched"], True)

    def test_the_same_handoff_arriving_twice_runs_the_receiver_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            name = next(iter(_handoffs(repo)))
            before = _handoffs(repo)
            code, out, err = run_cli_streams(
                ["announce", "deliver", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--handoff", name, "--json"])
            self.assertEqual(code, 0, out + err)
            self.assertEqual(_addressed(json.loads(out)), [], out)
            self.assertEqual(_handoffs(repo), before)

    def test_an_answered_handoff_dispatches_nobody(self):
        """Recorded and left pending, then answered: the pass dispatches nobody.

        ``--no-dispatch`` is what makes this about the *answer* rather than about
        the push having already spent the announcement — with the handoff still
        standing undelivered, a pass that dispatched nobody did so because the
        handoff was answered and for no other reason.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            self.assertIs(_item(repo)["announced"]["dispatched"], False)
            code, said = _answer(repo, name, "--now", AFTER_LEASE)
            self.assertEqual(code, 0, said)
            payload = _tick(repo)
            self.assertEqual(_addressed(payload, SUBJECT), [], payload["actions"])

    def test_a_handoff_left_pending_still_dispatches_when_unanswered(self):
        """The control for the test above: unanswered, the pass does dispatch."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            payload = _tick(repo)
            addressed = _addressed(payload, SUBJECT)
            self.assertEqual(len(addressed), 1, payload["actions"])
            self.assertEqual(addressed[0]["role"], "harness-engineer")

    def test_delivering_an_answered_handoff_withholds_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            _answer(repo, name, "--now", AFTER_LEASE)
            code, out, err = run_cli_streams(
                ["announce", "deliver", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--json"])
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            self.assertEqual(payload["actions"], [])
            self.assertEqual(len(payload["withheld"]), 1, payload)
            self.assertIn("answered", payload["withheld"][0]["reason"])

    def test_answering_a_handoff_records_that_it_was_answered(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            name = next(iter(_handoffs(repo)))
            _answer(repo, name, "--now", AFTER_LEASE)
            self.assertIn(f"answered: {AFTER_LEASE}", _handoffs(repo)[name])
            self.assertIn("answered_by: harness-engineer", _handoffs(repo)[name])

    def test_a_handoff_dispatches_its_addressee_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo)
            self.assertEqual(code, 0, out + err)
            addressed = _addressed(json.loads(out), SUBJECT)
            self.assertEqual(len(addressed), 1, out)
            self.assertEqual(addressed[0]["role"], "harness-engineer")
            payload = _tick(repo)
            self.assertEqual(_addressed(payload, SUBJECT), [], payload["actions"])


# ------------------------------------------------------- the action's shape

class AddressedDispatchShape(unittest.TestCase):
    """A dispatch carries its addressee, and an unaddressed one carries no key."""

    def test_an_unaddressed_dispatch_carries_no_role_key_at_all(self):
        """The payload of a tick that addressed nobody is what it always was."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            payload = _tick(repo)
            for action in payload["actions"]:
                self.assertNotIn("role", action, action)

    def test_an_item_that_never_announced_carries_no_announced_key(self):
        """`announced:` rides as an extra key, materialised only where there is one.

        `ITEM_KEYS` is this product's reading of the fields
        `spec/features/ledger.md` names, and that slice names no announcement —
        so the field is not added to it, and a record of items that have not
        announced anything is byte for byte what it was before this wave.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            record = record_path(repo / "ledger", VERSION)
            before = record.read_text(encoding="utf-8")
            self.assertNotIn("announced", before)
            for issue in (SUBJECT, CONTROL):
                self.assertNotIn("announced", _item(repo, issue))
            _finish(repo)
            self.assertNotIn("announced", _item(repo, CONTROL))

    def test_an_addressed_dispatch_reports_its_addressee_in_the_prose_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _finish(repo)
            code, out = run_cli(["tick", str(repo), "--ledger-dir",
                                 str(repo / "ledger"), "--now", AFTER_LEASE,
                                 "--executor", "librarian"])
            self.assertIn(code, (0, 1), out)
            self.assertIn("librarian", out)


# ============================================================================
# The fix round on PR #29: one class per finding ID from the architect's
# review. Each of these reproduces the defect first (as a failing test against
# 0c5d6a3) and then asserts the fix.
# ============================================================================


# --------------------------------------------------------------------- B1


class HandoffArrivalIsIdempotentByIdentity(unittest.TestCase):
    """B1: a handoff's identity is (version, item, to, asks, sorted paths)."""

    def test_replaying_the_same_arrival_writes_no_second_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            _record_handoff(repo)  # the same arrival, again
            self.assertEqual(len(_handoffs(repo)), 1, _handoffs(repo))

    def test_replaying_the_same_arrival_dispatches_no_second_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            code, out, err = _record_handoff(repo, "--no-dispatch", "--json")
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            self.assertEqual(payload["actions"], [])

    def test_replaying_an_answered_handoffs_arrival_exits_zero_with_a_note(self):
        """The test must replay the ARRIVAL (`announce handoff` twice), not
        `deliver` — that is the whole of what B1 requires and what the
        existing suite (replaying `deliver` only) missed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            name = next(iter(_handoffs(repo)))
            answer_code, _ = _answer(repo, name, "--now", AFTER_LEASE)
            self.assertEqual(answer_code, 0)
            code, out, err = _record_handoff(repo)
            self.assertEqual(code, 0, out + err)
            self.assertIn("already answered by", out)
            self.assertIn("harness-engineer", out)
            self.assertIn(AFTER_LEASE, out)
            self.assertEqual(len(_handoffs(repo)), 1, _handoffs(repo))

    def test_a_differently_worded_ask_is_a_different_handoff(self):
        """The control: identity is real, not "any handoff for this item"."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            _record_handoff(repo, "--asks", "a completely different ask")
            self.assertEqual(len(_handoffs(repo)), 2, _handoffs(repo))


# --------------------------------------------------------------------- B3


class QueueHoldsOnAnUnansweredHandoff(unittest.TestCase):
    """B3: an item whose standing announcement is an unanswered handoff is
    held rather than re-dispatched every tick — otherwise B1's idempotent
    handoff record sits under a queue that claims the item again regardless.
    """

    def test_the_item_is_held_rather_than_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            payload = _tick(repo)
            holds = [a for a in payload["actions"]
                     if a["kind"] == "hold" and a["item"] == SUBJECT]
            self.assertEqual(len(holds), 1, payload["actions"])
            self.assertIn("waiting on handoff", holds[0]["detail"])
            self.assertIn("harness-engineer", holds[0]["detail"])
            claims = [a for a in payload["actions"]
                      if a["kind"] == "claim" and a["item"] == SUBJECT]
            self.assertEqual(claims, [], payload["actions"])

    def test_answering_the_handoff_releases_the_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            _answer(repo, name, "--now", AFTER_LEASE)
            payload = _tick(repo)
            holds = [a for a in payload["actions"]
                     if a["kind"] == "hold" and a["item"] == SUBJECT
                     and "waiting on handoff" in a["detail"]]
            self.assertEqual(holds, [], payload["actions"])
            claims = [a for a in payload["actions"]
                      if a["kind"] == "claim" and a["item"] == SUBJECT]
            self.assertEqual(len(claims), 1, payload["actions"])


# --------------------------------------------------------------------- S1


class AnnouncementsToDifferentRolesBothDeliver(unittest.TestCase):
    """S1: superseding is per addressee — the portolan#33 shape, where a
    blocked run also opened a pull request, must dispatch both roles.
    """

    def test_a_blocked_run_that_also_opened_a_pr_dispatches_both_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            # The handoff (to harness-engineer) is left undelivered — a
            # transport that has not yet collected it.
            _record_handoff(repo, "--no-dispatch")
            self.assertIs(_item(repo)["announced"]["dispatched"], False)
            self.assertEqual(_item(repo)["announced"]["to"], "harness-engineer")
            # The same item also reports a pull request (to librarian), which
            # `ledger advance --pr` delivers immediately (S3).
            code, said = _finish(repo)
            self.assertEqual(code, 0, said)
            self.assertEqual(_item(repo)["announced"]["to"], "librarian")
            self.assertIs(_item(repo)["announced"]["dispatched"], True)
            # The handoff must not have been lost: it is still there, pending.
            pending = _item(repo).get("announced_pending") or []
            self.assertEqual(len(pending), 1, _item(repo))
            self.assertEqual(pending[0]["to"], "harness-engineer")
            self.assertIs(pending[0]["dispatched"], False)
            # A tick delivers it — both roles end up dispatched.
            payload = _tick(repo)
            addressed = {a["role"] for a in _addressed(payload, SUBJECT)}
            self.assertEqual(addressed, {"harness-engineer"}, payload["actions"])

    def test_the_pending_queue_is_cleared_once_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            _finish(repo)
            _tick(repo)
            self.assertNotIn("announced_pending", _item(repo))


# --------------------------------------------------------------------- S2


class DirectionRecordsAndDeliversInOneAct(unittest.TestCase):
    """S2: `vellum announce direction` needs no tick to reach its addressee."""

    def test_direction_updates_the_briefing_and_dispatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = run_cli_streams(
                ["announce", "direction", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--briefing", OWNER_REVIEW, "--json"])
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            addressed = _addressed(payload, SUBJECT)
            self.assertEqual(len(addressed), 1, payload)
            self.assertEqual(addressed[0]["role"], "librarian")
            self.assertIn(OWNER_REVIEW, addressed[0]["detail"])
            self.assertEqual(_item(repo)["briefing"], OWNER_REVIEW)
            self.assertIs(_item(repo)["announced"]["dispatched"], True)

    def test_direction_no_dispatch_leaves_it_pending_for_a_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli(["announce", "direction", str(repo), "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                     "--briefing", OWNER_REVIEW, "--no-dispatch"])
            self.assertIs(_item(repo)["announced"]["dispatched"], False)
            payload = _tick(repo)
            self.assertEqual(len(_addressed(payload, SUBJECT)), 1, payload["actions"])


# ------------------------------------------------------------------ S4/S3


class LedgerAdvanceAddressesExplicitly(unittest.TestCase):
    """S4: the addressee comes from an explicitly named checkout, never a
    guess at `--ledger-dir`'s parent; S3: it delivers in the same act.
    """

    def test_no_declared_holder_exits_non_zero_rather_than_a_stderr_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_git_intent_repo(Path(tmp), boundaries={})
            open_code, _ = run_cli(["ledger", "open", "--version", VERSION,
                                    "--ledger-dir", str(repo / "ledger"),
                                    "--approved", NOW])
            self.assertEqual(open_code, 0)
            item_code, _ = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(repo / "ledger"), "--item", str(SUBJECT), "--title", "t",
                "--repo", "app", "--item-state", "planned",
            ])
            self.assertEqual(item_code, 0)
            code, said = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(repo / "ledger"), "--checkout", str(repo), "--item",
                str(SUBJECT), "--pr", "7",
            ])
            self.assertNotEqual(code, 0, said)
            # The item's own state is still recorded despite the refusal.
            self.assertEqual(_item(repo)["pr"], 7)

    def test_the_checkout_is_never_guessed_from_the_ledger_dirs_parent(self):
        """The old code guessed the checkout as `ledger_dir`'s own parent —
        right by accident whenever the ledger sits directly under the
        checkout, and wrong the moment it does not: `ledger_dir`'s parent here
        (`<repo>/shared`) carries no installation config at all, and only
        naming the real checkout explicitly with `--checkout` resolves it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_git_intent_repo(Path(tmp), boundaries={
                "harness-engineer": ["harness"],
                "librarian": ["shared/ledger", ".vellum/memory"],
            })
            ledger_dir = repo / "shared" / "ledger"
            code, _ = run_cli(["ledger", "open", "--version", VERSION,
                               "--ledger-dir", str(ledger_dir), "--approved", NOW])
            self.assertEqual(code, 0)
            code, said = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(ledger_dir), "--item", str(SUBJECT), "--title", "t",
                "--repo", "app", "--item-state", "planned",
            ])
            self.assertEqual(code, 0, said)
            code, said = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(ledger_dir), "--checkout", str(repo), "--item",
                str(SUBJECT), "--pr", "7",
            ])
            self.assertEqual(code, 0, said)
            record = load(record_path(ledger_dir, VERSION))
            item = find_item(record, SUBJECT)
            self.assertEqual(item["announced"]["to"], "librarian")
            self.assertIs(item["announced"]["dispatched"], True)


# --------------------------------------------------------------------- S5


class HandoffEvidenceIsVerbatimAndCapped(unittest.TestCase):
    """S5: tried/observed/proved are stored verbatim, capped at 64 KiB, and
    required — an ask with no evidence is a question.
    """

    def test_evidence_is_stored_verbatim_not_truncated_or_flattened(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            long_tried = "line one\nline two\n" + ("x" * 500)
            code, out, err = _record_handoff(
                repo, "--tried", long_tried, "--observed", OBSERVED,
                "--proved", PROVED,
            )
            self.assertEqual(code, 0, out + err)
            text = next(iter(_handoffs(repo).values()))
            self.assertIn(long_tried, text)

    def test_a_handoff_with_no_tried_is_refused_as_a_question_not_a_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--tried", "")
            self.assertEqual(code, 2, out + err)
            self.assertIn("question protocol", out + err)
            self.assertEqual(_handoffs(repo), {})

    def test_a_handoff_with_no_proved_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--proved", "   ")
            self.assertEqual(code, 2, out + err)
            self.assertEqual(_handoffs(repo), {})

    def test_evidence_over_the_cap_is_refused_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            oversized = "x" * (64 * 1024 + 1)
            code, out, err = _record_handoff(repo, "--tried", oversized)
            self.assertEqual(code, 2, out + err)
            self.assertIn("65536", out + err)
            self.assertEqual(_handoffs(repo), {})


# ----------------------------------------------------------------- S6/SS4


class AddresseeIsReadFromTheFullNormalisedPath(unittest.TestCase):
    """S6/SS4: `holders()` matches the whole path, not `Path(p).parts[0]`."""

    def _handoff_argv(self, repo: Path, sender: str, path: str) -> list[str]:
        return ["announce", "handoff", str(repo), "--version", VERSION,
                "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                "--from", sender, "--asks", ASKS, "--tried", TRIED,
                "--observed", OBSERVED, "--proved", PROVED, "--path", path,
                "--now", NOW]

    def test_a_nested_tree_is_matched_by_its_full_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp), boundaries={
                "spec-author": ["spec/features"],
                "librarian": ["ledger", ".vellum/memory"],
            })
            code, out, err = run_cli_streams(
                self._handoff_argv(repo, "librarian", "spec/features/auth.md"))
            self.assertEqual(code, 0, out + err)
            text = next(iter(_handoffs(repo).values()))
            self.assertIn("to: spec-author", text)

    def test_vellum_memory_is_matched_by_its_full_path_not_vellum(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp), boundaries={
                "harness-engineer": ["harness"],
                "librarian": ["ledger", ".vellum/memory"],
            })
            code, out, err = run_cli_streams(
                self._handoff_argv(repo, "harness-engineer",
                                   ".vellum/memory/notes.md"))
            self.assertEqual(code, 0, out + err)
            text = next(iter(_handoffs(repo).values()))
            self.assertIn("to: librarian", text)


# ----------------------------------------------------------------- S7/SS5


class HandoffIsTheLedgerHoldersActOnTheSendersBehalf(unittest.TestCase):
    """S7/SS5: `--from` is attribution only; the command's writes stay
    inside ledger/ regardless of who it names.
    """

    def test_from_implementer_names_the_sender_and_stays_inside_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp), boundaries={
                "implementer": ["src"],
                "harness-engineer": ["harness"],
                "librarian": ["ledger", ".vellum/memory"],
            })
            before = git(repo, "status", "--porcelain")
            code, out, err = _record_handoff(repo, "--from", "implementer")
            self.assertEqual(code, 0, out + err)
            text = next(iter(_handoffs(repo).values()))
            self.assertIn("from: implementer", text)
            changed = git(repo, "status", "--porcelain")
            for line in changed.splitlines():
                path = line.split(maxsplit=1)[1] if line.strip() else ""
                self.assertTrue(path.startswith("ledger/"), changed)
            self.assertNotEqual(before, changed)


# --------------------------------------------------------------------- N1


class AnsweringSettlesTheAnnouncement(unittest.TestCase):
    """N1: tick stops repeating its note once a handoff is answered, and
    `announce list` shows it as answered.
    """

    def test_tick_settles_it_once_and_does_not_repeat_the_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            _answer(repo, name, "--now", AFTER_LEASE)
            first = _tick(repo)
            self.assertTrue(any("dispatches nobody" in n for n in first["notes"]),
                            first["notes"])
            self.assertIs(_item(repo)["announced"]["dispatched"], True)
            again = _tick(repo)
            self.assertFalse(any("dispatches nobody" in n for n in again["notes"]),
                             again["notes"])

    def test_announce_list_shows_it_as_answered(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            name = next(iter(_handoffs(repo)))
            _answer(repo, name, "--now", AFTER_LEASE)
            code, out, err = run_cli_streams(
                ["announce", "list", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--json"])
            self.assertEqual(code, 0, out + err)
            entry = json.loads(out)["handoffs"][0]
            self.assertEqual(entry["answered"], AFTER_LEASE)


# ----------------------------------------------------------------- N2/SS8


class DeliverMarksTheLiveEntryDirectly(unittest.TestCase):
    """N2/SS8: `--version` narrows delivery, and an item with `issue: null`
    is marked dispatched through the entry itself rather than a lookup by
    issue that could never find it again.
    """

    def _null_issue_record(self, repo: Path) -> None:
        path = record_path(repo / "ledger", VERSION)
        record = load(path)
        record["work_items"].append({
            "issue": None, "title": "unfiled", "repo": "app", "satisfies": [],
            "pr": None, "state": "planned", "briefing": None,
            "announced": {
                "kind": "finished", "to": "librarian", "asks": "unfiled work "
                "finished", "handoff": "", "dispatched": False,
            },
        })
        write(path, record)

    def test_an_item_with_a_null_issue_is_dispatched_once_not_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            self._null_issue_record(repo)
            code, out, err = run_cli_streams(
                ["announce", "deliver", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--json"])
            self.assertEqual(code, 0, out + err)
            first = json.loads(out)
            self.assertEqual(len(_addressed(first)), 1, first)
            code2, out2, err2 = run_cli_streams(
                ["announce", "deliver", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--json"])
            self.assertEqual(code2, 0, out2 + err2)
            self.assertEqual(_addressed(json.loads(out2)), [])

    def test_deliver_version_narrows_to_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            other = "b" * 40
            run_cli(["ledger", "open", "--version", other, "--ledger-dir",
                     str(repo / "ledger"), "--approved", NOW])
            run_cli(["ledger", "advance", "--version", other, "--ledger-dir",
                     str(repo / "ledger"), "--item", "9", "--title", "t",
                     "--repo", "app", "--item-state", "planned"])
            run_cli(["announce", "finished", str(repo), "--version", other,
                     "--ledger-dir", str(repo / "ledger"), "--item", "9",
                     "--to", "librarian", "--no-dispatch"])
            run_cli(["announce", "finished", str(repo), "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                     "--to", "librarian", "--no-dispatch"])
            code, out, err = run_cli_streams(
                ["announce", "deliver", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--version", VERSION, "--json"])
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            self.assertEqual(len(_addressed(payload)), 1, payload)
            self.assertEqual(_addressed(payload)[0]["item"], SUBJECT)


# --------------------------------------------------------------------- N3


class AnsweredByMustBeTheAddresseeOrTheLedgerHolder(unittest.TestCase):
    def test_an_unrelated_role_may_not_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp), boundaries={
                "harness-engineer": ["harness"],
                "librarian": ["ledger", ".vellum/memory"],
                "spectator": ["spec"],
            })
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            code, said = _answer(repo, name, by="spectator")
            self.assertEqual(code, 2, said)
            self.assertNotIn("answered_by: spectator", _handoffs(repo)[name])
            self.assertIn("answered:\n", _handoffs(repo)[name])

    def test_the_addressee_may_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            code, said = _answer(repo, name, by="harness-engineer")
            self.assertEqual(code, 0, said)
            self.assertIn("answered_by: harness-engineer", _handoffs(repo)[name])

    def test_the_ledger_holder_may_also_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            code, said = _answer(repo, name, by="librarian")
            self.assertEqual(code, 0, said)
            self.assertIn("answered_by: librarian", _handoffs(repo)[name])


# ---------------------------------------------------------------- security


class SymlinkWritesAreRefused(unittest.TestCase):
    """SB1: a symlinked handoffs directory, or a symlinked record name, is
    refused rather than written through — and `_next_number` counts a
    dangling symlink so a fresh handoff never reuses its name.
    """

    def test_a_symlinked_handoffs_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            (repo / "ledger" / "handoffs").symlink_to(
                repo / ".git" / "hooks", target_is_directory=True
            )
            code, out, err = _record_handoff(repo)
            self.assertNotEqual(code, 0, out + err)
            self.assertEqual(list((repo / ".git" / "hooks").glob("0*")), [])

    def test_a_dangling_symlinked_record_name_is_skipped_not_written_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            handoffs = repo / "ledger" / "handoffs"
            handoffs.mkdir(parents=True)
            target = Path(tmp) / "nonexistent-target" / "authorized_keys"
            (handoffs / "0001-fix-it.md").symlink_to(target)
            code, out, err = _record_handoff(repo)
            self.assertEqual(code, 0, out + err)
            names = sorted(p.name for p in handoffs.iterdir())
            self.assertIn("0001-fix-it.md", names)  # untouched
            self.assertFalse((handoffs / "0001-fix-it.md").is_file())  # still dangling
            created = [n for n in names if n != "0001-fix-it.md"]
            self.assertEqual(len(created), 1, names)
            self.assertTrue(created[0].startswith("0002-"), created)

    def test_answer_refuses_a_symlinked_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            path = repo / "ledger" / "handoffs" / name
            real_content = path.read_text(encoding="utf-8")
            target = Path(tmp) / "elsewhere.md"
            target.write_text("not a handoff", encoding="utf-8")
            path.unlink()
            path.symlink_to(target)
            code, said = _answer(repo, name)
            self.assertNotEqual(code, 0, said)
            self.assertEqual(target.read_text(encoding="utf-8"), "not a handoff")


class HandoffNameTraversalIsRefused(unittest.TestCase):
    """SB2: a handoff name is accepted only in its own shape, read through
    `paths.unsafe_read` — never a traversal, never an absolute path.
    """

    def test_answer_refuses_a_traversal_name_and_leaves_the_target_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            decoy = repo / "spec" / "0001-fix-it.md"
            decoy.parent.mkdir(parents=True, exist_ok=True)
            decoy.write_text("a genuine spec file, not a handoff", encoding="utf-8")
            code, said = _answer(repo, "../../spec/0001-fix-it.md")
            self.assertNotEqual(code, 0, said)
            self.assertEqual(decoy.read_text(encoding="utf-8"),
                             "a genuine spec file, not a handoff")

    def test_answer_refuses_an_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            outside = Path(tmp) / "outside.md"
            outside.write_text("not a handoff", encoding="utf-8")
            code, said = _answer(repo, str(outside))
            self.assertNotEqual(code, 0, said)
            self.assertEqual(outside.read_text(encoding="utf-8"), "not a handoff")

    def test_deliver_with_a_traversal_handoff_filter_matches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            code, out, err = run_cli_streams(
                ["announce", "deliver", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--handoff", "../../etc/passwd", "--json"])
            self.assertEqual(code, 0, out + err)
            self.assertEqual(json.loads(out)["actions"], [])


class FinishedToIsValidatedAndNeverSelfDispatches(unittest.TestCase):
    """SB3: `--to` on `finished` is a declared, printable role; a
    self-addressed announcement is recorded but not dispatched.
    """

    def test_a_workflow_command_injection_via_to_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = run_cli_streams(
                ["announce", "finished", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--to", "owner\n::add-mask::x"])
            self.assertNotEqual(code, 0, out + err)
            self.assertNotIn("::add-mask::x\n", out)

    def test_an_undeclared_to_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = run_cli_streams(
                ["announce", "finished", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--to", "nobody-declared"])
            self.assertEqual(code, 2, out + err)

    def test_self_dispatch_is_recorded_but_not_dispatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = run_cli_streams(
                ["announce", "finished", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--to", "librarian", "--from", "librarian", "--json"])
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            self.assertEqual(payload["actions"], [])
            self.assertEqual(len(payload["withheld"]), 1, payload)
            announced = _item(repo).get("announced")
            self.assertIsNotNone(announced)
            self.assertEqual(announced["to"], "librarian")


class HandoffInputsAreLexicallyChecked(unittest.TestCase):
    """SS1: `--from` is a declared role; each `--path` is checked the way
    `manifest.check_owned_path` checks an owned path; `--version` must be
    printable.
    """

    def test_an_absolute_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--path", "/etc/passwd")
            self.assertEqual(code, 2, out + err)
            self.assertEqual(_handoffs(repo), {})

    def test_a_traversal_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--path", "../../etc/passwd")
            self.assertEqual(code, 2, out + err)
            self.assertEqual(_handoffs(repo), {})

    def test_a_path_under_git_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--path", ".git/config",
                                             "--to", "librarian")
            self.assertEqual(code, 2, out + err)
            self.assertEqual(_handoffs(repo), {})

    def test_a_non_printable_version_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo)  # sanity: baseline works
            self.assertEqual(code, 0, out + err)
            code2, out2, err2 = run_cli_streams(
                ["announce", "handoff", str(repo), "--version", "a\nb",
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--from", "librarian", "--asks", ASKS, "--tried", TRIED,
                 "--observed", OBSERVED, "--proved", PROVED,
                 "--path", "harness/steps.py"])
            self.assertEqual(code2, 2, out2 + err2)

    def test_a_newline_in_from_cannot_inject_a_frontmatter_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--from", "librarian\nto: attacker")
            self.assertEqual(code, 2, out + err)
            self.assertEqual(_handoffs(repo), {})


class HandoffValidatesBeforeWriting(unittest.TestCase):
    """SS2: version and item are validated before the handoff file is
    written, so a refusal leaves no orphan record.
    """

    def test_an_unknown_item_leaves_no_orphan_handoff_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, item=9999)
            self.assertEqual(code, 2, out + err)
            self.assertFalse((repo / "ledger" / "handoffs").exists())


class ToMustHoldEveryProposedPath(unittest.TestCase):
    """SS3: an explicit `--to` must hold at least one tree, and must hold
    every `--path` given.
    """

    def test_a_role_with_no_declared_tree_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp), boundaries={
                "harness-engineer": ["harness"],
                "librarian": ["ledger", ".vellum/memory"],
                "empty-role": [],
            })
            code, out, err = _record_handoff(repo, "--to", "empty-role")
            self.assertEqual(code, 2, out + err)

    def test_a_to_that_does_not_hold_the_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(
                repo, "--to", "librarian", "--path", "harness/steps.py",
            )
            self.assertEqual(code, 2, out + err)
            self.assertIn("librarian", out + err)


class DeliveryIsLockedAgainstConcurrentDoubleDispatch(unittest.TestCase):
    """SS6: two concurrent deliveries of the same pending announcement must
    not both dispatch it.
    """

    def test_concurrent_delivers_dispatch_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli(["announce", "finished", str(repo), "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                     "--to", "librarian", "--no-dispatch"])
            ledger_dir = repo / "ledger"
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: deliver(ledger_dir), range(8)))
            total_sent = sum(len(sent) for sent, _held in results)
            self.assertEqual(total_sent, 1, results)


class HandoffReadsAreCappedAndNeverCrashATick(unittest.TestCase):
    """SS7: an unreadable handoff (too large, or not valid UTF-8) turns into
    an `AnnounceError` a caller can catch, and `vellum tick` does exactly
    that rather than crashing on it.
    """

    def test_an_oversized_handoff_is_refused_by_read_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            path = repo / "ledger" / "handoffs" / name
            path.write_text(path.read_text(encoding="utf-8") + "x" * (1 << 21),
                            encoding="utf-8")
            with self.assertRaises(AnnounceError):
                read_handoff(path)

    def test_a_non_utf8_handoff_does_not_crash_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            path = repo / "ledger" / "handoffs" / name
            with open(path, "ab") as handle:
                handle.write(b"\xff\xfe")
            payload = _tick(repo)
            self.assertTrue(
                any("could not be read" in n for n in payload["notes"]),
                payload["notes"],
            )


class ControlCharactersAreRefusedInEvidence(unittest.TestCase):
    """SN1: `\\n` and `\\t` are allowed in the evidence bodies; ESC and other
    control characters are refused everywhere.
    """

    def test_an_escape_sequence_in_asks_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--asks", "clear \x1b[2J the screen")
            self.assertEqual(code, 2, out + err)
            self.assertNotIn("\x1b[2J", out)
            self.assertEqual(_handoffs(repo), {})

    def test_an_escape_sequence_in_tried_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--tried", "ran it \x1b[2J")
            self.assertEqual(code, 2, out + err)
            self.assertEqual(_handoffs(repo), {})

    def test_newlines_and_tabs_are_allowed_in_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--tried", "line one\n\tindented")
            self.assertEqual(code, 0, out + err)


class CredentialsAreStrippedFromEvidenceUrls(unittest.TestCase):
    """SN2: `user:token@` is stripped from a URL inside an evidence field,
    reusing `ledger.clean_run_reference`, with a note that says "rotate"."""

    def test_a_credential_bearing_url_is_stripped_and_noted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(
                repo, "--tried",
                "reproduced it at https://user:hunter2@ci.example/run/7",
            )
            self.assertEqual(code, 0, out + err)
            self.assertIn("rotate", (out + err).lower())
            text = next(iter(_handoffs(repo).values()))
            self.assertNotIn("hunter2", text)
            self.assertIn("https://ci.example/run/7", text)


class NonDecimalDigitsDoNotCrashAHandoffRead(unittest.TestCase):
    """SN3: `isdecimal()`, not `isdigit()` — "²".isdigit() is True and
    int("²") raises.
    """

    def test_a_superscript_digit_in_item_reads_back_as_none_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            path = repo / "ledger" / "handoffs" / name
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    f"item: {SUBJECT}", "item: ²"
                ),
                encoding="utf-8",
            )
            handoff = read_handoff(path)
            self.assertIsNone(handoff.item)


class DeclaredBoundariesHasOneReader(unittest.TestCase):
    """N5: `declared_boundaries` reads through `product.role_trees`, the same
    reader `config.write_boundaries` uses for one role.
    """

    def test_a_malformed_entry_is_refused_the_same_way_for_every_role(self):
        from vellum.announce import declared_boundaries
        from vellum.config import write_boundaries as config_write_boundaries

        with tempfile.TemporaryDirectory() as tmp:
            repo = make_git_intent_repo(
                Path(tmp), boundaries={"librarian": ["../.."]}
            )
            with self.assertRaises(AnnounceError) as announce_exc:
                declared_boundaries(repo)
            with self.assertRaises(Exception) as config_exc:
                config_write_boundaries(repo, "librarian")
            self.assertIn("escapes the repository", str(announce_exc.exception))
            self.assertIn("escapes the repository", str(config_exc.exception))


if __name__ == "__main__":
    unittest.main()
