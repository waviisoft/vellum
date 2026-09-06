"""``vellum release tag``: the name a version mints, computed and never applied.

``spec/features/release-tags.md``, four scenarios. Three of them are about what
the command *says* — the name, the commit, the word "used", the refusal that
names the changelog — and one is about what it does not do, which is anything at
all. That last one gets the most instruments here for the reason the acceptance
fixture gives it three: a command whose whole subject is a *tag* can write
nothing into a working tree and still move a name, so the file bytes, the ref
table and ``HEAD`` are all compared across a run.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import git, run_cli, write_product  # noqa: E402

from vellum import tag  # noqa: E402
from vellum.gitver import ref_format_ok  # noqa: E402

#: The version a fixture checkout is at, and the tag its forge already minted.
USED = "0.4.0"
#: The version a bump declares, whose name nothing has minted.
NEW = "0.5.0"


def declare(root: Path, *, source: str = "VERSION", changelog: str | None = None) -> None:
    """Append the `release:` block to a product file that has none."""
    path = root / ".vellum" / "product.yaml"
    block = f"\nrelease:\n  version_source: {source}\n"
    if changelog is not None:
        block += f"  changelog: {changelog}\n"
    path.write_text(path.read_text(encoding="utf-8") + block, encoding="utf-8")


class TagCase(unittest.TestCase):
    def setUp(self):
        self.root = self.fresh()

    def fresh(self) -> Path:
        """A new temporary directory, cleaned up when the test ends.

        A method rather than one directory per test because two of the cases
        below are `subTest` loops over whole fixtures, and reassigning one
        `TemporaryDirectory` over another leaks the first — which surfaces as a
        `ResourceWarning` in the middle of an otherwise quiet run.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def product(self, version: str = USED, **kwargs) -> Path:
        """A committed product checkout declaring *version* in a `VERSION` file.

        Deliberately not `pyproject.toml`: the third kind of source needs no
        parser, so what these tests grade is the tagging rather than a TOML
        reader — which has tests of its own below, where it is the subject.
        """
        checkout = self.root / "product"
        checkout.mkdir(parents=True, exist_ok=True)
        git(checkout, "init", "-q", "-b", "main", ".")
        write_product(checkout)
        declare(checkout, **kwargs)
        (checkout / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-qm", "the product begins")
        return checkout

    def declare_source(self, checkout: Path, source: str) -> None:
        """Point an existing declaration at *source*, quoted so YAML keeps it."""
        path = checkout / ".vellum" / "product.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "version_source: VERSION", f'version_source: "{source}"'
            ),
            encoding="utf-8",
        )

    def refs(self, repo: Path) -> dict[str, str]:
        listed = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split() for line in listed.splitlines() if line.strip())

    def bytes_under(self, repo: Path) -> dict[str, bytes]:
        return {
            str(p.relative_to(repo)): p.read_bytes()
            for p in sorted(repo.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(repo).parts
        }


class ItNamesTheTagAndTheCommit(TagCase):
    """@id:release-tag-is-minted-on-the-default-branch"""

    def test_an_unused_name_is_reported_with_the_commit_it_would_name(self):
        checkout = self.product(NEW)
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn(f"v{NEW}", out)
        head = git(checkout, "rev-parse", "HEAD").strip()
        # The operator is told a name AND what it would name. A report with one
        # and not the other is a name nobody can check.
        self.assertIn(head[:12], out)

    def test_plan_is_the_same_answer(self):
        checkout = self.product(NEW)
        plain = run_cli(["release", "tag", str(checkout)])
        planned = run_cli(["release", "tag", str(checkout), "--plan"])
        self.assertEqual(plain, planned)

    def test_it_names_no_tag_but_the_one_it_would_mint(self):
        # A report that also listed the names already in the repository would
        # make "the tag this version mints" the reader's inference rather than
        # the command's answer.
        checkout = self.product(NEW)
        git(checkout, "tag", f"v{USED}")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertNotIn(f"v{USED}", out)

    def test_the_json_answer_carries_the_same_facts(self):
        checkout = self.product(NEW)
        code, out = run_cli(["release", "tag", str(checkout), "--json"])
        self.assertEqual(code, 0, out)
        answer = json.loads(out)
        self.assertEqual(answer["tag"], f"v{NEW}")
        self.assertEqual(answer["version"], NEW)
        self.assertEqual(answer["used"], False)
        self.assertEqual(answer["commit"], git(checkout, "rev-parse", "HEAD").strip())


class AUsedNameIsLeftAlone(TagCase):
    """@id:release-tag-leaves-a-used-name-alone"""

    def test_it_says_the_name_is_used_and_exits_zero(self):
        checkout = self.product(USED)
        git(checkout, "tag", f"v{USED}")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn(f"v{USED}", out)
        # The spec's own word for what happened to it. Matching on the stable
        # half of the sentence rather than a whole rendering: a reworded
        # parenthesis must not be a red.
        self.assertIn("used", out.lower())

    def test_nothing_is_written_by_either_answer(self):
        # Three instruments and none is redundant: the file bytes are what an
        # operator sees as a diff, the ref table is what says a TAG did not
        # move — the one thing this command could write without touching a
        # working tree — and HEAD is what a commit would have moved.
        for version, tagged in ((USED, True), (NEW, False)):
            with self.subTest(version=version):
                self.root = self.fresh()
                checkout = self.product(version)
                git(checkout, "tag", f"v{USED}")
                before, refs = self.bytes_under(checkout), self.refs(checkout)
                head = git(checkout, "rev-parse", "HEAD").strip()
                code, out = run_cli(["release", "tag", str(checkout)])
                self.assertEqual(code, 0, out)
                self.assertEqual(self.bytes_under(checkout), before)
                self.assertEqual(self.refs(checkout), refs)
                self.assertEqual(git(checkout, "rev-parse", "HEAD").strip(), head)
                # The control: one run took the used-name answer and one took
                # the would-mint answer, so "nothing written" is a claim about
                # both branches rather than twice about the same one.
                self.assertIs(tagged, f"refs/tags/v{version}" in refs)


class AMissingChangelogEntryIsARefusal(TagCase):
    """@id:release-tag-refuses-a-missing-changelog-entry"""

    def changelog(self, checkout: Path, text: str) -> None:
        (checkout / "CHANGELOG.md").write_text(text, encoding="utf-8")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-qm", "the changelog")

    def test_it_exits_one_naming_the_file_and_the_entry(self):
        checkout = self.product(NEW, changelog="CHANGELOG.md")
        # An entry that IS there, so the refusal is about the version rather
        # than about a changelog that is merely empty.
        self.changelog(checkout, f"# Changelog\n\n## v{USED}\n\n- the one before.\n")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("CHANGELOG.md", out)
        self.assertIn(f"v{NEW}", out)

    def test_no_tag_is_created_by_the_refusal(self):
        checkout = self.product(NEW, changelog="CHANGELOG.md")
        self.changelog(checkout, f"# Changelog\n\n## v{USED}\n\n- the one before.\n")
        git(checkout, "tag", f"v{USED}")
        refs = self.refs(checkout)
        self.assertEqual(run_cli(["release", "tag", str(checkout)])[0], 1)
        self.assertEqual(self.refs(checkout), refs)

    def test_either_spelling_of_the_entry_satisfies_it(self):
        # An entry may be headed `v0.5.0` or `0.5.0`, and refusing the second
        # would fail a project whose changelog has always been written that way.
        for heading in (f"## v{NEW}", f"## {NEW}"):
            with self.subTest(heading=heading):
                self.root = self.fresh()
                checkout = self.product(NEW, changelog="CHANGELOG.md")
                self.changelog(checkout, f"# Changelog\n\n{heading}\n\n- this one.\n")
                code, out = run_cli(["release", "tag", str(checkout)])
                self.assertEqual(code, 0, out)

    def test_an_unreadable_changelog_is_two_not_one(self):
        # "No answer", not "the release is undescribed". The `release-cut`
        # workflow fails the job on 1 by naming the changelog to write, and it
        # must not tell an operator to write an entry into a file this could
        # not open.
        checkout = self.product(NEW, changelog="CHANGELOG.md")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("CHANGELOG.md", out)


class ARepoThatHasNotDeclaredIsNotAnswered(TagCase):
    """Exit 2: the command cannot say what the tag would be, and says so."""

    def test_no_release_block_is_two(self):
        checkout = self.root / "product"
        checkout.mkdir(parents=True)
        git(checkout, "init", "-q", "-b", "main", ".")
        write_product(checkout)
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("release:", out)

    def test_a_checkout_that_is_not_a_product_checkout_is_two(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        code, out = run_cli(["release", "tag", str(elsewhere)])
        self.assertEqual(code, 2, out)
        self.assertIn("product.yaml", out)

    def test_a_missing_directory_is_two(self):
        code, out = run_cli(["release", "tag", str(self.root / "nowhere")])
        self.assertEqual(code, 2, out)

    def test_an_absent_version_source_is_two(self):
        checkout = self.product(USED)
        (checkout / "VERSION").unlink()
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("VERSION", out)

    def test_a_version_the_source_cannot_yield_is_two(self):
        # "a version the source cannot yield is exit 2". A `version_source`
        # pointing at prose would otherwise turn a paragraph into a ref name.
        checkout = self.product(USED)
        (checkout / "VERSION").write_text("not a version at all\n", encoding="utf-8")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)

    def test_a_bare_integer_is_not_a_dotted_version(self):
        checkout = self.product(USED)
        (checkout / "VERSION").write_text("7\n", encoding="utf-8")
        self.assertEqual(run_cli(["release", "tag", str(checkout)])[0], 2)

    def test_a_pre_release_suffix_is_a_version_somebody_chose(self):
        checkout = self.product(USED)
        (checkout / "VERSION").write_text("2.0.0-rc.1\n", encoding="utf-8")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("v2.0.0-rc.1", out)

    def test_a_source_outside_the_repository_is_refused(self):
        # The block is a file anyone who can land a pull request edits, and this
        # command opens what it names — in CI. A path that escapes the checkout
        # is refused rather than normalised: there is no sensible reading of
        # "the version lives outside this repository".
        checkout = self.product(USED)
        path = checkout / ".vellum" / "product.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "version_source: VERSION", "version_source: ../../etc/hostname"
            ),
            encoding="utf-8",
        )
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("..", out)

    def test_a_checkout_that_is_not_a_git_repository_is_two(self):
        # The tag names a commit, so a directory with no history is one this
        # cannot answer about — and it must not be a traceback.
        checkout = self.root / "loose"
        (checkout / ".vellum").mkdir(parents=True)
        write_product(checkout)
        declare(checkout)
        (checkout / "VERSION").write_text(f"{NEW}\n", encoding="utf-8")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)


class TheThreeVersionSources(TagCase):
    """Which reader runs is decided by the source's NAME, never its contents."""

    def source(self, name: str, text: str, version_source: str | None = None) -> Path:
        checkout = self.root / "product"
        checkout.mkdir(parents=True, exist_ok=True)
        git(checkout, "init", "-q", "-b", "main", ".")
        write_product(checkout)
        declare(checkout, source=version_source or name)
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text, encoding="utf-8")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-qm", "the product begins")
        return checkout

    def test_pyproject_yields_its_project_version(self):
        checkout = self.source(
            "pyproject.toml",
            '[project]\nname = "thing"\nversion = "1.2.3"\n',
        )
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("v1.2.3", out)

    def test_a_pyproject_with_no_project_version_is_two(self):
        checkout = self.source("pyproject.toml", '[tool.black]\nline-length = 88\n')
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("[project] version", out)

    def test_an_unparseable_pyproject_is_two_not_its_trimmed_contents(self):
        # The fallback reader must not catch this: "the trimmed contents" of a
        # broken TOML file is most of a TOML file, reported as a version.
        checkout = self.source("pyproject.toml", "[project\nversion = \n")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("TOML", out)

    def test_a_pyproject_whose_project_is_not_a_table_is_two(self):
        # `project = "0.4.0"` parses as valid TOML and is not a mapping. `.get`
        # on it was an AttributeError, which left this exiting 1 with a
        # traceback — and 1 is the code that must mean the changelog refusal, so
        # `release-cut` would have told an operator to write a changelog entry
        # for a version it never read.
        checkout = self.source("pyproject.toml", 'project = "0.4.0"\n')
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("pyproject.toml", out)
        self.assertNotIn("Traceback", out)

    def test_a_package_json_that_is_not_an_object_is_two(self):
        # The same guard one file over: a top-level array is JSON this parsed
        # and cannot read a `version` out of.
        checkout = self.source("package.json", '["0.4.0"]\n')
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)

    def test_package_json_yields_its_version(self):
        checkout = self.source("package.json", '{"name": "thing", "version": "3.1.0"}\n')
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("v3.1.0", out)

    def test_a_package_json_with_no_version_is_two(self):
        checkout = self.source("package.json", '{"name": "thing"}\n')
        self.assertEqual(run_cli(["release", "tag", str(checkout)])[0], 2)

    def test_any_other_path_is_its_trimmed_contents(self):
        checkout = self.source("version.txt", "  9.9.9  \n\n")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("v9.9.9", out)

    def test_the_reader_follows_the_name_not_the_directory(self):
        # A `pyproject.toml` in a subdirectory is still a pyproject.
        checkout = self.source(
            "packages/api/pyproject.toml",
            '[project]\nversion = "0.9.1"\n',
        )
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("v0.9.1", out)


class ADeclaredPathIsHeldToTheCheckout(TagCase):
    """The `release:` block is written by anyone who can land a pull request.

    `vellum release tag` opens what it names and prints what it read, in CI, on a
    runner holding `contents: write` for the repository it is about. So the
    string, the path it leads to and the bytes behind it are each held — and the
    refusals never quote what was found, because the contents are the pull
    request author's text and this command's report is a workflow log.
    """

    def outside(self, name: str, text: str) -> Path:
        """A file beside the checkout, which nothing in it may reach."""
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(parents=True, exist_ok=True)
        path = elsewhere / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_version_source_that_is_a_symlink_out_of_the_tree_is_refused(self):
        # The lexical check passes — `VERSION` is as repo-relative as a path
        # gets — and the file is still somebody else's. Only a walk over the
        # components can see it.
        checkout = self.product(USED)
        target = self.outside("VERSION", f"{NEW}\n")
        (checkout / "VERSION").unlink()
        (checkout / "VERSION").symlink_to(target)
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("symlink", out)
        # And the version it would have read is nowhere in the answer.
        self.assertNotIn(f"v{NEW}", out)

    def test_a_symlinked_directory_component_is_refused(self):
        # The leaf is an honest file; the directory it sits in is the link. A
        # check that looked only at the path it was given would open it.
        checkout = self.product(USED)
        elsewhere = self.root / "elsewhere" / "pkg"
        elsewhere.mkdir(parents=True)
        (elsewhere / "VERSION").write_text(f"{NEW}\n", encoding="utf-8")
        (checkout / "pkg").symlink_to(elsewhere, target_is_directory=True)
        self.declare_source(checkout, "pkg/VERSION")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("symlink", out)

    def test_a_version_source_this_would_read_forever_is_refused_by_length(self):
        # `/dev/zero` is the shape of it and a large regular file is the
        # testable half: the plain-contents reader is the one with no parser in
        # front of it, so it is the one that is capped.
        checkout = self.product(USED)
        marker = "NOBODY-SHOULD-SEE-THIS"
        (checkout / "VERSION").write_text(
            marker + "x" * (tag.VERSION_SOURCE_LIMIT * 2), encoding="utf-8"
        )
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn("VERSION", out)
        self.assertNotIn(marker, out)

    def test_a_source_under_git_is_refused(self):
        # `.git/config` carries the remotes and, on a runner, the credential
        # `actions/checkout` persisted there. It is refused lexically and by the
        # walk, and either one is enough — this asserts the answer, not which.
        checkout = self.product(USED)
        self.declare_source(checkout, ".git/config")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertIn(".git", out)

    def test_a_declared_path_carrying_a_workflow_command_is_refused(self):
        # A newline in the value is a line of its own in a CI log, and a line of
        # its own is all `::error` needs. Refused rather than flattened: a path
        # with a newline in it names no file anyway.
        checkout = self.product(USED)
        path = checkout / ".vellum" / "product.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "version_source: VERSION",
                'version_source: "VERSION\n::error title=pwned::owned"',
            ),
            encoding="utf-8",
        )
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        for line in out.splitlines():
            self.assertFalse(line.startswith("::"), out)

    def test_the_not_a_version_refusal_names_a_length_and_no_contents(self):
        # The message an operator gets is which file and that what came out of
        # it is not a version. What came out of it is their text, and this
        # report is piped into a step summary.
        checkout = self.product(USED)
        (checkout / "VERSION").write_text("not-a-version-at-all", encoding="utf-8")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        self.assertNotIn("not-a-version-at-all", out)
        self.assertIn("20 characters", out)


class AVersionIsANameGitWillTake(TagCase):
    """`VERSION_RE` first, `git check-ref-format` last, and both are needed.

    A version this minted a name from that git then refused would fail inside
    `release-cut`, with `contents: write` in hand and half the job done.
    """

    def refuses(self, version: str) -> str:
        checkout = self.product(USED)
        (checkout / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 2, out)
        return out

    def test_the_names_git_itself_refuses(self):
        for version in ("0.4.0-rc..1", "0.4.0-rc.", "0.4.0-rc.lock"):
            with self.subTest(version=version):
                self.root = self.fresh()
                self.refuses(version)

    def test_a_fullwidth_digit_is_not_a_digit_here(self):
        # `\d` matches every decimal digit Unicode has. A tag name is resolved
        # by a forge and typed back by an operator, so the digits are ASCII.
        self.refuses("０.４.０")

    def test_git_is_asked_about_the_name_that_would_be_minted(self):
        # The regex is this module's first word and `check-ref-format` is git's
        # last one. Asserted directly, because a version that satisfies the
        # regex and not git is exactly the gap the second check exists for.
        self.assertFalse(ref_format_ok(self.root, "refs/tags/v0.4.0.lock"))
        self.assertTrue(ref_format_ok(self.root, "refs/tags/v0.4.0"))

    def test_a_version_git_takes_is_still_minted(self):
        # The control: the guards refuse names git refuses, not pre-releases.
        checkout = self.product(USED)
        (checkout / "VERSION").write_text("2.0.0-rc.1\n", encoding="utf-8")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 0, out)
        self.assertIn("v2.0.0-rc.1", out)


class TheChangelogEntryIsLookedUpNotSearchedFor(TagCase):
    """Two branches: a YAML changelog is read, and anything else is matched.

    The substring test this replaced said yes to `10.4.0` when asked about
    `0.4.0`, to a pre-release of the version, and to the version named in a
    comment — three ways to tag a release nobody described, which is the one
    thing the refusal exists to prevent.
    """

    def changelog(self, checkout: Path, name: str, text: str) -> None:
        (checkout / name).write_text(text, encoding="utf-8")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-qm", "the changelog")

    def test_a_yaml_changelog_is_read_as_entries(self):
        checkout = self.product(NEW, changelog="CHANGES.yaml")
        self.changelog(
            checkout, "CHANGES.yaml",
            f"releases:\n  - release: v{NEW}\n    summary: this one\n",
        )
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 0, out)

    def test_the_unprefixed_spelling_is_an_entry_too(self):
        checkout = self.product(NEW, changelog="CHANGES.yaml")
        self.changelog(
            checkout, "CHANGES.yaml",
            f"releases:\n  - release: {NEW}\n    summary: this one\n",
        )
        self.assertEqual(run_cli(["release", "tag", str(checkout)])[0], 0)

    def test_a_version_named_only_in_a_comment_is_not_an_entry(self):
        # The case the substring test could not tell from a release: the file
        # mentions the version and describes a different one.
        checkout = self.product(NEW, changelog="CHANGES.yaml")
        self.changelog(
            checkout, "CHANGES.yaml",
            f"# the next one will be v{NEW}\n"
            f"releases:\n  - release: v{USED}\n    summary: the one before\n",
        )
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("CHANGES.yaml", out)

    def test_a_sentence_ending_period_does_not_extend_the_name(self):
        # `Released 0.5.0.` names 0.5.0; only a dot that continues the version
        # (`0.5.0.1`) makes it a longer name.
        checkout = self.product(NEW, changelog="CHANGELOG.md")
        self.changelog(checkout, "CHANGELOG.md", f"Released {NEW}.\n")
        self.assertEqual(run_cli(["release", "tag", str(checkout)])[0], 0)
        self.changelog(checkout, "CHANGELOG.md", f"## {NEW}.1\n")
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 1, out)

    def test_a_yaml_refusal_names_the_shape_it_read(self):
        checkout = self.product(NEW, changelog="CHANGES.yaml")
        self.changelog(
            checkout, "CHANGES.yaml",
            f"releases:\n  - version: {NEW}\n    summary: another key\n",
        )
        code, out = run_cli(["release", "tag", str(checkout)])
        self.assertEqual(code, 1, out)
        self.assertIn("no entry in `releases:`", out)

    def test_a_longer_version_containing_this_one_is_not_an_entry(self):
        # `10.4.0` contains `0.4.0`, and a changelog describing the first
        # describes nothing about the second.
        checkout = self.product("0.4.0", changelog="CHANGELOG.md")
        self.changelog(checkout, "CHANGELOG.md", "# Changelog\n\n## 10.4.0\n")
        self.assertEqual(run_cli(["release", "tag", str(checkout)])[0], 1)

    def test_a_pre_release_of_this_version_is_not_an_entry_for_it(self):
        checkout = self.product("0.4.0", changelog="CHANGELOG.md")
        self.changelog(checkout, "CHANGELOG.md", "# Changelog\n\n## v0.4.0-rc1\n")
        self.assertEqual(run_cli(["release", "tag", str(checkout)])[0], 1)

    def test_a_markdown_heading_at_a_boundary_is_an_entry(self):
        # The control for all three: the ordinary shapes still satisfy it.
        for heading in ("## v0.4.0", "## [0.4.0] - 2026-09-06", "* 0.4.0"):
            with self.subTest(heading=heading):
                self.root = self.fresh()
                checkout = self.product("0.4.0", changelog="CHANGELOG.md")
                self.changelog(
                    checkout, "CHANGELOG.md", f"# Changelog\n\n{heading}\n\n- it.\n"
                )
                code, out = run_cli(["release", "tag", str(checkout)])
                self.assertEqual(code, 0, out)


class ThisRepoDogfoodsIt(unittest.TestCase):
    """`waviisoft/vellum` declares its own `release:` block, and it answers.

    The alarm for the dogfooding half of the wave: this repo's caller workflow
    runs `vellum release tag . --plan` on every push to main, and a declaration
    that stopped reading — a renamed changelog, a version source moved — would
    otherwise be found by that workflow rather than by the suite.
    """

    def test_the_declaration_reads_and_names_this_checkouts_version(self):
        from vellum import __version__

        root = Path(__file__).resolve().parents[1]
        if not (root / ".vellum" / "product.yaml").is_file():  # pragma: no cover
            self.skipTest("not a checkout of this repo")
        declared = tag.declaration(root)
        self.assertEqual(declared.version_source, "pyproject.toml")
        self.assertEqual(declared.changelog, "src/vellum/seeds/CHANGES.yaml")
        self.assertEqual(tag.version_from(root, declared), __version__)

    def test_the_changelog_entry_for_this_version_is_the_pre_tag_alarm(self):
        # The same fact `tests/test_upgrade.py::TheReleaseThisCutIs` asserts,
        # made live: the check that used to be a test the owner ran before
        # tagging by hand is now what decides whether the forge tags at all.
        from vellum import __version__

        root = Path(__file__).resolve().parents[1]
        changelog = (root / "src" / "vellum" / "seeds" / "CHANGES.yaml").read_text(
            encoding="utf-8"
        )
        self.assertTrue(
            tag.changelog_names(changelog, __version__),
            f"CHANGES.yaml carries no v{__version__} entry, so `vellum release "
            f"tag` would refuse this repo's own release and the forge would not "
            f"tag it.",
        )


if __name__ == "__main__":
    unittest.main()
