"""``vellum mint``: the guards that came across from ``on-spec-merge.yml``.

Each guard was a shell step in that workflow before it was a branch in this
command. The tests are named for what the guard protects, not for the step, so
that a rewrite of the command cannot quietly drop one and still look covered.
"""

import subprocess
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
    write_record,
)
from vellum.ledger import RECORD_KEYS, dump, load
from vellum.mint import MintError, mint
from vellum.specfile import SpecTreeError

BLOCK = """Feature: Auth
  @id:auth-one
  Scenario: One
    Given a user
    When they log in
    Then they are in
"""

CHANGED = """Feature: Auth
  @id:auth-one
  Scenario: One
    Given a user
    When they log in twice
    Then they are in
"""


class MintCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_intent_repo(Path(self.tmp.name) / "intent")
        self.ledger = self.repo / "ledger"

    def spec_commit(self, block=BLOCK):
        return commit_area(self.repo, block)

    def record(self, sha):
        return load(self.ledger / f"{sha}.yaml")


class TestMintingAVersion(MintCase):
    def test_writes_a_sha_keyed_record_in_state_approved(self):
        sha = self.spec_commit()
        result = mint(self.repo)

        self.assertTrue(result.minted)
        self.assertEqual(result.sha, sha)
        self.assertEqual(result.record, self.ledger / f"{sha}.yaml")
        record = self.record(sha)
        self.assertEqual(record["spec_version"], sha)
        self.assertEqual(record["state"], "approved")

    def test_the_first_version_has_no_baseline(self):
        sha = self.spec_commit()
        self.assertIsNone(mint(self.repo).baseline)
        self.assertIsNone(self.record(sha)["baseline"])

    def test_the_baseline_is_the_previous_spec_commit(self):
        first = self.spec_commit()
        second = commit_area(self.repo, CHANGED)

        result = mint(self.repo)

        self.assertEqual(result.sha, second)
        self.assertEqual(result.baseline, first)
        self.assertEqual(self.record(second)["baseline"], first)

    def test_the_baseline_skips_commits_that_are_not_versions(self):
        # The reason the version guard and the baseline read one rev-list: a
        # commit that does not touch the spec tree is not a version, so it is
        # not a baseline either, however recently it landed.
        first = self.spec_commit()
        commit_elsewhere(self.repo, "a ledger commit")
        second = commit_area(self.repo, CHANGED)

        self.assertEqual(mint(self.repo).baseline, first)
        self.assertEqual(self.record(second)["baseline"], first)

    def test_the_name_counts_spec_versions_not_commits(self):
        self.spec_commit()
        # Distinct messages: `commit_elsewhere` writes its message into the
        # file, and two identical ones would make an empty second commit that
        # git refuses.
        commit_elsewhere(self.repo, "ledger: open spec-v1")
        commit_elsewhere(self.repo, "harness: a fixture")
        commit_area(self.repo, CHANGED)

        # Four commits, two of them versions.
        self.assertEqual(mint(self.repo).name, "spec-v2")

    def test_the_name_is_written_into_the_record(self):
        sha = self.spec_commit()
        mint(self.repo)
        self.assertEqual(self.record(sha)["name"], "spec-v1")

    def test_ref_mints_a_commit_other_than_head(self):
        first = self.spec_commit()
        commit_elsewhere(self.repo)

        result = mint(self.repo, ref=first)

        self.assertTrue(result.minted)
        self.assertEqual(result.sha, first)

    def test_the_record_is_in_the_ledgers_emission_shape(self):
        # Not a second opinion about the record's shape: the point is that
        # minting goes through `open_record`, so a record it writes is
        # byte-identical to one `ledger open` writes and re-dumping changes
        # nothing. That is what makes `ledger/<sha>.yaml` one format.
        sha = self.spec_commit()
        mint(self.repo)
        text = (self.ledger / f"{sha}.yaml").read_text(encoding="utf-8")

        self.assertEqual(text, dump(yaml.safe_load(text)))
        self.assertEqual(list(yaml.safe_load(text)), list(RECORD_KEYS))

    def test_a_spec_pr_and_labels_reach_the_record(self):
        sha = self.spec_commit()
        mint(self.repo, spec_pr=38, labels=["spec:feature"])
        record = self.record(sha)
        self.assertEqual(record["spec_pr"], 38)
        self.assertEqual(record["labels"], ["spec:feature"])


class TestRacingMergeIsANoOp(MintCase):
    """The version half of the workflow's guard.

    ``workflow_dispatch`` reaches any commit, and the likeliest hand-run is the
    one just after the job pushes its own ledger commit. Recording that commit
    would write a baseline one version too old — silently, into the ledger's
    only trusted writer.
    """

    def test_a_commit_that_does_not_touch_the_spec_tree_mints_nothing(self):
        self.spec_commit()
        stray = commit_elsewhere(self.repo, "ledger: open spec-v1")

        result = mint(self.repo)

        self.assertFalse(result.minted)
        self.assertEqual(result.reason, "not-a-spec-version")
        self.assertEqual(result.sha, stray)
        self.assertEqual(list(self.ledger.glob("*.yaml")), [])

    def test_it_exits_zero_because_a_racing_merge_is_benign(self):
        self.spec_commit()
        commit_elsewhere(self.repo)
        code, out = run_cli(["mint", str(self.repo)])
        self.assertEqual(code, 0)
        self.assertIn("no-op", out)

    def test_it_names_the_version_it_found_instead(self):
        sha = self.spec_commit()
        commit_elsewhere(self.repo)
        self.assertIn(sha, mint(self.repo).report())

    def test_a_repo_with_no_spec_version_at_all_is_also_a_no_op(self):
        # A spec tree on disk that no commit has ever touched: `rev-list` comes
        # back empty, and `versions[-1]` on an empty list would be an
        # IndexError rather than a no-op. Only `notes.md` is staged, because
        # `git add -A` would sweep the untracked tree in and make this commit a
        # version after all.
        (self.repo / "notes.md").write_text("seed\n", encoding="utf-8")
        git(self.repo, "add", "--", "notes.md")
        git(self.repo, "commit", "-qm", "seed")

        result = mint(self.repo)

        self.assertFalse(result.minted)
        self.assertEqual(result.reason, "not-a-spec-version")
        self.assertIn("No spec version exists", result.report())


class TestReplayGuard(MintCase):
    """The record either exists for this commit or it does not (decision D11)."""

    def test_a_second_run_mints_nothing(self):
        sha = self.spec_commit()
        mint(self.repo)

        result = mint(self.repo)

        self.assertFalse(result.minted)
        self.assertEqual(result.reason, "replay")
        self.assertEqual(result.record, self.ledger / f"{sha}.yaml")

    def test_a_replay_does_not_rewrite_a_record_whose_wave_has_advanced(self):
        # The reason idempotence matters at all: by the time a run is replayed,
        # the record may be carrying a whole wave's state.
        sha = self.spec_commit()
        mint(self.repo)
        path = self.ledger / f"{sha}.yaml"
        record = load(path)
        record["state"] = "implementing"
        record["work_items"] = [{"issue": 7}]
        path.write_text(dump(record), encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        mint(self.repo)

        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_a_replay_still_reports_the_name_the_caller_may_need_to_tag(self):
        # The tag push is `continue-on-error`, so a version can end up recorded
        # and unnamed. A replay that withheld the name would leave no way to
        # finish naming it without recomputing the count by hand.
        self.spec_commit()
        mint(self.repo)
        self.assertEqual(mint(self.repo).name, "spec-v1")

    def test_a_record_under_a_different_filename_still_counts_as_a_replay(self):
        # `find_record` matches on the record's `spec_version` field, so a
        # record renamed by hand is found. Minting a second one over it would
        # give one version two records.
        sha = self.spec_commit()
        write_record(self.ledger, sha, name="spec-v1")
        (self.ledger / f"{sha}.yaml").rename(self.ledger / "renamed.yaml")

        result = mint(self.repo)

        self.assertFalse(result.minted)
        self.assertEqual(result.reason, "replay")
        self.assertEqual(len(list(self.ledger.glob("*.yaml"))), 1)

    def test_an_unreadable_record_at_the_shas_own_filename_is_not_overwritten(self):
        # `find_record` skips a record it cannot parse. Treating that as "no
        # record" would mint a second one straight over it, destroying whatever
        # the file was trying to say.
        sha = self.spec_commit()
        self.ledger.mkdir(exist_ok=True)
        (self.ledger / f"{sha}.yaml").write_text("{ not: valid: yaml", encoding="utf-8")

        result = mint(self.repo)

        self.assertFalse(result.minted)
        self.assertEqual(result.reason, "replay")
        self.assertIn("not: valid", (self.ledger / f"{sha}.yaml").read_text(encoding="utf-8"))


class TestShallowCloneIsRefused(MintCase):
    """Not a race and not a replay: a misconfigured checkout.

    Under truncation all three of mint's answers are wrong, and the count is
    wrong in the way that reuses a name: measured on the real intent repo, a
    ``--depth 3`` clone counts 1 spec commit where the full history counts 16,
    so the sixteenth version would be minted as ``spec-v1``.
    """

    def shallow(self) -> Path:
        self.spec_commit()
        commit_area(self.repo, CHANGED)
        clone = Path(self.tmp.name) / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{self.repo}", str(clone)],
            check=True, capture_output=True,
        )
        return clone

    def test_it_raises_rather_than_minting_a_wrong_name(self):
        clone = self.shallow()
        with self.assertRaises(MintError) as caught:
            mint(clone)
        self.assertIn("shallow", str(caught.exception))

    def test_the_cli_exits_one_the_repo_is_the_problem(self):
        code, out = run_cli(["mint", str(self.shallow())])
        self.assertEqual(code, 1)
        self.assertIn("fetch-depth: 0", out)

    def test_nothing_is_written(self):
        clone = self.shallow()
        with self.assertRaises(MintError):
            mint(clone)
        self.assertEqual(list((clone / "ledger").glob("*.yaml")), [])


class TestCommitting(MintCase):
    def test_by_default_nothing_is_committed(self):
        self.spec_commit()
        result = mint(self.repo)
        self.assertFalse(result.committed)
        self.assertIn("ledger/", git(self.repo, "status", "--porcelain"))

    def test_commit_makes_one_commit_under_the_orchestrator_identity(self):
        sha = self.spec_commit()
        result = mint(self.repo, commit=True)

        self.assertTrue(result.committed)
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")
        self.assertEqual(
            git(self.repo, "log", "-1", "--format=%s%n%an%n%ae").strip().split("\n"),
            ["ledger: open spec-v1", "vellum-orchestrator", "orchestrator@vellum.invalid"],
        )
        self.assertIn(f"ledger/{sha}.yaml", git(self.repo, "show", "--stat", "--format=", "HEAD"))

    def test_the_message_is_fixed_and_never_the_head_commits(self):
        # A commit message is attacker-supplied text: anyone who can land a
        # commit on main writes it. The workflow passed it through `env` so a
        # shell never parsed it; this command does not read it at all.
        hostile = 'spec: "; rm -rf / #\n\nsecond line'
        (self.repo / "spec" / "features" / "auth.md").write_text(
            "---\nid: auth\ntitle: Auth\nsince: spec-v1\n---\n\n# Auth\n", encoding="utf-8"
        )
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", hostile)

        mint(self.repo, commit=True)

        self.assertEqual(git(self.repo, "log", "-1", "--format=%B").strip(), "ledger: open spec-v1")

    def test_commit_never_pushes(self):
        # There is no remote at all, so a push would raise. The assertion is
        # that minting completes: pushing needs a credential and a branch, and
        # both are the caller's.
        self.spec_commit()
        self.assertTrue(mint(self.repo, commit=True).committed)

    def test_a_replay_commits_nothing(self):
        self.spec_commit()
        mint(self.repo, commit=True)
        head = git(self.repo, "rev-parse", "HEAD").strip()

        result = mint(self.repo, commit=True)

        self.assertFalse(result.committed)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), head)

    def test_a_record_already_in_the_tree_is_not_re_committed(self):
        sha = self.spec_commit()
        write_record(self.ledger, sha, name="spec-v1")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "ledger by hand")
        head = git(self.repo, "rev-parse", "HEAD").strip()

        result = mint(self.repo, commit=True)

        self.assertFalse(result.committed)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), head)


class TestEmit(MintCase):
    def emitted(self, path):
        return dict(
            line.split("=", 1)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line
        )

    def test_it_writes_the_run_as_key_value_lines(self):
        sha = self.spec_commit()
        out = Path(self.tmp.name) / "out.txt"

        code, _ = run_cli(["mint", str(self.repo), "--emit", str(out)])

        self.assertEqual(code, 0)
        pairs = self.emitted(out)
        self.assertEqual(pairs["sha"], sha)
        self.assertEqual(pairs["minted"], "yes")
        self.assertEqual(pairs["name"], "spec-v1")
        self.assertEqual(pairs["reason"], "")
        self.assertEqual(pairs["committed"], "no")

    def test_a_no_op_is_distinguishable_from_a_mint_without_the_exit_code(self):
        # Both exit 0. This is the whole reason `--emit` carries `minted` and
        # `reason`: a caller has to be able to skip its non-idempotent steps.
        self.spec_commit()
        commit_elsewhere(self.repo)
        out = Path(self.tmp.name) / "racing.txt"
        run_cli(["mint", str(self.repo), "--emit", str(out)])
        self.assertEqual(self.emitted(out)["reason"], "not-a-spec-version")

        commit_area(self.repo, CHANGED)
        mint(self.repo)
        replay = Path(self.tmp.name) / "replay.txt"
        run_cli(["mint", str(self.repo), "--emit", str(replay)])
        self.assertEqual(self.emitted(replay)["reason"], "replay")

    def test_it_appends_so_an_earlier_steps_outputs_survive(self):
        self.spec_commit()
        out = Path(self.tmp.name) / "out.txt"
        out.write_text("earlier=kept\n", encoding="utf-8")

        run_cli(["mint", str(self.repo), "--emit", str(out)])

        self.assertEqual(self.emitted(out)["earlier"], "kept")

    def test_an_empty_emit_path_is_an_error_not_a_silent_skip(self):
        # `--emit "$GITHUB_OUTPUT"` with the variable unset expands to `""`.
        # Treating that as "no --emit was asked for" takes the whole job green
        # with every downstream step skipped — the exact failure the flag
        # exists to prevent, arriving silently.
        self.spec_commit()
        code, out = run_cli(["mint", str(self.repo), "--emit", ""])
        self.assertEqual(code, 1)
        self.assertIn("GITHUB_OUTPUT", out)

    def test_a_value_spanning_lines_is_refused_rather_than_forging_a_key(self):
        from vellum.cli import _emit

        with self.assertRaises(MintError):
            _emit(str(Path(self.tmp.name) / "out.txt"), {"name": "spec-v1\nminted=yes"})


class TestInvocationErrors(MintCase):
    def test_a_path_that_is_not_a_spec_tree_raises_spec_tree_error(self):
        with self.assertRaises(SpecTreeError):
            mint(Path(self.tmp.name) / "nowhere")

    def test_the_cli_exits_two_for_it(self):
        # 2 means "the path you gave me is not a spec tree"; 1 means the
        # repository is the problem. A caller distinguishing a bad invocation
        # from a bad checkout reads the code, not the message.
        code, _ = run_cli(["mint", str(Path(self.tmp.name) / "nowhere")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
