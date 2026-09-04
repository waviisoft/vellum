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

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from support import REPO_ROOT, run_cli, run_cli_streams
from vellum import changes, install, manifest, owned, seeds
from vellum.gitver import tags

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
#: every test copies. Built once: neither is mutated by any test, and
#: provisioning runs lint and doctor over its seed, which is not worth paying
#: for per test.
SANDBOX: Path
TEMPLATE: Path


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

        The pair is the point. A manifest edited on its own leaves the stubs
        pinned somewhere else, and `upgrade` then reports all three as edited —
        correctly, because they are not what the release the manifest names
        stamped. `vellum init --ref <ref> --force` is the one command that moves
        both, which is why these tests use it rather than writing the manifest.
        """
        code, out = run_cli(["init", str(self.intent), "--ref", ref, "--force"])
        self.assertEqual(code, 0, out)
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
        self.assertRegex(self.out, r"unchanged\s+ledger/releases\.yaml")

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
        # It is pasted into the stubs' `uses:` lines and handed to git; the same
        # refusal `init --ref` makes, for the same reason.
        code, out = self.upgrade(to="v1.0.0 && rm -rf /")
        self.assertEqual(code, 2, out)
        self.assertIn("check-ref-format", out)

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
