"""``vellum certify record|check``: sha-bound certification, and the lease.

The two scenarios this is written against
(``spec/features/certification-and-releases.md``):

* ``@id:no-self-certified-merge`` — "Given an implementation PR whose in-session
  checks pass / And no certification run is recorded for its head commit / Then
  the PR does not auto-merge."
* ``@id:new-commit-invalidates-cert`` — "Given a certified merge candidate /
  When a new commit is pushed to its branch / Then the recorded certification
  no longer authorizes merge."

Both are assertions about a *denial*, so the tests assert exit code 1 rather
than "non-zero": 2 means the command could not answer, and a merge gate that
cannot tell "this head is uncertified" from "that is not an intent checkout"
reports the second as the first the moment a path is mistyped.
"""

import datetime
import tempfile
import unittest
from pathlib import Path

from support import run_cli
from vellum.ledger import (
    LedgerError,
    active_lease,
    advance,
    certification_authorizes,
    certify,
    clear_lease,
    find_item,
    load,
    open_record,
    record_path,
    take_lease,
)

VERSION = "9c8b70a71089fc8fa6f585ff5f287bb740eff141"
#: The PR head a certification run proved. Full forty: a certification binds to
#: one commit, and this CLI refuses to decide an authorization on a prefix.
HEAD = "1ce87cb5dd140bf2d9b125f9124d256fc4a19303"
#: The commit pushed after that run — the whole of @id:new-commit-invalidates-cert.
NEW_HEAD = "2906dfb4a92e66e42cf07bd7e7e6e2e72f6dc66b"
ISSUE = 121


class CertifyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkout = Path(self.tmp.name)
        self.dir = self.checkout / "ledger"
        open_record(self.dir, VERSION)
        advance(self.dir, VERSION, issue=ISSUE, title="Session expiry", repo="app", pr=124)

    def item(self):
        return find_item(load(record_path(self.dir, VERSION)), ISSUE)

    def check(self, head=HEAD, item=ISSUE, version=VERSION):
        return run_cli([
            "certify", "check", str(self.checkout),
            "--version", version, "--item", str(item), "--head", head,
        ])

    def record(self, sha=HEAD, result="green", extra=()):
        return run_cli([
            "certify", "record", str(self.checkout),
            "--version", VERSION, "--item", str(ISSUE),
            "--sha", sha, "--result", result, *extra,
        ])


class TestNoSelfCertifiedMerge(CertifyCase):
    """@id:no-self-certified-merge — passing your own checks is not evidence."""

    def test_an_uncertified_head_is_denied(self):
        # The PR's in-session checks passing is not an input to this command at
        # all: there is nothing to pass it through, which is the point.
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("DENIED", output)
        self.assertIn("no certification is recorded", output)

    def test_the_denial_names_what_would_authorize(self):
        _, output = self.check()
        self.assertIn("recorded green certification run", output)

    def test_a_certification_on_another_work_item_does_not_carry_over(self):
        advance(self.dir, VERSION, issue=999, title="other", repo="app")
        certify(self.dir, VERSION, 999, HEAD, "green")
        self.assertEqual(self.check()[0], 1)

    def test_a_red_certification_at_the_head_is_denied(self):
        self.record(result="red")
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("'red'", output)

    def test_a_green_certification_at_the_head_authorizes(self):
        # The negative control: without this, every assertion above would still
        # pass if `check` simply always denied.
        self.assertEqual(self.record()[0], 0)
        code, output = self.check()
        self.assertEqual(code, 0)
        self.assertIn("AUTHORIZED", output)


class TestNewCommitInvalidatesCert(CertifyCase):
    """@id:new-commit-invalidates-cert — certification binds to a sha."""

    def test_a_certification_at_one_commit_does_not_authorize_another(self):
        self.record(sha=HEAD)
        self.assertEqual(self.check(head=HEAD)[0], 0)
        code, output = self.check(head=NEW_HEAD)
        self.assertEqual(code, 1)
        self.assertIn(HEAD[:12], output)
        self.assertIn(NEW_HEAD[:12], output)

    def test_the_recorded_certification_is_left_alone_by_a_denied_check(self):
        # "the recorded certification no longer authorizes merge" — it is not
        # erased by being asked about, and re-asking at the certified head
        # still authorizes. `check` writes nothing.
        self.record(sha=HEAD)
        before = record_path(self.dir, VERSION).read_text()
        self.check(head=NEW_HEAD)
        self.assertEqual(record_path(self.dir, VERSION).read_text(), before)
        self.assertEqual(self.check(head=HEAD)[0], 0)

    def test_re_certifying_at_the_new_head_authorizes_it_and_only_it(self):
        self.record(sha=HEAD)
        self.record(sha=NEW_HEAD)
        self.assertEqual(self.check(head=NEW_HEAD)[0], 0)
        # The superseded certification does not go on authorizing its own
        # commit: one certification is recorded, not a set to search.
        self.assertEqual(self.check(head=HEAD)[0], 1)

    def test_invalidation_needs_no_notion_of_later(self):
        # "any subsequent commit invalidates" is implemented as "any commit
        # that is not the certified one", which needs no ancestry and so cannot
        # be fooled by a force-push that rewrites what `subsequent` means.
        certify(self.dir, VERSION, ISSUE, NEW_HEAD, "green")
        authorized, reason = certification_authorizes(self.item(), HEAD)
        self.assertFalse(authorized)
        self.assertIn("binds to a sha", reason)


class TestAbbreviationsAreRefused(CertifyCase):
    """An authorization is about one commit, so a prefix is not resolved."""

    def test_an_abbreviated_head_is_a_bad_invocation_not_a_denial(self):
        self.record(sha=HEAD)
        code, output = self.check(head=HEAD[:7])
        self.assertEqual(code, 2)
        self.assertIn("full commit sha", output)

    def test_an_abbreviated_certified_sha_is_refused(self):
        code, output = self.record(sha=HEAD[:10])
        self.assertEqual(code, 2)
        self.assertIn("full commit sha", output)

    def test_a_prefix_of_the_certified_sha_does_not_authorize(self):
        # The failure this refusal exists to prevent, asserted at the library
        # level so it cannot be reintroduced below the CLI.
        certify(self.dir, VERSION, ISSUE, HEAD, "green")
        with self.assertRaises(LedgerError):
            certification_authorizes(self.item(), HEAD[:7])


class TestRecording(CertifyCase):
    def test_a_certification_carries_every_field_the_spec_names(self):
        self.record(extra=["--run", "https://forge/run/9", "--at", "2026-08-31T04:00:00Z"])
        self.assertEqual(
            self.item()["certification"],
            {"sha": HEAD, "run": "https://forge/run/9", "at": "2026-08-31T04:00:00Z",
             "result": "green"},
        )

    def test_a_red_run_is_recorded_and_the_command_succeeds(self):
        # Recording a red is not a failure of the recorder; the denial is
        # `check`'s to give, and a red that could not be written would leave
        # the ledger unable to say a run had happened at all.
        code, _ = self.record(result="red")
        self.assertEqual(code, 0)
        self.assertEqual(self.item()["certification"]["result"], "red")

    def test_a_result_that_is_neither_green_nor_red_is_refused(self):
        # argparse refuses the choice and exits 2 itself, which is the same
        # code this CLI would give it: a result that is not green or red is a
        # bad invocation, not a certification that failed. Asserted through
        # SystemExit rather than a return value so a later move of the check
        # out of `choices` and into the library has to keep the number.
        with self.assertRaises(SystemExit) as caught:
            self.record(result="passed")
        self.assertEqual(caught.exception.code, 2)
        self.assertIsNone(self.item()["certification"])

    def test_the_library_refuses_an_unknown_result_too(self):
        # The same rule below the CLI, so it is not carried by `choices` alone.
        with self.assertRaises(LedgerError):
            certify(self.dir, VERSION, ISSUE, HEAD, "passed")

    def test_certifying_an_unknown_work_item_cannot_answer(self):
        code, output = run_cli([
            "certify", "record", str(self.checkout), "--version", VERSION,
            "--item", "404", "--sha", HEAD, "--result", "green",
        ])
        self.assertEqual(code, 2)
        self.assertIn("404", output)

    def test_checking_an_unknown_work_item_cannot_answer(self):
        # Distinct from a denial on purpose: naming the wrong issue is a
        # mistyped invocation, and reporting it as "not certified" would send
        # someone looking for a certification run that was never asked for.
        code, output = self.check(item=404)
        self.assertEqual(code, 2)
        self.assertIn("404", output)

    def test_a_checkout_with_no_ledger_cannot_answer(self):
        empty = Path(self.tmp.name) / "elsewhere"
        empty.mkdir()
        code, output = run_cli([
            "certify", "check", str(empty), "--version", VERSION,
            "--item", str(ISSUE), "--head", HEAD,
        ])
        self.assertEqual(code, 2)
        self.assertIn("no ledger directory", output)

    def test_the_ledger_dir_can_be_named_directly(self):
        code, _ = run_cli([
            "certify", "record", str(self.checkout), "--ledger-dir", str(self.dir),
            "--version", VERSION, "--item", str(ISSUE),
            "--sha", HEAD, "--result", "green",
        ])
        self.assertEqual(code, 0)


class TestCorruptCertification(CertifyCase):
    """A field that is not a certification denies; it does not crash or pass."""

    def deny(self, value):
        path = record_path(self.dir, VERSION)
        record = load(path)
        find_item(record, ISSUE)["certification"] = value
        from vellum.ledger import write
        write(path, record)
        return self.check()

    def test_a_certification_that_is_not_a_mapping_is_denied(self):
        code, output = self.deny("green")
        self.assertEqual(code, 1)
        self.assertIn("not a mapping", output)

    def test_a_certification_naming_no_sha_is_denied(self):
        code, output = self.deny({"sha": None, "run": None, "at": None, "result": "green"})
        self.assertEqual(code, 1)
        self.assertIn("naming no sha", output)

    def test_a_certification_with_no_result_is_denied(self):
        code, output = self.deny({"sha": HEAD, "run": None, "at": None, "result": None})
        self.assertEqual(code, 1)
        self.assertIn("no result", output)


class TestLease(CertifyCase):
    """`lease: {executor, taken, expires}` — transient claim state.

    "The reconciler writes it at claim, clears it at report, and treats an
    expired lease as no lease, returning the item to the queue. Mid-run means
    holding an unexpired lease." (``spec/features/ledger.md``)

    No `vellum lease` command is wired: nothing in the spec asks a scenario of
    the lease that a caller could drive today. ``@id:fire-and-collect`` turns on
    "an executor mid-run on a claimed work item", and the party that claims,
    reports and lapses is the reconciler, which is not built yet. So the wave
    lands the schema and the reading of it, and these tests drive the helpers
    the reconciler will use — which is the behavior that would otherwise be
    re-decided, in a hurry, inside it.
    """

    def at(self, when):
        return datetime.datetime.fromisoformat(when.replace("Z", "+00:00"))

    def test_an_item_with_no_lease_is_unclaimed(self):
        self.assertIsNone(active_lease(self.item()))

    def test_a_lease_in_the_future_is_held(self):
        take_lease(self.dir, VERSION, ISSUE, "claude-actions", "2026-08-31T14:00:00Z",
                   taken="2026-08-31T13:00:00Z")
        lease = active_lease(self.item(), now=self.at("2026-08-31T13:30:00Z"))
        self.assertEqual(
            lease,
            {"executor": "claude-actions", "taken": "2026-08-31T13:00:00Z",
             "expires": "2026-08-31T14:00:00Z"},
        )

    def test_an_expired_lease_is_no_lease(self):
        take_lease(self.dir, VERSION, ISSUE, "claude-actions", "2026-08-31T14:00:00Z")
        self.assertIsNone(active_lease(self.item(), now=self.at("2026-08-31T14:00:01Z")))

    def test_a_lease_expiring_exactly_now_is_not_held(self):
        # Exclusive, so a lease is held *until* it expires. Stated as a test
        # because the boundary is the only part of expiry a reader can get
        # wrong without noticing.
        take_lease(self.dir, VERSION, ISSUE, "claude-actions", "2026-08-31T14:00:00Z")
        self.assertIsNone(active_lease(self.item(), now=self.at("2026-08-31T14:00:00Z")))

    def test_clearing_a_lease_returns_the_item_to_the_queue(self):
        take_lease(self.dir, VERSION, ISSUE, "claude-actions", "2036-01-01T00:00:00Z")
        self.assertIsNotNone(active_lease(self.item()))
        clear_lease(self.dir, VERSION, ISSUE)
        self.assertIsNone(active_lease(self.item()))
        # Cleared, not removed: the field stays part of the item's shape, so a
        # released claim reads differently in a diff from one never taken.
        self.assertIn("lease", self.item())
        self.assertIsNone(self.item()["lease"])

    def test_a_lease_with_an_unreadable_expiry_is_no_lease(self):
        path = record_path(self.dir, VERSION)
        record = load(path)
        find_item(record, ISSUE)["lease"] = {
            "executor": "claude-actions", "taken": None, "expires": "whenever",
        }
        from vellum.ledger import write
        write(path, record)
        # The direction that costs a second executor a restart, rather than the
        # one that strands the item behind a claim no clock can retire.
        self.assertIsNone(active_lease(self.item()))

    def test_a_lease_that_could_never_expire_is_refused_at_write(self):
        with self.assertRaises(LedgerError):
            take_lease(self.dir, VERSION, ISSUE, "claude-actions", "whenever")

    def test_a_lease_with_no_executor_is_refused(self):
        with self.assertRaises(LedgerError):
            take_lease(self.dir, VERSION, ISSUE, "  ", "2036-01-01T00:00:00Z")

    def test_a_lease_does_not_certify_and_a_certification_does_not_claim(self):
        # The two fields are read on different clocks and answer different
        # questions; nothing should make one imply the other.
        take_lease(self.dir, VERSION, ISSUE, "claude-actions", "2036-01-01T00:00:00Z")
        self.assertEqual(self.check()[0], 1)
        certify(self.dir, VERSION, ISSUE, HEAD, "green")
        clear_lease(self.dir, VERSION, ISSUE)
        self.assertEqual(self.check()[0], 0)


if __name__ == "__main__":
    unittest.main()
