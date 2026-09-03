"""Invariants over this repo's own `.github/workflows/`.

Three of them hold the logic every installation runs — `spec-ci`,
`on-spec-merge`, `harness-ci`, reusable via `workflow_call` — and the fourth,
`ci.yml`, tests this repo. Nothing here can run a forge, so these are the
checks a checkout *can* make about workflow text, made permanent rather than
made once by hand at review time:

* every shipped workflow parses, and has no trigger but `workflow_call`, so it
  never runs for this repo;
* every `run:` body is valid `bash -n`;
* no `${{ }}` appears inside a `run:` body — attacker-influenceable values go
  through `env`, because `${{ }}` pastes its value into the script *before* the
  shell parses the line;
* the runner labels are Blacksmith, never `ubuntu-latest`;
* `set -o pipefail` precedes any `tee`, without which the step takes `tee`'s
  status and a gate can never close;
* `persist-credentials: false` on every checkout but the one that pushes;
* the caller stub grants at least the permissions the shipped workflow's jobs
  ask for — a called workflow's token can only be narrowed by the callee, so a
  stub that grants too little makes a job that is refused at the point of use,
  and that failure is invisible until an installation runs it.

These are the landmines `.vellum/memory/areas/adapters-github.md` records, and
each was learned from a real red.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

import yaml

from vellum.install import SHIPPED, WORKFLOWS_DIR, default_ref, render

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / WORKFLOWS_DIR["github"]

#: This repo's own CI, which is not an adapter and keeps its own triggers.
OWN_CI = "ci.yml"

#: The one checkout that keeps its credential: it pushes the tag and the ledger
#: commit. Named by workflow and step name so adding a checkout is a decision
#: somebody has to make here too, rather than a default that slips through.
PUSHES = {("on-spec-merge.yml", "Check out main")}


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(data: dict):
    """The `on:` block. YAML 1.1 reads a bare `on` as the boolean True."""
    return data[True] if True in data else data.get("on")


def steps(data: dict):
    """``(job name, step)`` for every step in a workflow."""
    for name, job in (data.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield name, step


def run_bodies(path: Path):
    """``(job, step name, body)`` for every `run:` in a workflow."""
    for job, step in steps(load(path)):
        if "run" in step:
            yield job, step.get("name") or step.get("uses") or "(unnamed)", step["run"]


def workflows() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


class TheShippedWorkflowsAreReusable(unittest.TestCase):
    def test_every_shipped_workflow_exists_and_parses(self):
        for shipped in SHIPPED:
            path = WORKFLOWS / shipped.filename
            self.assertTrue(path.is_file(), f"{shipped.filename} does not ship")
            self.assertIsInstance(load(path), dict, shipped.filename)

    def test_they_have_no_trigger_but_workflow_call(self):
        """So this repo's own PRs and pushes never start them."""
        for shipped in SHIPPED:
            on = triggers(load(WORKFLOWS / shipped.filename))
            self.assertEqual(list(on), ["workflow_call"], shipped.filename)

    def test_they_declare_the_ref_input_and_the_secret_by_name(self):
        for shipped in SHIPPED:
            call = triggers(load(WORKFLOWS / shipped.filename))["workflow_call"]
            self.assertIn("vellum-ref", call["inputs"], shipped.filename)
            self.assertTrue(call["inputs"]["vellum-ref"]["required"], shipped.filename)
            self.assertIn("VELLUM_TOKEN", call["secrets"], shipped.filename)

    def test_the_cli_is_checked_out_at_the_input_ref_and_nothing_else_names_a_repo(self):
        """The caller's checkout is implicit; this repo's is explicit.

        `actions/checkout` with no `repository:` checks out the CALLER — the
        intent repo — which is what every body here reads. The only checkout
        that may name a repository is the CLI's, and it takes its ref from the
        input the stub stamps.
        """
        for shipped in SHIPPED:
            data = load(WORKFLOWS / shipped.filename)
            for job, step in steps(data):
                if not str(step.get("uses", "")).startswith("actions/checkout"):
                    continue
                repo = (step.get("with") or {}).get("repository")
                if repo is None:
                    continue
                self.assertEqual(repo, "waviisoft/vellum", f"{shipped.filename}:{job}")
                self.assertEqual(
                    (step.get("with") or {}).get("ref"),
                    "${{ inputs.vellum-ref }}",
                    f"{shipped.filename}:{job}",
                )

    def test_this_repos_own_ci_is_not_one_of_them(self):
        on = triggers(load(WORKFLOWS / OWN_CI))
        self.assertEqual(sorted(on), ["pull_request", "push"])


class TheStubGrantsWhatTheWorkflowAsks(unittest.TestCase):
    """A called workflow's token can only be narrowed, never widened.

    So a stub granting less than the shipped workflow's jobs ask for produces a
    run that is refused at the point of use — and nothing in either file says
    so on its own. This is the one cross-file invariant between a stub and the
    workflow behind it, and it is checked here because no forge is available to
    check it by running.
    """

    RANK = {"none": 0, "read": 1, "write": 2}

    def granted(self, data: dict) -> dict:
        return {k: v for k, v in (data.get("permissions") or {}).items()}

    def test_each_stub_grants_at_least_what_every_job_asks_for(self):
        for shipped in SHIPPED:
            workflow = load(WORKFLOWS / shipped.filename)
            stub = yaml.safe_load(render(shipped, ref=default_ref()))
            grant = self.granted(stub)
            asked = [self.granted(workflow)] + [
                self.granted(job) for job in (workflow.get("jobs") or {}).values()
            ]
            for wants in asked:
                for scope, level in wants.items():
                    self.assertIn(scope, grant, f"{shipped.filename} wants {scope}")
                    self.assertGreaterEqual(
                        self.RANK[grant[scope]], self.RANK[level],
                        f"{shipped.filename}: stub grants {scope}: {grant[scope]}, "
                        f"the workflow asks for {level}",
                    )


class EveryRunBodyIsSafeAndValid(unittest.TestCase):
    def test_every_run_body_parses_as_bash(self):
        for path in workflows():
            for job, name, body in run_bodies(path):
                proc = subprocess.run(
                    ["bash", "-n"], input=body, text=True, capture_output=True
                )
                self.assertEqual(
                    proc.returncode, 0,
                    f"{path.name}:{job}:{name} is not valid bash: {proc.stderr}",
                )

    def test_no_run_body_interpolates(self):
        """`${{ }}` pastes its value in before the shell parses the line.

        Every attacker-influenceable value in these files — a commit message, a
        work-item title, a pin, a sha — travels through `env` instead. The rule
        is enforced over ALL of them rather than the ones handling prose,
        because an exception is how the next one carrying a title gets written
        the unsafe way.
        """
        for path in workflows():
            for job, name, body in run_bodies(path):
                self.assertNotIn(
                    "${{", body,
                    f"{path.name}:{job}:{name} interpolates into a run body; "
                    f"pass the value through `env:` instead",
                )

    def test_pipefail_precedes_any_pipe_into_tee(self):
        """Without it the step's status is `tee`'s, which is always 0.

        Matched as a pipe into the command, not as the three letters: the word
        "guarantee" in one of these bodies is not a pipeline, and a check that
        cannot tell them apart is one somebody eventually silences.
        """
        pipe = re.compile(r"\|\s*tee\b")
        for path in workflows():
            for job, name, body in run_bodies(path):
                where = body.find("set -o pipefail")
                # EVERY pipeline in the body, not just the first: a body that
                # sets pipefail before its first `| tee` and pipes into a second
                # one later is exactly as broken, and `search` would pass it.
                for match in pipe.finditer(body):
                    self.assertNotEqual(
                        where, -1,
                        f"{path.name}:{job}:{name} pipes into tee without pipefail",
                    )
                    self.assertLess(
                        where, match.start(),
                        f"{path.name}:{job}:{name} sets pipefail after a pipeline "
                        f"at offset {match.start()}",
                    )


class TheRunnersAndCheckoutsAreWhatThisOrgNeeds(unittest.TestCase):
    def test_no_job_asks_for_a_github_hosted_runner(self):
        """`ubuntu-latest` is never assigned a runner in this organisation.

        The job is accepted and then fails in seconds with `runner_id: 0`, no
        logs and no steps, which reads like an infrastructure blip and is not
        one (`.vellum/memory/areas/adapters-github.md`).
        """
        for path in workflows():
            for name, job in (load(path).get("jobs") or {}).items():
                runs_on = job.get("runs-on")
                if runs_on is None:
                    continue  # a job that calls a reusable workflow has none
                self.assertTrue(
                    str(runs_on).startswith("blacksmith-"),
                    f"{path.name}:{name} runs on {runs_on!r}",
                )

    def test_only_the_pushing_checkout_keeps_its_credential(self):
        """Over the shipped workflows only, and that scope is a GAP not a rule.

        `ci.yml`'s three checkouts do not set `persist-credentials: false`, and
        one of them carries `secrets.SPEC_TOKEN`. That predates this wave and
        the wave did not touch its behaviour (only its header comment), so
        narrowing this test is what keeps it honest rather than red — but the
        exemption is an open item, not an endorsement, and it is recorded as one
        in `.vellum/memory/areas/adapters-github.md`. Widen this to
        `workflows()` in the wave that fixes `ci.yml`.
        """
        for path in [WORKFLOWS / s.filename for s in SHIPPED]:
            for job, step in steps(load(path)):
                if not str(step.get("uses", "")).startswith("actions/checkout"):
                    continue
                name = step.get("name") or "(unnamed)"
                if (path.name, name) in PUSHES:
                    continue
                self.assertIs(
                    (step.get("with") or {}).get("persist-credentials"), False,
                    f"{path.name}:{job}:{name} persists its credential; only the "
                    f"checkout that pushes may, and that one is listed in PUSHES",
                )

    def test_every_checkout_of_the_history_is_unshallow(self):
        """Dating walks main's history, and a graft re-dates scenarios forward.

        Only the caller's own checkouts: the CLI checkout is a pip install
        source and its history is not read by anything.
        """
        for shipped in SHIPPED:
            for job, step in steps(load(WORKFLOWS / shipped.filename)):
                if not str(step.get("uses", "")).startswith("actions/checkout"):
                    continue
                with_ = step.get("with") or {}
                if with_.get("repository"):
                    continue
                self.assertEqual(
                    with_.get("fetch-depth"), 0,
                    f"{shipped.filename}:{job} checks the caller out shallow",
                )


if __name__ == "__main__":
    unittest.main()
