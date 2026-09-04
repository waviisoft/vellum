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
            # file and this pins the CLI the workflow installs. QUOTED — see
            # `TheRefSurvivesBeingReadBack`.
            self.assertIn('vellum-ref: "v9.9.9"', text)

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

    def test_a_stub_passing_no_secret_is_not_a_finding(self):
        """It was `no-secret` while the shipped workflows required the token.

        They declare it `required: false` now and check the CLI out with the
        caller's own `github.token` when nothing is passed, so a stub that omits
        the key describes an installation that needs no token — not one whose
        every run dies in its first step. Reporting it would be doctor calling a
        correct configuration drift, which is the failure mode `on.push.branches`
        already taught this command.
        """
        checkout = self.install()
        stub = checkout / WORKFLOWS / "on-spec-merge.yml"
        text = stub.read_text(encoding="utf-8")
        text = text.replace("      VELLUM_TOKEN: ${{ secrets.VELLUM_TOKEN }}\n", "")
        text = text.replace("    secrets:\n", "")
        stub.write_text(text, encoding="utf-8")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertNotIn("no-secret", out)

    def test_an_empty_secrets_block_is_not_a_finding_either(self):
        """`secrets:` with nothing under it reads back as None, not as a dict.

        The same installation as the test above, written the other way an
        operator writes it. The remap loop runs over a mapping; a key that is
        there and empty must not fall through it into some other finding.
        """
        checkout = self.install()
        stub = checkout / WORKFLOWS / "harness-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace(
                "      VELLUM_TOKEN: ${{ secrets.VELLUM_TOKEN }}\n", ""
            ),
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 0, out)

    def test_a_malformed_secrets_value_is_a_finding(self):
        """`secrets:` that is neither `inherit` nor a mapping is reported.

        `secrets: [VELLUM_TOKEN]` is how a stub looks when someone remembers
        the name and forgets the shape. The forge refuses it at parse time, so
        nothing leaks — but doctor's job is installed-matches-shipped, and a
        stub the forge will not run is not that. Before this branch existed the
        value fell through both arms and doctor said `ok`.
        """
        checkout = self.install()
        stub = checkout / WORKFLOWS / "harness-ci.yml"
        text = stub.read_text(encoding="utf-8")
        text = text.replace("      VELLUM_TOKEN: ${{ secrets.VELLUM_TOKEN }}\n", "")
        text = text.replace("    secrets:\n", "    secrets: [VELLUM_TOKEN]\n")
        stub.write_text(text, encoding="utf-8")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("secrets-malformed", out)
        self.assertIn("harness-ci.yml", out)

    def test_a_stub_that_passes_no_secret_still_has_its_other_halves_checked(self):
        """Dropping the secret must not drop the rest of the audit with it.

        The `no-secret` branch is gone; the `uses:`, the ref and the caller half
        are not, and a stub with no secret and a broken delegation is still a
        finding.
        """
        checkout = self.install()
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        text = stub.read_text(encoding="utf-8")
        text = text.replace("      VELLUM_TOKEN: ${{ secrets.VELLUM_TOKEN }}\n", "")
        text = text.replace("    secrets:\n", "")
        text = text.replace('vellum-ref: "', 'vellum-ref: "not-the-')
        stub.write_text(text, encoding="utf-8")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("ref-mismatch", out)

    def test_the_two_refs_coming_apart_is_a_finding(self):
        checkout = self.install(ref="v0.1.0")
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace(
                'vellum-ref: "v0.1.0"', 'vellum-ref: "main"'
            ),
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


class TheBranchIsInstallationDataNotLogic(InstallCase):
    """`on-spec-merge` watches the repository's default branch, whatever it is.

    Hard-coding `branches: [main]` in the compare made an installation whose
    default branch is `trunk` one that could never be doctor-green — the check
    reporting the installation's own correct configuration as drift, which is
    the failure mode that teaches people to ignore a check. So the branch list
    is `init --branch` data and doctor's `on:` compare exempts it.

    Exempts *it*, and nothing around it: the tests below pin that `push` must
    still be there, that everything else under it is still compared, and that a
    trigger added beside it is still drift.
    """

    def test_a_stub_on_another_default_branch_is_not_drift(self):
        checkout = self.intent()
        code, out = run_cli(["init", str(checkout), "--branch", "trunk"])
        self.assertEqual(code, 0, out)
        stub = (checkout / WORKFLOWS / "on-spec-merge.yml").read_text(encoding="utf-8")
        self.assertIn('branches: ["trunk"]', stub)
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 0, out)

    def test_an_added_trigger_is_still_drift(self):
        """The exemption is the branch list, not the `on:` block."""
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        stub = checkout / WORKFLOWS / "harness-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace(
                "on:\n  pull_request:", "on:\n  pull_request:\n  workflow_dispatch:"
            ),
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("drifted", out)
        self.assertIn("`on:`", out)

    def test_a_paths_filter_beside_the_branch_list_is_still_drift(self):
        """Everything else under `push:` is compared as it always was."""
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        stub = checkout / WORKFLOWS / "on-spec-merge.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace("      - 'spec/**'", "      - 'nope/**'"),
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("drifted", out)

    def test_a_stub_that_no_longer_runs_on_a_push_is_drift(self):
        """`push` must be present: on-spec-merge is the push half of the pipeline."""
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        stub = checkout / WORKFLOWS / "on-spec-merge.yml"
        text = stub.read_text(encoding="utf-8")
        text = text.replace(
            "on:\n  push:\n    branches: [\"main\"]\n    paths:\n      - 'spec/**'\n",
            "on:\n",
        )
        stub.write_text(text, encoding="utf-8")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("`on:`", out)

    def test_a_branch_that_would_reshape_the_trigger_is_refused(self):
        """`--branch` lands in a YAML flow sequence, so it is held to REF_RE too."""
        checkout = self.intent()
        code, out = run_cli(["init", str(checkout), "--branch", 'main"], evil: [x'])
        self.assertEqual(code, 2, out)
        self.assertFalse((checkout / WORKFLOWS).exists())


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


class TheDelegatingJobCarriesNothingOfItsOwn(InstallCase):
    """Item by item, every key that can be added beside `uses:`.

    Reading the `uses:` and stopping there checked the delegation and nothing
    about the job carrying it, so each row below exited 0 "ok" while changing
    what the installation actually does — and several of them while *reporting
    success*. `if: false` is the sharpest: a skipped job reports success to
    branch protection, so the write-boundary gate goes green having run nothing,
    which is worse than a gate that is missing.

    An allowlist rather than a row of detectors, because the list of ways to be
    wrong here is open-ended; these tests are the rows that were named, not the
    rows the check knows about.
    """

    def install(self) -> Path:
        checkout = self.intent()
        run_cli(["init", str(checkout), "--ref", "v0.1.0"])
        return checkout

    def add_key(self, checkout: Path, block: str, name: str = "spec-ci") -> None:
        """Put *block* on the delegating job, above its `uses:` line."""
        stub = checkout / WORKFLOWS / f"{name}.yml"
        text = stub.read_text(encoding="utf-8")
        marker = "    uses: waviisoft/vellum/"
        assert text.count(marker) == 1, name
        stub.write_text(text.replace(marker, f"{block}\n{marker}"), encoding="utf-8")

    def assert_carries_logic(self, checkout: Path, key: str) -> None:
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("carries-logic", out)
        self.assertIn(key, out)

    def test_if_false_is_a_finding(self):
        """A SKIPPED job reports SUCCESS to branch protection.

        So `if: false` on `harness-ci`'s delegation is a write-boundary gate
        that is green on every PR and has run nothing — the one row here that
        fails by passing.
        """
        checkout = self.install()
        self.add_key(checkout, "    if: false", name="harness-ci")
        self.assert_carries_logic(checkout, "if")

    def test_a_matrix_is_a_finding(self):
        """N runs of the reusable workflow inside one run of the caller.

        For `on-spec-merge` that is N minters racing the same ledger push.
        """
        checkout = self.install()
        self.add_key(
            checkout, "    strategy:\n      matrix:\n        n: [1, 2, 3]",
            name="on-spec-merge",
        )
        self.assert_carries_logic(checkout, "strategy")

    def test_needs_is_a_finding(self):
        checkout = self.install()
        self.add_key(checkout, "    needs: [something]")
        self.assert_carries_logic(checkout, "needs")

    def test_a_job_level_permissions_grant_is_a_finding(self):
        """Narrower than the shipped grant, and refused at the point of use.

        The top-level block compares equal, so nothing else here would see it.
        """
        checkout = self.install()
        self.add_key(checkout, "    permissions:\n      contents: read")
        self.assert_carries_logic(checkout, "permissions")

    def test_a_timeout_is_a_finding(self):
        checkout = self.install()
        self.add_key(checkout, "    timeout-minutes: 1")
        self.assert_carries_logic(checkout, "timeout-minutes")

    def test_continue_on_error_is_a_finding(self):
        """A required check that reports success whatever the callee decided."""
        checkout = self.install()
        self.add_key(checkout, "    continue-on-error: true")
        self.assert_carries_logic(checkout, "continue-on-error")

    def test_env_is_a_finding(self):
        checkout = self.install()
        self.add_key(checkout, "    env:\n      VELLUM_REF: somewhere-else")
        self.assert_carries_logic(checkout, "env")

    def test_a_container_is_a_finding(self):
        checkout = self.install()
        self.add_key(checkout, "    container: alpine:3")
        self.assert_carries_logic(checkout, "container")

    def test_a_renamed_delegating_job_is_a_finding(self):
        """The forge derives the CHECK NAME from the job id.

        A job that calls a reusable workflow reports as `<job id> / <called job
        name>`, so renaming it leaves branch protection requiring names that no
        longer report and every PR waiting forever — and nothing in a checkout
        can see branch protection to say so twice.
        """
        checkout = self.install()
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace(
                "jobs:\n  spec-ci:\n", "jobs:\n  vellum-spec-ci:\n"
            ),
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("renamed-job", out)
        self.assertIn("vellum-spec-ci", out)

    def test_a_freshly_stamped_job_carries_only_the_three(self):
        """The allowlist is what `render` writes, checked from the other side."""
        checkout = self.install()
        for shipped in SHIPPED:
            data = yaml.safe_load(self.stub(checkout, shipped.name).read_text("utf-8"))
            (job,) = data["jobs"].values()
            self.assertEqual(sorted(job), sorted(["uses", "with", "secrets"]))


class TheSecretIsPassedByNameAndByValue(InstallCase):
    """`VELLUM_TOKEN: ${{ secrets.ORG_ADMIN_PAT }}` passes a by-key check.

    And hands the reusable workflow a different credential under the name it
    audits — very possibly a wider one. "Passes each secret by name" is a claim
    about the value as much as the key, so doctor reads the referenced secret
    back out of the expression and compares it to the key it is passed as.

    This survives the secret becoming optional, and the distinction is the whole
    point of that change being a narrowing rather than a deletion: a stub is
    free to pass NO token, and not free to pass the wrong one under this name.
    """

    def install(self) -> Path:
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        return checkout

    def remap(self, checkout: Path, value: str) -> tuple[int, str]:
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace(
                "VELLUM_TOKEN: ${{ secrets.VELLUM_TOKEN }}", f"VELLUM_TOKEN: {value}"
            ),
            encoding="utf-8",
        )
        return run_cli(["doctor", str(checkout)])

    def test_a_remapped_secret_is_a_finding(self):
        code, out = self.remap(self.install(), "${{ secrets.ORG_ADMIN_PAT }}")
        self.assertEqual(code, 1, out)
        self.assertIn("secret-remapped", out)
        self.assertIn("ORG_ADMIN_PAT", out)

    def test_a_literal_value_is_a_finding(self):
        code, out = self.remap(self.install(), "'hunter2'")
        self.assertEqual(code, 1, out)
        self.assertIn("secret-remapped", out)

    def test_spacing_an_operator_changed_is_not_a_finding(self):
        """Read back, not text-compared — the same posture as the caller half."""
        code, out = self.remap(self.install(), "${{   secrets.VELLUM_TOKEN   }}")
        self.assertEqual(code, 0, out)


class TheRefSurvivesBeingReadBack(InstallCase):
    """A ref is a string, and an unquoted scalar is whatever YAML says it is.

    `--ref 1.10` stamped `vellum-ref: 1.10`, which reads back as the float 1.1;
    `010` as the int 10 in YAML 1.1; `null`, `true` and `on` as None and
    booleans. So a freshly stamped installation failed its OWN doctor with
    `ref-mismatch` or `no-cli-ref` — the check calling the thing it had just
    written wrong. The `@ref` half was never affected: it is part of a longer
    scalar. Quoting the input makes the two halves the same kind of thing.
    """

    def round_trip(self, ref: str) -> tuple[int, str]:
        checkout = self.intent()
        code, out = run_cli(["init", str(checkout), "--ref", ref])
        self.assertEqual(code, 0, out)
        return run_cli(["doctor", str(checkout)])

    def test_a_dotted_ref_that_looks_like_a_float_round_trips_green(self):
        code, out = self.round_trip("1.10")
        self.assertEqual(code, 0, out)

    def test_a_zero_padded_ref_that_looks_like_an_int_round_trips_green(self):
        code, out = self.round_trip("010")
        self.assertEqual(code, 0, out)

    def test_the_stamped_input_is_quoted_and_reads_back_as_a_string(self):
        checkout = self.intent()
        run_cli(["init", str(checkout), "--ref", "1.10"])
        for shipped in SHIPPED:
            data = yaml.safe_load(self.stub(checkout, shipped.name).read_text("utf-8"))
            (job,) = data["jobs"].values()
            self.assertEqual(job["with"]["vellum-ref"], "1.10")

    def test_an_unquoted_ref_left_by_hand_is_a_finding_that_says_why(self):
        """The shape an installation stamped by an older CLI still has."""
        checkout = self.intent()
        run_cli(["init", str(checkout), "--ref", "1.10"])
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace(
                'vellum-ref: "1.10"', "vellum-ref: 1.10"
            ),
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("ref-mismatch", out)
        self.assertIn("not a string", out)

    def test_refs_git_itself_would_refuse_are_refused(self):
        """`git check-ref-format`: no `..`, no `//`, no leading `.`, no `.lock`.

        A ref git will not accept is one the forge resolves to nothing, which
        fails at `uses:` on every run rather than here, once.
        """
        for i, bad in enumerate(("v1..2", "heads//main", ".hidden", "main.lock",
                                 "refs/x.lock/y")):
            with self.subTest(ref=bad):
                checkout = make_installable_intent(self.root / f"intent-{i}")
                code, out = run_cli(["init", str(checkout), "--ref", bad])
                self.assertEqual(code, 2, out)
                self.assertFalse((checkout / WORKFLOWS).exists())


class DoctorLooksForLogicInJobsOnly(InstallCase):
    """`defaults: {run: {shell: bash}}` is a declaration, not a body.

    Walking the whole document for a `run:` key made a false `carries-logic`
    out of a legal top-level block — and a check that fires on correct files is
    one people learn to work around. Everything outside `jobs:` a stub may carry
    is enumerated by the caller half and the job-key allowlist instead.
    """

    def test_a_top_level_defaults_run_is_not_a_finding(self):
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8").replace(
                "jobs:\n", "defaults:\n  run:\n    shell: bash\n\njobs:\n"
            ),
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 0, out)

    def test_a_run_nested_in_a_job_is_still_a_finding(self):
        """Narrowing the walk must not narrow what it finds inside a job."""
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        stub = checkout / WORKFLOWS / "spec-ci.yml"
        stub.write_text(
            stub.read_text(encoding="utf-8")
            + "  extra:\n    runs-on: blacksmith-2vcpu-ubuntu-2204\n    steps:\n"
              "      - run: echo 'a local tweak'\n",
            encoding="utf-8",
        )
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("carries-logic", out)


class DoctorFindsAStrayWorkflow(InstallCase):
    """The retired full copy is where this wave's own history comes back.

    Before the stubs each adapter was a full copy in `.github/workflows/`.
    Renaming one aside — `spec-ci-legacy.yml` — leaves a file that still runs on
    every PR, still holds logic nothing keeps equal to what ships, and is
    invisible to a check that only ever opens the three files it stamped.
    """

    def install(self) -> Path:
        checkout = self.intent()
        run_cli(["init", str(checkout)])
        return checkout

    def write(self, checkout: Path, name: str, text: str) -> None:
        (checkout / WORKFLOWS / name).write_text(text, encoding="utf-8")

    def test_a_retired_copy_running_vellum_is_a_finding_naming_the_file(self):
        checkout = self.install()
        self.write(checkout, "spec-ci-legacy.yml", (
            "name: spec-ci-legacy\non:\n  pull_request:\njobs:\n"
            "  lint:\n    runs-on: blacksmith-2vcpu-ubuntu-2204\n    steps:\n"
            "      - run: vellum lint .\n"
        ))
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("stray-workflow", out)
        self.assertIn("spec-ci-legacy.yml", out)

    def test_a_second_unmanaged_caller_is_a_finding(self):
        checkout = self.install()
        self.write(checkout, "spec-ci-also.yml", (
            "name: also\non:\n  pull_request:\njobs:\n"
            "  also:\n    uses: waviisoft/vellum/.github/workflows/spec-ci.yml@main\n"
        ))
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("stray-workflow", out)
        self.assertIn("spec-ci-also.yml", out)

    def test_an_unrelated_workflow_is_not_this_commands_business(self):
        """An intent repo's own CI is not a stray, and saying so would be noise."""
        checkout = self.install()
        self.write(checkout, "docs.yml", (
            "name: docs\non:\n  pull_request:\njobs:\n"
            "  build:\n    runs-on: blacksmith-2vcpu-ubuntu-2204\n    steps:\n"
            "      - run: make docs\n"
        ))
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 0, out)

    def test_a_file_the_forge_could_not_parse_is_passed_over(self):
        """A workflow that does not parse does not run, which is the point."""
        checkout = self.install()
        self.write(checkout, "broken.yml", "jobs: [\n")
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 0, out)


class DoctorReadsTheWorkspaceLikeInitDoes(InstallCase):
    def test_a_workspace_with_no_intent_key_is_two_from_both(self):
        """One accessor, so the two commands refuse the same files.

        Reading less than `init` did left doctor reporting a workspace it could
        not name the installation from as three missing stubs and exit 1 — "a
        finding" for what is plainly "I could not answer".
        """
        checkout = self.root / "no-intent"
        checkout.mkdir(parents=True)
        write_workspace(checkout, intent=None)
        self.assertEqual(run_cli(["init", str(checkout)])[0], 2)
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("intent", out)

    def test_the_report_names_the_installation_it_judged(self):
        checkout = self.intent(intent="acme/product-intent")
        run_cli(["init", str(checkout)])
        code, out = run_cli(["doctor", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("acme/product-intent", out)


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
