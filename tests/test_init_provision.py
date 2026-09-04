"""``vellum init --shape …``: provisioning a repo pair (installation, part 2).

``spec/features/installation.md``, five scenarios:
``@id:init-plan-creates-nothing``, ``@id:greenfield-seed-is-green``,
``@id:no-transport-prints-the-checklist``,
``@id:brownfield-stages-docs-for-the-surveyor`` and
``@id:init-refuses-an-existing-installation``.

Everything here drives the **real command** — ``vellum.cli.main(["init", …])``
— against local directories (``--into``), which is the rung the acceptance suite
drives too, so what these assert is what an operator gets. Nothing is faked
below the command except the forge itself.

The forge is faked with a **fake `gh` executable on PATH** that records its
argv, and PATH is set explicitly in every test that cares whether a transport
was found — both to make "no gh" a fact of the test rather than of the machine
it runs on, and because a real `gh` on a developer's PATH would otherwise make
the no-transport tests pass for the wrong reason and the trace test try to
create repositories.

Exit codes are asserted by *number*, for the reason ``vellum.cli``'s docstring
gives: 1 is a finding a caller blocks on, 2 is "I could not answer", and a test
that accepts either lets the two merge back together. Provisioning adds one
number to ``init``'s vocabulary — 1, for a seed that does not lint or doctor
green — and it is still doctor's sentence to pass.
"""

from __future__ import annotations

import io
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

from support import run_cli, run_cli_streams, write_workspace
from vellum import provision
from vellum.install import SHIPPED, WORKFLOWS_DIR

WORKFLOWS = WORKFLOWS_DIR["github"]

#: A fake ``gh``: it records every invocation as one JSON line and answers the
#: two questions the real one is asked. ``repo view`` exits 1 (the repository
#: does not exist), which is the answer greenfield needs; everything else
#: succeeds. Stdin is read ONLY for `secret set`, because a fake that read it
#: unconditionally would block on an inherited terminal.
FAKE_GH = """#!{python}
import json, os, sys

argv = sys.argv[1:]
entry = {{"argv": argv}}
if argv[:2] == ["secret", "set"]:
    entry["stdin"] = sys.stdin.read()
with open(os.environ["GH_TRACE"], "a", encoding="utf-8") as trace:
    trace.write(json.dumps(entry) + "\\n")
sys.exit(1 if argv[:2] == ["repo", "view"] else 0)
"""


class ProvisionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # Every provisioning run reads the *checkout* argument to refuse an
        # existing installation, and defaults it to `.`. Running from a
        # directory this test owns keeps a developer's own checkout out of it.
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        self.enterContext = getattr(self, "enterContext", None)
        previous = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)
        self.trace = self.root / "gh-trace.jsonl"

    # ---------------------------------------------------------------- PATH

    def _bin(self, name: str, *, with_gh: bool) -> str:
        """A PATH directory holding `git`, and `gh` only when asked for.

        "No authenticated forge CLI" is made true here rather than assumed: the
        directory holds exactly what the test means it to hold, so the answer
        does not depend on what is installed on the machine.
        """
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        git = shutil.which("git")
        self.assertIsNotNone(git, "these tests need git")
        target = directory / "git"
        if not target.exists():
            target.symlink_to(git)
        if with_gh:
            fake = directory / "gh"
            fake.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
            fake.chmod(0o755)
        return str(directory)

    def without_gh(self) -> None:
        """No `gh` anywhere on PATH, as a fact of this test.

        The restore is registered from the value read BEFORE the assignment; a
        cleanup that read it afterwards would put the stripped PATH back and
        every later test in the process would find no git either.
        """
        self.addCleanup(os.environ.__setitem__, "PATH", os.environ["PATH"])
        os.environ["PATH"] = self._bin("bin-plain", with_gh=False)

    def with_fake_gh(self, **secrets: str) -> None:
        self.addCleanup(os.environ.__setitem__, "PATH", os.environ["PATH"])
        self.addCleanup(self._restore, ["GH_TRACE", *secrets])
        os.environ["PATH"] = self._bin("bin-gh", with_gh=True)
        os.environ["GH_TRACE"] = str(self.trace)
        for name, value in secrets.items():
            os.environ[name] = value

    def _restore(self, names) -> None:
        for name in names:
            os.environ.pop(name, None)

    def recorded(self) -> list[dict]:
        if not self.trace.is_file():
            return []
        return [json.loads(line) for line in
                self.trace.read_text(encoding="utf-8").splitlines() if line.strip()]

    # ------------------------------------------------------------- fixtures

    def greenfield(self, into: Path, *extra: str) -> list[str]:
        return [
            "init", "--shape", "greenfield", "--product", "acme",
            "--org", "waviisoft", "--area", "billing",
            "--into", str(into), "--yes", *extra,
        ]

    def git(self, repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        ).stdout.strip()


# =====================================================================


class ThePlanIsCompleteAndCreatesNothing(ProvisionCase):
    """@id:init-plan-creates-nothing"""

    def setUp(self):
        super().setUp()
        self.without_gh()
        self.into = self.root / "out"

    def plan(self) -> str:
        code, out, err = run_cli_streams(self.greenfield(self.into, "--plan"))
        self.assertEqual(code, 0, out + err)
        return out

    def test_it_names_both_repositories_and_their_visibility(self):
        out = self.plan()
        self.assertIn("waviisoft/acme-intent", out)
        self.assertIn("waviisoft/acme", out)
        self.assertRegex(out, r"intent\s+waviisoft/acme-intent\s+private")
        self.assertRegex(out, r"product\s+waviisoft/acme\s+private")

    def test_it_names_the_secret_pair_and_which_repo_each_is_set_on(self):
        out = self.plan()
        self.assertRegex(out, r"VELLUM_TOKEN\s+on waviisoft/acme-intent\s+reads waviisoft/acme")
        self.assertRegex(out, r"SPEC_TOKEN\s+on waviisoft/acme\s+reads waviisoft/acme-intent")

    def test_it_names_every_stub(self):
        out = self.plan()
        for shipped in SHIPPED:
            self.assertIn((WORKFLOWS / shipped.filename).as_posix(), out)

    def test_it_names_every_file_it_would_seed(self):
        out = self.plan()
        # The plan's list and the seed's files are one dict, so this asserts
        # they agree rather than that two lists were kept in step by hand.
        answers = provision.resolve(_flags(), provision.Console(tty=False))
        for path in provision.intent_seed(answers):
            self.assertIn(path, out, path)
        for path in provision.product_seed(answers, provision.PIN_PLACEHOLDER):
            self.assertIn(path, out, path)

    def test_it_names_the_actions_access_change_on_the_product_repo(self):
        self.assertIn("access_level=organization", self.plan())

    def test_it_names_the_steps_no_transport_takes(self):
        out = self.plan()
        self.assertIn("no transport takes this one; it is yours", out)
        # Branch protection is out of scope for the installer and named by the
        # plan for that reason (spec/features/installation.md, Out of scope).
        self.assertIn("branch protection", out.lower())

    def test_no_repository_file_or_secret_is_created(self):
        self.plan()
        self.assertFalse(self.into.exists(), f"{self.into} was created by --plan")
        self.assertEqual(self.recorded(), [])
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])

    def test_two_runs_of_one_command_line_print_the_same_plan(self):
        self.assertEqual(self.plan(), self.plan())


# =====================================================================


class AGreenfieldSeedIsGreen(ProvisionCase):
    """@id:greenfield-seed-is-green"""

    def setUp(self):
        super().setUp()
        self.without_gh()
        self.into = self.root / "out"
        code, self.out, err = run_cli_streams(self.greenfield(self.into))
        self.assertEqual(code, 0, self.out + err)
        self.intent = self.into / "acme-intent"
        self.product = self.into / "acme"

    def test_the_seeded_tree_passes_vellum_lint(self):
        code, out = run_cli(["lint", str(self.intent)])
        self.assertEqual(code, 0, out)

    def test_the_seeded_checkout_passes_vellum_doctor(self):
        code, out = run_cli(["doctor", str(self.intent)])
        self.assertEqual(code, 0, out)

    def test_init_itself_reports_both_checks_green(self):
        self.assertIn("vellum lint    OK", self.out)
        self.assertIn("vellum doctor  OK", self.out)

    def test_the_pin_names_the_seeds_first_spec_commit(self):
        pinned = yaml.safe_load(
            (self.product / ".vellum" / "product.yaml").read_text(encoding="utf-8")
        )["pin"]["commit"]
        first = self.git(self.intent, "log", "--reverse", "--format=%H", "--", "spec")
        self.assertEqual(pinned, first.splitlines()[0])
        # And it is the FIRST, not the head: the stubs land in a second commit
        # and must not date the spec.
        self.assertNotEqual(pinned, self.git(self.intent, "rev-parse", "HEAD"))

    def test_the_seed_is_the_tree_spec_ci_needs_to_run_once(self):
        for relative in ("spec/product.md", "spec/index.md", "spec/features/billing.md",
                         ".vellum/config.yaml", ".vellum/workspace.yaml",
                         "ledger/releases.yaml", "harness/run.py",
                         "harness/support/adapter.py", "harness/steps/__init__.py"):
            self.assertTrue((self.intent / relative).is_file(), relative)

    def test_the_ledger_has_conformed_nothing(self):
        releases = yaml.safe_load(
            (self.intent / "ledger" / "releases.yaml").read_text(encoding="utf-8")
        )
        self.assertIsNone(releases["channels"]["production"]["spec_conformed"])
        self.assertEqual(releases["cuts"], [])

    def test_the_workspace_maps_the_product_at_the_installations_slugs(self):
        workspace = yaml.safe_load(
            (self.intent / ".vellum" / "workspace.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(workspace["intent"], "waviisoft/acme-intent")
        self.assertEqual(workspace["forge"], "github")
        self.assertEqual(workspace["products"]["acme"]["repo"], "waviisoft/acme")

    def test_the_seeded_config_carries_every_key_the_cli_reads(self):
        from vellum.config import divergence_cap, write_boundaries
        from vellum.deps import registries

        self.assertEqual(divergence_cap(self.intent), 3)
        self.assertEqual(write_boundaries(self.intent, "harness-engineer"), ["harness"])
        self.assertEqual(registries(self.intent), ["pypi.org"])
        config = yaml.safe_load(
            (self.intent / ".vellum" / "config.yaml").read_text(encoding="utf-8")
        )
        # `vellum budget` and `vellum tick` read these; a seed missing one would
        # make the command exit 2 on a freshly provisioned installation.
        for key in ("per_item_usd", "period_usd", "period"):
            self.assertIn(key, config["budgets"], key)
        self.assertIn("timebox_hours", config["questions"])

    def test_the_seeded_suite_extracts_with_one_placeholder_scenario(self):
        code, out, err = run_cli_streams(["suite", "extract", str(self.intent), "-o", "-"])
        self.assertEqual(code, 0, err)
        suite = json.loads(out)
        self.assertEqual([s["id"] for s in suite["scenarios"]], ["billing-placeholder"])
        self.assertFalse(suite["scenarios"][0]["pending"])

    def test_the_harness_skeleton_is_importable_and_names_no_deployment(self):
        # Shipped as package data; a packaging change that dropped it would
        # leave a seed whose harness cannot start, and nothing else would say so.
        text = (self.intent / "harness" / "support" / "adapter.py").read_text(encoding="utf-8")
        self.assertIn("no_deployment", text)
        proc = subprocess.run(
            [sys.executable, "-c", "import steps, support.adapter as a; "
                                   "print(sorted(a.CAPABILITIES))"],
            cwd=str(self.intent / "harness"), capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "['deployment']")

    def test_the_product_repo_gets_its_memory_map(self):
        self.assertTrue((self.product / ".vellum" / "memory" / "map.md").is_file())

    def test_the_shipped_skeleton_is_exactly_this_set_of_files(self):
        # The seed comes out of package data, and `pyproject.toml`'s
        # `[tool.setuptools.package-data]` entry is what ships it. Without that
        # entry setuptools ships the `.py` files by heuristic and silently drops
        # `harness/README.md` — a wheel that provisions a seed missing a file,
        # with nothing else to say so. Naming the set here is what turns that
        # into a failing test rather than a surprise on somebody's first install.
        from vellum import seeds

        self.assertEqual(sorted(seeds.harness_files()), [
            "harness/README.md",
            "harness/run.py",
            "harness/steps/__init__.py",
            "harness/support/__init__.py",
            "harness/support/adapter.py",
            "harness/support/registry.py",
            "harness/support/report.py",
            "harness/support/runner.py",
            "harness/support/world.py",
        ])


# =====================================================================


class WithoutAForgeCliTheStepsAreAChecklist(ProvisionCase):
    """@id:no-transport-prints-the-checklist"""

    def setUp(self):
        super().setUp()
        self.without_gh()
        # Deliberately no `--into`: `--into` is "no forge at all" and would
        # never look for a transport, so it could not show that the absence of
        # one was *detected*. This is the rung an operator without `gh` lands
        # on — the local half in a staging directory, and a checklist.
        code, self.out, err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "acme",
            "--org", "waviisoft", "--area", "billing", "--yes",
        ])
        self.assertEqual(code, 0, self.out + err)
        self.staging = Path(
            self.out.split("Staging the local half in ")[1].splitlines()[0]
        )
        self.addCleanup(shutil.rmtree, self.staging, True)

    def test_it_exits_zero_for_the_half_it_did(self):
        self.assertTrue((self.staging / "acme-intent" / "spec" / "index.md").is_file())
        self.assertTrue((self.staging / "acme" / ".vellum" / "product.yaml").is_file())

    def test_the_forge_steps_are_printed_in_order_with_their_exact_values(self):
        checklist = self.out.split("Do these yourself, in order")[1]
        wanted = [
            "gh repo create waviisoft/acme-intent --private",
            "gh repo create waviisoft/acme --private",
            "gh secret set VELLUM_TOKEN --repo waviisoft/acme-intent --body -",
            "gh secret set SPEC_TOKEN --repo waviisoft/acme --body -",
            "gh api -X PUT repos/waviisoft/acme/actions/permissions/access "
            "-f access_level=organization",
        ]
        found = [checklist.find(text) for text in wanted]
        for text, at in zip(wanted, found):
            self.assertNotEqual(at, -1, f"{text!r} is not in the checklist")
        self.assertEqual(found, sorted(found), "the checklist is out of order")

    def test_the_checklist_is_the_list_the_plan_carried(self):
        # One list, printed twice (`provision.forge_steps`). A checklist that had
        # drifted from the plan would be wrong exactly where it is trusted most.
        plan, checklist = self.out.split("Do these yourself, in order")
        for step in provision.forge_steps(
            provision.resolve(_flags(), provision.Console(tty=False)),
            host="waviisoft/vellum",
        ):
            self.assertIn(step.what, plan, step.what[:60])
            self.assertIn(step.what, checklist, step.what[:60])

    def test_nothing_is_created_on_a_forge(self):
        self.assertIsNone(shutil.which("gh"))
        self.assertEqual(self.recorded(), [])
        self.assertIn("Nothing was created on a forge.", self.out)

    def test_it_says_the_transport_it_did_not_find(self):
        self.assertIn("no authenticated `gh`", self.out)


# =====================================================================


class BrownfieldWithDocsStagesTheSurveySources(ProvisionCase):
    """@id:brownfield-stages-docs-for-the-surveyor"""

    def setUp(self):
        super().setUp()
        self.without_gh()
        self.docs = self.cwd / "docs"
        self.docs.mkdir()
        for name in ("architecture.md", "api.md"):
            (self.docs / name).write_text(f"# {name}\n", encoding="utf-8")
        self.into = self.root / "out"
        code, self.out, err = run_cli_streams([
            "init", "--shape", "brownfield-with-docs", "--product", "legacy",
            "--org", "waviisoft", "--area", "billing", "--area", "accounts",
            "--docs", "docs/architecture.md", "--docs", "docs/api.md",
            "--into", str(self.into), "--yes",
        ])
        self.assertEqual(code, 0, self.out + err)
        self.intent = self.into / "legacy-intent"
        self.index = (self.intent / "spec" / "index.md").read_text(encoding="utf-8")

    def test_the_seeded_index_marks_each_area_unsurveyed(self):
        self.assertIn("| Billing | features/billing.md | unsurveyed |", self.index)
        self.assertIn("| Accounts | features/accounts.md | unsurveyed |", self.index)

    def test_each_area_file_carries_the_unsurveyed_status(self):
        for slug in ("billing", "accounts"):
            text = (self.intent / "spec" / "features" / f"{slug}.md").read_text(encoding="utf-8")
            self.assertIn("status: unsurveyed", text)
            # An unsurveyed area has no scenarios: the survey brings scenarios
            # that pass against the CURRENT deployment, and a placeholder here
            # would be a claim about a product nobody has looked at.
            self.assertNotIn("```gherkin", text)

    def test_both_documentation_paths_are_listed_as_survey_sources(self):
        sources = self.index.split("### Survey sources")[1]
        self.assertIn("`docs/architecture.md`", sources)
        self.assertIn("`docs/api.md`", sources)

    def test_the_seed_still_lints(self):
        # The docs paths are quoted deliberately: lint resolves bare `.md` paths
        # in prose as cross-references, and these name files in the PRODUCT repo,
        # which this tree cannot resolve.
        code, out = run_cli(["lint", str(self.intent)])
        self.assertEqual(code, 0, out)

    def test_the_products_vellum_arrives_on_a_branch_not_the_default(self):
        product = self.into / "legacy"
        self.assertEqual(
            self.git(product, "rev-parse", "--abbrev-ref", "HEAD"),
            provision.ADOPT_BRANCH,
        )
        on_main = self.git(product, "ls-tree", "-r", "--name-only", "main")
        self.assertNotIn(".vellum/product.yaml", on_main.splitlines())

    def test_docs_are_refused_for_a_shape_that_stages_none(self):
        code, out = run_cli([
            "init", "--shape", "greenfield", "--product", "legacy", "--org", "waviisoft",
            "--area", "billing", "--docs", "docs/api.md",
            "--into", str(self.root / "other"), "--yes",
        ])
        self.assertEqual(code, 2, out)
        self.assertIn("--docs", out)

    def test_a_documentation_path_that_does_not_exist_is_two(self):
        code, out = run_cli([
            "init", "--shape", "brownfield-with-docs", "--product", "legacy",
            "--org", "waviisoft", "--area", "billing", "--docs", "docs/nope.md",
            "--into", str(self.root / "other"), "--yes",
        ])
        self.assertEqual(code, 2, out)
        self.assertIn("docs/nope.md", out)
        self.assertFalse((self.root / "other").exists())


# =====================================================================


class ProvisioningOverAnInstallationIsRefused(ProvisionCase):
    """@id:init-refuses-an-existing-installation"""

    def test_a_checkout_carrying_a_workspace_is_two_naming_the_installation(self):
        write_workspace(self.cwd)
        code, out = run_cli(self.greenfield(self.root / "out"))
        self.assertEqual(code, 2, out)
        self.assertIn(".vellum/workspace.yaml", out)
        self.assertFalse((self.root / "out").exists())

    def test_the_same_checkout_with_no_shape_is_still_part_ones_stamping(self):
        # The refusal must not swallow part 1. `vellum init <checkout>` with no
        # provisioning argument is the stub-stamping command it has always been.
        write_workspace(self.cwd)
        code, out = run_cli(["init", str(self.cwd)])
        self.assertEqual(code, 0, out)
        for shipped in SHIPPED:
            self.assertTrue((self.cwd / WORKFLOWS / shipped.filename).is_file())

    def test_the_refusal_is_reached_before_any_prompt(self):
        # Asked before the conversation, so an operator who made this mistake is
        # told so rather than interviewed first. With no TTY an unanswerable
        # prompt is also a 2, so the message is what distinguishes them.
        write_workspace(self.cwd)
        code, out = run_cli(["init", "--shape", "greenfield", "--yes"])
        self.assertEqual(code, 2, out)
        self.assertIn("is an installation", out)
        self.assertNotIn("--product", out)


# =====================================================================


class EveryPromptIsAnswerableByAFlag(ProvisionCase):
    """`spec/features/installation.md`: "every prompt is answerable by a flag,
    so an unattended run is the same command with no prompts left"."""

    def setUp(self):
        super().setUp()
        self.without_gh()

    def test_a_prompted_run_and_a_flagged_run_seed_identical_trees(self):
        flagged = self.root / "flagged"
        code, out, err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--intent-repo", "acme-intent", "--product-repo", "acme",
            "--visibility", "private", "--branch", "main", "--area", "billing",
            "--into", str(flagged), "--yes",
        ])
        self.assertEqual(code, 0, out + err)

        prompted = self.root / "prompted"
        answers = iter([
            "greenfield", "acme", "waviisoft", "acme-intent", "acme",
            "private", "main", "billing", "y",
        ])
        console = provision.Console(tty=True, ask=lambda _: next(answers),
                                    ask_secret=lambda _: "")
        code = provision.run(
            str(self.cwd), shape=None, product=None, org=None, intent_repo=None,
            product_repo=None, visibility=None, intent_visibility=None,
            product_visibility=None, branch=None, areas=(), docs=(),
            into=str(prompted), plan_only=False, yes=False, console=console,
            out=io.StringIO(),
        )
        self.assertEqual(code, 0)

        self.assertEqual(_tree(flagged / "acme-intent"), _tree(prompted / "acme-intent"))
        self.assertEqual(_tree(flagged / "acme"), _tree(prompted / "acme"))

    def test_a_missing_answer_with_no_tty_is_two_naming_the_flag(self):
        code, out = run_cli(["init", "--product", "acme", "--org", "waviisoft",
                             "--area", "billing", "--into", str(self.root / "o"),
                             "--yes"])
        self.assertEqual(code, 2, out)
        self.assertIn("--shape", out)

    def test_yes_answers_the_prompts_that_have_defaults_and_not_the_others(self):
        # `--yes` "accepts defaults", so it settles --intent-repo, --product-repo,
        # --visibility and --branch and settles nothing that has no default.
        code, out, err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--into", str(self.root / "d"), "--yes", "--plan",
        ])
        self.assertEqual(code, 0, out + err)
        self.assertIn("waviisoft/acme-intent", out)
        self.assertIn("private", out)

    def test_the_plan_must_be_confirmed_and_there_is_no_terminal_to_confirm_on(self):
        code, out = run_cli([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--into", str(self.root / "c"),
        ])
        self.assertEqual(code, 2, out)
        self.assertIn("--yes", out)
        self.assertFalse((self.root / "c").exists())


# =====================================================================


class ValuesAreValidatedBeforeThePlan(ProvisionCase):
    def setUp(self):
        super().setUp()
        self.without_gh()

    def refuse(self, *extra: str) -> str:
        code, out = run_cli([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--into", str(self.root / "o"), "--yes", *extra,
        ])
        self.assertEqual(code, 2, out)
        self.assertFalse((self.root / "o").exists(), "a refused run created something")
        # The refusal comes before the plan, so no plan was printed.
        self.assertNotIn("Nothing below has happened yet", out)
        return out

    def test_a_product_that_is_not_a_slug_is_two(self):
        self.assertIn("--product", self.refuse("--product", "Acme Corp"))

    def test_an_org_that_is_not_an_org_is_two(self):
        self.assertIn("--org", self.refuse("--org", "wavii/soft"))

    def test_a_repo_name_carrying_a_path_separator_is_two(self):
        self.assertIn("--intent-repo", self.refuse("--intent-repo", "a/b"))

    def test_an_area_that_is_not_a_slug_is_two(self):
        # It becomes a spec file's `id:`; lint would refuse it at seed time, so
        # it is refused before the plan instead.
        self.assertIn("--area", self.refuse("--area", "Billing"))

    def test_a_repeated_area_is_two(self):
        self.assertIn("--area", self.refuse("--area", "billing"))

    def test_a_branch_git_would_not_accept_is_two(self):
        self.assertIn("--branch", self.refuse("--branch", "not a branch"))

    def test_one_repository_for_both_halves_is_two(self):
        out = self.refuse("--product-repo", "acme-intent")
        self.assertIn("cannot be one repository", out)

    def test_an_unknown_shape_is_refused_by_the_parser(self):
        # argparse's own refusal, which exits 2 by raising rather than by
        # returning — the same number, reached one layer earlier.
        with self.assertRaises(SystemExit) as raised:
            run_cli_streams(["init", "--shape", "mystery"])
        self.assertEqual(raised.exception.code, 2)


# =====================================================================


class TheTransportIsTheForgeCli(ProvisionCase):
    """The gh rung: the exact sequence, and where the secret travels.

    `spec/decisions/2026-09-03-installer-transport-is-the-forge-cli.md`: "v1's
    transport is the operator's own forge CLI (`gh` on GitHub), using the
    operator's credentials: it creates the repositories, sets the secrets, and …
    opens the product repo's workflows to reuse from the organization".
    """

    INTENT_TOKEN = "intent-token-must-not-be-logged"
    PRODUCT_TOKEN = "product-token-must-not-be-logged"

    def setUp(self):
        super().setUp()
        self.with_fake_gh(VELLUM_TOKEN=self.INTENT_TOKEN, SPEC_TOKEN=self.PRODUCT_TOKEN)
        code, self.out, self.err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--yes",
        ])
        self.assertEqual(code, 0, self.out + self.err)
        self.calls = self.recorded()

    def _staging(self) -> Path:
        return Path(self.out.split("Staging the local half in ")[1].splitlines()[0])

    def tearDown(self):
        shutil.rmtree(self._staging(), ignore_errors=True)

    def test_the_exact_sequence_for_greenfield(self):
        staging = self._staging()
        self.assertEqual(
            [call["argv"] for call in self.calls],
            [
                ["auth", "status"],
                ["repo", "view", "waviisoft/acme-intent", "--json", "name"],
                ["repo", "view", "waviisoft/acme", "--json", "name"],
                ["repo", "create", "waviisoft/acme-intent", "--private",
                 "--source", str(staging / "acme-intent"), "--remote", "origin",
                 "--push", "--description",
                 "Intent repo for acme: spec, harness, ledger (vellum init)"],
                ["repo", "create", "waviisoft/acme", "--private",
                 "--source", str(staging / "acme"), "--remote", "origin",
                 "--push", "--description", "acme — product repo (vellum init)"],
                ["secret", "set", "VELLUM_TOKEN", "--repo", "waviisoft/acme-intent",
                 "--body", "-"],
                ["secret", "set", "SPEC_TOKEN", "--repo", "waviisoft/acme",
                 "--body", "-"],
                ["api", "-X", "PUT",
                 "repos/waviisoft/acme/actions/permissions/access",
                 "-f", "access_level=organization"],
            ],
        )

    def test_the_secret_value_arrived_on_stdin_and_never_in_argv(self):
        secrets = {call["argv"][2]: call for call in self.calls
                   if call["argv"][:2] == ["secret", "set"]}
        self.assertEqual(secrets["VELLUM_TOKEN"]["stdin"], self.INTENT_TOKEN)
        self.assertEqual(secrets["SPEC_TOKEN"]["stdin"], self.PRODUCT_TOKEN)
        for call in self.calls:
            for argument in call["argv"]:
                self.assertNotIn(self.INTENT_TOKEN, argument)
                self.assertNotIn(self.PRODUCT_TOKEN, argument)

    def test_no_secret_value_reaches_the_output(self):
        # The plan, the report and the checklist all print the step; none of
        # them may print what it carries. `ForgeStep.stdin` holds a description.
        for token in (self.INTENT_TOKEN, self.PRODUCT_TOKEN):
            self.assertNotIn(token, self.out)
            self.assertNotIn(token, self.err)

    def test_the_seed_was_checked_before_any_push(self):
        checked = self.out.index("Seed checks")
        created = self.out.index("Forge steps taken")
        self.assertLess(checked, created)

    def test_a_public_product_repo_needs_no_access_change(self):
        self.trace.unlink()
        code, out, err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "other", "--org", "waviisoft",
            "--area", "billing", "--visibility", "public", "--yes",
        ])
        self.assertEqual(code, 0, out + err)
        self.addCleanup(shutil.rmtree,
                        out.split("Staging the local half in ")[1].splitlines()[0],
                        True)
        self.assertNotIn(
            ["api"], [call["argv"][:1] for call in self.recorded()]
        )
        self.assertIn("is public, so its workflows are already reusable", out)


class TheForgeRefusesANameItAlreadyHas(ProvisionCase):
    """`init` "refuses a repository name the forge already has unless the
    operator names it as the existing product repo of a brownfield shape"."""

    #: A fake `gh` whose `repo view` SUCCEEDS: every name already exists.
    EXISTS = """#!{python}
import json, os, sys
with open(os.environ["GH_TRACE"], "a", encoding="utf-8") as trace:
    trace.write(json.dumps({{"argv": sys.argv[1:]}}) + "\\n")
sys.exit(0)
"""

    def setUp(self):
        super().setUp()
        self.with_fake_gh()
        (Path(os.environ["PATH"]) / "gh").write_text(
            self.EXISTS.format(python=sys.executable), encoding="utf-8"
        )

    def test_an_existing_intent_repo_is_two(self):
        code, out = run_cli(["init", "--shape", "greenfield", "--product", "acme",
                             "--org", "waviisoft", "--area", "billing", "--yes"])
        self.assertEqual(code, 2, out)
        self.assertIn("waviisoft/acme-intent already exists", out)

    def test_an_existing_product_repo_is_two_for_greenfield(self):
        # The intent repo has to be free for this to be the refusal under test,
        # so the fake answers "exists" for one name only.
        (Path(os.environ["PATH"]) / "gh").write_text(
            FAKE_GH.format(python=sys.executable).replace(
                'sys.exit(1 if argv[:2] == ["repo", "view"] else 0)',
                'sys.exit(1 if argv[:3] == ["repo", "view", "waviisoft/acme-intent"] '
                'else 0)',
            ),
            encoding="utf-8",
        )
        code, out = run_cli(["init", "--shape", "greenfield", "--product", "acme",
                             "--org", "waviisoft", "--area", "billing", "--yes"])
        self.assertEqual(code, 2, out)
        self.assertIn("waviisoft/acme already exists", out)
        self.assertIn("brownfield", out)


class ASeedThatIsNotGreenIsNotPushed(ProvisionCase):
    """The one finding `init` may now report, and it is doctor's.

    `spec/features/installation.md`: "The seed lints clean and doctors green
    before it is pushed."
    """

    def test_a_red_seed_exits_one_and_reaches_no_forge(self):
        self.with_fake_gh(VELLUM_TOKEN="t", SPEC_TOKEN="t")
        # A `--from` that renders stubs naming a workflow doctor then looks for
        # under a different host is the narrowest way to make the seed red
        # without reaching into the seed's own templates.
        original = provision.check_seed

        def red(directory, *, host):
            check = original(directory, host=host)
            check.doctor_findings = list(check.doctor_findings) + [
                _Finding("spec-ci.yml", "invented", "a finding, for this test")
            ]
            return check

        provision.check_seed = red
        self.addCleanup(setattr, provision, "check_seed", original)
        into = self.root / "out"
        code, out, err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--into", str(into), "--yes",
        ])
        self.assertEqual(code, 1, out + err)
        self.assertIn("not green", err)
        self.assertEqual(self.recorded(), [])


class _Finding:
    def __init__(self, file, code, detail):
        self.file, self.code, self.detail = file, code, detail


def _flags(**overrides):
    """The greenfield answers these tests use, as `resolve` reads them."""
    values = dict(
        shape="greenfield", product="acme", org="waviisoft", intent_repo=None,
        product_repo=None, visibility=None, intent_visibility=None,
        product_visibility=None, branch=None, areas=["billing"], docs=[], yes=True,
    )
    values.update(overrides)
    return provision._Resolved(**values)


#: Any full commit sha in a seeded file. Exactly one appears — the pin in
#: `.vellum/product.yaml` — and it is the one thing two identical runs cannot
#: agree on: a commit's sha includes its timestamp, so two runs a second apart
#: pin different shas for byte-identical content. Blanking it is what makes the
#: equivalence assertion about the ANSWERS rather than about the clock; the pin
#: itself is asserted against its own repository in `AGreenfieldSeedIsGreen`.
_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")


def _tree(root: Path) -> dict[str, str]:
    """Every non-git file under *root*, by repo-relative path, shas blanked."""
    return {
        path.relative_to(root).as_posix():
            _SHA_RE.sub("<sha>", path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git/" not in path.relative_to(root).as_posix()
    }


if __name__ == "__main__":
    unittest.main()
