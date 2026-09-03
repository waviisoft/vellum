"""``vellum init`` and ``vellum doctor``: the adapters install thin.

``spec/features/installation.md``, three scenarios:
``@id:stubs-have-nothing-to-drift`` (each shipped workflow gets one stub, at a
pinned ref, and a second run writes nothing), ``@id:doctor-fails-a-stub-carrying-
logic``, and ``@id:doctor-reports-a-stale-ref`` (reported with the newest
release named, exit zero).

The two commands' contracts are asserted by *number*, not by "non-zero", for the
reason ``vellum.cli``'s docstring gives: 1 is the answer a caller blocks a merge
on and 2 is "I could not answer", and a test that accepts either lets the two
merge back together.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from support import (
    make_installable_intent,
    make_releases_repo,
    run_cli,
    run_cli_streams,
    write_workspace,
)
from vellum.install import SHIPPED, WORKFLOWS_DIR, default_ref, render

WORKFLOWS = WORKFLOWS_DIR["github"]


class InstallCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def intent(self, **kwargs) -> Path:
        return make_installable_intent(self.root / "intent", **kwargs)

    def stub(self, checkout: Path, name: str) -> Path:
        return checkout / WORKFLOWS / f"{name}.yml"


class InitStampsTheStubs(InstallCase):
    """@id:stubs-have-nothing-to-drift"""

    def test_every_shipped_workflow_gets_one_stub(self):
        checkout = self.intent()
        code, out = run_cli(["init", str(checkout)])
        self.assertEqual(code, 0, out)
        for shipped in SHIPPED:
            self.assertTrue(self.stub(checkout, shipped.name).is_file(), shipped.name)
        # Exactly the shipped set: a stub for something that does not ship is a
        # file with no reusable workflow behind it.
        self.assertEqual(
            sorted(p.name for p in (checkout / WORKFLOWS).iterdir()),
            sorted(s.filename for s in SHIPPED),
        )

    def test_each_stub_delegates_at_a_pinned_ref(self):
        checkout = self.intent()
        run_cli(["init", str(checkout), "--ref", "v9.9.9"])
        for shipped in SHIPPED:
            text = self.stub(checkout, shipped.name).read_text(encoding="utf-8")
            self.assertIn(
                f"uses: waviisoft/vellum/{WORKFLOWS.as_posix()}/{shipped.filename}@v9.9.9",
                text,
            )
            # The CLI's own ref travels with it: the `@ref` pins the workflow
            # file and this pins the CLI the workflow installs.
            self.assertIn("vellum-ref: v9.9.9", text)

    def test_the_secret_is_passed_by_name_and_never_inherited(self):
        """Read out of the parsed stub, not grepped for.

        The stub's own comment says the words "secrets: inherit" while
        explaining why it does not use them, so a text search answers the wrong
        question here — as it would for any check about what a file *does*.
        """
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        for shipped in SHIPPED:
            data = yaml.safe_load(self.stub(checkout, shipped.name).read_text("utf-8"))
            (job,) = data["jobs"].values()
            self.assertEqual(
                job["secrets"], {"VELLUM_TOKEN": "${{ secrets.VELLUM_TOKEN }}"}
            )

    def test_running_again_writes_nothing(self):
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        before = {
            p: p.read_text(encoding="utf-8") for p in (checkout / WORKFLOWS).iterdir()
        }
        code, out = run_cli(["init", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing to do", out)
        self.assertEqual(
            {p: p.read_text(encoding="utf-8") for p in (checkout / WORKFLOWS).iterdir()},
            before,
        )

    def test_a_stub_that_differs_is_left_alone_and_reported(self):
        """Writing is init's job; judging a difference is doctor's.

        Silently restamping an operator's file would make `init` the one command
        here that destroys work it did not write.
        """
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        edited = self.stub(checkout, "spec-ci")
        edited.write_text("name: mine\n", encoding="utf-8")
        code, out = run_cli(["init", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("left alone", out)
        self.assertEqual(edited.read_text(encoding="utf-8"), "name: mine\n")

    def test_force_restamps_which_is_how_a_ref_is_bumped(self):
        checkout = self.intent()
        run_cli(["init", str(checkout), "--ref", "v0.1.0"])
        code, out = run_cli(["init", str(checkout), "--ref", "v0.2.0", "--force"])
        self.assertEqual(code, 0, out)
        for shipped in SHIPPED:
            text = self.stub(checkout, shipped.name).read_text(encoding="utf-8")
            self.assertIn("@v0.2.0", text)
            self.assertNotIn("@v0.1.0", text)

    def test_the_default_ref_is_this_clis_version_and_says_it_is_unconfirmed(self):
        """"pinning ... the CLI's own version by default", and saying so.

        The tag may not exist — this product has cut none — and an intent
        checkout cannot see the product repo's tags. Guessing a ref that does
        exist would be the failure: the stub would resolve, to the wrong thing.
        """
        checkout = self.intent()
        code, out = run_cli(["init", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn(f"at {default_ref()}", out)
        self.assertIn("can confirm that waviisoft/vellum carries the tag", out)

    def test_the_report_names_the_installation_it_stamped(self):
        checkout = self.intent(
            intent="acme/product-intent", products={"web": "acme/web", "api": "acme/api"}
        )
        code, out = run_cli(["init", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("acme/product-intent", out)
        self.assertIn("web (acme/web)", out)
        self.assertIn("api (acme/api)", out)


class InitCannotAnswer(InstallCase):
    def test_no_workspace_file_is_two(self):
        bare = self.root / "bare"
        bare.mkdir()
        code, out = run_cli(["init", str(bare)])
        self.assertEqual(code, 2, out)
        self.assertIn("workspace.yaml", out)

    def test_a_forge_with_no_stubs_is_two(self):
        checkout = self.intent(forge="gitlab")
        code, out = run_cli(["init", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("gitlab", out)
        self.assertFalse((checkout / WORKFLOWS).exists())

    def test_a_missing_checkout_is_two(self):
        code, out = run_cli(["init", str(self.root / "nowhere")])
        self.assertEqual(code, 2, out)

    def test_an_absent_forge_key_means_github(self):
        """Every workspace file written before the installer has no `forge`.

        Defaulting is safe only because a forge that IS named and unadapted is
        refused — so the silent case cannot be the wrong one.
        """
        checkout = self.intent()
        self.assertNotIn("forge:", (checkout / ".vellum" / "workspace.yaml").read_text())
        self.assertEqual(run_cli(["init", str(checkout)])[0], 0)

    def test_a_workspace_declaring_no_products_is_two(self):
        checkout = self.root / "intent"
        checkout.mkdir(parents=True)
        write_workspace(checkout, products={})
        code, out = run_cli(["init", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("products", out)

    def test_a_tree_it_cannot_write_is_two_not_a_traceback(self):
        """This command's whole contract is its exit code."""
        checkout = self.intent()
        (checkout / ".github").mkdir()
        (checkout / WORKFLOWS).write_text("not a directory\n", encoding="utf-8")
        code, out = run_cli(["init", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("cannot write the stub", out)

    def test_a_host_that_would_reshape_the_uses_line_is_refused(self):
        """`--from` lands in the same `uses:` line `--ref` does.

        A value carrying a newline and two spaces of indent does not name a
        fork: it closes the `uses:` line and opens a second job, in a file this
        command stamps with `pull-requests: write`.
        """
        checkout = self.intent()
        hostile = "x@v1\n  evil:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo pwned\n  x2"
        code, out = run_cli(["init", str(checkout), "--from", hostile])
        self.assertEqual(code, 2, out)
        self.assertFalse((checkout / WORKFLOWS).exists())

    def test_a_stub_that_is_not_utf8_text_is_left_alone_not_rewritten(self):
        """`UnicodeDecodeError` is a ValueError, not an OSError.

        Uncaught it left this command exiting 1 with a traceback, and `init` has
        no 1. Caught as "there is no file here" it would have this command
        destroying a file it could not read, which is the one thing it promises
        not to do.
        """
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_bytes(b"\xff\xfe")
        code, out = run_cli(["init", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("left alone", out)
        self.assertEqual(stub.read_bytes(), b"\xff\xfe")

    def test_a_ref_that_would_reshape_the_uses_line_is_refused(self):
        checkout = self.intent()
        code, out = run_cli(["init", str(checkout), "--ref", "v1 @evil/repo"])
        self.assertEqual(code, 2, out)
        self.assertFalse((checkout / WORKFLOWS).exists())


class DoctorOverAFreshCheckout(InstallCase):
    def test_no_stubs_at_all_is_a_finding_per_workflow(self):
        checkout = self.intent()
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        for shipped in SHIPPED:
            self.assertIn(shipped.filename, out)
        self.assertIn("missing", out)

    def test_no_workspace_file_is_two_not_one(self):
        """"cannot answer" is a different colour of red from "a finding"."""
        bare = self.root / "bare"
        bare.mkdir()
        code, out = run_cli(["doctor", str(bare)])
        self.assertEqual(code, 2, out)

    def test_it_is_still_two_when_forge_is_given_on_the_command_line(self):
        """`--forge` makes the forge knowable without reading the workspace.

        Without reading it anyway, `doctor --forge github .` over any directory
        at all reported three missing stubs and exited 1 — "a finding" for what
        is plainly "I could not answer", pointing at a file that is not there.
        """
        bare = self.root / "bare-forge"
        bare.mkdir()
        code, out = run_cli(["doctor", str(bare), "--forge", "github"])
        self.assertEqual(code, 2, out)

    def test_a_non_utf8_stub_is_a_finding_not_a_traceback(self):
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        (checkout / WORKFLOWS / "spec-ci.yml").write_bytes(b"\xff\xfe")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("UTF-8", out)


class DoctorOverAnInstalledCheckout(InstallCase):
    def install(self, ref: str | None = None) -> Path:
        checkout = self.intent()
        argv = ["init", str(checkout)] + (["--ref", ref] if ref else [])
        run_cli(argv)
        return checkout

    def test_a_freshly_stamped_installation_is_green(self):
        code, out = run_cli(["doctor", str(self.install())])
        self.assertEqual(code, 0, out)
        self.assertIn("OK:", out)

    def test_it_says_what_a_checkout_cannot_know(self):
        """"doctor says it cannot check, rather than passing over."

        Asserted on the GREEN run deliberately: a report that lists its blind
        spots only when something else went wrong is one nobody reads at the
        moment they matter.
        """
        code, out = run_cli(["doctor", str(self.install())])
        self.assertEqual(code, 0, out)
        self.assertIn("VELLUM_TOKEN secret is set", out)
        self.assertIn("Accessible from repositories in the organization", out)

    def test_a_missing_stub_is_a_finding_naming_the_file(self):
        checkout = self.install()
        (checkout / WORKFLOWS / "harness-ci.yml").unlink()
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("harness-ci.yml", out)

    def test_a_stub_naming_another_repos_workflow_is_a_finding(self):
        checkout = self.install()
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace("waviisoft/vellum/", "attacker/vellum/"),
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("wrong-workflow", out)

    def test_an_unpinned_uses_is_a_finding(self):
        checkout = self.install()
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace(f"spec-ci.yml@{default_ref()}", "spec-ci.yml"),
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("unpinned", out)

    def test_secrets_inherit_is_a_finding(self):
        checkout = self.install()
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        text = stub.read_text(encoding="utf-8")
        text = text.replace(
            "    secrets:\n      VELLUM_TOKEN: ${{ secrets.VELLUM_TOKEN }}\n",
            "    secrets: inherit\n",
        )
        stub.write_text(text, encoding="utf-8")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("secrets-inherit", out)

    def test_a_stub_passing_no_secret_is_a_finding(self):
        checkout = self.install()
        stub = checkout / WORKFLOWS / "on-spec-merge.yml"
        text = stub.read_text(encoding="utf-8")
        text = text.replace("      VELLUM_TOKEN: ${{ secrets.VELLUM_TOKEN }}\n", "")
        text = text.replace("    secrets:\n", "")
        stub.write_text(text, encoding="utf-8")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("no-secret", out)

    def test_the_two_refs_coming_apart_is_a_finding(self):
        checkout = self.install(ref="v0.1.0")
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace("vellum-ref: v0.1.0", "vellum-ref: main"),
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("ref-mismatch", out)

    def test_an_unparseable_stub_is_a_finding_not_a_crash(self):
        checkout = self.install()
        (checkout / WORKFLOWS / "spec-ci.yml").write_text("jobs: [\n", encoding="utf-8")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("unparseable", out)


class DoctorChecksTheCallerHalf(InstallCase):
    """A stub carries three blocks besides its `uses:` job, and each fails silently.

    A narrowed trigger is a required check that never reports and a PR that
    waits forever; a narrowed `permissions` grant is a job refused at the point
    of use; a renamed `concurrency` group stops serialising what it exists to
    serialise. None of the three reddens on its own, which is the exact class of
    drift this whole wave is about — so "installed matches shipped" has to cover
    them or the sentence is wider than the check.
    """

    def install(self) -> Path:
        checkout = self.intent()
        run_cli(["init", str(checkout), "--ref", "v0.1.0"])
        return checkout

    def edit(self, checkout: Path, name: str, old: str, new: str) -> None:
        stub = checkout / WORKFLOWS / f"{name}.yml"
        text = stub.read_text(encoding="utf-8")
        assert text.count(old) == 1, (name, old)
        stub.write_text(text.replace(old, new), encoding="utf-8")

    def test_a_narrowed_trigger_branch_is_a_finding(self):
        checkout = self.install()
        self.edit(checkout, "on-spec-merge", "branches: [main]", "branches: [nope]")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("drifted", out)
        self.assertIn("`on:`", out)

    def test_a_paths_filter_added_to_harness_ci_is_a_finding(self):
        """The landmine harness-ci's own header documents."""
        checkout = self.install()
        self.edit(checkout, "harness-ci", "on:\n  pull_request:",
                  "on:\n  pull_request:\n    paths: ['harness/**']")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("drifted", out)

    def test_a_narrowed_permission_grant_is_a_finding(self):
        checkout = self.install()
        self.edit(checkout, "on-spec-merge",
                  "permissions:\n  contents: write\n  issues: write",
                  "permissions:\n  contents: read")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("`permissions:`", out)

    def test_a_renamed_concurrency_group_is_a_finding(self):
        checkout = self.install()
        self.edit(checkout, "on-spec-merge", "group: on-spec-merge", "group: mine")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("`concurrency:`", out)

    def test_an_added_comment_is_not_drift(self):
        """Compared parsed, not as text.

        A stub an operator has annotated has not drifted, and a check that said
        otherwise is one people learn to ignore.
        """
        checkout = self.install()
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            "# a note from the operator\n" + stub.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 0, out)

    def test_a_stub_pinned_to_an_old_ref_is_not_drift(self):
        """Currency is reported; drift is failed. They must not bleed together.

        The caller half carries neither the ref nor the host, so an installation
        two releases behind compares equal on all three blocks.
        """
        checkout = self.intent()
        run_cli(["init", str(checkout), "--ref", "v0.0.1"])
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 0, out)


class DoctorFailsAStubCarryingLogic(InstallCase):
    """@id:doctor-fails-a-stub-carrying-logic"""

    def install(self) -> Path:
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        return checkout

    def test_a_run_body_in_a_stub_is_a_finding_naming_that_stub(self):
        checkout = self.install()
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8")
            + "    # smuggled in beside the delegation\n",
            encoding="utf-8",
        )
        # A `run:` inside the delegating job itself, which is the shape a
        # "just one extra check" edit actually takes.
        text = stub.read_text(encoding="utf-8").replace(
            "  spec-ci:\n    uses:",
            "  extra:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: echo 'a local tweak'\n  spec-ci:\n    uses:",
        )
        stub.write_text(text, encoding="utf-8")
        code, out, err = run_cli_streams(["doctor", str(checkout)])
        self.assertEqual(code, 1, out + err)
        self.assertIn("carries-logic", out)
        self.assertIn("spec-ci.yml", out)
        # Named by file: the other two stubs are untouched and reported ok.
        # Asserted on the marked lines rather than by the absence of a string,
        # which any report format change would satisfy vacuously.
        marks = {
            line.split()[1]: line.split()[0]
            for line in out.splitlines()
            if line.startswith(("  ok ", "  FINDING "))
        }
        self.assertEqual(marks[".github/workflows/spec-ci.yml"], "FINDING")
        self.assertEqual(marks[".github/workflows/harness-ci.yml"], "ok")
        self.assertEqual(marks[".github/workflows/on-spec-merge.yml"], "ok")
        self.assertIn("doctor", err)

    def test_a_second_job_is_a_finding_even_with_no_run(self):
        checkout = self.install()
        stub = checkout / WORKFLOWS / "harness-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8")
            + "  also:\n    uses: ./.github/workflows/something.yml\n",
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("carries-logic", out)
        self.assertIn("harness-ci.yml", out)


class DoctorReportsAStaleRef(InstallCase):
    """@id:doctor-reports-a-stale-ref — reported with the newest named, exit 0."""

    def setUp(self):
        super().setUp()
        self.releases = make_releases_repo(self.root / "vellum", ("v0.1.0", "v0.2.0"))

    def install(self, ref: str) -> Path:
        checkout = self.intent()
        run_cli(["init", str(checkout), "--ref", ref])
        return checkout

    def test_a_ref_behind_the_newest_release_is_reported_and_exits_zero(self):
        checkout = self.install("v0.1.0")
        code, out = run_cli(
            ["doctor", str(checkout), "--releases-from", str(self.releases)]
        )
        self.assertEqual(code, 0, out)
        self.assertIn("behind by 1 release", out)
        self.assertIn("v0.2.0", out)
        self.assertIn("never failed on", out)

    def test_the_newest_release_is_reported_current(self):
        checkout = self.install("v0.2.0")
        code, out = run_cli(
            ["doctor", str(checkout), "--releases-from", str(self.releases)]
        )
        self.assertEqual(code, 0, out)
        self.assertIn("current", out)

    def test_releases_order_as_versions_not_lexically(self):
        """`v0.10.0` is newer than `v0.9.0`, and `sort` disagrees."""
        releases = make_releases_repo(self.root / "many", ("v0.9.0", "v0.10.0"))
        checkout = self.install("v0.9.0")
        code, out = run_cli(["doctor", str(checkout), "--releases-from", str(releases)])
        self.assertEqual(code, 0, out)
        self.assertIn("newest is v0.10.0", out)

    def test_a_ref_that_is_not_a_release_tag_is_reported_not_judged(self):
        checkout = self.install("main")
        code, out = run_cli(
            ["doctor", str(checkout), "--releases-from", str(self.releases)]
        )
        self.assertEqual(code, 0, out)
        self.assertIn("not a release tag", out)

    def test_a_stale_ref_alongside_a_finding_still_exits_one_for_the_finding(self):
        """Currency never contributes to the verdict — in either direction."""
        checkout = self.install("v0.1.0")
        (checkout / WORKFLOWS / "spec-ci.yml").unlink()
        code, out = run_cli(
            ["doctor", str(checkout), "--releases-from", str(self.releases)]
        )
        self.assertEqual(code, 1, out)
        self.assertIn("behind by 1 release", out)

    def test_without_a_releases_checkout_it_says_currency_was_not_checked(self):
        checkout = self.install("v0.1.0")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("not checked", out)

    def test_a_releases_path_that_is_not_a_checkout_reports_and_does_not_fail(self):
        """A report that can fail the check is not a report."""
        checkout = self.install("v0.1.0")
        code, out = run_cli(
            ["doctor", str(checkout), "--releases-from", str(self.root / "nowhere")]
        )
        self.assertEqual(code, 0, out)
        self.assertIn("not checked", out)

    def test_a_product_with_no_release_tags_says_so(self):
        releases = make_releases_repo(self.root / "untagged", ())
        checkout = self.install("main")
        code, out = run_cli(["doctor", str(checkout), "--releases-from", str(releases)])
        self.assertEqual(code, 0, out)
        self.assertIn("no v* release tags", out)


class TheCommittedTemplatesAreWhatInitWrites(unittest.TestCase):
    """`adapters/github/` is a rendering, and a rendering that drifts is a copy.

    The whole wave is about a copied file going stale beside the thing it copies.
    An installed CLI is a wheel and cannot read `adapters/`, so `init` renders
    from the table in `vellum.install` — which makes the committed files a second
    artifact, and this the check that keeps them one.
    """

    def test_each_committed_stub_is_byte_identical_to_the_render(self):
        adapters = Path(__file__).resolve().parents[1] / "adapters" / "github"
        for shipped in SHIPPED:
            committed = (adapters / shipped.filename).read_text(encoding="utf-8")
            self.assertEqual(
                committed,
                render(shipped, ref=default_ref()),
                f"{shipped.filename} has drifted from what `vellum init` writes; "
                f"re-render it (see tests/test_install.py).",
            )

    def test_adapters_holds_no_workflow_it_does_not_ship(self):
        adapters = Path(__file__).resolve().parents[1] / "adapters" / "github"
        self.assertEqual(
            sorted(p.name for p in adapters.glob("*.yml")),
            sorted(s.filename for s in SHIPPED),
        )


if __name__ == "__main__":
    unittest.main()
