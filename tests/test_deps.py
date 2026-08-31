"""``vellum verify deps``: which registry each declared dependency resolves to.

The scenario is ``@id:unlisted-registry-fails`` in
``spec/behaviors/security.md``: a PR adding a dependency from a registry not in
the policy fails with a supply-chain finding. The fixture below is the one the
intent repo's harness builds for that scenario, verbatim — ``requirements.txt``
gaining ``sandbox-widget @ https://packages.example.invalid/...``.
"""

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from support import INTENT_ENV, make_intent_repo, run_cli
from vellum.deps import (
    DEFAULT_INDEX,
    DependencyError,
    _from_parsed,
    _scan_toml_arrays,
    check,
    host_of,
    registries,
)

POLICY = """version_prefix: spec-v
budgets:
  divergence_cap: 3
dependency_policy:
  registries: [{registries}]
  lockfile_required: true
"""

#: A `[dependency-groups]` array mixing a requirement string with a PEP 735
#: `{include-group = "..."}` entry. Valid TOML that `tomllib` parses happily,
#: which is the whole point: the reader that had the real parser was the one
#: that dropped the array — the unlisted host in it included — and exited 0,
#: while the 3.10 fallback refused the same file. Both readers refuse it now.
MIXED_DEPENDENCY_GROUP = """[dependency-groups]
test = ["evil @ https://evil.invalid/x.tar.gz", {include-group = "base"}]
base = ["listed"]
"""


class DepsCase(unittest.TestCase):
    def setUp(self):
        # `run_cli` calls `main()` in-process and `_verify` reads
        # VELLUM_INTENT_REPO at call time, so a test that changed it would leak
        # into every module discovered after it — the failure that made the
        # conformance job green and hollow once already (see the CLI area note).
        # `patch.dict` restores the whole environment; `addCleanup(pop)` would
        # delete a variable the conformance job set.
        before = dict(os.environ)
        self.addCleanup(lambda: self.assertEqual(dict(os.environ), before))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.intent = make_intent_repo(self.root / "intent")
        self.policy("pypi.org")
        self.product = self.root / "app"
        self.product.mkdir(parents=True)

    def policy(self, *allowed):
        (self.intent / ".vellum" / "config.yaml").write_text(
            POLICY.format(registries=", ".join(allowed)), encoding="utf-8"
        )

    def manifest(self, name, text):
        (self.product / name).write_text(text, encoding="utf-8")

    def check(self, **kwargs):
        return check(self.product, self.intent, **kwargs)


class TestTheScenario(DepsCase):
    def test_the_allowed_baseline_passes(self):
        self.manifest("requirements.txt", "PyYAML>=6.0,<7\n")
        result = self.check()
        self.assertFalse(result.violated)
        self.assertEqual(result.requirements[0].registry, "pypi.org")

    def test_a_dependency_from_an_unlisted_registry_fails(self):
        # The harness fixture for @id:unlisted-registry-fails, verbatim.
        self.manifest(
            "requirements.txt",
            "PyYAML>=6.0,<7\n"
            "sandbox-widget @ https://packages.example.invalid/sandbox-widget-1.0.tar.gz\n",
        )
        result = self.check()
        self.assertTrue(result.violated)
        self.assertEqual([r.registry for r in result.offending], ["packages.example.invalid"])

    def test_the_cli_exits_one_with_a_supply_chain_finding(self):
        self.manifest(
            "requirements.txt",
            "sandbox-widget @ https://packages.example.invalid/sandbox-widget-1.0.tar.gz\n",
        )
        code, out = run_cli(
            ["verify", "deps", str(self.product), "--intent", str(self.intent)]
        )
        self.assertEqual(code, 1)
        self.assertIn("supply chain", out)
        self.assertIn("packages.example.invalid", out)

    def test_the_governed_products_own_dependencies_pass_the_live_policy(self):
        # `dependency_policy.registries` was `[npmjs.org]` while the product it
        # governs is a Python package — harness/NOTES.md finding 2. It is
        # `[pypi.org]` now, and the guard has to agree that this repo's own two
        # runtime dependencies are admitted, or its first real run is a false red.
        self.manifest("requirements.txt", "PyYAML>=6.0,<7\ngherkin-official>=29,<43\n")
        self.assertFalse(self.check().violated)


class TestHowARegistryIsDecided(DepsCase):
    def test_a_plain_requirement_resolves_to_the_default_index(self):
        self.manifest("requirements.txt", "requests\n")
        self.assertEqual(self.check().requirements[0].registry, DEFAULT_INDEX)

    def test_index_url_replaces_the_default_for_the_whole_file(self):
        self.manifest(
            "requirements.txt",
            "--index-url https://internal.example.invalid/simple\nrequests\n",
        )
        result = self.check()
        self.assertTrue(result.violated)
        self.assertEqual(
            {r.registry for r in result.requirements}, {"internal.example.invalid"}
        )

    def test_an_index_url_declared_after_a_requirement_still_serves_it(self):
        # pip applies the option to the file, not to the lines below it, so
        # attribution is redone once the whole file's options are known.
        self.manifest(
            "requirements.txt",
            "requests\n--index-url https://internal.example.invalid/simple\n",
        )
        self.assertEqual(
            {r.registry for r in self.check().requirements}, {"internal.example.invalid"}
        )

    def test_a_value_containing_its_own_equals_sign_is_not_cut_in_half(self):
        self.manifest(
            "requirements.txt",
            "--index-url https://internal.example.invalid/simple?token=abc\nrequests\n",
        )
        self.assertEqual(
            {r.registry for r in self.check().requirements}, {"internal.example.invalid"}
        )

    def test_an_index_url_written_with_an_equals_sign_is_read(self):
        self.manifest(
            "requirements.txt", "--index-url=https://internal.example.invalid/simple\nrequests\n"
        )
        self.assertEqual(
            {r.registry for r in self.check().requirements}, {"internal.example.invalid"}
        )

    def test_an_extra_index_url_is_a_registry_in_use(self):
        # Every plain requirement in the file may be served from it, so it is
        # counted rather than treated as decoration.
        self.manifest(
            "requirements.txt",
            "--extra-index-url https://mirror.example.invalid/simple\nrequests\n",
        )
        result = self.check()
        self.assertTrue(result.violated)
        self.assertIn("mirror.example.invalid", {r.registry for r in result.offending})

    def test_a_vcs_requirement_resolves_to_its_transport_host(self):
        self.manifest("requirements.txt", "thing @ git+https://github.com/o/r@main\n")
        self.assertEqual(self.check().requirements[0].registry, "github.com")

    def test_a_local_path_resolves_to_no_registry_and_is_not_a_finding(self):
        self.manifest("requirements.txt", "-e .\n./vendor/thing\n")
        result = self.check()
        self.assertEqual([r.registry for r in result.requirements], [None, None])
        self.assertFalse(result.violated)

    def test_a_file_url_resolves_to_no_registry(self):
        self.manifest("requirements.txt", "thing @ file:///opt/wheels/thing.whl\n")
        self.assertFalse(self.check().violated)

    def test_comments_and_continuations_are_not_requirements(self):
        self.manifest(
            "requirements.txt",
            "# a comment\n"
            "requests  # trailing\n"
            "urllib3 \\\n    >=2\n",
        )
        texts = [r.text for r in self.check().requirements]
        self.assertEqual(texts, ["requests", "urllib3     >=2"])

    def test_an_egg_fragment_is_not_read_as_a_comment(self):
        self.manifest(
            "requirements.txt",
            "thing @ https://packages.example.invalid/t.tar.gz#egg=thing\n",
        )
        result = self.check()
        self.assertEqual(result.requirements[0].registry, "packages.example.invalid")


class TestHostComparison(DepsCase):
    """An allowlist compared by substring is an allowlist that is not one."""

    def test_a_host_that_merely_contains_an_allowed_one_is_unlisted(self):
        self.manifest("requirements.txt", "t @ https://pypi.org.evil.invalid/t.tar.gz\n")
        self.assertTrue(self.check().violated)

    def test_userinfo_shaped_like_an_allowed_host_does_not_launder_one(self):
        # `https://pypi.org@evil.invalid/simple` is a request to evil.invalid.
        self.assertEqual(host_of("https://pypi.org@evil.invalid/simple"), "evil.invalid")
        self.manifest("requirements.txt", "t @ https://pypi.org@evil.invalid/t.tar.gz\n")
        self.assertTrue(self.check().violated)

    def test_a_port_and_case_do_not_change_the_host(self):
        self.policy("Internal.Example.Invalid")
        self.manifest("requirements.txt", "t @ https://internal.example.invalid:8443/t.tar.gz\n")
        self.assertFalse(self.check().violated)

    def test_a_policy_entry_may_be_written_as_an_index_url(self):
        self.policy("https://pypi.org/simple")
        self.assertEqual(registries(self.intent), ["pypi.org"])


class TestReadingPyproject(DepsCase):
    PYPROJECT = """[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "app"
version = "0.1.0"
dependencies = [
    "PyYAML>=6.0,<7",
    # a comment inside the array
    "widget @ https://packages.example.invalid/widget-1.0.tar.gz",
]

[project.optional-dependencies]
dev = ["pytest>=8"]
"""

    def test_every_dependency_table_is_read(self):
        self.manifest("pyproject.toml", self.PYPROJECT)
        found = {r.text for r in self.check().requirements}
        self.assertEqual(
            found,
            {"setuptools>=61", "PyYAML>=6.0,<7", "pytest>=8",
             "widget @ https://packages.example.invalid/widget-1.0.tar.gz"},
        )

    def test_an_unlisted_registry_in_pyproject_is_a_finding(self):
        self.manifest("pyproject.toml", self.PYPROJECT)
        self.assertEqual(
            [r.registry for r in self.check().offending], ["packages.example.invalid"]
        )

    def test_a_dev_requirements_file_is_read_too(self):
        # A dev dependency is executed on a machine holding credentials just as
        # a runtime one is, so the default glob is `requirements*.txt`.
        self.manifest("requirements.txt", "PyYAML>=6.0,<7\n")
        self.manifest("requirements-dev.txt", "t @ https://packages.example.invalid/t.tar.gz\n")
        self.assertTrue(self.check().violated)

    def test_manifest_overrides_the_default_globs(self):
        self.manifest("requirements.txt", "PyYAML>=6.0,<7\n")
        self.manifest("requirements-dev.txt", "t @ https://packages.example.invalid/t.tar.gz\n")
        self.assertFalse(self.check(patterns=["requirements.txt"]).violated)

    def test_a_manifest_named_twice_is_read_once(self):
        self.manifest("requirements.txt", "PyYAML>=6.0,<7\n")
        result = self.check(patterns=["requirements.txt", "requirements*.txt"])
        self.assertEqual(len(result.requirements), 1)


@unittest.skipIf(sys.version_info < (3, 11), "tomllib arrived in 3.11")
class TestTheTomlFallbackAgreesWithTheRealParser(DepsCase):
    """The 3.10 reader is checked against the parser it stands in for.

    A fallback that finds *fewer* dependencies than the file declares is the one
    dangerous outcome for this guard, and nothing on 3.11 would notice — which
    is exactly why the agreement is asserted here rather than left to a 3.10
    run to discover.
    """

    def agree(self, text):
        import tomllib

        self.assertEqual(
            _scan_toml_arrays(text, "pyproject.toml"), _from_parsed(tomllib.loads(text))
        )

    def agree_refuses(self, text):
        """Both readers refuse *text*, rather than one of them answering.

        The other half of agreement, and the half that was missing: two readers
        that return the same dict on every input either can read still disagree
        if one of them *drops* what it cannot classify and the other raises.
        ``agree`` above cannot see that, because the input it feeds is input
        both readers accept.
        """
        import tomllib

        with self.assertRaises(DependencyError):
            _scan_toml_arrays(text, "pyproject.toml")
        with self.assertRaises(DependencyError):
            _from_parsed(tomllib.loads(text), "pyproject.toml")

    def test_on_this_repos_own_pyproject(self):
        self.agree(Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text())

    def test_on_a_fixture_using_every_table(self):
        self.agree(TestReadingPyproject.PYPROJECT)

    def test_on_a_literal_string_which_processes_no_escapes(self):
        self.agree("[project]\ndependencies = ['a\\\\b']\n")

    def test_on_a_non_ascii_value(self):
        # `unicode_escape` round-trips non-ASCII through latin-1, so a value
        # decoded unconditionally comes back mangled. Only a basic string
        # carrying a backslash is decoded at all.
        self.agree('[project]\ndependencies = ["pakket-\u00e9\u00e9n>=1"]\n')

    def test_on_a_single_line_array(self):
        self.agree('[project]\ndependencies = ["a", "b"]\n')

    def test_on_an_empty_array(self):
        self.agree("[project]\ndependencies = []\n")

    def test_a_dependencies_key_outside_a_dependency_table_is_ignored_by_both(self):
        self.agree('[tool.other]\ndependencies = ["not-a-dependency"]\n')

    def test_a_mixed_dependency_group_is_refused_by_both(self):
        # The input that showed the two readers disagreeing in the dangerous
        # direction. `{include-group = "..."}` is valid PEP 735 and tomllib
        # parses this file without complaint, so the real parser handed back an
        # array that `_from_parsed` then dropped *whole* — the string
        # requirement beside it included — while the fallback raised.
        self.agree_refuses(MIXED_DEPENDENCY_GROUP)

    def test_a_non_string_in_an_every_key_table_is_refused_by_both(self):
        # `project.optional-dependencies` is the other table where every key is
        # a dependency list, so every key is held to the same rule.
        self.agree_refuses('[project.optional-dependencies]\ndev = ["pytest", 7]\n')

    def test_a_non_string_in_a_named_key_table_is_refused_by_both(self):
        self.agree_refuses('[build-system]\nrequires = ["setuptools", false]\n')

    def test_a_non_array_value_is_ignored_by_both_rather_than_refused(self):
        # The rule is about an array's *contents*. A cared-about table may hold
        # ordinary scalar keys — `[project]` always holds `name` and `version` —
        # and the fallback simply does not match those lines, so neither reader
        # may treat one as a dependency declaration it failed to read.
        self.agree('[project]\nname = "app"\nversion = "0.1.0"\n')

    def test_the_refusal_names_the_requirements_it_will_not_report_alone(self):
        # Keeping the string elements is what makes the refusal actionable:
        # they are the requirements a silent drop would have hidden, so the
        # operator has to see them — here, the unlisted host itself.
        import tomllib

        with self.assertRaises(DependencyError) as caught:
            _from_parsed(tomllib.loads(MIXED_DEPENDENCY_GROUP), "pyproject.toml")
        self.assertIn("evil.invalid", str(caught.exception))
        self.assertIn("include-group", str(caught.exception))


class TestAMixedDependencyGroupEndToEnd(DepsCase):
    """The whole command, on the tree that made the default interpreter fail open.

    Deliberately not skipped below 3.11. Both readers refuse this file now, so
    the assertion is the same on every interpreter — and a regression that put
    the drop back on the 3.11 path would show up on the interpreter that
    actually runs this guard, rather than only in the fallback-agreement class
    that 3.10 skips.
    """

    def test_it_is_refused_rather_than_answered(self):
        self.manifest("pyproject.toml", MIXED_DEPENDENCY_GROUP)
        code, _ = run_cli(
            ["verify", "deps", str(self.product), "--intent", str(self.intent)]
        )
        # 2, not 1: the guard could not read the manifest, which is "no answer".
        # Not 0, which is what it used to give while never seeing evil.invalid.
        self.assertEqual(code, 2)

    def test_the_refusal_names_the_unlisted_host_it_would_have_dropped(self):
        self.manifest("pyproject.toml", MIXED_DEPENDENCY_GROUP)
        _, out = run_cli(
            ["verify", "deps", str(self.product), "--intent", str(self.intent)]
        )
        self.assertIn("evil.invalid", out)

    def test_check_raises_rather_than_reporting_a_shorter_answer(self):
        self.manifest("pyproject.toml", MIXED_DEPENDENCY_GROUP)
        with self.assertRaises(DependencyError):
            self.check()

    def test_the_same_file_with_the_group_spelled_as_strings_is_read_whole(self):
        # The requirement is not "refuse dependency-groups"; it is "never
        # report fewer than the file declares". Written as strings throughout,
        # both entries are found and the unlisted host is the finding it
        # always should have been.
        self.manifest(
            "pyproject.toml",
            '[dependency-groups]\n'
            'test = ["evil @ https://evil.invalid/x.tar.gz"]\n'
            'base = ["listed"]\n',
        )
        result = self.check()
        self.assertEqual(
            {r.text for r in result.requirements},
            {"evil @ https://evil.invalid/x.tar.gz", "listed"},
        )
        self.assertEqual([r.registry for r in result.offending], ["evil.invalid"])


class TestTheTomlFallbackRefusesRatherThanUnderReporting(unittest.TestCase):
    def test_an_array_it_cannot_read_raises(self):
        # A dependency expressed as something other than a string literal: the
        # fallback must not skip it into silence.
        with self.assertRaises(DependencyError):
            _scan_toml_arrays('[project]\ndependencies = [{ name = "x" }]\n', "pyproject.toml")

    def test_an_unterminated_array_raises(self):
        with self.assertRaises(DependencyError):
            _scan_toml_arrays('[project]\ndependencies = [\n  "a",\n', "pyproject.toml")

    def test_a_multi_line_array_with_comments_is_read_whole(self):
        found = _scan_toml_arrays(
            '[project]\ndependencies = [\n  # why\n  "a>=1",\n  "b",\n]\n', "pyproject.toml"
        )
        self.assertEqual(found, {"project.dependencies": ["a>=1", "b"]})


class TestFollowingIncludes(DepsCase):
    def test_a_requirements_file_including_another_is_followed(self):
        self.manifest("requirements.txt", "-r base.txt\n")
        self.manifest("base.txt", "t @ https://packages.example.invalid/t.tar.gz\n")
        self.assertTrue(self.check().violated)

    def test_an_include_leaving_the_checkout_is_refused(self):
        # The path is text out of the repository, so following it unguarded
        # makes this guard a file-read primitive aimed by whoever writes the
        # manifest.
        self.manifest("requirements.txt", "-r ../../../etc/passwd\n")
        with self.assertRaises(DependencyError) as caught:
            self.check()
        self.assertIn("leaves the checkout", str(caught.exception))

    def test_the_cli_exits_two_for_it(self):
        self.manifest("requirements.txt", "-r ../../../etc/passwd\n")
        code, out = run_cli(
            ["verify", "deps", str(self.product), "--intent", str(self.intent)]
        )
        self.assertEqual(code, 2)
        self.assertIn("leaves the checkout", out)

    def test_a_cycle_of_includes_terminates(self):
        self.manifest("requirements.txt", "-r other.txt\n")
        self.manifest("other.txt", "-r requirements.txt\nrequests\n")
        self.assertEqual([r.text for r in self.check().requirements], ["requests"])


class TestThePolicy(DepsCase):
    def test_a_missing_dependency_policy_is_an_error_not_a_default(self):
        # "Everything is allowed" is not a policy, and a policy that disappears
        # when its key is misspelled is not one either.
        (self.intent / ".vellum" / "config.yaml").write_text(
            "budgets:\n  divergence_cap: 3\n", encoding="utf-8"
        )
        self.manifest("requirements.txt", "t @ https://packages.example.invalid/t.tar.gz\n")
        with self.assertRaises(DependencyError) as caught:
            self.check()
        self.assertIn("dependency_policy.registries", str(caught.exception))

    def test_an_empty_registry_list_is_refused(self):
        self.policy()
        with self.assertRaises(DependencyError):
            registries(self.intent)

    def test_an_entry_naming_no_host_is_refused_rather_than_dropped(self):
        # Dropping it shortens the allowlist, and a shorter allowlist fails
        # closed — sending a reviewer hunting a supply-chain finding that is
        # really a typo in policy.
        self.policy('""')
        with self.assertRaises(DependencyError):
            registries(self.intent)

    def test_a_missing_config_cannot_be_answered(self):
        with self.assertRaises(DependencyError):
            check(self.product, self.root / "nowhere")

    def test_the_cli_needs_an_intent_checkout(self):
        # With nothing to point at, the refusal is `pin advance`'s: the policy
        # is installation policy and only the intent repo carries it.
        with unittest.mock.patch.dict("os.environ"):
            os.environ.pop(INTENT_ENV, None)
            code, out = run_cli(["verify", "deps", str(self.product)])
        self.assertEqual(code, 2)
        self.assertIn("VELLUM_INTENT_REPO", out)

    def test_the_environment_names_the_intent_checkout(self):
        self.manifest("requirements.txt", "PyYAML>=6.0,<7\n")
        with unittest.mock.patch.dict(
            "os.environ", {"VELLUM_INTENT_REPO": str(self.intent)}
        ):
            code, _ = run_cli(["verify", "deps", str(self.product)])
        self.assertEqual(code, 0)


class TestTheReport(DepsCase):
    def test_a_passing_run_names_the_allowlist_it_used(self):
        self.manifest("requirements.txt", "PyYAML>=6.0,<7\n")
        code, out = run_cli(
            ["verify", "deps", str(self.product), "--intent", str(self.intent)]
        )
        self.assertEqual(code, 0)
        self.assertIn("allowed registries: pypi.org", out)

    def test_a_checkout_with_no_manifests_says_so(self):
        self.assertIn("(none found)", self.check().report())


if __name__ == "__main__":
    unittest.main()
