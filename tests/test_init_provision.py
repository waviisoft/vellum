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
from vellum import manifest, owned, provision
from vellum.install import SHIPPED, WORKFLOWS_DIR, default_ref

WORKFLOWS = WORKFLOWS_DIR["github"]

#: What the plan calls the staging directory before there is one. A checklist
#: still carrying it would be a checklist naming a directory that never existed.
STAGING_PLACEHOLDER = provision.STAGING

#: A fake ``gh``: it records every invocation as one JSON line and answers the
#: two questions the real one is asked. ``repo view`` exits 1 (the repository
#: does not exist), which is the answer greenfield needs; everything else
#: succeeds.
#:
#: Stdin is read ONLY for a `secret set` that carries no `--body`, which is what
#: the real `gh` does: its `getBody()` returns `--body`'s value whenever that
#: value is non-empty and falls back to stdin only when the flag is absent. So
#: `--body -` does not mean "read stdin" — it sets the secret to the literal
#: string `-`, and the value on the pipe is never read. A fake that read stdin
#: unconditionally would have recorded the token either way and called the bug
#: passing; this one reproduces the real command's actual rule, which is why
#: `test_the_secret_value_arrived_on_stdin_and_never_in_argv` now fails against
#: the argv that carried `--body -`.
FAKE_GH = """#!{python}
import json, os, subprocess, sys
from pathlib import Path

argv = sys.argv[1:]
entry = {{"argv": argv}}
if argv[:2] == ["secret", "set"] and "--body" not in argv:
    entry["stdin"] = sys.stdin.read()
with open(os.environ["GH_TRACE"], "a", encoding="utf-8") as trace:
    trace.write(json.dumps(entry) + "\\n")


def git(*args):
    subprocess.run(["git", "-c", "user.name=fake", "-c", "user.email=f@f", *args],
                   check=True, capture_output=True)


# `repo clone` is the one command a fake cannot merely record: the brownfield
# rung branches the adoption off the clone's history and pushes back to it, so
# a clone of nothing would fail two steps later for a reason that has nothing to
# do with what is under test. This serves a real local repository with one
# commit, standing in for the operator's existing product repo.
if argv[:2] == ["repo", "clone"]:
    bare = Path(os.environ["GH_REMOTES"]) / (argv[2].replace("/", "_") + ".git")
    git("init", "-q", "--bare", "-b", "main", str(bare))
    seed = Path(os.environ["GH_REMOTES"]) / "seed"
    git("init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("the product, before Vellum\\n")
    git("-C", str(seed), "add", "-A")
    git("-C", str(seed), "commit", "-qm", "existing product")
    git("-C", str(seed), "push", "-q", str(bare), "main")
    git("clone", "-q", str(bare), argv[4])
    sys.exit(0)

# A forge that says no part way through. `GH_FAIL_CREATE=2` fails the SECOND
# `repo create`, which is the interesting shape: the first one succeeded and is
# real, so the run cannot be retried and cannot be rolled back.
fail = os.environ.get("GH_FAIL_CREATE")
if fail and argv[:2] == ["repo", "create"]:
    with open(os.environ["GH_TRACE"], encoding="utf-8") as trace:
        seen = sum(1 for line in trace if line.strip()
                   and json.loads(line)["argv"][:2] == ["repo", "create"])
    if seen == int(fail):
        sys.stderr.write("HTTP 500: the forge said no\\n")
        sys.exit(1)

sys.exit(1 if argv[:2] == ["repo", "view"] and argv[2].endswith("-intent") else
         (1 if argv[:2] == ["repo", "view"] and os.environ.get("GH_NO_REPOS") else 0))
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

    def with_fake_gh(self, *, no_repos: bool = True, **secrets: str) -> None:
        """A fake `gh` on PATH. *no_repos*: every `repo view` says "not found".

        Greenfield needs both names free; the brownfield rung needs the product
        name to EXIST, which is the one difference between the two fixtures.
        """
        self.addCleanup(os.environ.__setitem__, "PATH", os.environ["PATH"])
        self.addCleanup(self._restore, ["GH_TRACE", "GH_REMOTES", "GH_NO_REPOS",
                                        "GH_FAIL_CREATE", *secrets])
        os.environ.pop("GH_FAIL_CREATE", None)
        os.environ["PATH"] = self._bin("bin-gh", with_gh=True)
        os.environ["GH_TRACE"] = str(self.trace)
        remotes = self.root / "forge"
        remotes.mkdir(exist_ok=True)
        os.environ["GH_REMOTES"] = str(remotes)
        os.environ.pop("GH_NO_REPOS", None)
        if no_repos:
            os.environ["GH_NO_REPOS"] = "1"
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

    def test_it_names_the_reuse_setting_as_one_no_transport_takes(self):
        # The plan used to promise `access_level=organization` on the product
        # repo. It is not a change this installation needs — the workflows the
        # stubs resolve against are the host's — so the plan now says nothing is
        # changed on either repo and names the host's setting as the operator's.
        out = self.plan()
        self.assertNotIn("access_level=organization", out)
        self.assertIn("nothing is changed on waviisoft/acme-intent or waviisoft/acme",
                      out)
        self.assertIn("confirm waviisoft/vellum", out)

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

    def test_bytecode_beside_the_seed_is_not_seeded_as_a_file(self):
        # Installing this package byte-compiles it, so an INSTALLED
        # `vellum/seeds/harness/` holds `__pycache__/` beside every module. The
        # walk read a `.pyc` as UTF-8 and took the whole command down — from a
        # wheel, and never from the development checkout the rest of these tests
        # run against, which is exactly why it needs its own test.
        import compileall

        from vellum import seeds

        package = Path(seeds.__file__).parent / seeds.HARNESS
        caches = list(package.rglob(seeds.BYTECODE))
        compileall.compile_dir(str(package), quiet=2)
        self.addCleanup(lambda: [shutil.rmtree(c, ignore_errors=True)
                                 for c in package.rglob(seeds.BYTECODE)
                                 if c not in caches])
        self.assertTrue(list(package.rglob("*.pyc")), "nothing was compiled")
        self.assertTrue(
            all(name.endswith(".py") for name in seeds.harness_files()),
            sorted(seeds.harness_files()),
        )

    def test_the_seeded_harness_readme_names_the_product(self):
        # Rendered from a template rather than copied, because it names the
        # product — the same line `vellum.install` draws between a stub it
        # generates and a file it copies.
        readme = (self.intent / "harness" / "README.md").read_text(encoding="utf-8")
        self.assertIn("Acme", readme)

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

    def test_each_side_of_the_pair_gets_a_manifest(self):
        # "on each side of the pair" (spec/features/installation.md): a product
        # repo carries a Vellum-seeded file too — its memory map — and a side
        # with no manifest is a side no upgrade can reason about.
        for checkout, side in ((self.intent, owned.INTENT),
                               (self.product, owned.PRODUCT)):
            found = manifest.load(checkout)
            self.assertEqual(found.release, default_ref(), side)
            self.assertEqual(list(found.owned), list(owned.for_side(side)), side)

    def test_the_seeded_manifest_owns_the_stubs_the_config_and_the_machinery(self):
        listed = manifest.load(self.intent).owned
        for relative in (".vellum/config.yaml", "ledger/releases.yaml",
                         "harness/run.py", "harness/support/runner.py",
                         (WORKFLOWS / "spec-ci.yml").as_posix()):
            self.assertIn(relative, listed, relative)

    def test_the_seeded_manifest_owns_no_spec_file_and_nothing_that_is_ours(self):
        # The spec is the product's own words; the workspace file is the repo
        # map an installation edits every time it adds a product; the pin is the
        # pin. `vellum.owned`'s table says why for each, one row at a time.
        listed = manifest.load(self.intent).owned
        for relative in ("spec/product.md", "spec/index.md",
                         "spec/features/billing.md", ".vellum/workspace.yaml",
                         "harness/README.md", "harness/support/adapter.py",
                         "harness/steps/__init__.py"):
            self.assertNotIn(relative, listed, relative)
        self.assertNotIn(".vellum/product.yaml", manifest.load(self.product).owned)

    def test_every_owned_path_is_a_file_the_seed_actually_wrote(self):
        # The manifest is written from `vellum.owned`'s table and the seed from
        # `intent_seed`; a row that named a file the seed does not write would
        # be an installation whose first upgrade reports a missing owned file.
        for checkout in (self.intent, self.product):
            for relative in manifest.load(checkout).owned:
                self.assertTrue((checkout / relative).is_file(), relative)

    def test_the_shipped_skeleton_is_exactly_this_set_of_files(self):
        # The seed comes out of package data, and a wheel carries it only
        # because every file under `seeds/harness/` is a module of an ordinary
        # package (`seeds/harness/__init__.py` has the argument). Naming the set
        # here turns a packaging change that dropped one into a failing test
        # rather than a surprise on somebody's first install — and pins the one
        # file that is packaging rather than seed, `harness/__init__.py`, as
        # NOT seeded.
        from vellum import seeds

        self.assertNotIn("harness/__init__.py", seeds.harness_files())
        self.assertFalse((self.intent / "harness" / "__init__.py").exists())
        self.assertEqual(sorted(seeds.harness_files()), [
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
            'printf %s "$VELLUM_TOKEN" | gh secret set VELLUM_TOKEN '
            "--repo waviisoft/acme-intent",
            'printf %s "$SPEC_TOKEN" | gh secret set SPEC_TOKEN '
            "--repo waviisoft/acme",
        ]
        found = [checklist.find(text) for text in wanted]
        for text, at in zip(wanted, found):
            self.assertNotEqual(at, -1, f"{text!r} is not in the checklist")
        self.assertEqual(found, sorted(found), "the checklist is out of order")

    def test_each_secret_line_pipes_the_variable_that_holds_that_secret(self):
        # `$TOKEN` was a variable nothing in the plan ever told the operator to
        # set, and it was the same on both lines — so following the checklist
        # set one repo's secret to the other's value, or to nothing.
        checklist = self.out.split("Do these yourself, in order")[1]
        self.assertNotIn("$TOKEN", checklist)
        self.assertIn('printf %s "$VELLUM_TOKEN" | gh secret set VELLUM_TOKEN',
                      checklist)
        self.assertIn('printf %s "$SPEC_TOKEN" | gh secret set SPEC_TOKEN',
                      checklist)

    def test_no_checklist_line_carries_a_body_flag(self):
        # The line is executable as printed only without it: `--body -` sets the
        # secret to `-`. `printf … |` is a pipe, not a terminal, so `gh` reads
        # the value from stdin exactly as the transport's `subprocess` does.
        self.assertNotIn("--body", self.out.split("Do these yourself, in order")[1])

    def test_the_host_repos_reuse_setting_is_the_one_named_and_it_is_manual(self):
        # The product repo's Actions access step is gone; what replaced it is
        # nothing, because the setting that matters is on the host repo and no
        # transport here can reach it.
        self.assertNotIn("access_level=organization", self.out)
        self.assertIn("confirm waviisoft/vellum", self.out)
        self.assertIn("nothing here can set it", self.out)

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

    def test_the_checklist_carries_no_unfilled_placeholder(self):
        # "with the exact values" (spec/features/installation.md). Where each
        # checkout is, is not known when the step list is built — without
        # `--into` the staging directory must not exist until the plan is
        # confirmed — so the paths travel as placeholders and are filled in for
        # both renderings from one mapping.
        #
        # Driven over ALL THREE shapes, because the greenfield checklist is the
        # only one that was: the brownfield shapes carry three more placeholders
        # (`<product clone>`, `<adopt PR body>`, `<adopt base>`) and one of them
        # — the PR body — reached an operator as the literal text
        # `<adopt PR body>`, because `places` knew only the two checkout keys.
        for shape, product, extra in (
            ("greenfield", "acme", []),
            ("brownfield", "legacy", []),
            ("brownfield-with-docs", "legacy", ["--docs", "docs/api.md"]),
        ):
            with self.subTest(shape=shape):
                out, staging = self._checklist_run(shape, product, extra)
                checklist = out.split("Do these yourself, in order")[1]
                for placeholder in ("<intent checkout>", "<product checkout>",
                                    "<product clone>", "<adopt PR body>",
                                    "<adopt base>", STAGING_PLACEHOLDER):
                    self.assertNotIn(placeholder, checklist)
                # And nothing else that looks like one either. `<job>` is prose:
                # branch protection names checks that do not exist until the
                # stubs have run once, and no value here could fill it in.
                left = [found for found in re.findall(r"<[^>\n]{2,60}>", checklist)
                        if found != "<job>"]
                self.assertEqual(left, [], f"{shape}: unfilled {left}")
                self.assertIn(str(staging / f"{product}-intent"), checklist)
                self.assertIn(str(staging / product), checklist)

    def _checklist_run(self, shape, product, extra):
        """One no-transport run of *shape*, returning (output, staging dir)."""
        docs = self.cwd / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "api.md").write_text("# api\n", encoding="utf-8")
        code, out, err = run_cli_streams([
            "init", "--shape", shape, "--product", product, "--org", "waviisoft",
            "--area", "billing", "--yes", *extra,
        ])
        self.assertEqual(code, 0, out + err)
        staging = Path(out.split("Staging the local half in ")[1].splitlines()[0])
        self.addCleanup(shutil.rmtree, staging, True)
        return out, staging

    def test_the_brownfield_clone_step_names_a_path_it_could_clone_into(self):
        # It named `<product checkout>` — the stand-in this run had just built
        # and filled with two files and a root commit — so the very first line
        # of the checklist was `gh repo clone … -- <a non-empty directory>`,
        # which fails. The clone goes to a sibling, and the push and the pull
        # request name that sibling, so the three lines are one story.
        out, staging = self._checklist_run("brownfield", "legacy", [])
        checklist = out.split("Do these yourself, in order")[1]
        clone = f"{staging / 'legacy'}-clone"
        self.assertIn(f"gh repo clone waviisoft/legacy -- {clone}", checklist)
        self.assertIn(f"git -C {clone} push -u origin vellum/adopt", checklist)
        self.assertTrue((staging / "legacy").is_dir())
        self.assertFalse(Path(clone).exists(), "the checklist's clone target exists")

    def test_the_adoption_pr_body_is_a_file_the_checklist_can_point_at(self):
        # `--body-file <adopt PR body>` was never substituted on this rung: the
        # operator was told to pass a filename that was four words of English.
        # The body is written into the product checkout, uncommitted.
        out, staging = self._checklist_run("brownfield", "legacy", [])
        body = staging / "legacy" / ".vellum" / "ADOPT_PR.md"
        self.assertIn(f"--body-file {body}", out)
        self.assertTrue(body.is_file())
        self.assertIn("Adopt Vellum", body.read_text(encoding="utf-8"))
        # Uncommitted: the branch carries the two seeded files and nothing else.
        tracked = self.git(staging / "legacy", "ls-tree", "-r", "--name-only", "HEAD")
        self.assertNotIn(".vellum/ADOPT_PR.md", tracked.splitlines())

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
                # No `--body`, and that absence is the whole mechanism: `gh`
                # reads the value from stdin only when the flag is not given.
                ["secret", "set", "VELLUM_TOKEN", "--repo", "waviisoft/acme-intent"],
                ["secret", "set", "SPEC_TOKEN", "--repo", "waviisoft/acme"],
            ],
        )

    def test_no_step_carries_a_body_flag(self):
        # Stated on its own, because `--body -` looked exactly like "read
        # stdin" and passed review: `gh` takes `--body`'s value whenever it is
        # non-empty, so that argv set both secrets to the one-character string
        # `-` and every workflow in the new installation would have failed
        # authenticating with it.
        for call in self.calls:
            self.assertNotIn("--body", call["argv"], call["argv"])

    def test_no_step_touches_the_product_repos_actions_access(self):
        # Removed deliberately. `actions/permissions/access` governs whether a
        # repository's OWN workflows may be reused by others, and the workflows
        # these stubs resolve against live in the host repo — which no
        # installation owns. On a user-owned account the call fails outright.
        for call in self.calls:
            self.assertNotEqual(call["argv"][:1], ["api"], call["argv"])
            for argument in call["argv"]:
                self.assertNotIn("permissions/access", argument)

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

    def test_visibility_changes_nothing_about_which_steps_are_taken(self):
        # It used to: a private product repo got an extra `gh api` call. That
        # step is gone, so the two visibilities now take the same four steps —
        # which is the assertion that would fail if it ever came back.
        self.trace.unlink()
        code, out, err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "other", "--org", "waviisoft",
            "--area", "billing", "--visibility", "public", "--yes",
        ])
        self.assertEqual(code, 0, out + err)
        self.addCleanup(shutil.rmtree,
                        out.split("Staging the local half in ")[1].splitlines()[0],
                        True)
        public = [call["argv"][:2] for call in self.recorded()]
        self.assertEqual(
            public,
            [["auth", "status"], ["repo", "view"], ["repo", "view"],
             ["repo", "create"], ["repo", "create"],
             ["secret", "set"], ["secret", "set"]],
        )
        self.assertEqual(
            public, [call["argv"][:2] for call in self.calls],
            "a public run and a private run take different forge steps",
        )


class TheForgeRefusesANameItAlreadyHas(ProvisionCase):
    """`init` "refuses a repository name the forge already has unless the
    operator names it as the existing product repo of a brownfield shape"."""

    def test_an_existing_product_repo_is_two_for_greenfield(self):
        # The fake answers "exists" for every name that is not `*-intent`, so
        # the intent name is free and the product name is taken: exactly the
        # refusal under test, with nothing else in the way.
        self.with_fake_gh(no_repos=False)
        code, out = run_cli(["init", "--shape", "greenfield", "--product", "acme",
                             "--org", "waviisoft", "--area", "billing", "--yes"])
        self.assertEqual(code, 2, out)
        self.assertIn("waviisoft/acme already exists", out)
        self.assertIn("brownfield", out)

    def test_a_product_repo_the_forge_does_not_have_is_two_for_brownfield(self):
        # And the mirror image: a brownfield shape ADOPTS an existing product
        # repository, so a name the forge does not have is a 2 rather than a
        # silent create.
        self.with_fake_gh(no_repos=True)
        code, out = run_cli(["init", "--shape", "brownfield", "--product", "acme",
                             "--org", "waviisoft", "--area", "billing", "--yes"])
        self.assertEqual(code, 2, out)
        self.assertIn("does not exist on the forge", out)


class TheBrownfieldRungAdoptsTheExistingRepo(ProvisionCase):
    """The product repo is a guest's host: `.vellum/` arrives on a branch.

    `spec/features/installation.md`: "its `.vellum/` arrives on a branch as a
    pull request, never as a push to its default branch".
    """

    def setUp(self):
        super().setUp()
        self.with_fake_gh(no_repos=False, VELLUM_TOKEN="t1", SPEC_TOKEN="t2")
        code, self.out, err = run_cli_streams([
            "init", "--shape", "brownfield", "--product", "legacy",
            "--org", "waviisoft", "--area", "billing", "--yes",
        ])
        self.assertEqual(code, 0, self.out + err)
        self.staging = Path(
            self.out.split("Staging the local half in ")[1].splitlines()[0]
        )
        self.addCleanup(shutil.rmtree, self.staging, True)
        self.calls = [call["argv"] for call in self.recorded()]

    def test_the_clone_comes_before_the_branch_it_is_branched_from(self):
        # The one forge step the local half depends on. Without it the adoption
        # branch would sit on no history and push to nothing.
        self.assertEqual(
            self.calls[3],
            ["repo", "clone", "waviisoft/legacy", "--", str(self.staging / "legacy")],
        )

    def test_the_adoption_branches_off_the_existing_history(self):
        product = self.staging / "legacy"
        self.assertEqual(
            self.git(product, "rev-parse", "--abbrev-ref", "HEAD"),
            provision.ADOPT_BRANCH,
        )
        log = self.git(product, "log", "--format=%s").splitlines()
        self.assertEqual(len(log), 2, log)
        self.assertEqual(log[-1], "existing product")
        self.assertIn(".vellum/product.yaml",
                      self.git(product, "ls-tree", "-r", "--name-only", "HEAD"))

    def remote(self) -> Path:
        """The bare repository the fake `gh` cloned from: the forge's copy."""
        return Path(os.environ["GH_REMOTES"]) / "waviisoft_legacy.git"

    def test_it_pushes_the_branch_and_opens_a_pull_request(self):
        # The push is `git`, not `gh`, so it is not in the gh trace — and
        # asserting the forge's own copy is the better question anyway.
        branches = self.git(self.remote(), "for-each-ref", "--format=%(refname)",
                            "refs/heads").splitlines()
        self.assertIn(f"refs/heads/{provision.ADOPT_BRANCH}", branches)
        pr = next(call for call in self.calls if call[:2] == ["pr", "create"])
        self.assertEqual(pr[:8], ["pr", "create", "--repo", "waviisoft/legacy",
                                  "--base", "main", "--head", provision.ADOPT_BRANCH])

    def test_the_products_default_branch_was_never_written(self):
        # Vellum is a guest in a repository it did not create. `main` on the
        # forge's copy is exactly the commit that was there before.
        remote = self.remote()
        self.assertEqual(
            self.git(remote, "log", "--format=%s", "main").splitlines(),
            ["existing product"],
        )
        self.assertNotIn(
            ".vellum/product.yaml",
            self.git(remote, "ls-tree", "-r", "--name-only", "main").splitlines(),
        )

    def test_the_merge_of_that_pull_request_stays_on_the_checklist(self):
        checklist = self.out.split("Do these yourself, in order")[1]
        self.assertIn("review and merge the adoption pull request", checklist)


class TheAdoptionIsAGuestInTheCheckout(ProvisionCase):
    """Adoption over a checkout the operator already has.

    `spec/features/installation.md`: the product repo's `.vellum/` "arrives on a
    branch as a pull request, never as a push to its default branch". A guest
    that swept the host's working tree into that pull request, or moved a branch
    it found there, would be keeping the letter of that and none of it — so all
    three are refused, and the commit it does make carries two files.
    """

    def setUp(self):
        super().setUp()
        self.without_gh()
        self.into = self.root / "out"
        self.product = self.into / "legacy"
        self.product.mkdir(parents=True)
        self.host("init", "-q", "-b", "main", ".")
        (self.product / "README.md").write_text("before Vellum\n", encoding="utf-8")
        self.host("add", "-A")
        self.host("commit", "-qm", "existing product")
        self.first = self.host("rev-parse", "HEAD")

    def host(self, *args: str) -> str:
        """One git command in the operator's own product checkout."""
        return subprocess.run(
            ["git", "-C", str(self.product), "-c", "user.name=operator",
             "-c", "user.email=operator@localhost", *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def adopt(self, *extra: str):
        return run_cli([
            "init", "--shape", "brownfield", "--product", "legacy",
            "--org", "waviisoft", "--area", "billing",
            "--into", str(self.into), "--yes", *extra,
        ])

    def assertNothingHappened(self):
        """No intent checkout, and the product repo exactly as it was."""
        self.assertFalse((self.into / "legacy-intent").exists())
        self.assertEqual(self.host("log", "--format=%s").splitlines(),
                         ["existing product"])
        self.assertEqual(
            self.host("for-each-ref", "--format=%(refname)", "refs/heads"),
            "refs/heads/main",
        )

    def test_a_dirty_tree_is_refused_naming_what_is_in_it(self):
        # `git add -A` swept everything in the checkout into the adoption
        # commit — an untracked `.env` included — and then a pull request
        # carried it to the forge. The tree is the operator's; this command
        # does not get to decide what of it is committed.
        (self.product / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
        (self.product / "README.md").write_text("work in progress\n", encoding="utf-8")
        code, out = self.adopt()
        self.assertEqual(code, 2, out)
        self.assertIn(".env", out)
        self.assertIn("README.md", out)
        self.assertNothingHappened()
        self.assertEqual((self.product / ".env").read_text(encoding="utf-8"),
                         "SECRET=hunter2\n")

    def test_a_checkout_that_is_already_an_installation_is_refused(self):
        # The product-side twin of the refusal `run` opens with. Seeding over it
        # replaces the pin this repository already answers to.
        (self.product / ".vellum").mkdir()
        (self.product / ".vellum" / "product.yaml").write_text(
            "intent:\n  repo: waviisoft/other-intent\n", encoding="utf-8")
        self.host("add", "-A")
        self.host("commit", "-qm", "already adopted")
        code, out = self.adopt()
        self.assertEqual(code, 2, out)
        self.assertIn("product.yaml", out)
        self.assertFalse((self.into / "legacy-intent").exists())
        # And the file it would have overwritten is untouched.
        self.assertIn("waviisoft/other-intent",
                      (self.product / ".vellum" / "product.yaml").read_text())

    def test_an_existing_adopt_branch_is_refused_rather_than_reset(self):
        # `checkout -B` resets a branch that is already there. Whatever is on it
        # is somebody's — an adoption already in review, most likely.
        self.host("branch", provision.ADOPT_BRANCH)
        on_it = self.host("rev-parse", provision.ADOPT_BRANCH)
        code, out = self.adopt()
        self.assertEqual(code, 2, out)
        self.assertIn(provision.ADOPT_BRANCH, out)
        self.assertEqual(self.host("rev-parse", provision.ADOPT_BRANCH), on_it)
        self.assertFalse((self.into / "legacy-intent").exists())

    def test_a_default_branch_the_checkout_does_not_have_is_refused(self):
        # `--branch` names the INTENT repo's default branch, and assuming the
        # product repo agrees is how an adoption branches off nothing.
        self.host("branch", "-m", "main", "trunk")
        code, out = self.adopt()
        self.assertEqual(code, 2, out)
        self.assertIn("--branch", out)

    def test_the_adoption_commit_carries_the_seeded_files_and_nothing_else(self):
        # Three since the upgrades wave: the manifest is a seeded file like any
        # other (`vellum.manifest`), so it arrives in the adoption pull request
        # rather than being written into somebody's default branch afterwards.
        # Asserted against `product_seed`'s own keys, so a fourth seeded file
        # updates this in one place and a file that is NOT seeded still fails it.
        code, out = self.adopt()
        self.assertEqual(code, 0, out)
        answers = provision.resolve(_flags(shape=provision.BROWNFIELD,
                                           product_repo="legacy"),
                                    provision.Console(tty=False))
        self.assertEqual(
            sorted(self.host("show", "--name-only", "--format=", "HEAD").split()),
            sorted(provision.product_seed(answers, "0" * 40)),
        )

    def test_the_adoption_commit_is_parented_on_the_default_branch(self):
        # `checkout -B` branched off whatever HEAD was. A pull request opened
        # from a branch parented on somebody's feature work carries that work.
        self.host("checkout", "-q", "-b", "feature")
        (self.product / "feature.txt").write_text("mid-flight\n", encoding="utf-8")
        self.host("add", "-A")
        self.host("commit", "-qm", "a feature in progress")
        feature = self.host("rev-parse", "HEAD")

        code, out = self.adopt()
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.host("rev-parse", f"{provision.ADOPT_BRANCH}^"), self.first)
        self.assertNotIn(
            feature,
            self.host("rev-list", provision.ADOPT_BRANCH).splitlines(),
        )
        self.assertNotIn(
            "feature.txt",
            self.host("ls-tree", "-r", "--name-only", provision.ADOPT_BRANCH).split(),
        )

    def test_the_pr_body_is_the_only_thing_left_uncommitted(self):
        code, out = self.adopt()
        self.assertEqual(code, 0, out)
        self.assertEqual(
            [line.split()[-1] for line in
             self.host("status", "--porcelain").splitlines()],
            [".vellum/ADOPT_PR.md"],
        )


class AForgeFailureMidRunStillReportsWhatItDid(ProvisionCase):
    """A transport failure is not a rollback.

    `gh` has already created whatever it created. A run that raised and printed
    nothing left an operator to work out for themselves which of eight steps had
    happened — and re-running would then refuse at the name the forge now has.
    """

    def setUp(self):
        super().setUp()
        self.with_fake_gh(VELLUM_TOKEN="t1", SPEC_TOKEN="t2")
        os.environ["GH_FAIL_CREATE"] = "2"
        code, self.out, self.err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--yes",
        ])
        self.code = code
        staging = self.out.split("Staging the local half in ")[1].splitlines()[0]
        self.staging = Path(staging)
        self.addCleanup(shutil.rmtree, self.staging, True)

    def test_it_exits_two(self):
        self.assertEqual(self.code, 2, self.out + self.err)

    def test_the_step_that_failed_is_named_on_stderr(self):
        self.assertIn("repo create", self.err)

    def test_the_report_names_what_was_taken(self):
        taken = self.out.split("Forge steps taken before it stopped")[1]
        self.assertIn("create the intent repository waviisoft/acme-intent",
                      taken.split("Left to you")[0])

    def test_every_step_from_the_failure_onward_is_handed_back(self):
        left = self.out.split("Left to you")[1]
        for what in ("create the product repository waviisoft/acme",
                     "set VELLUM_TOKEN", "set SPEC_TOKEN",
                     "protect main on waviisoft/acme-intent"):
            self.assertIn(what, left, what)
        # With their commands, so the checklist is followable from here.
        self.assertIn('printf %s "$SPEC_TOKEN" | gh secret set SPEC_TOKEN', left)

    def test_no_secret_value_reaches_the_interrupted_report(self):
        for token in ("t1", "t2"):
            self.assertNotIn(f'"{token}"', self.out)

    def test_the_checkouts_it_names_are_still_there(self):
        # The report's commands name them, so deleting them would destroy the
        # only thing the run has left to offer.
        self.assertIn(str(self.staging / "acme-intent"), self.out)
        self.assertTrue((self.staging / "acme-intent" / "spec" / "index.md").is_file())
        self.assertTrue((self.staging / "acme" / ".vellum" / "product.yaml").is_file())


class TheOutwardChecksComeBeforeTheConfirmation(ProvisionCase):
    """A refusal an operator has already said yes to is a refusal too late.

    And it used to cost a directory: the staging `mkdtemp` was made first, so a
    name the forge already had left an empty `vellum-init-*` behind every time.
    """

    def staging_dirs(self) -> set:
        return set(Path(tempfile.gettempdir()).glob("vellum-init-*"))

    def test_a_taken_name_is_refused_before_any_prompt_and_leaves_no_directory(self):
        self.with_fake_gh(no_repos=False)   # every non-`-intent` name is taken
        before = self.staging_dirs()
        asked = []
        console = provision.Console(
            tty=True,
            ask=lambda question: asked.append(question) or "y",
            ask_secret=lambda _: "",
        )
        with self.assertRaises(provision.ProvisionError) as raised:
            provision.run(
                str(self.cwd), shape="greenfield", product="acme", org="waviisoft",
                intent_repo="acme-intent", product_repo="acme",
                visibility="private", intent_visibility=None,
                product_visibility=None, branch="main",
                areas=["billing"], docs=(), into=None, plan_only=False, yes=False,
                console=console, out=io.StringIO(),
            )
        self.assertIn("already exists on the forge", str(raised.exception))
        self.assertEqual(asked, [], f"it asked before it checked: {asked}")
        self.assertEqual(self.staging_dirs(), before, "a staging directory was left")

    def test_declining_the_plan_is_two_and_creates_nothing(self):
        # Exit 2, not 0: declining is "this command did not do what it was asked
        # to do", and a script reading 0 as "the installation is there" would be
        # wrong. Same number as the no-terminal case above it, for one reason.
        self.without_gh()
        before = self.staging_dirs()
        into = self.root / "declined"
        console = provision.Console(tty=True, ask=lambda _: "n",
                                    ask_secret=lambda _: "")
        with self.assertRaises(provision.ProvisionError) as raised:
            provision.run(
                str(self.cwd), shape="greenfield", product="acme", org="waviisoft",
                intent_repo="acme-intent", product_repo="acme",
                visibility="private", intent_visibility=None,
                product_visibility=None, branch="main",
                areas=["billing"], docs=(), into=str(into), plan_only=False,
                yes=False, console=console, out=io.StringIO(),
            )
        self.assertIn("not confirmed", str(raised.exception))
        self.assertFalse(into.exists())
        self.assertEqual(self.staging_dirs(), before)

    def test_a_local_build_that_fails_leaves_no_staging_directory(self):
        # The other half of the same promise: the directory is this run's until
        # the seed it holds has passed both guards.
        self.without_gh()
        before = self.staging_dirs()
        broken = provision.build_intent

        def fail(*args, **kwargs):
            raise provision.ProvisionError("git said no, for this test")

        provision.build_intent = fail
        self.addCleanup(setattr, provision, "build_intent", broken)
        code, out = run_cli([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--yes",
        ])
        self.assertEqual(code, 2, out)
        self.assertEqual(self.staging_dirs(), before)


class PlanReachesNoForge(ProvisionCase):
    """`--plan` "prints it and stops, creating nothing" (the spec).

    `detect_gh()` runs `gh auth status`, which is a call to the forge — so the
    one command whose whole promise is that it does nothing was contacting one
    before it printed a word.
    """

    def setUp(self):
        super().setUp()
        self.with_fake_gh()

    def test_plan_invokes_the_forge_cli_not_at_all(self):
        code, out, err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--yes", "--plan",
        ])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.recorded(), [], "--plan reached the forge")

    def test_it_says_what_it_found_without_claiming_it_is_authenticated(self):
        # Whether that `gh` is logged in is exactly the question it declined to
        # ask, so the label says so rather than promising a transport.
        code, out, err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--yes", "--plan",
        ])
        self.assertEqual(code, 0, out + err)
        self.assertIn("transport: gh (if authenticated)", out)
        self.assertEqual(self.recorded(), [])


class TheSurveySourcesAreProductRepoPaths(ProvisionCase):
    """`--docs` becomes a line in `spec/index.md` that a surveyor opens.

    Every refusal here is a promise the index would otherwise not keep.
    """

    def setUp(self):
        super().setUp()
        self.without_gh()
        self.docs = self.cwd / "docs"
        self.docs.mkdir()
        (self.docs / "api.md").write_text("# api\n", encoding="utf-8")
        self.into = self.root / "out"

    def adopt(self, *docs: str, into=None):
        return run_cli([
            "init", "--shape", "brownfield-with-docs", "--product", "legacy",
            "--org", "waviisoft", "--area", "billing",
            "--into", str(into or self.into), "--yes",
            *[argument for path in docs for argument in ("--docs", path)],
        ])

    def index(self) -> str:
        return (self.into / "legacy-intent" / "spec" / "index.md").read_text(
            encoding="utf-8")

    def test_a_path_outside_the_product_checkout_is_two(self):
        # An absolute path on this operator's laptop means nothing to the next
        # person reading `spec/index.md`.
        outside = self.root / "elsewhere.md"
        outside.write_text("# elsewhere\n", encoding="utf-8")
        code, out = self.adopt(str(outside))
        self.assertEqual(code, 2, out)
        self.assertIn("outside", out)
        self.assertFalse(self.into.exists())

    def test_a_path_climbing_out_with_dot_dot_is_two(self):
        (self.root / "escape.md").write_text("# escape\n", encoding="utf-8")
        code, out = self.adopt("../escape.md")
        self.assertEqual(code, 2, out)
        self.assertIn("outside", out)

    def test_a_path_carrying_a_backtick_is_two(self):
        # The index wraps each source in inline code, and lint's masking — what
        # keeps these paths from being read as cross-references — depends on
        # that quoting holding.
        weird = self.docs / "we`ird.md"
        weird.write_text("# weird\n", encoding="utf-8")
        code, out = self.adopt("docs/we`ird.md")
        self.assertEqual(code, 2, out)
        self.assertIn("backtick", out)

    def test_a_path_carrying_a_newline_is_two(self):
        code, out = self.adopt("docs/api.md\nnot-a-source")
        self.assertEqual(code, 2, out)
        self.assertIn("control character", out)

    def test_an_absolute_path_inside_the_checkout_lands_relative(self):
        # What reaches the index is the repo-relative path, never this machine's.
        code, out = self.adopt(str(self.docs / "api.md"))
        self.assertEqual(code, 0, out)
        sources = self.index().split("### Survey sources")[1]
        self.assertIn("`docs/api.md`", sources)
        self.assertNotIn(str(self.cwd), self.index())

    def test_a_long_path_is_written_whole(self):
        # `one_line` truncated at 120 characters with an ellipsis, which is a
        # path the surveyor cannot open — a promise the index cannot keep, which
        # is the exact wording of the refusal for a path that does not exist.
        deep = self.docs / ("nested/" * 20)
        deep.mkdir(parents=True)
        (deep / "architecture.md").write_text("# deep\n", encoding="utf-8")
        relative = f"docs/{'nested/' * 20}architecture.md"
        self.assertGreater(len(relative), 120)
        code, out = self.adopt(relative)
        self.assertEqual(code, 0, out)
        self.assertIn(f"`{relative}`", self.index())
        self.assertNotIn("…", self.index())

    def test_the_seed_still_lints_with_a_long_path(self):
        deep = self.docs / ("nested/" * 20)
        deep.mkdir(parents=True)
        (deep / "architecture.md").write_text("# deep\n", encoding="utf-8")
        code, out = self.adopt(f"docs/{'nested/' * 20}architecture.md")
        self.assertEqual(code, 0, out)
        code, out = run_cli(["lint", str(self.into / "legacy-intent")])
        self.assertEqual(code, 0, out)


class TheSeedsOwnIdsAreNotAvailableAsAreas(ProvisionCase):
    """`--area product` seeded a second file claiming `id: product`.

    Lint would not say so — its duplicate-id check (`GH003`) is about SCENARIO
    ids — so the seed went green and the collision surfaced later.
    """

    def setUp(self):
        super().setUp()
        self.without_gh()

    def refuse(self, *areas: str) -> str:
        code, out = run_cli([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--into", str(self.root / "o"), "--yes",
            *[argument for area in areas for argument in ("--area", area)],
        ])
        self.assertEqual(code, 2, out)
        self.assertFalse((self.root / "o").exists())
        # Before the plan, like every other value refusal.
        self.assertNotIn("Nothing below has happened yet", out)
        return out

    def test_an_area_named_product_is_two(self):
        self.assertIn("spec/product.md", self.refuse("product"))

    def test_an_area_named_index_is_two(self):
        self.assertIn("spec/index.md", self.refuse("index"))

    def test_a_reserved_name_beside_a_good_one_is_still_two(self):
        self.assertIn("--area", self.refuse("billing", "index"))

    def test_a_name_yaml_reads_as_a_boolean_is_two(self):
        # It becomes a spec file's `id:` and a workspace key, and the parser
        # that reads the seed back would answer `False`.
        for name in ("no", "yes", "on", "off", "null"):
            with self.subTest(name=name):
                self.assertIn("YAML", self.refuse(name))

    def test_a_product_yaml_reads_as_a_boolean_is_two(self):
        code, out = run_cli([
            "init", "--shape", "greenfield", "--product", "off", "--org", "waviisoft",
            "--area", "billing", "--into", str(self.root / "o"), "--yes",
        ])
        self.assertEqual(code, 2, out)
        self.assertIn("--product", out)
        self.assertIn("YAML", out)

    def test_the_pin_is_a_string_even_when_it_is_all_digits(self):
        # A sha is a string and only accidentally not a number. Quoted in the
        # template, so a 40-digit sha does not come back as an int.
        answers = provision.resolve(_flags(), provision.Console(tty=False))
        seeded = provision.product_seed(answers, "1" * 40)[".vellum/product.yaml"]
        self.assertEqual(yaml.safe_load(seeded)["pin"]["commit"], "1" * 40)


class APromptWithNoAnswerIsAnAnswerNotATraceback(ProvisionCase):
    """The prompts are the operator's only conversation with this command."""

    def setUp(self):
        super().setUp()
        self.without_gh()

    def _run(self, console, **overrides):
        values = dict(
            shape=None, product="acme", org="waviisoft", intent_repo=None,
            product_repo=None, visibility=None, intent_visibility=None,
            product_visibility=None, branch=None, areas=["billing"], docs=(),
            into=str(self.root / "o"), plan_only=False, yes=True,
        )
        values.update(overrides)
        return provision.run(str(self.cwd), console=console, out=io.StringIO(),
                             **values)

    def test_a_terminal_that_closes_mid_prompt_is_two_naming_the_flag(self):
        # `input()` raises EOFError at end of input. It reached the operator as
        # a traceback, which is the one thing a command that has just asked a
        # question must not answer with.
        def closed(_):
            raise EOFError

        console = provision.Console(tty=True, ask=closed, ask_secret=lambda _: "")
        with self.assertRaises(provision.ProvisionError) as raised:
            self._run(console)
        self.assertIn("--shape", str(raised.exception))
        self.assertIn("terminal closed", str(raised.exception))
        self.assertFalse((self.root / "o").exists())

    def test_an_answer_that_is_not_a_choice_re_asks_and_says_why(self):
        answers = iter(["mystery", "greenfield"])
        console = provision.Console(tty=True, ask=lambda _: next(answers),
                                    ask_secret=lambda _: "")
        errors = io.StringIO()
        stderr, sys.stderr = sys.stderr, errors
        try:
            code = self._run(console)
        finally:
            sys.stderr = stderr
        self.assertEqual(code, 0)
        self.assertIn("'mystery' is not one of", errors.getvalue())
        self.assertIn("greenfield", errors.getvalue())

    def test_a_checkout_path_that_is_a_file_is_two_and_not_a_traceback(self):
        # `iterdir()` on a file raises NotADirectoryError. A file where a
        # checkout should go is exactly what this refusal is about.
        into = self.root / "o"
        into.mkdir()
        (into / "acme-intent").write_text("not a directory\n", encoding="utf-8")
        code, out = run_cli([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--into", str(into), "--yes",
        ])
        self.assertEqual(code, 2, out)
        self.assertIn("already exists", out)

    def test_a_relative_into_is_resolved_before_anything_is_built(self):
        # Every path the report and the checklist print has to be one an
        # operator can paste from another directory.
        code, out, err = run_cli_streams([
            "init", "--shape", "greenfield", "--product", "acme", "--org", "waviisoft",
            "--area", "billing", "--into", "relative-out", "--yes",
        ])
        self.assertEqual(code, 0, out + err)
        self.assertIn(str(self.cwd.resolve() / "relative-out" / "acme-intent"), out)
        self.assertNotIn("--source relative-out", out)


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
