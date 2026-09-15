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

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from support import commit_files, git, make_git_intent_repo, run_cli, run_cli_streams
from vellum.ledger import find_item, load, record_path

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
            code, said = run_cli(["ledger", "advance", "--version", VERSION,
                                  "--ledger-dir", str(repo / "ledger"),
                                  "--item", str(SUBJECT), "--pr", "7"])
            self.assertEqual(code, 0, said)
            announced = _item(repo).get("announced")
            self.assertIsNotNone(announced, _item(repo))
            self.assertEqual(announced["kind"], "finished")
            self.assertEqual(announced["to"], "librarian")
            self.assertIs(announced["dispatched"], False)
            self.assertIn("7", announced["asks"])

    def test_an_item_that_did_not_finish_announces_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli(["ledger", "advance", "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"),
                     "--item", str(SUBJECT), "--pr", "7"])
            self.assertIsNone(_item(repo, CONTROL).get("announced"))

    def test_a_pending_announcement_is_delivered_as_an_addressed_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli(["ledger", "advance", "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"),
                     "--item", str(SUBJECT), "--pr", "7"])
            payload = _tick(repo)
            addressed = _addressed(payload, SUBJECT)
            self.assertEqual(len(addressed), 1, payload["actions"])
            self.assertEqual(addressed[0]["role"], "librarian")

    def test_an_ordinary_work_dispatch_is_addressed_to_nobody(self):
        """The control item is dispatched for work and carries no addressee."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli(["ledger", "advance", "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"),
                     "--item", str(SUBJECT), "--pr", "7"])
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
            run_cli(["ledger", "advance", "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"),
                     "--item", str(SUBJECT), "--pr", "7"])
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
            run_cli(["ledger", "advance", "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"),
                     "--item", str(SUBJECT), "--pr", "7"])
            self.assertEqual(_item(repo)["announced"]["kind"], "finished")
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
            run_cli(["ledger", "advance", "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"),
                     "--item", str(SUBJECT), "--pr", "7"])
            first = _tick(repo)
            self.assertEqual(len(_addressed(first, SUBJECT)), 1, first["actions"])
            again = _tick(repo)
            self.assertEqual(_addressed(again, SUBJECT), [], again["actions"])

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
            code, said = run_cli(["announce", "answer", str(repo), "--ledger-dir",
                                  str(repo / "ledger"), "--handoff", name,
                                  "--now", AFTER_LEASE])
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
            run_cli(["announce", "answer", str(repo), "--ledger-dir",
                     str(repo / "ledger"), "--handoff", name, "--now", AFTER_LEASE])
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
            run_cli(["announce", "answer", str(repo), "--ledger-dir",
                     str(repo / "ledger"), "--handoff", name, "--now", AFTER_LEASE])
            self.assertIn(f"answered: {AFTER_LEASE}", _handoffs(repo)[name])

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
            run_cli(["ledger", "advance", "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"),
                     "--item", str(SUBJECT), "--pr", "7"])
            self.assertNotIn("announced", _item(repo, CONTROL))

    def test_an_addressed_dispatch_reports_its_addressee_in_the_prose_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli(["ledger", "advance", "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"),
                     "--item", str(SUBJECT), "--pr", "7"])
            code, out = run_cli(["tick", str(repo), "--ledger-dir",
                                 str(repo / "ledger"), "--now", AFTER_LEASE,
                                 "--executor", "librarian"])
            self.assertIn(code, (0, 1), out)
            self.assertIn("librarian", out)


if __name__ == "__main__":
    unittest.main()
