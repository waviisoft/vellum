"""``vellum upgrade``: only the owned files, and only as a pull request.

``spec/features/installation.md``, four scenarios:
``@id:upgrade-rewrites-only-owned-files``,
``@id:upgrade-refuses-an-edited-owned-file``,
``@id:upgrade-plan-names-the-shape-changes`` and
``@id:doctor-reports-the-local-cli-against-the-stubs``.

The fixture, and why it is a real repository
--------------------------------------------
Everything here runs against a **greenfield installation this suite provisions**
and a **sandbox release built as a git clone of this repo**, because the command
under test reads a release's templates with ``git show <ref>:<path>`` and lands
its change on a branch off a default branch. A fixture that faked either would be
testing a different program: the two failures that matter — a file rewritten
that the manifest did not name, and a commit that reached the default branch —
are both facts about a git repository.

The sandbox's base tag is ``v9.9.8`` and the installation is provisioned at it,
so the manifest names a release the sandbox can answer for. The newer tag is
``v9.9.9`` and its templates differ. The seeds are **overlaid from the working
tree** before the base tag is cut, so the sandbox describes the code under test
rather than the last commit — a test that passed only after committing would be
one nobody could use while writing.

Exit codes are asserted by number, never as "non-zero": 0 is done or planned, 1
is an owned file this installation edited, and 2 is "I could not answer". Those
three are the command's whole contract (``vellum.cli``'s docstring).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from support import REPO_ROOT, run_cli, run_cli_streams
from vellum import __version__ as vellum_version
from vellum import changes, install, manifest, owned, seeds, upgrade as upgrade_module
from vellum.gitver import show, tags

#: The release the installation is provisioned at, and the one it is upgraded
#: to. Both are far above anything this product will cut, so a test that started
#: reading real tags would fail loudly rather than pass by coincidence.
OLDER = "v9.9.7"
BASE = "v9.9.8"
NEWER = "v9.9.9"

#: A ref in the sandbox that is NOT a release tag, pointing at the same commit
#: as :data:`BASE`. An installation stamped `--ref main` before any release was
#: cut is a real one, and this is how the tests get one whose templates still
#: line up with what it has installed.
BRANCH_REF = "at-the-base"

#: What the sandbox's newer release changes, and where. One template that is
#: verbatim package data and one that is part of the harness machinery, so the
#: rewrite is exercised on both kinds — and everything else stays equal, so
#: "unchanged between the releases" has files to be true of.
CHANGED = (
    f"src/vellum/seeds/{seeds.TEMPLATES}/{owned.CONFIG_TEMPLATE}",
    "src/vellum/seeds/harness/support/runner.py",
)

#: The entry the sandbox release adds to its own shape changelog. A
#: configuration key WITH a default, because a key without one is refused by
#: `vellum.changes` — which `TheShapeChangelogIsWellFormed` pins directly.
SANDBOX_ENTRY = """
  - release: v9.9.8
    summary: The sandbox release the installation starts at.
    config_keys_added: []
    files_added: []
    files_retired: []
    stub_inputs: []

  - release: v9.9.9
    summary: A sandbox release, built by tests/test_upgrade.py.
    config_keys_added:
      - key: sandbox_key
        default: 7
        read_by: nothing; this release exists only in the test suite
    files_added: []
    files_retired: []
    stub_inputs:
      - The sandbox release passes what v9.9.8 passed.
"""

_TMP: tempfile.TemporaryDirectory | None = None

#: The sandbox `waviisoft/vellum` checkout, and the provisioned installation
#: every test copies. Both are replaced by `setUpModule` and nothing reads them
#: before it runs; they are bound here rather than merely annotated so a reader
#: (and a linter) can see that they are module state and not a typo. Built once:
#: neither is mutated by any test, and provisioning runs lint and doctor over
#: its seed, which is not worth paying for per test.
SANDBOX = Path()
TEMPLATE = Path()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=tests@vellum.invalid",
         "-c", "user.name=vellum tests", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _only_git_on_path(root: Path) -> str:
    """A PATH holding ``git`` and nothing else.

    "No authenticated forge CLI" is made true rather than assumed, the way
    ``tests/test_init_provision.py`` makes it true: whether these tests print
    the forge commands or try to run them must not depend on what happens to be
    installed on the machine.
    """
    directory = root / "bin"
    directory.mkdir(exist_ok=True)
    found = shutil.which("git")
    assert found, "these tests need git"
    target = directory / "git"
    if not target.exists():
        target.symlink_to(found)
    return str(directory)


def _build_sandbox(root: Path) -> Path:
    """A clone of this repo carrying two release tags whose seeds differ."""
    sandbox = root / "vellum-sandbox"
    _git(REPO_ROOT, "clone", "--quiet", str(REPO_ROOT), str(sandbox))
    _git(sandbox, "checkout", "-q", "-b", "sandbox")
    # The seeds as they are in the WORKING TREE, not as they were last
    # committed: this fixture stands in for "the release the installation is
    # at", and the release this suite is testing is the one being written.
    shutil.rmtree(sandbox / "src" / "vellum" / "seeds")
    shutil.copytree(REPO_ROOT / "src" / "vellum" / "seeds",
                    sandbox / "src" / "vellum" / "seeds",
                    ignore=shutil.ignore_patterns(seeds.BYTECODE))
    _git(sandbox, "add", "-A")
    _git(sandbox, "commit", "-qm", "the release the installation is at",
         "--allow-empty")
    _git(sandbox, "tag", BASE)
    _git(sandbox, "tag", OLDER)
    _git(sandbox, "branch", BRANCH_REF)
    for relative in CHANGED:
        path = sandbox / relative
        path.write_text(
            path.read_text(encoding="utf-8") + f"\n# {NEWER} changed this file\n",
            encoding="utf-8",
        )
    changelog = sandbox / seeds.source_path(seeds.CHANGES)
    changelog.write_text(
        changelog.read_text(encoding="utf-8") + SANDBOX_ENTRY, encoding="utf-8"
    )
    _git(sandbox, "add", "-A")
    _git(sandbox, "commit", "-qm", "the newer release")
    _git(sandbox, "tag", NEWER)
    return sandbox


def setUpModule() -> None:
    global _TMP, SANDBOX, TEMPLATE
    _TMP = tempfile.TemporaryDirectory()
    root = Path(_TMP.name)
    SANDBOX = _build_sandbox(root)

    previous_path, previous_cwd = os.environ["PATH"], os.getcwd()
    cwd = root / "cwd"
    cwd.mkdir()
    os.environ["PATH"] = _only_git_on_path(root)
    os.chdir(cwd)
    try:
        TEMPLATE = root / "installed"
        code, out, err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "acme",
            "--org", "waviisoft", "--area", "billing",
            "--into", str(TEMPLATE), "--ref", BASE, "--yes",
        ])
        assert code == 0, out + err
    finally:
        os.chdir(previous_cwd)
        os.environ["PATH"] = previous_path


def tearDownModule() -> None:
    if _TMP is not None:
        _TMP.cleanup()


class UpgradeCase(unittest.TestCase):
    """A fresh copy of the provisioned pair, and no forge CLI on PATH."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        shutil.copytree(TEMPLATE, self.root / "installed", symlinks=True)
        self.intent = self.root / "installed" / "acme-intent"
        self.product = self.root / "installed" / "acme"
        self.addCleanup(os.environ.__setitem__, "PATH", os.environ["PATH"])
        os.environ["PATH"] = _only_git_on_path(self.root)

    # ------------------------------------------------------------- helpers

    def upgrade(self, *extra: str, checkout: Path | None = None, to: str = NEWER,
                source: bool = True):
        argv = ["upgrade", str(checkout or self.intent), "--to", to]
        if source:
            argv += ["--from", str(SANDBOX)]
        return run_cli(argv + list(extra))

    def git(self, repo: Path, *args: str) -> str:
        return _git(repo, *args).strip()

    def branches(self, repo: Path) -> list[str]:
        return sorted(
            line.strip().lstrip("* ").strip()
            for line in self.git(repo, "branch", "--format=%(refname:short)").splitlines()
            if line.strip()
        )

    def manifest_of(self, repo: Path):
        return manifest.load(repo)

    def files_at(self, repo: Path, ref: str) -> dict[str, str]:
        """Every tracked file at *ref*, by path — the oracle for "untouched".

        Read WITHOUT stripping: a trailing newline is a byte like any other, and
        "byte-identical" is the claim these tests make.
        """
        names = self.git(repo, "ls-tree", "-r", "--name-only", ref).splitlines()
        return {name: _git(repo, "show", f"{ref}:{name}") for name in names if name}

    def restamp(self, ref: str) -> None:
        """Move the installation to *ref*: the stubs and the manifest together.

        Two commands, because a stamp deliberately does only half of it. `vellum
        init --ref <ref> --force` restamps the stubs and then HOLDS the
        manifest's release line, since this installation owns seeded files a
        stamp does not write and moving the line without them would arm a
        refusal on every one (`install.stamp_manifest`). These tests want an
        installation genuinely AT *ref*, which in the real world is what `vellum
        upgrade` produces — so the manifest is written directly here rather than
        pretending a stamp did it.
        """
        code, out = run_cli(["init", str(self.intent), "--ref", ref, "--force"])
        self.assertEqual(code, 0, out)
        manifest.write(self.intent, ref, manifest.load(self.intent).owned)
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", f"stamped at {ref}")


# =====================================================================


class AnUpgradeRewritesOnlyOwnedFiles(UpgradeCase):
    """@id:upgrade-rewrites-only-owned-files"""

    def setUp(self):
        super().setUp()
        # A file the product wrote, beside the owned ones. It is not in the
        # manifest, so no upgrade may touch it — which is the whole claim.
        self.product_owned = self.intent / "harness" / "steps" / "billing.py"
        self.product_owned.write_text("# the harness engineer's own\n", encoding="utf-8")
        self.before = self.product_owned.read_text(encoding="utf-8")
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "the installation's own work")
        self.default_before = self.files_at(self.intent, "main")
        code, self.out = self.upgrade()
        self.assertEqual(code, 0, self.out)

    def test_the_owned_file_matches_the_newer_releases_template(self):
        shipped = _git(SANDBOX, "show", f"{NEWER}:{CHANGED[0]}")
        self.assertEqual(
            (self.intent / ".vellum" / "config.yaml").read_text(encoding="utf-8"),
            shipped,
        )
        self.assertIn(f"# {NEWER} changed this file", shipped)

    def test_the_product_owned_file_is_byte_identical(self):
        self.assertEqual(self.product_owned.read_text(encoding="utf-8"), self.before)

    def test_a_seeded_file_the_manifest_does_not_name_is_untouched(self):
        # `harness/support/adapter.py` and `harness/README.md` are seeded by
        # `vellum init` and are NOT owned (`vellum.owned` says why for each).
        # Seeded-but-not-owned is the interesting case: a rule that rewrote
        # "everything init writes" would fail exactly here.
        for relative in ("harness/support/adapter.py", "harness/README.md",
                         "spec/index.md", ".vellum/workspace.yaml"):
            self.assertNotIn(relative, self.manifest_of(self.intent).owned, relative)
            self.assertEqual(
                (self.intent / relative).read_text(encoding="utf-8"),
                self.default_before[relative],
                relative,
            )

    def test_the_manifest_names_the_newer_release(self):
        self.assertEqual(self.manifest_of(self.intent).release, NEWER)

    def test_the_owned_list_is_carried_forward_unchanged(self):
        # An upgrade never edits `owned:`. Adding to it would silently re-take a
        # file the operator had removed, which is the one edit the refusal
        # exists to invite (`vellum.manifest`).
        self.assertEqual(
            self.manifest_of(self.intent).owned,
            manifest.parse(
                self.git(self.intent, "show",
                         f"main:{manifest.MANIFEST_RELPATH.as_posix()}"),
                self.intent,
            ).owned,
        )

    def test_the_stubs_name_the_newer_release(self):
        for shipped in install.SHIPPED:
            text = (self.intent / install.WORKFLOWS_DIR["github"]
                    / shipped.filename).read_text(encoding="utf-8")
            self.assertIn(f"@{NEWER}", text, shipped.name)
            self.assertIn(f'{install.REF_INPUT}: "{NEWER}"', text, shipped.name)

    def test_the_change_sits_on_a_branch_and_not_the_default_branch(self):
        self.assertIn(f"vellum/upgrade-{NEWER}", self.branches(self.intent))
        self.assertEqual(self.files_at(self.intent, "main"), self.default_before)

    def test_the_upgraded_installation_still_doctors_green(self):
        code, out = run_cli(["doctor", str(self.intent)])
        self.assertEqual(code, 0, out)

    def test_the_commands_the_transport_did_not_take_are_printed(self):
        # No `gh` on PATH, so the forge half is the operator's and the report
        # carries the exact commands rather than a description of them.
        self.assertIn(f"push -u origin vellum/upgrade-{NEWER}", self.out)
        self.assertIn("gh pr create", self.out)
        self.assertIn("--base main", self.out)

    def test_the_pull_request_body_is_written_and_not_committed(self):
        from vellum.upgrade import PR_BODY_RELPATH

        body = self.intent / PR_BODY_RELPATH
        self.assertTrue(body.is_file())
        self.assertIn(f"{BASE} → {NEWER}", body.read_text(encoding="utf-8"))
        self.assertNotIn(
            PR_BODY_RELPATH,
            self.git(self.intent, "show", "--name-only", "--format=", "HEAD"),
        )


class TheProductSideUpgradesToo(UpgradeCase):
    def test_the_product_checkout_is_upgraded_by_its_own_manifest(self):
        code, out = self.upgrade(checkout=self.product)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.manifest_of(self.product).release, NEWER)
        self.assertIn(f"vellum/upgrade-{NEWER}", self.branches(self.product))

    def test_the_side_is_read_from_the_file_that_defines_it(self):
        from vellum.upgrade import side_of

        self.assertEqual(side_of(self.intent), owned.INTENT)
        self.assertEqual(side_of(self.product), owned.PRODUCT)

    def test_a_checkout_that_is_neither_side_is_two(self):
        elsewhere = self.root / "not-an-installation"
        elsewhere.mkdir()
        code, out = self.upgrade(checkout=elsewhere)
        self.assertEqual(code, 2, out)
        self.assertIn("not an installation", out)


class AnEditedOwnedFileStopsTheUpgrade(UpgradeCase):
    """@id:upgrade-refuses-an-edited-owned-file"""

    def setUp(self):
        super().setUp()
        self.edited = self.intent / ".vellum" / "config.yaml"
        self.edited.write_text(
            self.edited.read_text(encoding="utf-8") + "\n# raised by hand\n",
            encoding="utf-8",
        )
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "the installation tunes its config")
        self.tree = self.files_at(self.intent, "HEAD")
        self.code, self.out = self.upgrade()

    def test_it_exits_one_naming_that_file(self):
        self.assertEqual(self.code, 1, self.out)
        self.assertIn(".vellum/config.yaml", self.out)
        self.assertIn("edited", self.out)

    def test_nothing_is_written(self):
        self.assertEqual(self.files_at(self.intent, "HEAD"), self.tree)
        self.assertEqual(
            self.edited.read_text(encoding="utf-8"), self.tree[".vellum/config.yaml"]
        )
        self.assertEqual(self.manifest_of(self.intent).release, BASE)

    def test_no_branch_is_created(self):
        self.assertNotIn(f"vellum/upgrade-{NEWER}", self.branches(self.intent))

    def test_the_report_names_both_ways_out(self):
        self.assertIn(f"`{manifest.OWNED_KEY}:`", self.out)
        self.assertIn("put the file back", self.out)

    def test_an_untouched_owned_file_beside_it_is_still_not_written(self):
        # The refusal is about the RUN, not about the file: a partial upgrade
        # would leave an installation half at one release and half at another,
        # which is the state the manifest exists to make impossible.
        runner = self.intent / "harness" / "support" / "runner.py"
        self.assertEqual(
            runner.read_text(encoding="utf-8"), self.tree["harness/support/runner.py"]
        )

    def test_the_plan_refuses_too_rather_than_reporting_success(self):
        # A plan whose answer is "this would not run" says so with the code that
        # means it. Exiting 0 would make a plan and a refusal indistinguishable
        # to a caller that only reads the number.
        code, out = self.upgrade("--plan")
        self.assertEqual(code, 1, out)
        self.assertIn(".vellum/config.yaml", out)
        self.assertEqual(self.branches(self.intent), ["main"])

    def test_taking_the_line_out_of_the_manifest_lets_the_upgrade_through(self):
        found = self.manifest_of(self.intent)
        manifest.write(
            self.intent, found.release,
            [p for p in found.owned if p != ".vellum/config.yaml"],
        )
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "the config is ours now")
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertIn("# raised by hand", self.edited.read_text(encoding="utf-8"))
        self.assertEqual(self.manifest_of(self.intent).release, NEWER)


class ThePlanNamesWhatWouldChangeAndCreatesNothing(UpgradeCase):
    """@id:upgrade-plan-names-the-shape-changes"""

    def setUp(self):
        super().setUp()
        self.before = self.files_at(self.intent, "HEAD")
        self.code, self.out = self.upgrade("--plan")
        self.assertEqual(self.code, 0, self.out)

    def test_it_lists_every_owned_file_it_would_rewrite(self):
        for relative in (".vellum/config.yaml", "harness/support/runner.py",
                         ".github/workflows/spec-ci.yml"):
            self.assertRegex(self.out, rf"rewrite\s+{relative}")

    def test_it_names_the_files_unchanged_between_the_releases(self):
        self.assertRegex(self.out, r"unchanged\s+harness/support/world\.py")
        self.assertRegex(self.out, r"unchanged\s+harness/support/report\.py")

    def test_the_release_ledger_is_not_owned_so_it_is_not_in_the_plan(self):
        # Pipeline-written state, not a template Vellum keeps current. Owning it
        # made every installation that had cut a release refuse its first
        # upgrade by name (`vellum.owned` states the rule).
        self.assertNotIn("ledger/releases.yaml", self.manifest_of(self.intent).owned)
        self.assertNotIn("ledger/releases.yaml", self.out)

    def test_it_names_the_installation_shape_changes_of_the_range(self):
        self.assertIn(f"({BASE}, {NEWER}]", self.out)
        self.assertIn("configuration keys added", self.out)
        self.assertIn("sandbox_key", self.out)
        self.assertIn("default: 7", self.out)

    def test_nothing_is_created(self):
        self.assertEqual(self.files_at(self.intent, "HEAD"), self.before)
        self.assertEqual(self.branches(self.intent), ["main"])
        self.assertEqual(self.manifest_of(self.intent).release, BASE)
        self.assertFalse((self.intent / ".vellum" / "UPGRADE_PR.md").exists())
        self.assertEqual(self.git(self.intent, "status", "--porcelain"), "")


class ARangeOfMoreThanOneRelease(UpgradeCase):
    def test_the_plan_prints_every_entry_in_the_range(self):
        self.restamp(OLDER)
        code, out = self.upgrade("--plan")
        self.assertEqual(code, 0, out)
        self.assertIn(f"({OLDER}, {NEWER}]", out)
        for release in (BASE, NEWER):
            self.assertIn(f"  {release} — ", out, release)

    def test_a_manifest_naming_something_that_is_not_a_release_still_plans(self):
        # An installation stamped `--ref main` before any release was cut is a
        # real one, and refusing to plan its upgrade would strand it. The range
        # then has no lower bound this can place, and the plan says so rather
        # than printing a range it invented.
        self.restamp(BRANCH_REF)
        code, out = self.upgrade("--plan")
        self.assertEqual(code, 0, out)
        self.assertIn("not a release tag", out)
        self.assertIn(f"  {NEWER} — ", out)


class WithoutAReachableSourceItCannotAnswer(UpgradeCase):
    def test_a_to_this_cli_does_not_carry_is_two(self):
        code, out = self.upgrade(source=False)
        self.assertEqual(code, 2, out)
        self.assertIn("--from", out)
        self.assertIn(install.HOST_REPO, out)

    def test_it_writes_nothing_and_creates_no_branch(self):
        before = self.files_at(self.intent, "HEAD")
        self.upgrade(source=False)
        self.assertEqual(self.files_at(self.intent, "HEAD"), before)
        self.assertEqual(self.branches(self.intent), ["main"])

    def test_a_from_that_is_not_a_checkout_is_two(self):
        elsewhere = self.root / "not-a-checkout"
        elsewhere.mkdir()
        code, out = run_cli(["upgrade", str(self.intent), "--to", NEWER,
                             "--from", str(elsewhere)])
        self.assertEqual(code, 2, out)
        self.assertIn("not a readable git checkout", out)

    def test_a_ref_the_checkout_does_not_carry_is_two(self):
        code, out = self.upgrade(to="v9.9.6")
        self.assertEqual(code, 2, out)
        self.assertIn("carries no ref", out)

    def test_a_to_that_is_not_a_usable_ref_is_two(self):
        # It is pasted into the stubs' `uses:` lines and handed to git.
        code, out = self.upgrade(to="v1.0.0 && rm -rf /")
        self.assertEqual(code, 2, out)
        self.assertIn("is not a release", out)

    def test_a_to_that_is_a_branch_rather_than_a_release_is_two(self):
        # "Upgrading is adopting a cut": installations pin releases, never a
        # branch. A branch is a pin that moves under the manifest without
        # anybody having upgraded anything, so the manifest would record a claim
        # about files that stopped being true the next time it moved. The
        # sandbox carries `at-the-base` as a real branch, so this is refused for
        # being a branch and not for being unknown.
        code, out = self.upgrade(to=BRANCH_REF)
        self.assertEqual(code, 2, out)
        self.assertIn("is not a release", out)
        self.assertEqual(self.branches(self.intent), ["main"])

    def test_a_to_that_is_a_plain_word_is_two(self):
        code, out = self.upgrade(to="sandbox")
        self.assertEqual(code, 2, out)
        self.assertIn("is not a release", out)

    def test_an_installation_with_no_manifest_is_two(self):
        (self.intent / manifest.MANIFEST_RELPATH).unlink()
        code, out = self.upgrade()
        self.assertEqual(code, 2, out)
        self.assertIn("carries no manifest", out)

    def test_a_dirty_tree_is_two_before_a_branch_exists(self):
        (self.intent / "scratch.txt").write_text("mid-flight\n", encoding="utf-8")
        code, out = self.upgrade()
        self.assertEqual(code, 2, out)
        self.assertIn("uncommitted changes", out)
        self.assertEqual(self.branches(self.intent), ["main"])

    def test_an_upgrade_branch_that_already_exists_is_two(self):
        self.git(self.intent, "branch", f"vellum/upgrade-{NEWER}")
        code, out = self.upgrade()
        self.assertEqual(code, 2, out)
        self.assertIn("already has", out)


class AMissingOwnedFileIsSkippedNotRecreated(UpgradeCase):
    """The intent repo this product pairs with has no `harness-ci.yml` by design."""

    def setUp(self):
        super().setUp()
        self.stub = (self.intent / install.WORKFLOWS_DIR["github"] / "harness-ci.yml")
        self.stub.unlink()
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "this installation runs no harness CI")

    def test_it_is_reported_and_left_absent(self):
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"missing\s+\.github/workflows/harness-ci\.yml")
        self.assertIn("--restore", out)
        self.assertFalse(self.stub.exists())

    def test_restore_writes_it_back_at_the_new_release(self):
        code, out = self.upgrade("--restore")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.stub.is_file())
        self.assertIn(f"@{NEWER}", self.stub.read_text(encoding="utf-8"))

    def test_the_rest_of_the_upgrade_still_happens(self):
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.manifest_of(self.intent).release, NEWER)


class AnOwnedPathNoReleaseShipsIsReportedNotDeleted(UpgradeCase):
    def setUp(self):
        super().setUp()
        self.retired = self.intent / "harness" / "support" / "old.py"
        self.retired.write_text("# a file a past release shipped\n", encoding="utf-8")
        found = self.manifest_of(self.intent)
        manifest.write(self.intent, found.release,
                       [*found.owned, "harness/support/old.py"])
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "a retired file, still owned")
        self.code, self.out = self.upgrade()

    def test_it_is_reported_as_retired(self):
        self.assertEqual(self.code, 0, self.out)
        self.assertRegex(self.out, r"retired\s+harness/support/old\.py")

    def test_the_file_is_left_exactly_as_it_is(self):
        self.assertTrue(self.retired.is_file())
        self.assertEqual(
            self.retired.read_text(encoding="utf-8"),
            "# a file a past release shipped\n",
        )

    def test_it_stays_in_the_owned_list_because_upgrade_never_edits_it(self):
        self.assertIn("harness/support/old.py", self.manifest_of(self.intent).owned)


class DoctorReportsTheLocalCliAgainstTheStubs(UpgradeCase):
    """@id:doctor-reports-the-local-cli-against-the-stubs"""

    def setUp(self):
        super().setUp()
        self.code, self.out = run_cli(["doctor", str(self.intent)])

    def test_the_report_names_both_versions(self):
        # The installation is stamped at v9.9.8 and this CLI is its own version,
        # so the two are genuinely apart — which is the case the line exists for.
        self.assertIn(install.default_ref(), self.out)
        self.assertIn(f"installs {BASE}", self.out)
        self.assertIn("NOT this CLI", self.out)

    def test_doctor_exits_zero(self):
        self.assertEqual(self.code, 0, self.out)

    def test_it_is_printed_on_a_green_run_and_says_it_never_fails(self):
        self.assertIn("reported, never failed on", self.out)

    def test_a_cli_that_matches_the_stubs_is_reported_as_the_same(self):
        code, out = run_cli(["init", str(self.intent), "--force"])
        self.assertEqual(code, 0, out)
        code, out = run_cli(["doctor", str(self.intent)])
        self.assertEqual(code, 0, out)
        self.assertIn("the same", out)
        self.assertNotIn("NOT this CLI", out)


class AnUpgradeRunsOnTheBranchItCutsFrom(UpgradeCase):
    """@id:upgrade-rewrites-only-owned-files — the half about WHICH tree.

    The upgrade branch is cut from the default branch and its pull request
    merges back into it, so the default branch is the tree this rewrite lands
    on. A run from anywhere else compares one tree and writes another, and it is
    wrong in both directions: an edit made on `main` and hidden by a feature
    branch checked out over it compares as unedited and gets overwritten, and an
    edit made only on the feature branch is reported as one the installation
    made to `main`. The comparison reads `main` (below), and this refuses to run
    off it at all — either alone still leaves one direction live.
    """

    def test_a_checkout_standing_on_a_feature_branch_is_refused(self):
        self.git(self.intent, "checkout", "-q", "-b", "feature/work")
        code, out = self.upgrade()
        self.assertEqual(code, 2, out)
        self.assertIn("feature/work", out)
        self.assertIn("main", out)

    def test_it_creates_no_branch_and_writes_nothing(self):
        before = self.files_at(self.intent, "HEAD")
        self.git(self.intent, "checkout", "-q", "-b", "feature/work")
        self.upgrade()
        self.assertEqual(self.branches(self.intent), ["feature/work", "main"])
        self.assertEqual(self.files_at(self.intent, "HEAD"), before)

    def test_the_plan_is_refused_too(self):
        # A plan computed off the wrong branch describes an upgrade nobody would
        # get. Reporting it as though it were the plan is the failure.
        self.git(self.intent, "checkout", "-q", "-b", "feature/work")
        code, out = self.upgrade("--plan")
        self.assertEqual(code, 2, out)
        self.assertIn("feature/work", out)

    def test_a_detached_head_is_refused(self):
        self.git(self.intent, "checkout", "-q", "--detach", "HEAD")
        code, out = self.upgrade()
        self.assertEqual(code, 2, out)
        self.assertIn("main", out)


class TheComparisonReadsTheBaseBranchNotTheWorkingTree(UpgradeCase):
    """The other half: what `main` carries decides, not what is on disk.

    Set up as the dangerous direction. The installation edited an owned file and
    committed it to `main`; the working tree then carries the pristine template
    again — a stash popped elsewhere, a `git checkout <ref> -- <file>`, an editor
    reverting. Reading the tree, the file is exactly what the release shipped and
    the upgrade rewrites it, silently landing a pull request that undoes the
    installation's edit. Reading `main`, it is an edit and the upgrade refuses.
    """

    def setUp(self):
        super().setUp()
        self.owned_file = self.intent / ".vellum" / "config.yaml"
        self.pristine = self.owned_file.read_text(encoding="utf-8")
        self.owned_file.write_text(self.pristine + "\n# raised by hand\n",
                                   encoding="utf-8")
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "the installation tunes its config")
        # And the tree hides it again.
        self.owned_file.write_text(self.pristine, encoding="utf-8")

    def test_the_edit_on_the_base_branch_is_found_though_the_tree_hides_it(self):
        code, out = self.upgrade("--plan")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"edited\s+\.vellum/config\.yaml")


class AStubIsComparedAtTheRefItItselfPins(UpgradeCase):
    """Stubs ahead of the manifest are ordinary, not three edits.

    `vellum init --ref <new> --force` restamps the stubs and deliberately holds
    the manifest's release line when the installation owns files a stamp does
    not write (`install.stamp_manifest`). So an installation whose stubs pin a
    newer ref than its manifest names is the expected state after that command —
    and rendering each stub at the MANIFEST's release reported all three of them
    as edits the installation had made, refusing an upgrade with nothing wrong
    with it.
    """

    def setUp(self):
        super().setUp()
        code, out = run_cli(["init", str(self.intent), "--ref", NEWER, "--force"])
        self.assertEqual(code, 0, out)
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "restamped the stubs alone")

    def test_the_manifest_is_held_at_the_older_release(self):
        self.assertEqual(self.manifest_of(self.intent).release, BASE)
        for shipped in install.SHIPPED:
            text = (self.intent / install.WORKFLOWS_DIR["github"]
                    / shipped.filename).read_text(encoding="utf-8")
            self.assertIn(f"@{NEWER}", text, shipped.name)

    def test_no_stub_is_reported_as_edited(self):
        code, out = self.upgrade("--plan")
        self.assertEqual(code, 0, out)
        for shipped in install.SHIPPED:
            relative = (install.WORKFLOWS_DIR["github"] / shipped.filename).as_posix()
            self.assertNotRegex(out, rf"edited\s+{re.escape(relative)}")

    def test_the_upgrade_goes_through_and_records_the_newer_release(self):
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.manifest_of(self.intent).release, NEWER)


class AnUpgradeBranchThatAlreadyExistsIsRefused(UpgradeCase):
    def test_one_that_exists_only_on_the_remote_is_two(self):
        # One branch per release by design, so a second run for the same release
        # is a retry or a second operator — and if the first run pushed, that
        # branch has a pull request on it. Only what this checkout ALREADY knows
        # is consulted: nothing fetches, because a refusal that reached the
        # network would answer differently on a laptop with no signal.
        self.git(self.intent, "update-ref",
                 f"refs/remotes/origin/{upgrade_module.BRANCH_PREFIX}{NEWER}", "HEAD")
        code, out = self.upgrade()
        self.assertEqual(code, 2, out)
        self.assertIn("on origin", out)
        self.assertIn("nothing here fetches", out)
        self.assertEqual(self.branches(self.intent), ["main"])


class APathTheTreeRedirectsIsNeverWritten(UpgradeCase):
    """An `owned:` line is not permission to write wherever the tree points.

    The manifest is a file in the repository, so its list is written by whoever
    can land a pull request — and `upgrade` writes every path on it after a
    `mkdir -p`. The lexical checks (`vellum.manifest`) cannot see a symlink;
    these are the ones that look at the checkout.
    """

    def exclude(self, checkout: Path, *paths: str) -> None:
        """Hide untracked paths from `git status`, without touching the tree.

        `.git/info/exclude` is git's own per-checkout ignore file: it is not
        committed, so using it here does not change what the branch carries —
        which is the point. An attacker's symlink that showed up in `git status`
        would be refused by the dirty-tree check before any of this was reached,
        and a `.gitignore` covering it is one commit away.
        """
        info = checkout / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "exclude").write_text("\n".join(paths) + "\n", encoding="utf-8")

    def test_a_dangling_symlink_into_git_hooks_is_refused(self):
        stub = self.intent / install.WORKFLOWS_DIR["github"] / "harness-ci.yml"
        relative = (install.WORKFLOWS_DIR["github"] / "harness-ci.yml").as_posix()
        stub.unlink()
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "this installation runs no harness CI")
        self.exclude(self.intent, relative)
        stub.symlink_to(Path("../../.git/hooks/pre-commit"))
        hook = self.intent / ".git" / "hooks" / "pre-commit"
        self.assertFalse(hook.exists())

        code, out = self.upgrade("--restore")
        self.assertEqual(code, 1, out)
        self.assertIn("symlink", out)
        self.assertIn(relative, out)
        # The hook was not installed, so the commit this run would have made
        # could not have executed it.
        self.assertFalse(hook.exists())
        self.assertEqual(self.branches(self.intent), ["main"])

    def test_a_directory_symlink_out_of_the_checkout_is_refused(self):
        memory = self.product / ".vellum" / "memory"
        self.git(self.product, "rm", "-r", "-q", "--", ".vellum/memory")
        self.git(self.product, "commit", "-qm", "no memory map here")
        self.exclude(self.product, ".vellum/memory")
        outside = self.root / "outside"
        outside.mkdir()
        memory.symlink_to(outside, target_is_directory=True)

        code, out = self.upgrade("--restore", checkout=self.product)
        self.assertEqual(code, 1, out)
        self.assertIn("symlink", out)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.branches(self.product), ["main"])

    def test_a_git_path_in_the_owned_list_is_refused_by_the_manifest(self):
        # Written as raw text, because `manifest.write` refuses it too: this is
        # the manifest an attacker commits, not one Vellum could produce.
        path = manifest.path_for(self.intent)
        path.write_text(
            path.read_text(encoding="utf-8") + "  - .git/hooks/pre-commit\n",
            encoding="utf-8",
        )
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "an owned path in the git directory")
        code, out = self.upgrade()
        self.assertEqual(code, 2, out)
        self.assertIn(".git", out)
        self.assertEqual(self.branches(self.intent), ["main"])


class ACommitThatFailsLeavesNothingStaged(UpgradeCase):
    """The wind-back runs after `git add -A`; it must not trust the index."""

    def test_a_failing_pre_commit_hook_leaves_main_clean(self):
        hook = self.intent / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        code, out = self.upgrade()
        self.assertEqual(code, 2, out)
        self.assertEqual(self.git(self.intent, "rev-parse", "--abbrev-ref", "HEAD"),
                         "main")
        self.assertEqual(self.branches(self.intent), ["main"])
        self.assertEqual(self.git(self.intent, "status", "--porcelain"), "")
        self.assertIn("back on 'main'", out)
        text = (self.intent / ".vellum" / "install.yaml").read_text(encoding="utf-8")
        self.assertIn(f'"{BASE}"', text)


class TheBaseIsTheBranchTheStubsWatch(UpgradeCase):
    """No `origin/HEAD` to read: the stubs say which branch is the default."""

    def test_an_installation_on_trunk_with_no_remote_is_not_refused(self):
        self.git(self.intent, "branch", "-m", "main", "trunk")
        run_cli(["init", str(self.intent), "--ref", BASE, "--branch", "trunk",
                 "--force"])
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "the stubs watch trunk")

        code, out = self.upgrade("--plan")
        self.assertEqual(code, 0, out)
        self.assertNotIn("an upgrade runs on 'main'", out)


class AHalfWrittenUpgradeIsWoundBack(UpgradeCase):
    """A failure part way through the writes leaves the checkout as it was.

    "Nothing is written" is the refusals' promise, and this is the same promise
    arrived at from the other side: without it, the first unwritable path left an
    operator standing on `vellum/upgrade-<release>` with half a release's files
    in their tree and a command that had already exited.
    """

    def test_a_regular_file_where_a_parent_belongs_is_refused_before_the_branch(self):
        self.git(self.intent, "rm", "-r", "-q", "--", "harness/support")
        (self.intent / "harness" / "support").write_text("not a directory\n",
                                                         encoding="utf-8")
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "harness/support is a file now")
        code, out = self.upgrade("--restore")
        self.assertEqual(code, 1, out)
        self.assertIn("harness/support is a file", out)
        self.assertEqual(self.branches(self.intent), ["main"])
        self.assertEqual(self.git(self.intent, "rev-parse", "--abbrev-ref", "HEAD"),
                         "main")

    def test_a_write_that_fails_part_way_puts_the_checkout_back(self):
        # A directory where a file belongs passes every check a path can be
        # given — its parent is a directory, nothing is a symlink — and fails at
        # the write itself, which is exactly the case the wind-back is for.
        self.git(self.intent, "rm", "-q", "--", ".vellum/config.yaml")
        blocking = self.intent / ".vellum" / "config.yaml"
        blocking.mkdir()
        (blocking / "keep.txt").write_text("in the way\n", encoding="utf-8")
        self.git(self.intent, "add", "-A")
        self.git(self.intent, "commit", "-qm", "a directory where the config was")

        code, out = self.upgrade("--restore")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.git(self.intent, "rev-parse", "--abbrev-ref", "HEAD"),
                         "main")
        self.assertEqual(self.branches(self.intent), ["main"])
        self.assertEqual(self.git(self.intent, "status", "--porcelain"), "")
        self.assertIn("back on 'main'", out)
        # The stubs it had already rewritten before the failure are back as they
        # were, rather than left at the new release on a branch nobody has.
        for shipped in install.SHIPPED:
            text = (self.intent / install.WORKFLOWS_DIR["github"]
                    / shipped.filename).read_text(encoding="utf-8")
            self.assertIn(f"@{BASE}", text, shipped.name)


class TheBodyLivesInTheGitDirectory(UpgradeCase):
    """`.git` is a file in a worktree; the body still has somewhere to go."""

    def test_an_upgrade_run_in_a_worktree_writes_its_body_under_that_git_dir(self):
        from vellum.provision import git_dir
        from vellum.upgrade import PR_BODY_UNDER_GIT
        # Free `main` so a worktree can hold it: the upgrade runs on the default
        # branch and refuses anywhere else.
        self.git(self.intent, "checkout", "-q", "--detach")
        worktree = self.root / "intent-worktree"
        self.git(self.intent, "worktree", "add", "-q", str(worktree), "main")
        self.assertTrue((worktree / ".git").is_file())

        code, out = self.upgrade(checkout=worktree)
        self.assertEqual(code, 0, out)
        body = git_dir(worktree) / PR_BODY_UNDER_GIT
        self.assertTrue(body.is_file(), out)
        self.assertIn(str(body), out)
        self.assertNotEqual(body, worktree / ".git" / PR_BODY_UNDER_GIT)


class TheForgeHalfNamesTheRepository(UpgradeCase):
    """`--yes` opens the pull request against a repository it NAMES.

    `gh pr create` resolves the repository from the directory it runs in, and
    the transport's directory was this process's — wherever the operator was
    standing when they ran `vellum upgrade <some other checkout>`. Both halves
    are fixed: the command carries `--repo`, and the transport is given the
    checkout as its working directory.
    """

    FAKE_GH = """#!{python}
import json, os, sys
with open(os.environ["GH_TRACE"], "a", encoding="utf-8") as trace:
    trace.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.com/waviisoft/acme-intent/pull/7")
sys.exit(0)
"""

    def setUp(self):
        super().setUp()
        self.trace = self.root / "gh-trace.jsonl"
        directory = Path(os.environ["PATH"].split(os.pathsep)[0])
        fake = directory / "gh"
        fake.write_text(self.FAKE_GH.format(python=sys.executable), encoding="utf-8")
        fake.chmod(0o755)
        self.addCleanup(fake.unlink)
        self.addCleanup(os.environ.pop, "GH_TRACE", None)
        os.environ["GH_TRACE"] = str(self.trace)
        # A remote whose URL is a forge one and whose PUSH url is a bare
        # repository beside it: the slug has to come from a real remote, and
        # this test must not touch a network to prove it.
        self.bare = self.root / "origin.git"
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.git(self.intent, "remote", "add", "origin",
                 "https://github.com/waviisoft/acme-intent.git")
        self.git(self.intent, "remote", "set-url", "--push", "origin", str(self.bare))

    def recorded(self) -> list[dict]:
        if not self.trace.is_file():
            return []
        return [json.loads(line) for line in
                self.trace.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_pr_create_names_the_repo_and_runs_in_the_checkout(self):
        code, out = self.upgrade("--yes")
        self.assertEqual(code, 0, out)
        created = [e for e in self.recorded() if e["argv"][:2] == ["pr", "create"]]
        self.assertEqual(len(created), 1, self.recorded())
        argv = created[0]["argv"]
        self.assertIn("--repo", argv)
        self.assertEqual(argv[argv.index("--repo") + 1], "waviisoft/acme-intent")
        self.assertEqual(argv[argv.index("--base") + 1], "main")
        self.assertEqual(argv[argv.index("--head") + 1],
                         f"{upgrade_module.BRANCH_PREFIX}{NEWER}")
        self.assertEqual(Path(created[0]["cwd"]).resolve(), self.intent.resolve())

    def test_the_body_file_it_is_given_is_outside_the_working_tree(self):
        self.upgrade("--yes")
        created = [e for e in self.recorded() if e["argv"][:2] == ["pr", "create"]]
        argv = created[0]["argv"]
        body = Path(argv[argv.index("--body-file") + 1])
        self.assertIn(".git", body.parts)
        self.assertEqual(self.git(self.intent, "status", "--porcelain"), "")

    def test_the_body_is_removed_once_gh_has_taken_it(self):
        self.upgrade("--yes")
        self.assertFalse((self.intent / upgrade_module.PR_BODY_RELPATH).exists())

    def test_the_pull_request_url_is_reported(self):
        code, out = self.upgrade("--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("https://github.com/waviisoft/acme-intent/pull/7", out)

    def test_the_printed_commands_carry_the_real_repo_when_gh_is_not_asked(self):
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertIn("--repo waviisoft/acme-intent", out)

    def test_a_checkout_with_no_readable_origin_refuses_yes_before_writing(self):
        self.git(self.intent, "remote", "remove", "origin")
        before = self.files_at(self.intent, "HEAD")
        code, out = self.upgrade("--yes")
        self.assertEqual(code, 2, out)
        self.assertIn("no `origin`", out)
        self.assertEqual(self.branches(self.intent), ["main"])
        self.assertEqual(self.files_at(self.intent, "HEAD"), before)
        self.assertEqual(self.recorded(), [])


class ThePullRequestBodyFencesTheChangelogItQuotes(unittest.TestCase):
    """A summary carrying backticks must not close the fence around it.

    The fenced block is a release's own changelog prose, and prose about a tool
    says `like this`. A fixed three-backtick fence closes on the first line
    carrying three of its own, and everything after it renders as markup in the
    pull request body — a heading, a link, a checkbox somebody's summary
    happened to contain. CommonMark closes a fence only with a run at least as
    long as the opener, so the opener counts.
    """

    def test_it_is_longer_than_the_longest_run_in_the_content(self):
        lines = ["a ``` b", "c ````` d"]
        self.assertEqual(upgrade_module._fence(lines), "`" * 6)

    def test_plain_content_keeps_the_ordinary_fence(self):
        self.assertEqual(upgrade_module._fence(["nothing to see", "`one`"]),
                         upgrade_module.FENCE)


class TheOriginUrlIsReadAsAForgeRepository(unittest.TestCase):
    def test_the_two_shapes_a_forge_remote_takes(self):
        for url in ("https://github.com/waviisoft/vellum.git",
                    "https://github.com/waviisoft/vellum",
                    "https://user@github.example/waviisoft/vellum.git",
                    "ssh://git@github.com/waviisoft/vellum.git",
                    "git@github.com:waviisoft/vellum.git",
                    "git@github.com:waviisoft/vellum"):
            self.assertEqual(upgrade_module.slug_of(url), "waviisoft/vellum", url)

    def test_anything_that_is_not_one_is_not_guessed_at(self):
        # A local clone's last two path components are not a forge repository,
        # and handing them to `gh pr create --repo` would name somebody else's.
        for url in ("", "/home/me/work/acme", "../acme", "file:///tmp/x/acme.git",
                    "https://github.com/waviisoft", "https://github.com/a/b/c"):
            self.assertIsNone(upgrade_module.slug_of(url), url)


class ReleasesFromBeforeTemplatesExistedCanStillBeRead(unittest.TestCase):
    """@id:upgrade-rewrites-only-owned-files, for the installations that exist.

    Every installation in the world was provisioned by v0.2.0 or earlier, and at
    those releases the seeded config, the release ledger and the memory map were
    string constants in `src/vellum/provision.py` rather than files under
    `src/vellum/seeds/templates/`. Without the fallback, an upgrade off such an
    installation reads no template for them at the release the manifest names
    and reports every one as `unverifiable` — permanently, because that release
    never changes by itself. So the fallback is what lets an existing
    installation own a seeded file at all.
    """

    OLD = "v0.2.0"

    def setUp(self):
        try:
            found = tags(REPO_ROOT, self.OLD)
        except Exception as exc:  # not a checkout, or git is unavailable
            self.skipTest(f"release tags could not be read: {exc}")
        if not found:
            self.skipTest(f"this checkout carries no {self.OLD} tag")
        self.source = upgrade_module.Templates(checkout=REPO_ROOT)

    def paths(self):
        for name in (owned.CONFIG_TEMPLATE, owned.RELEASES_TEMPLATE,
                     owned.MEMORY_MAP_TEMPLATE):
            yield name, seeds.source_path(seeds.TEMPLATES, name)

    def test_that_release_really_ships_none_of_these_files(self):
        # The premise, asserted rather than assumed: if `templates/` did exist at
        # v0.2.0 the fallback would be dead code and the tests below would be
        # passing on the ordinary path.
        for name, path in self.paths():
            self.assertIsNone(show(REPO_ROOT, self.OLD, path), name)

    def test_it_reads_back_what_that_release_seeded(self):
        # Byte for byte against the templates as they are shipped today, because
        # the move out of `provision.py` was byte for byte. A release that
        # genuinely CHANGES one of these templates makes this assertion wrong
        # rather than the code: freeze v0.2.0's bytes as a fixture then, and
        # keep comparing against those.
        for name, path in self.paths():
            self.assertEqual(self.source.read(self.OLD, path), seeds.template(name),
                             name)

    def test_the_interpolation_that_release_applied_is_reproduced(self):
        config = self.source.read(
            self.OLD, seeds.source_path(seeds.TEMPLATES, owned.CONFIG_TEMPLATE)
        )
        self.assertIn("divergence_cap: 3", config)
        self.assertNotIn("{divergence_cap}", config)

    def test_a_placeholder_the_checkout_fills_is_left_alone(self):
        # `{intent_slug}` is still a placeholder today; the upgrade fills it from
        # `.vellum/product.yaml`. Substituting it here would hand the comparison
        # a template with somebody's slug already in it.
        memory = self.source.read(
            self.OLD, seeds.source_path(seeds.TEMPLATES, owned.MEMORY_MAP_TEMPLATE)
        )
        self.assertIn("{intent_slug}", memory)

    def test_a_release_after_the_move_is_not_read_this_way(self):
        # The fallback is history, not a general mechanism: a release that ships
        # no template ships no template, and saying otherwise would make every
        # `files_retired` entry unverifiable instead.
        self.assertIsNone(
            self.source.pre_templates(
                "v9.9.9", seeds.source_path(seeds.TEMPLATES, owned.CONFIG_TEMPLATE)
            )
        )
        self.assertIsNone(
            self.source.pre_templates(self.OLD, seeds.source_path(seeds.CHANGES))
        )


# =====================================================================
# The shape changelog itself
# =====================================================================


class TheShapeChangelogIsWellFormed(unittest.TestCase):
    def setUp(self):
        self.changes = changes.load()

    def test_it_ships_with_the_cli_as_package_data(self):
        # Read out of the INSTALLED package rather than off disk: `.yaml` under
        # a package is not shipped by setuptools' defaults the way `.py` is, and
        # a `pyproject.toml` that stopped declaring it would leave `upgrade`
        # with no changelog and `init` with no config to seed.
        self.assertIn("schema:", seeds.changes_text())
        for name in (owned.CONFIG_TEMPLATE, owned.RELEASES_TEMPLATE,
                     owned.MEMORY_MAP_TEMPLATE):
            self.assertTrue(seeds.template(name).strip(), name)

    def test_every_configuration_key_it_adds_carries_a_default(self):
        # The rule the decision states — "always with a default; never required
        # without one" — asserted against the reader that enforces it, so an
        # entry written without one fails here and not in somebody's upgrade.
        for entry in self.changes.entries:
            for row in entry.sections["config_keys_added"]:
                self.assertIn("default:", row, f"{entry.release}: {row}")

    def test_a_key_without_a_default_is_refused(self):
        with self.assertRaises(changes.ChangesError) as raised:
            changes.parse(
                "schema: 1\nreleases:\n  - release: v1.0.0\n"
                "    config_keys_added:\n      - {key: budgets.new_gate}\n"
            )
        self.assertIn("default", str(raised.exception))

    def test_it_carries_a_template_entry_that_is_not_a_release(self):
        self.assertIsNotNone(self.changes.template)
        self.assertNotIn(
            self.changes.template.release, [e.release for e in self.changes.entries]
        )
        found, _ = self.changes.between("v0.0.0", "v99.0.0")
        self.assertNotIn(self.changes.template.release, [e.release for e in found])

    def test_the_template_carries_every_section(self):
        raw = yaml.safe_load(seeds.changes_text())["template"]
        for name, _ in changes.SECTIONS:
            self.assertIn(name, raw, name)

    def test_the_first_two_releases_are_recorded(self):
        self.assertEqual([e.release for e in self.changes.entries][:2],
                         ["v0.1.0", "v0.2.0"])

    def test_v0_2_0_records_the_token_becoming_optional(self):
        entry = self.changes.by_release("v0.2.0")
        joined = " ".join(entry.sections["stub_inputs"])
        self.assertIn("required: false", joined)
        self.assertIn(install.SECRET, joined)

    def test_entries_order_as_versions_not_lexically(self):
        found = changes.parse(
            "schema: 1\nreleases:\n"
            "  - {release: v0.9.0}\n  - {release: v0.10.0}\n  - {release: v0.2.0}\n"
        )
        self.assertEqual([e.release for e in found.entries],
                         ["v0.2.0", "v0.9.0", "v0.10.0"])

    def test_a_range_is_open_at_the_top_and_closed_at_the_bottom(self):
        found = changes.parse(
            "schema: 1\nreleases:\n"
            "  - {release: v1.0.0}\n  - {release: v2.0.0}\n  - {release: v3.0.0}\n"
        )
        picked, _ = found.between("v1.0.0", "v2.0.0")
        self.assertEqual([e.release for e in picked], ["v2.0.0"])

    def test_a_schema_it_does_not_understand_is_refused(self):
        with self.assertRaises(changes.ChangesError):
            changes.parse("schema: 99\nreleases: []\n")


class TheReleaseThisCutIs(unittest.TestCase):
    """The pre-tag alarm: three files state one release, before anybody tags it.

    `EveryReleaseTagHasAShapeEntry` below is the post-tag alarm and it cannot
    fire earlier — it reads the repository's real tags, and a release that has
    not been cut has none. That left the window where the mistake actually
    happens: a wave lands, the version is still the previous one, and nothing
    says so until somebody pushes a tag with no changelog entry behind it. So
    these two assertions read the version the working tree CLAIMS to be and hold
    the other two files to it.
    """

    def test_the_two_files_that_state_the_version_agree(self):
        # `src/vellum/__init__.py` is what `vellum --version` prints and what
        # every stub is stamped with by default; `pyproject.toml` is what a
        # wheel carries. Two files, one fact, and an installation stamped from a
        # CLI whose wheel says something else is one nobody can reason about.
        declared = yaml.safe_load(
            "\n".join(
                line for line in
                (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines()
                if line.startswith("version = ")
            ).replace(" = ", ": ")
        )
        self.assertEqual(declared["version"], vellum_version)

    def test_this_versions_release_tag_has_a_shape_entry(self):
        recorded = {entry.release for entry in changes.load().entries}
        self.assertIn(
            f"v{vellum_version}", recorded,
            f"this checkout calls itself v{vellum_version} and "
            f"{seeds.source_path(seeds.CHANGES)} has no entry for it. A release "
            f"is cut by bumping the version, writing the entry, and THEN tagging "
            f"— the entry goes in before the tag, not after it.",
        )


class EveryReleaseTagHasAShapeEntry(unittest.TestCase):
    """A cut release with no entry is a `--plan` that cannot describe it.

    The alarm for forgetting, and deliberately a red test rather than a note in
    a checklist: `adapters/github/` is kept honest the same way
    (`TheCommittedTemplatesAreWhatInitWrites`). Skipped where the tags cannot be
    read — a shallow CI clone, a fresh archive — because an absent tag list is a
    fact about the environment and not about this file.
    """

    def test_every_v_tag_in_this_repo_is_in_the_changelog(self):
        try:
            found = [t for t in tags(REPO_ROOT, "v*") if install.RELEASE_RE.match(t)]
        except Exception as exc:  # not a checkout, or git is unavailable
            self.skipTest(f"release tags could not be read: {exc}")
        if not found:
            self.skipTest("this checkout carries no v* release tags")
        recorded = {entry.release for entry in changes.load().entries}
        self.assertEqual(
            sorted(set(found) - recorded), [],
            "a release was cut without its installation-shape entry; add it to "
            f"{seeds.source_path(seeds.CHANGES)} (the `template:` key is the shape)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
