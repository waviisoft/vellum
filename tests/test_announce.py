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


def _log(item: dict) -> list[dict]:
    """*item*'s whole ``announcements:`` log, oldest first."""
    found = item.get("announcements")
    return [e for e in found if isinstance(e, dict)] if isinstance(found, list) else []


def _latest(item: dict) -> dict | None:
    """The most recently appended entry of *item*'s log, or None."""
    log = _log(item)
    return log[-1] if log else None


def _by_kind(item: dict, kind: str) -> list[dict]:
    """Every log entry of *kind*, oldest first."""
    return [e for e in _log(item) if e.get("kind") == kind]


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


def _answer(repo: Path, name: str, *extra, by: str | None = "harness-engineer"):
    argv = ["announce", "answer", str(repo), "--ledger-dir", str(repo / "ledger"),
            "--handoff", name]
    if by is not None:
        argv += ["--by", by]
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
            announced = _latest(_item(repo))
            self.assertIsNotNone(announced, _item(repo))
            self.assertEqual(announced["kind"], "finished")
            self.assertEqual(announced["to"], "librarian")
            self.assertEqual(announced["id"], "finished:pr7")
            # K3: `ledger advance --pr` records and addresses it, but leaves
            # `dispatched: false` by default — `deliver`/`tick` (or --json)
            # perform and report the actual dispatch, so the event is never
            # marked delivered without anything emitting it.
            self.assertIs(announced["dispatched"], False)
            self.assertIn("7", announced["asks"])

    def test_reporting_a_pull_request_says_so_accurately_in_its_note(self):
        """The printed note is accurate about what actually happened (S3)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, said = _finish(repo)
            self.assertEqual(code, 0, said)
            self.assertIn("librarian", said)
            self.assertIn("deliver", said.lower())

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
            self.assertIs(_latest(_item(repo))["dispatched"], False)
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
            self.assertIs(_latest(_item(repo))["dispatched"], True)


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
        """Rule 2: a pending finish and a new direction addressed to the same
        role are one dispatch, not two — the log keeps both entries, but they
        are delivered together rather than as separate redundant dispatches."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _intent(root)
            code, said = _finish(repo)
            self.assertEqual(code, 0, said)
            self.assertEqual(_latest(_item(repo))["kind"], "finished")
            self.assertIs(_latest(_item(repo))["dispatched"], False)
            observed = self._observed(root, directions=[
                {"version": VERSION, "item": SUBJECT, "briefing": OWNER_REVIEW}])
            payload = _tick(repo, "--observed", str(observed))
            self.assertEqual(len(_addressed(payload, SUBJECT)), 1, payload["actions"])
            # An addressed dispatch's detail is content the receiver acts on,
            # not a log line for a human to skim — so unlike every other
            # action kind's free-text explanation, it is never narrowed to
            # 120 characters (`_Reconciler.act`'s `limit=None`). A grouped
            # dispatch combining a finish and a direction must still carry
            # the direction's full text, or the receiver is told the news
            # exists and has to go find it — exactly what this scenario
            # (waviisoft/vellum-intent's `an-owner-review-dispatches-the-
            # role-that-must-act`) grades.
            self.assertIn(OWNER_REVIEW, _addressed(payload, SUBJECT)[0]["detail"])
            # Nothing is actually lost, though: the full, un-narrowed text of
            # both events is still in the log this dispatch was grouped from.
            log = _log(_item(repo))
            self.assertEqual(_by_kind(_item(repo), "direction")[-1]["asks"], OWNER_REVIEW)
            self.assertTrue(all(e["dispatched"] for e in log), log)

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
        """The log is deduplicated by id, so replaying the same `announce
        finished --pr 7` appends nothing new and does not rewrite the entry
        back to undispatched and redeliver it.
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
            self.assertEqual(len(_by_kind(_item(repo), "finished")), 1, _log(_item(repo)))
            self.assertIs(_latest(_item(repo))["dispatched"], True)

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
            self.assertIs(_latest(_item(repo))["dispatched"], False)
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

    def test_the_acceptance_shape_exactly_an_answered_handoff_dispatches_nobody(self):
        """Mirrors `@id:an-answered-handoff-dispatches-nobody` as
        `harness/support/sandbox.py`'s `announce_handoff` and
        `waviisoft/vellum-intent#114` actually drive it: `announce handoff
        --json`, `announce answer` with no `--by`, the identical `announce
        handoff --json` again, then a `tick`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code1, out1, err1 = _record_handoff(repo, "--json")
            self.assertEqual(code1, 0, out1 + err1)
            name = next(iter(_handoffs(repo)))
            answer_code, said = _answer(repo, name, by=None)
            self.assertEqual(answer_code, 0, said)
            before = dict(_handoffs(repo))
            code2, out2, err2 = _record_handoff(repo, "--json")
            self.assertEqual(code2, 0, out2 + err2)
            payload2 = json.loads(out2)
            # (a) no dispatch carrying a role for this item.
            self.assertEqual(_addressed(payload2, SUBJECT), [], payload2)
            # (b) ledger/handoffs/ is byte-identical to before the replay.
            self.assertEqual(_handoffs(repo), before)
            # (c) a following tick addresses no role for that item.
            tick_payload = _tick(repo)
            self.assertEqual(_addressed(tick_payload, SUBJECT), [],
                             tick_payload["actions"])


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

    def test_a_later_direction_does_not_release_an_unanswered_handoff(self):
        """Rule 5: the hold reads every handoff the log names, not just
        whichever entry a reader would call "standing". Under the old
        standing-announcement-plus-pending-queue shape, a fresh direction
        recorded after the handoff replaced `announced.kind` with "direction"
        and the handoff's own hold silently stopped applying — this is
        exactly the replay this closes: the item must stay held even though a
        `direction` entry is now the newest thing in the log, and the item is
        still otherwise queueable (no PR reported).
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _intent(root)
            _record_handoff(repo, "--no-dispatch")
            self.assertEqual(_latest(_item(repo))["kind"], "handoff")
            observed = root / "observed.yaml"
            observed.write_text(yaml.safe_dump({
                "issues": [SUBJECT, CONTROL],
                "directions": [{"version": VERSION, "item": SUBJECT,
                                "briefing": "reconsider the approach"}],
            }), encoding="utf-8")
            payload = _tick(repo, "--observed", str(observed))
            self.assertEqual(_latest(_item(repo))["kind"], "direction")
            self.assertIsNone(_item(repo).get("pr"))
            holds = [a for a in payload["actions"]
                     if a["kind"] == "hold" and a["item"] == SUBJECT
                     and "waiting on handoff" in a["detail"]]
            self.assertEqual(len(holds), 1, payload["actions"])
            claims = [a for a in payload["actions"]
                      if a["kind"] == "claim" and a["item"] == SUBJECT]
            self.assertEqual(claims, [], payload["actions"])


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
            handoff_entry = _latest(_item(repo))
            self.assertIs(handoff_entry["dispatched"], False)
            self.assertEqual(handoff_entry["to"], "harness-engineer")
            # The same item also reports a pull request (to librarian).
            code, said = _finish(repo)
            self.assertEqual(code, 0, said)
            finished_entry = _latest(_item(repo))
            self.assertEqual(finished_entry["to"], "librarian")
            self.assertIs(finished_entry["dispatched"], False)
            # The handoff must not have been lost: the append-only log keeps
            # both entries, undispatched, rather than one superseding the
            # other.
            pending = [e for e in _log(_item(repo)) if not e["dispatched"]]
            self.assertEqual(len(pending), 2, _log(_item(repo)))
            self.assertEqual({e["to"] for e in pending}, {"harness-engineer", "librarian"})
            # A tick delivers both — the finished announcement and the
            # queued, previously-undelivered handoff.
            payload = _tick(repo)
            addressed = {a["role"] for a in _addressed(payload, SUBJECT)}
            self.assertEqual(addressed, {"harness-engineer", "librarian"},
                             payload["actions"])

    def test_both_entries_are_marked_dispatched_once_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            _finish(repo)
            _tick(repo)
            log = _log(_item(repo))
            self.assertEqual(len(log), 2, log)
            self.assertTrue(all(e["dispatched"] for e in log), log)


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
            self.assertIs(_latest(_item(repo))["dispatched"], True)

    def test_direction_no_dispatch_leaves_it_pending_for_a_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli(["announce", "direction", str(repo), "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                     "--briefing", OWNER_REVIEW, "--no-dispatch"])
            self.assertIs(_latest(_item(repo))["dispatched"], False)
            payload = _tick(repo)
            self.assertEqual(len(_addressed(payload, SUBJECT)), 1, payload["actions"])


# ------------------------------------------------------------------ S4/S3


class LedgerAdvanceAddressesExplicitly(unittest.TestCase):
    """Corrected S4 ruling: the default checkout is the git work tree
    containing --ledger-dir (never cwd, never a textual parent guess); when
    still no addressee can be found, `ledger advance --pr` records the state
    change and an undelivered announcement, warns, and exits 0 — it must
    never fail a state change over an address it could not compute. Only the
    explicit `announce finished` keeps refusing outright.
    """

    def test_no_declared_holder_records_undelivered_and_exits_zero(self):
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
            self.assertEqual(code, 0, said)
            self.assertIn("undelivered", said)
            self.assertEqual(_item(repo)["pr"], 7)
            announced = _latest(_item(repo))
            self.assertEqual(announced["to"], "")
            self.assertIs(announced["dispatched"], False)

    def test_a_checkout_with_no_config_at_all_keeps_pr_reporting_working(self):
        """Found running the intent repo's own acceptance suite against this
        fix round: certification, chain-resolution and release scenarios all
        call `ledger advance --pr` against sandboxes that carry no
        `.vellum/config.yaml` at all, because they have nothing to do with
        continuous engineering.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "intent"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main", ".")
            commit_files(repo, {"README.md": "no .vellum/ at all\n"}, "start")
            self.assertFalse((repo / ".vellum").exists())
            code, _ = run_cli(["ledger", "open", "--version", VERSION,
                               "--ledger-dir", str(repo / "ledger"),
                               "--approved", NOW])
            self.assertEqual(code, 0)
            code, said = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(repo / "ledger"), "--item", str(SUBJECT), "--title", "t",
                "--repo", "app", "--item-state", "planned",
            ])
            self.assertEqual(code, 0, said)
            code, said = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(repo / "ledger"), "--item", str(SUBJECT), "--pr", "7",
            ])
            self.assertEqual(code, 0, said)
            self.assertEqual(_item(repo)["pr"], 7)
            self.assertEqual(_latest(_item(repo))["to"], "")

    def test_a_ledger_dir_outside_any_git_work_tree_falls_back_to_its_parent(self):
        """Note 4: `ledger_dir.parent` is the fallback for the one case the
        first-cut S4 guess got right — no git work tree at all — not a
        replacement for trying the git toplevel first.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Not a git repo at all — `git -C <ledger_dir> rev-parse
            # --show-toplevel` fails outright, so the parent is tried.
            ledger_dir = Path(tmp) / "loose" / "ledger"
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
                str(ledger_dir), "--item", str(SUBJECT), "--pr", "7",
            ])
            self.assertEqual(code, 0, said)
            # The parent (`loose/`) has no `.vellum/config.yaml` either, so
            # addressing still fails — but via "cannot read the installation
            # config", not "not inside a git work tree", since the parent was
            # actually tried.
            self.assertIn("undelivered", said)
            self.assertNotIn("not inside a git work tree", said)
            record = load(record_path(ledger_dir, VERSION))
            item = find_item(record, SUBJECT)
            self.assertEqual(item["pr"], 7)
            self.assertEqual(_latest(item)["to"], "")

    def test_the_parent_fallback_actually_addresses_when_it_can(self):
        """The positive case: no git work tree, but the ledger's own parent
        directory carries a real installation config — the fallback must
        actually resolve it, not merely avoid crashing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            from support import write_intent_config

            checkout = Path(tmp) / "no_git_here"
            write_intent_config(checkout, boundaries={
                "librarian": ["ledger", ".vellum/memory"],
            })
            ledger_dir = checkout / "ledger"
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
                str(ledger_dir), "--item", str(SUBJECT), "--pr", "7",
            ])
            self.assertEqual(code, 0, said)
            record = load(record_path(ledger_dir, VERSION))
            self.assertEqual(_latest(find_item(record, SUBJECT))["to"], "librarian")

    def test_no_checkout_flag_defaults_to_the_git_toplevel(self):
        """The corrected default — the git work tree containing --ledger-dir
        — so the harness's own call (no `--checkout` at all) keeps working.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, said = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(repo / "ledger"), "--item", str(SUBJECT), "--pr", "7",
            ])
            self.assertEqual(code, 0, said)
            self.assertEqual(_latest(_item(repo))["to"], "librarian")

    def test_checkout_flag_overrides_the_git_toplevel_default(self):
        """`ledger_dir` here sits inside `outer/nested`, a plain directory
        with its own installation config but no git history of its own — the
        *git* work tree containing it is `outer`, which declares nothing.
        Only naming `--checkout outer/nested` explicitly resolves it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            from support import write_intent_config

            outer = Path(tmp) / "outer"
            outer.mkdir(parents=True)
            git(outer, "init", "-q", "-b", "main", ".")
            nested = outer / "nested"
            write_intent_config(nested, boundaries={
                "harness-engineer": ["harness"],
                "librarian": ["ledger", ".vellum/memory"],
            })
            commit_files(outer, {}, "start")
            ledger_dir = nested / "ledger"
            code, _ = run_cli(["ledger", "open", "--version", VERSION,
                               "--ledger-dir", str(ledger_dir), "--approved", NOW])
            self.assertEqual(code, 0)
            code, said = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(ledger_dir), "--item", str(SUBJECT), "--title", "t",
                "--repo", "app", "--item-state", "planned",
            ])
            self.assertEqual(code, 0, said)
            # Without --checkout: the git toplevel is `outer`, which declares
            # no roles at all, so the announcement is recorded undelivered.
            code, said = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(ledger_dir), "--item", str(SUBJECT), "--pr", "7",
            ])
            self.assertEqual(code, 0, said)
            record = load(record_path(ledger_dir, VERSION))
            self.assertEqual(_latest(find_item(record, SUBJECT))["to"], "")
            # With --checkout naming `nested` explicitly: resolves. Rule 4:
            # this is the *same* `finished:pr7` id as the first call, so this
            # retries the address on that same entry in place rather than
            # minting a second one for the same news.
            code, said = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(ledger_dir), "--checkout", str(nested), "--item",
                str(SUBJECT), "--pr", "7",
            ])
            self.assertEqual(code, 0, said)
            record = load(record_path(ledger_dir, VERSION))
            item = find_item(record, SUBJECT)
            self.assertEqual(len(_by_kind(item, "finished")), 1, _log(item))
            self.assertEqual(_latest(item)["to"], "librarian")

    def test_json_delivers_in_the_same_act(self):
        """K3: `--json` is the opt-in for eager delivery — reporting the
        dispatch the way `announce finished --json` does — because marking
        `dispatched` with nothing printed anywhere would lose the event.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = run_cli_streams([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(repo / "ledger"), "--item", str(SUBJECT), "--pr", "7",
                "--json",
            ])
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            addressed = _addressed(payload, SUBJECT)
            self.assertEqual(len(addressed), 1, payload)
            self.assertEqual(addressed[0]["role"], "librarian")
            self.assertIs(_latest(_item(repo))["dispatched"], True)

    def test_without_json_dispatched_stays_false_for_tick_to_deliver(self):
        """The default: recorded, left pending, and a following `vellum tick`
        — not `ledger advance` itself — is what shows the addressed dispatch.
        This is the exact shape waviisoft/vellum-intent's
        `a-finished-run-dispatches-who-commissioned-it` scenario drives.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, said = run_cli([
                "ledger", "advance", "--version", VERSION, "--ledger-dir",
                str(repo / "ledger"), "--item", str(SUBJECT), "--pr", "7",
            ])
            self.assertEqual(code, 0, said)
            self.assertIs(_latest(_item(repo))["dispatched"], False)
            payload = _tick(repo)
            addressed = _addressed(payload, SUBJECT)
            self.assertEqual(len(addressed), 1, payload["actions"])
            self.assertEqual(addressed[0]["role"], "librarian")

    def test_announce_finished_still_refuses_outright_with_no_addressee(self):
        """Only the implicit announcement inside `ledger advance` softens —
        the explicit `announce finished` command keeps exiting non-zero.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_git_intent_repo(Path(tmp), boundaries={})
            run_cli(["ledger", "open", "--version", VERSION, "--ledger-dir",
                     str(repo / "ledger"), "--approved", NOW])
            run_cli(["ledger", "advance", "--version", VERSION, "--ledger-dir",
                     str(repo / "ledger"), "--item", str(SUBJECT), "--title",
                     "t", "--repo", "app", "--item-state", "planned"])
            code, out, err = run_cli_streams(
                ["announce", "finished", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--pr", "7"])
            self.assertNotEqual(code, 0, out + err)

    def test_a_retry_after_fixing_the_config_still_announces(self):
        """K3: the decision to (re-)attempt addressing is based on the
        announcement's own state, not on whether --pr's number changed — a
        second call with the *same* pr, now that config declares a holder,
        must still succeed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_git_intent_repo(Path(tmp), boundaries={})
            run_cli(["ledger", "open", "--version", VERSION, "--ledger-dir",
                     str(repo / "ledger"), "--approved", NOW])
            run_cli(["ledger", "advance", "--version", VERSION, "--ledger-dir",
                     str(repo / "ledger"), "--item", str(SUBJECT), "--title",
                     "t", "--repo", "app", "--item-state", "planned"])
            code, said = run_cli(["ledger", "advance", "--version", VERSION,
                                  "--ledger-dir", str(repo / "ledger"),
                                  "--checkout", str(repo), "--item",
                                  str(SUBJECT), "--pr", "7"])
            self.assertEqual(code, 0, said)
            self.assertEqual(_latest(_item(repo))["to"], "")

            from support import write_intent_config

            write_intent_config(repo, boundaries={
                "librarian": ["ledger", ".vellum/memory"],
            })
            commit_files(repo, {}, "declare roles")
            code, said = run_cli(["ledger", "advance", "--version", VERSION,
                                  "--ledger-dir", str(repo / "ledger"),
                                  "--checkout", str(repo), "--item",
                                  str(SUBJECT), "--pr", "7"])
            self.assertEqual(code, 0, said)
            self.assertEqual(_latest(_item(repo))["to"], "librarian")


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
            self.assertIs(_latest(_item(repo))["dispatched"], True)
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
            "announcements": [{
                "id": "finished:pr0", "kind": "finished", "to": "librarian",
                "asks": "unfiled work finished", "handoff": "", "dispatched": False,
            }],
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

    def test_by_is_optional_and_defaults_to_the_addressee(self):
        """Narrowed by the architect's 2026-09-24 note: the harness's own
        call (`announce answer <checkout> --ledger-dir <dir> --handoff
        <name>`, no `--by` at all) must still record who effectively
        answered — the addressee.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            code, said = _answer(repo, name, by=None)
            self.assertEqual(code, 0, said)
            self.assertIn("answered_by: harness-engineer", _handoffs(repo)[name])

    def test_by_equal_to_the_sender_is_refused_even_if_also_the_ledger_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch", "--from", "librarian")
            name = next(iter(_handoffs(repo)))
            code, said = _answer(repo, name, by="librarian")
            self.assertEqual(code, 2, said)
            self.assertIn("answered:\n", _handoffs(repo)[name])

    def test_the_ledger_holder_may_also_answer(self):
        """The ledger holder, when it is *not* the handoff's own sender."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp), boundaries={
                "harness-engineer": ["harness"],
                "implementer": ["src"],
                "librarian": ["ledger", ".vellum/memory"],
            })
            _record_handoff(repo, "--no-dispatch", "--from", "implementer")
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
            announced = _latest(_item(repo))
            self.assertIsNotNone(announced)
            self.assertEqual(announced["to"], "librarian")

    def test_self_dispatch_is_settled_not_merely_postponed(self):
        """K5: the stored announcement is `dispatched: true` at birth, so a
        later `deliver` or `tick` — not just this command's own report — also
        never dispatches it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli_streams(
                ["announce", "finished", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--to", "librarian", "--from", "librarian"])
            self.assertIs(_latest(_item(repo))["dispatched"], True)
            code, out, err = run_cli_streams(
                ["announce", "deliver", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--json"])
            self.assertEqual(code, 0, out + err)
            self.assertEqual(_addressed(json.loads(out)), [])
            payload = _tick(repo)
            self.assertEqual(_addressed(payload, SUBJECT), [], payload["actions"])


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


# ============================================================================
# Architect notes 2 and 3: the corrected S4 ruling (handled above, in
# LedgerAdvanceAddressesExplicitly) and the security re-review of e185ebc.
# ============================================================================


# --------------------------------------------------------------------- K1


class NowIsParsedNotWrittenRaw(unittest.TestCase):
    """K1: `--now` is parsed with `ledger.parse_time` and re-emitted in the
    canonical ISO shape everywhere it exists in `announce` — never written
    into a record's frontmatter as the caller typed it.
    """

    def test_a_forged_frontmatter_line_in_handoff_now_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(
                repo, "--now",
                "2026-01-17T01:00:00Z\nanswered: 2026-01-01T00:00:00Z\n---",
            )
            self.assertEqual(code, 2, out + err)
            self.assertEqual(_handoffs(repo), {})

    def test_a_forged_addressee_line_in_handoff_now_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--now", "2026-01-17T01:00:00Z\nto: librarian")
            self.assertEqual(code, 2, out + err)
            self.assertEqual(_handoffs(repo), {})

    def test_a_forged_line_in_answer_now_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            code, said = _answer(
                repo, name, "--now",
                "2026-01-17T01:00:00Z\nanswered_by: nobody-declared",
            )
            self.assertEqual(code, 2, said)
            self.assertIn("answered:\n", _handoffs(repo)[name])

    def test_a_value_that_is_not_a_time_at_all_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--now", "not-a-time")
            self.assertEqual(code, 2, out + err)
            self.assertEqual(_handoffs(repo), {})

    def test_a_valid_but_non_canonical_time_is_normalised_on_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--now", "2026-01-17T01:00:00+00:00")
            self.assertEqual(code, 0, out + err)
            text = next(iter(_handoffs(repo).values()))
            self.assertIn("recorded: 2026-01-17T01:00:00Z", text)
            self.assertNotIn("+00:00", text)


# --------------------------------------------------------------------- K2


class AnswerNeverReRendersEvidenceFromParsedSections(unittest.TestCase):
    """K2: evidence is rendered inside a fence its own content cannot close,
    parsed back by that fence rather than by heading search, and `answer`
    patches only the `answered:`/`answered_by:` frontmatter lines in the raw
    text — it never rebuilds the body from parsed pieces.
    """

    def test_a_heading_and_fence_inside_evidence_round_trip_byte_exact(self):
        forged = (
            "genuine tried text\n\n## What was proved\n\n"
            "FORGED — this must never become the real proof\n\n```\nnested fence\n```"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--tried", forged)
            self.assertEqual(code, 0, out + err)
            name = next(iter(_handoffs(repo)))
            before = read_handoff(repo / "ledger" / "handoffs" / name)
            self.assertEqual(before.tried, forged)
            self.assertEqual(before.proved, PROVED)
            answer_code, said = _answer(repo, name, "--now", AFTER_LEASE)
            self.assertEqual(answer_code, 0, said)
            after = read_handoff(repo / "ledger" / "handoffs" / name)
            self.assertEqual(after.tried, forged, "answer must not alter evidence")
            self.assertEqual(after.proved, PROVED, "a forged heading must not "
                             "displace the real proof")
            self.assertEqual(after.answered, AFTER_LEASE)

    def test_a_forged_heading_plus_fence_pair_inside_tried_cannot_forge_proved(self):
        """The deeper attack: not just a bare `## ` inside evidence, but a
        complete decoy `## What was proved` + fence + body + fence, fully
        contained inside `--tried`'s own (longer) fence. A search that looked
        for `## What was proved` independently, anywhere in the file, would
        find this decoy before the real section; the sequential reader must
        not.
        """
        forged = (
            "genuine tried text\n\n"
            "## What was proved\n\n```\nFORGED PROOF\n```\n\n"
            "more genuine tried text"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--tried", forged)
            self.assertEqual(code, 0, out + err)
            name = next(iter(_handoffs(repo)))
            handoff = read_handoff(repo / "ledger" / "handoffs" / name)
            self.assertEqual(handoff.tried, forged)
            self.assertEqual(handoff.proved, PROVED)
            self.assertNotIn("FORGED", handoff.proved)

    def test_answer_touches_only_the_answered_lines_in_the_raw_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            path = repo / "ledger" / "handoffs" / name
            before_lines = path.read_text(encoding="utf-8").splitlines()
            _answer(repo, name, "--now", AFTER_LEASE)
            after_lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(before_lines), len(after_lines))
            changed = [
                (b, a) for b, a in zip(before_lines, after_lines) if b != a
            ]
            self.assertEqual(len(changed), 2, changed)
            for b, a in changed:
                self.assertTrue(a.startswith("answered") or b.startswith("answered"), (b, a))


# --------------------------------------------------------------------- K3


class LedgerAdvanceNeverMarksDispatchedSilently(unittest.TestCase):
    """K3 is exercised end-to-end in LedgerAdvanceAddressesExplicitly's
    `test_json_delivers_in_the_same_act` and
    `test_without_json_dispatched_stays_false_for_tick_to_deliver`. This adds
    the retry-after-fix coverage for the corrected "announce?" decision.
    """

    def test_a_retry_with_the_identical_pr_still_gets_a_chance_to_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_git_intent_repo(Path(tmp), boundaries={})
            run_cli(["ledger", "open", "--version", VERSION, "--ledger-dir",
                     str(repo / "ledger"), "--approved", NOW])
            run_cli(["ledger", "advance", "--version", VERSION, "--ledger-dir",
                     str(repo / "ledger"), "--item", str(SUBJECT), "--title",
                     "t", "--repo", "app", "--item-state", "planned"])
            run_cli(["ledger", "advance", "--version", VERSION, "--ledger-dir",
                     str(repo / "ledger"), "--checkout", str(repo), "--item",
                     str(SUBJECT), "--pr", "7"])
            self.assertEqual(_latest(_item(repo))["to"], "")
            from support import write_intent_config

            write_intent_config(repo, boundaries={
                "librarian": ["ledger", ".vellum/memory"],
            })
            commit_files(repo, {}, "declare roles")
            # Same --pr 7 again — the old `reported = item.get("pr") != pr`
            # gate would have skipped announcing entirely here.
            code, said = run_cli(["ledger", "advance", "--version", VERSION,
                                  "--ledger-dir", str(repo / "ledger"),
                                  "--checkout", str(repo), "--item",
                                  str(SUBJECT), "--pr", "7"])
            self.assertEqual(code, 0, said)
            self.assertEqual(_latest(_item(repo))["to"], "librarian")


# --------------------------------------------------------------------- K5


# (see FinishedToIsValidatedAndNeverSelfDispatches.test_self_dispatch_is_settled_not_merely_postponed)


# --------------------------------------------------------------------- K4


class LedgerWritesAreLockedAndAtomic(unittest.TestCase):
    """K4: every read-modify-write of a ledger record holds the shared lock,
    and B1's replay repairs a lost update rather than trusting the record.
    """

    def test_concurrent_handoffs_on_different_items_lose_neither(self):
        """S-10: deterministic, not timing-based. ``vellum.announce.write`` is
        instrumented to block mid-critical-section, and the assertion is that
        a second, concurrent ``record_handoff`` call cannot even enter its own
        read until the first one's write has completed and the lock is
        released — proving mutual exclusion rather than hoping two real
        threads happen to race inside the small window ``ThreadPoolExecutor``
        gave the old version of this test no way to guarantee.

        Calls ``announce.record_handoff`` directly rather than
        ``run_cli_streams`` in a thread (the old version's own nit):
        ``run_cli_streams`` redirects the process-wide ``sys.stdout`` for its
        duration, and two threads doing that concurrently race on the same
        global, which can leak one thread's output into the other's captured
        stream.
        """
        import threading

        from vellum import announce as announce_mod

        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp), boundaries={
                "harness-engineer": ["harness"],
                "librarian": ["ledger", ".vellum/memory"],
            })
            entered_write = threading.Event()
            release_write = threading.Event()
            original_write = announce_mod.write

            def _blocking_write(path, record):
                entered_write.set()
                release_write.wait(timeout=5)
                original_write(path, record)

            announce_mod.write = _blocking_write
            try:
                def _raise(item, path):
                    announce_mod.record_handoff(
                        repo / "ledger", repo, VERSION, item, "librarian",
                        asks=f"fix item {item}", tried=TRIED, observed=OBSERVED,
                        proved=PROVED, paths=[path],
                    )

                first = threading.Thread(target=_raise, args=(SUBJECT, "harness/steps.py"))
                first.start()
                self.assertTrue(entered_write.wait(timeout=5),
                                "the first call never reached its write")

                second_done = threading.Event()
                second = threading.Thread(
                    target=lambda: (_raise(CONTROL, "harness/other.py"), second_done.set())
                )
                second.start()
                # The first call is blocked mid-write, still holding the lock.
                # A bounded, non-blocking wait proves the second call cannot
                # even begin its own read-modify-write while that lock stands.
                self.assertFalse(second_done.wait(timeout=0.3),
                                 "a concurrent record_handoff call was not blocked by the lock")
                release_write.set()
                first.join(timeout=5)
                second.join(timeout=5)
                self.assertTrue(second_done.is_set(),
                               "the second call never completed after the lock was released")
            finally:
                announce_mod.write = original_write

            self.assertTrue(_log(_item(repo, SUBJECT)),
                            "item 1's announcement was lost to a concurrent write")
            self.assertTrue(_log(_item(repo, CONTROL)),
                            "item 2's announcement was lost to a concurrent write")

    def test_a_replay_repairs_an_announcement_a_lost_update_dropped(self):
        """The B1 early-return used to skip `record_announcement` entirely on
        a match; now a replay still calls it, so a record whose
        ``announcements:`` log was lost some other way (a hand edit, a lost
        update elsewhere) is repaired by the next identical arrival rather
        than staying lost.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            path = record_path(repo / "ledger", VERSION)
            record = load(path)
            item = find_item(record, SUBJECT)
            del item["announcements"]
            write(path, record)
            self.assertEqual(_log(_item(repo)), [])
            code, out, err = _record_handoff(repo, "--no-dispatch")
            self.assertEqual(code, 0, out + err)
            self.assertTrue(_log(_item(repo)))
            self.assertEqual(_latest(_item(repo))["to"], "harness-engineer")

    def test_ledger_write_leaves_no_temp_file_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _finish(repo)
            names = [p.name for p in (repo / "ledger").iterdir()]
            leftovers = [n for n in names if n.startswith(".") and VERSION in n]
            self.assertEqual(leftovers, [], names)


# --------------------------------------------------------------------- R2


class LockFileNeverEntersTrackedLedgerTree(unittest.TestCase):
    """R2: the lock lives under git's own directory, never inside the
    tracked `ledger/` tree — a workflow that commits `ledger/` must never
    pick it up, and `verify boundaries` must never count it as a crossing.
    """

    def test_git_status_is_clean_of_lock_files_after_a_deliver(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli(["announce", "finished", str(repo), "--version", VERSION,
                     "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                     "--to", "librarian", "--no-dispatch"])
            code, out, err = run_cli_streams(
                ["announce", "deliver", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--json"])
            self.assertEqual(code, 0, out + err)
            status = git(repo, "status", "--porcelain")
            self.assertNotIn("lock", status.lower())

    def test_the_lock_is_not_inside_the_ledger_directory(self):
        from vellum.ledger import _lock_path

        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            path = _lock_path(repo / "ledger")
            self.assertNotEqual(path.parent.resolve(), (repo / "ledger").resolve())
            self.assertIn(".git", path.parts)


# --------------------------------------------------------------------- S-8


class LockFallsBackToAPrivateTempdirOnFailure(unittest.TestCase):
    """S-8: `locked()` falls back to a per-user, mode-0700 tempdir when the
    git-directory lock path cannot actually be opened — not only when there
    is no git directory at all — and raises `LedgerError` (never a raw
    `OSError`) when neither path can be opened."""

    def test_a_git_dir_that_cannot_be_opened_into_falls_back(self):
        """A regular file standing where a directory is expected fails
        ``os.open`` with ``NotADirectoryError`` for any account, root
        included — unlike a permission bit, which root's own DAC-override
        ignores — so this is what actually exercises the fallback in a test
        sandbox that may run as root."""
        import unittest.mock

        import vellum.ledger as ledger_mod

        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            ledger_dir = repo / "ledger"
            bogus = Path(tmp) / "not-a-directory"
            bogus.write_text("x", encoding="utf-8")
            with unittest.mock.patch.object(ledger_mod, "_git_dir", return_value=bogus):
                with ledger_mod.locked(ledger_dir):
                    pass

    def test_the_fallback_directory_is_private_to_this_user(self):
        from vellum.ledger import _user_lock_dir

        base = _user_lock_dir()
        self.assertEqual(base.stat().st_mode & 0o777, 0o700)

    def test_both_paths_failing_raises_ledger_error(self):
        import unittest.mock

        import vellum.ledger as ledger_mod
        from vellum.ledger import LedgerError

        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            ledger_dir = repo / "ledger"
            bogus = Path(tmp) / "not-a-directory"
            bogus.write_text("x", encoding="utf-8")
            with unittest.mock.patch.object(ledger_mod, "_git_dir", return_value=bogus), \
                 unittest.mock.patch.object(ledger_mod, "_tempdir_lock_path",
                                            return_value=bogus / "lock"):
                with self.assertRaises(LedgerError):
                    with ledger_mod.locked(ledger_dir):
                        pass


# --------------------------------------------------------------------- S-7


class LedgerWritesUseTheOrdinaryFileMode(unittest.TestCase):
    """S-7: `ledger.write` publishes the record at `0666 & ~umask`, not the
    `0600` `tempfile.mkstemp` leaves a file at by default — a checkout shared
    between accounts must be able to read a record another one last wrote."""

    def test_a_written_record_is_not_left_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _finish(repo)
            path = record_path(repo / "ledger", VERSION)
            mode = path.stat().st_mode & 0o777
            umask = os.umask(0)
            os.umask(umask)
            self.assertEqual(mode, 0o666 & ~umask)


# --------------------------------------------------------------------- S2


class HandoffIdentityUsesTheFullAsksAndVersion(unittest.TestCase):
    """S2: identity hashes the full ask (not the 200-char truncation), and
    resolves --version to the record's own full spec version first.
    """

    def test_two_asks_sharing_a_199_character_prefix_are_different_handoffs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            prefix = "x" * 199
            code1, out1, err1 = _record_handoff(repo, "--asks", prefix + "A")
            self.assertEqual(code1, 0, out1 + err1)
            code2, out2, err2 = _record_handoff(repo, "--asks", prefix + "B")
            self.assertEqual(code2, 0, out2 + err2)
            self.assertEqual(len(_handoffs(repo)), 2, _handoffs(repo))

    def test_an_abbreviated_version_does_not_mint_a_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            argv_short = ["announce", "handoff", str(repo), "--version", VERSION[:10],
                         "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                         "--from", "librarian", "--asks", ASKS, "--tried", TRIED,
                         "--observed", OBSERVED, "--proved", PROVED,
                         "--path", "harness/steps.py", "--now", NOW]
            code, out, err = run_cli_streams(argv_short)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(len(_handoffs(repo)), 1, _handoffs(repo))


# --------------------------------------------------------------------- S3


class HandoffNumbersPastFourDigitsAreNotWedged(unittest.TestCase):
    """S3: `\\d{4,}`, not `\\d{4}` — names are `:04d`, so the 10000th handoff
    is five digits, and a four-digit-only pattern refuses it outright.
    """

    def test_a_five_digit_handoff_name_is_valid_and_findable(self):
        from vellum.announce import valid_handoff_name

        self.assertTrue(valid_handoff_name("10000-apply-a-fix.md"))
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            handoffs_dir = repo / "ledger" / "handoffs"
            handoffs_dir.mkdir(parents=True)
            (handoffs_dir / "10000-existing.md").write_text(
                "---\nto: harness-engineer\nfrom: librarian\npaths:\nasks: x\n"
                "recorded: 2026-01-01T00:00:00Z\nanswered:\nanswered_by:\n---\n",
                encoding="utf-8",
            )
            code, out, err = _record_handoff(repo)
            self.assertEqual(code, 0, out + err)
            names = sorted(p.name for p in handoffs_dir.iterdir())
            self.assertIn("10000-existing.md", names)
            created = [n for n in names if n != "10000-existing.md"]
            self.assertEqual(len(created), 1, names)
            self.assertTrue(created[0].startswith("10001-"), names)


# --------------------------------------------------------------------- S5


class DeliverIsNarrowedToItsOwnVersion(unittest.TestCase):
    """S5: the push-delivery path passes --version through, so a finished/
    direction/handoff announcement on one version's item never also
    dispatches another version's pending announcement for the same item
    number.
    """

    def test_finishing_one_version_does_not_dispatch_another_versions_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            other = "b" * 40
            run_cli(["ledger", "open", "--version", other, "--ledger-dir",
                     str(repo / "ledger"), "--approved", NOW])
            run_cli(["ledger", "advance", "--version", other, "--ledger-dir",
                     str(repo / "ledger"), "--item", str(SUBJECT), "--title",
                     "t", "--repo", "app", "--item-state", "planned"])
            run_cli(["announce", "finished", str(repo), "--version", other,
                     "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                     "--to", "librarian", "--no-dispatch"])
            code, out, err = run_cli_streams(
                ["announce", "finished", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--to", "librarian", "--json"])
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            addressed = _addressed(payload)
            self.assertEqual(len(addressed), 1, payload)
            # Only this version's item was dispatched — the other version's
            # item 1 must still be pending.
            record = load(record_path(repo / "ledger", other))
            self.assertIs(_latest(find_item(record, SUBJECT))["dispatched"], False)


# --------------------------------------------------------------------- S6


class DirectionRefusesControlCharacters(unittest.TestCase):
    """S6: `record_direction` runs the same control-character refusal every
    other text field does, over the extended set (S6): `\\r`, C1 controls,
    and bidi override/isolate characters, alongside the original ESC family.
    """

    def test_an_escape_sequence_in_briefing_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = run_cli_streams(
                ["announce", "direction", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--briefing", "clear \x1b[2J the screen"])
            self.assertEqual(code, 2, out + err)
            self.assertIsNone(_item(repo).get("announced"))

    def test_a_bare_carriage_return_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--tried", "line one\rline two")
            self.assertEqual(code, 2, out + err)

    def test_a_bidi_override_character_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--asks", "safe‮evil")
            self.assertEqual(code, 2, out + err)

    def test_a_c1_control_character_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--tried", "prefix\x85suffix")
            self.assertEqual(code, 2, out + err)


# --------------------------------------------------------------------- S7


class CredentialScrubbingCoversMoreShapes(unittest.TestCase):
    """S7: beyond URL userinfo, a token-shaped query parameter, a Bearer
    value, and a `*_TOKEN`/`*_SECRET`/`*_KEY` assignment are all stripped —
    and the same scrubbing runs over `--asks` and `--briefing` too.
    """

    def test_a_query_string_access_token_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(
                repo, "--tried", "ran https://ci.example/run/7?access_token=SECRET1")
            self.assertEqual(code, 0, out + err)
            self.assertIn("rotate", (out + err).lower())
            text = next(iter(_handoffs(repo).values()))
            self.assertNotIn("SECRET1", text)

    def test_a_bearer_token_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(
                repo, "--observed", "curl -H 'Authorization: Bearer SECRET2'")
            self.assertEqual(code, 0, out + err)
            text = next(iter(_handoffs(repo).values()))
            self.assertNotIn("SECRET2", text)

    def test_an_env_style_token_assignment_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(
                repo, "--proved", "reproduced with GITHUB_TOKEN=SECRET3 set")
            self.assertEqual(code, 0, out + err)
            text = next(iter(_handoffs(repo).values()))
            self.assertNotIn("SECRET3", text)

    def test_a_credential_in_asks_is_also_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(
                repo, "--asks", "rotate API_KEY=SECRET4 then retry")
            self.assertEqual(code, 0, out + err)
            text = next(iter(_handoffs(repo).values()))
            self.assertNotIn("SECRET4", text)

    def test_a_credential_in_briefing_is_also_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = run_cli_streams(
                ["announce", "direction", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--briefing", "use AWS_SECRET=SECRET5 in the retry"])
            self.assertEqual(code, 0, out + err)
            self.assertIn("rotate", (out + err).lower())
            self.assertNotIn("SECRET5", _item(repo)["briefing"])


# --------------------------------------------------------------------- nit


class DeliverHoldsRatherThanDispatchesOnAnUnreadableHandoff(unittest.TestCase):
    """nit: an unreadable handoff must hold its announcement, matching
    `reconcile`'s behavior, rather than reading the failure as "not
    withheld" and dispatching anyway.
    """

    def test_deliver_holds_when_the_handoff_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            path = repo / "ledger" / "handoffs" / name
            with open(path, "ab") as handle:
                handle.write(b"\xff\xfe")
            code, out, err = run_cli_streams(
                ["announce", "deliver", str(repo), "--ledger-dir",
                 str(repo / "ledger"), "--json"])
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            self.assertEqual(_addressed(payload), [], payload)
            self.assertEqual(len(payload["withheld"]), 1, payload)


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


# --------------------------------------------------------------------- S-1


class URLScanningIsNotQuadratic(unittest.TestCase):
    """S-1: `_URL_RE`'s scheme is bounded, so a long run of word characters
    that never reaches `://` cannot make the regex engine backtrack
    catastrophically."""

    def test_a_long_adversarial_evidence_field_scrubs_quickly(self):
        import time

        from vellum.announce import scrub_credentials

        adversarial = ("a" * 60_000) + "!"  # never matches `\w+://`
        start = time.monotonic()
        scrub_credentials("tried", adversarial, [])
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5, f"took {elapsed:.3f}s — the regex is backtracking")

    def test_asks_over_the_cap_is_refused(self):
        from vellum.announce import AnnounceError, MAX_ASKS_BYTES

        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = _record_handoff(repo, "--asks", "x" * (MAX_ASKS_BYTES + 1))
            self.assertNotEqual(code, 0, out + err)
            self.assertIn("byte", (out + err).lower())

    def test_briefing_over_the_cap_is_refused(self):
        from vellum.announce import MAX_BRIEFING_BYTES

        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            code, out, err = run_cli_streams(
                ["announce", "direction", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--briefing", "x" * (MAX_BRIEFING_BYTES + 1)])
            self.assertNotEqual(code, 0, out + err)
            self.assertIn("byte", (out + err).lower())


# --------------------------------------------------------------------- S-2


class DeliveriesResolvesAnAbbreviatedVersion(unittest.TestCase):
    """S-2: `deliveries(..., version=...)` resolves an abbreviated sha to its
    record the same way `find_record` does everywhere else this project
    takes `--version`, rather than comparing it as a literal string against
    `spec_version` (which an abbreviation can never equal)."""

    def test_an_abbreviated_version_still_narrows_to_its_record(self):
        from vellum.announce import deliveries

        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _finish(repo)
            found = deliveries(repo / "ledger", version=VERSION[:10])
            self.assertEqual(len(found), 1, found)
            self.assertEqual(found[0].role, "librarian")

    def test_an_unmatched_abbreviation_finds_nothing_rather_than_erroring(self):
        from vellum.announce import deliveries

        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _finish(repo)
            self.assertEqual(deliveries(repo / "ledger", version="0" * 10), [])


# --------------------------------------------------------------------- S-6


class HandoffCreationIsNeverVisibleHalfWritten(unittest.TestCase):
    """S-6: a handoff record is written whole to a temp file and published
    with `os.link`, so a reader racing its creation never sees a name that
    exists but is not yet fully written — and a name already occupied (by
    another handoff, or by an attacker-planted symlink) is skipped rather
    than clobbered."""

    def test_no_temp_file_is_left_behind_after_a_successful_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo)
            names = [p.name for p in (repo / "ledger" / "handoffs").iterdir()]
            self.assertFalse([n for n in names if n.startswith(".handoff-")], names)

    def test_an_occupied_name_is_skipped_rather_than_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            tree = repo / "ledger" / "handoffs"
            tree.mkdir(parents=True, exist_ok=True)
            occupied = tree / "0001-fix-item-1.md"
            occupied.write_text("not a handoff record\n", encoding="utf-8")
            code, out, err = _record_handoff(repo, "--no-dispatch")
            self.assertEqual(code, 0, out + err)
            # The pre-existing file at 0001 was never touched; the real
            # handoff landed at the next free number instead.
            self.assertEqual(occupied.read_text(encoding="utf-8"), "not a handoff record\n")
            new_names = [p.name for p in tree.iterdir() if p.name != occupied.name]
            self.assertEqual(len(new_names), 1, new_names)
            self.assertNotEqual(new_names[0], "0001-fix-item-1.md")


# ------------------------------------------------------------------ rule 3


class SelfDispatchAndAnswerBothSettleTheLogEntry(unittest.TestCase):
    """Rule 3: an entry is marked `settled` — "self" at birth for a
    self-addressed finish, "answered" once its handoff is answered — rather
    than only ever carrying `dispatched`."""

    def test_a_self_dispatch_is_born_settled(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            run_cli_streams(
                ["announce", "finished", str(repo), "--version", VERSION,
                 "--ledger-dir", str(repo / "ledger"), "--item", str(SUBJECT),
                 "--to", "librarian", "--from", "librarian"])
            entry = _latest(_item(repo))
            self.assertEqual(entry["settled"], "self")
            self.assertIs(entry["dispatched"], True)

    def test_answering_a_handoff_settles_its_log_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _intent(Path(tmp))
            _record_handoff(repo, "--no-dispatch")
            name = next(iter(_handoffs(repo)))
            entry = _by_kind(_item(repo), "handoff")[-1]
            self.assertNotIn("settled", entry)
            code, said = _answer(repo, name, "--now", AFTER_LEASE)
            self.assertEqual(code, 0, said)
            entry = _by_kind(_item(repo), "handoff")[-1]
            self.assertEqual(entry["settled"], "answered")


if __name__ == "__main__":
    unittest.main()
