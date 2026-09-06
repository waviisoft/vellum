"""``vellum upgrade --to <release>`` — rewrite the owned files, as a pull request.

``spec/features/installation.md``: "**``vellum upgrade --to <release>``** rewrites
only owned files from that release's templates, re-stamps the stubs at the same
ref, records the release in the manifest, and lands the change on a branch as a
pull request — never a push to the default branch."

The three things this command is built around
---------------------------------------------

**Ownership is read, never inferred.** The set of files this rewrites is
``.vellum/install.yaml``'s ``owned:`` list and nothing else. Not "every file the
seed writes", not "every file untouched since seeding" — the decision rejected
that second one by name, because "a product that edited a seeded file once and
reverted it would silently flip ownership". A path this command has a template
for but the manifest does not list is *not touched*, and the reverse — a path
listed that no release ships — is reported and left alone. Both directions
matter: the first is how an operator takes a file back for good, and the second
is how a retired file stops being Vellum's without anybody deleting anything.

**One branch decides everything.** This runs on the installation's default
branch and refuses anywhere else, because that one ref does three jobs at once:
it is what every owned file is read out of, it is what the upgrade branch is cut
from, and it is what the pull request merges back into. Reading the *working
tree* instead made the first of those disagree with the other two the moment
``HEAD`` was anything else — an edit made on the default branch and hidden by a
feature branch checked out over it compared as unedited and was overwritten, and
an edit made only on the feature branch was reported as one the installation had
made to the default branch. Neither the read nor the refusal is sufficient
alone; both are here.

**An edited owned file is a refusal, and the refusal writes nothing at all.**
Every owned file is compared, before anything is written, against the template
of the release the manifest **currently** names — not the one being upgraded to.
A file that differs is an edit the installation made, and the answer is exit 1
naming it, with no branch created and no file touched. The decision considered a
three-way merge and rejected it: it "decides, in the middle of an upgrade, what
an operator meant by an edit; a refusal with two named ways out costs one review
and loses nothing". The two ways out are both edits to the manifest's world:
put the file back as the release shipped it, or take its line out of ``owned:``.

**Nothing here reaches the network.** A release's templates come from a checkout
the operator names (``--from``, read with ``git show <ref>:<path>`` — no
worktree, no fetch) or from this CLI's own package data when the CLI *is* the
release being asked about. A command that fetched a release would be a command
whose behavior depends on what a server said today, in the one place where
"what did release X ship" has to be answerable identically forever.

Why ``--from`` is usually required, and why that is not a wart
--------------------------------------------------------------
Two refs are needed, not one: the release the manifest names (to prove the file
is unedited) and the release being upgraded to (to write it). A checkout serves
any ref it carries. This CLI serves exactly one — its own version — because a
wheel carries one release's templates and no more. So unless the installation is
already at this CLI's version *and* being upgraded to it, the CLI alone cannot
answer both questions, and this exits 2 naming ``--from`` rather than skipping
the check it cannot make. Skipping it is the one thing this command must never
do: the check is the whole safety property, and an upgrade that quietly stopped
performing it would overwrite the very edits it exists to protect.

The stubs are re-stamped, not template-copied
---------------------------------------------
A caller stub interpolates the host, the ref and the branch, and none of those
is a release's to choose — ``vellum init`` stamps them and ``doctor`` compares an
installation's against a fresh render. So an owned stub is rendered by
:func:`vellum.install.render` at the two refs rather than read out of a release,
with the host and the branch recovered from the stub already installed
(:func:`vellum.install.installed_shape`) so that an installation on ``trunk``, or
one pointed at a fork, is not reported as having edited its stubs.

The limitation is worth stating plainly, because it is the one place this
command's answer is narrower than its sentence: the stub is re-stamped in **this
CLI's** shape at the new ref, not in the new release's shape. A release that
changed what a stub *contains* — a new trigger path, a second input — delivers
that when a CLI at that release stamps it, which is ``vellum init --ref <new>
--force`` run from that CLI, and which ``doctor`` asks for by comparing the
caller half against what ships. The manifest's release line and doctor's new
local-CLI-against-the-stubs line are both there to make that visible rather than
silent.

An `owned:` line is not permission to write anywhere
----------------------------------------------------
The manifest lives in the repository, so its list is written by anyone who can
land a pull request there — and this command writes every path on it, after a
``mkdir -p``. ``vellum.manifest`` holds the lexical half of that (no absolute
path, no ``..``, nothing under ``.git/``, nothing unprintable) and
:func:`unsafe_write` holds the half that needs a filesystem: a symlink among a
path's components, a parent that is a regular file, a directory that resolves
outside the checkout. All of them are refusals computed with the rest of the
list, before a byte is written — and the one that matters most is the first,
because ``.git/hooks/`` is reachable through a symlink and a hook written there
would be run by this command's own ``git commit``.

A missing owned file is skipped, not recreated
----------------------------------------------
An installation that deleted an owned file deleted it on purpose — the intent
repo this product's own installation pairs with carries no ``harness-ci.yml``
stub by design. Recreating it would make an upgrade undo a decision nobody
re-opened, in a pull request about something else. So a missing owned file is
reported and skipped, and ``--restore`` is how an operator asks for it back.
"""

from __future__ import annotations

import ast
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from vellum import changes, install, manifest, owned, product, seeds
from vellum.gitver import GitUnavailable, blob_at, resolve, show
from vellum.provision import Gh, ProvisionError, default_branch, detect_gh, git, git_dir
from vellum.text import one_line
from vellum.workspace import SLUG_RE, WORKSPACE_RELPATH

#: The branch an upgrade lands on. One per release, so two upgrades in flight
#: are two branches and neither is ``main``: "the upgrade pull request is
#: reviewable and revertible by pinning back"
#: (``spec/decisions/2026-09-04-vellum-owned-files-and-upgrades.md``).
BRANCH_PREFIX = "vellum/upgrade-"

#: Where the pull request's body is written, and the reason it is under ``.git/``
#: rather than in the working tree. The body has to survive the command — the
#: printed rung passes the same path to ``gh pr create --body-file``, so an
#: operator following it sends the body the transport would have sent rather
#: than a placeholder — and it must never be a file the *next* run trips over.
#: In the tree it was both: uncommitted and untracked, so ``_clean`` refused the
#: second upgrade on the leavings of the first. ``.git/`` is the one directory
#: that is per-checkout, never committed and never in ``git status``.
PR_BODY_RELPATH = ".git/vellum/UPGRADE_PR.md"
#: The same path relative to the checkout's git directory, which is what the
#: write actually uses: `.git` is a directory in a clone and a FILE in a
#: worktree, and ``provision.git_dir`` answers for both.
PR_BODY_UNDER_GIT = "vellum/UPGRADE_PR.md"

#: What happened to one owned file. Every one of these is decided *before*
#: anything is written, so a run that refuses has computed the whole list and
#: touched nothing.
REWRITE = "rewrite"
UNCHANGED = "unchanged"
EDITED = "edited"
MISSING = "missing"
RESTORE = "restore"
RETIRED = "retired"
NEW = "new"
UNVERIFIABLE = "unverifiable"
#: A path this refuses to write at all — a symlink among its components, a
#: parent that is a regular file, a resolved parent outside the checkout. Not an
#: edit and not an "I cannot answer": it is an ownership claim the checkout will
#: not honour, and it is the one outcome that is about the *shape of the tree*
#: rather than about the file's contents.
UNSAFE = "unsafe"

#: The outcomes that mean a file gets written.
WRITES = (REWRITE, RESTORE, NEW)


#: The last release whose seeded templates were Python string constants rather
#: than files under ``src/vellum/seeds/templates/``. Everything at or below it
#: is read through :meth:`Templates.pre_templates`.
PRE_TEMPLATES = (0, 2, 0)

#: Where those constants lived, and what each one was called. Frozen history:
#: these names are what ``v0.1.0`` and ``v0.2.0`` shipped and no later release
#: is read this way, so nothing here moves when the module does.
LEGACY_MODULE = "src/vellum/provision.py"
LEGACY_CONSTANTS = {
    owned.CONFIG_TEMPLATE: "CONFIG_YAML",
    owned.RELEASES_TEMPLATE: "RELEASES_YAML",
    owned.MEMORY_MAP_TEMPLATE: "MEMORY_MAP",
}

#: The interpolation the seeder of the day applied on the way out, per template.
#: Only ``config.yaml`` had one, and its value was the literal in the seed
#: (``CONFIG_YAML.format(divergence_cap=3)``). The memory map's ``{intent_slug}``
#: is deliberately absent: that one is still a placeholder today and
#: :func:`_template_text` fills it from the checkout.
LEGACY_VALUES = {owned.CONFIG_TEMPLATE: {"divergence_cap": 3}}


class UpgradeError(Exception):
    """The command could not answer: no manifest, no templates, a tree it will not touch."""


def _template_name(path: str) -> str | None:
    """``config.yaml`` for ``src/vellum/seeds/templates/config.yaml``, else None."""
    prefix = seeds.source_path(seeds.TEMPLATES) + "/"
    return path[len(prefix):] if path.startswith(prefix) else None


def _string_constant(source: str, name: str) -> str | None:
    """The module-level ``NAME = "…"`` in *source*, by parsing it. Never imports."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    return None


# =====================================================================
# Where a release's templates come from
# =====================================================================


@dataclass(frozen=True)
class Templates:
    """One place a release's templates can be read from.

    Two kinds, and the difference between them is *which refs they can answer
    for*: a checkout answers for every ref it carries, and this CLI answers for
    exactly one — its own version. :meth:`serves` is the whole of that, and
    every "cannot answer" this command returns comes back to it.
    """

    #: A ``waviisoft/vellum`` checkout, or None when this is the CLI itself.
    checkout: Path | None = None

    @property
    def kind(self) -> str:
        return "checkout" if self.checkout is not None else "cli"

    def serves(self, ref: str) -> bool:
        return self.checkout is not None or ref == install.default_ref()

    def describe(self) -> str:
        if self.checkout is not None:
            return f"the checkout at {self.checkout}"
        return f"this CLI's own package data ({install.default_ref()})"

    def read(self, ref: str, path: str) -> str | None:
        """The file at *path* as *ref* shipped it, or None when it shipped none.

        None is a real answer and not an error: a release that did not carry a
        template is a release that did not ship that file, which is exactly what
        "files added" and "files retired" mean one layer up.
        """
        if self.checkout is not None:
            found = show(self.checkout, ref, path)
            return found if found is not None else self.pre_templates(ref, path)
        try:
            return seeds.read_source(path)
        except seeds.SeedsMissing:
            return None

    def pre_templates(self, ref: str, path: str) -> str | None:
        """The same template as a release *before* ``templates/`` existed shipped it.

        Every installation in the world today was provisioned by ``v0.2.0`` or
        earlier, and at those releases the seeded config, the release ledger and
        the memory map were **string constants in ``src/vellum/provision.py``**
        rather than files under ``src/vellum/seeds/templates/``. Without this, an
        upgrade off such an installation reads no template for them at ``was``
        and reports every one as ``unverifiable`` — permanently, because the
        release the manifest names never changes by itself. The fallback is what
        makes an existing installation able to own a seeded file at all.

        The module is **parsed, never imported**: :func:`ast.parse` over the text
        ``git show`` gave back, reading module-level assignments of plain string
        literals and nothing else. Importing another release's code to ask what
        it shipped would run it.

        :data:`LEGACY_VALUES` reproduces the one call the seeder of the day made
        (``CONFIG_YAML.format(divergence_cap=3)``); ``tests/test_upgrade.py``
        asserts the bytes this returns for ``v0.2.0`` against the moved
        templates, so a mapping that stopped reproducing them cannot pass.
        """
        if self.checkout is None:
            return None
        name = _template_name(path)
        if name is None or name not in LEGACY_CONSTANTS:
            return None
        version = changes.version_of(ref)
        if version is None or version > PRE_TEMPLATES:
            return None
        source = show(self.checkout, ref, LEGACY_MODULE)
        if source is None:
            return None
        text = _string_constant(source, LEGACY_CONSTANTS[name])
        values = LEGACY_VALUES.get(name)
        if text is None or not values:
            return text
        try:
            return text.format(**values)
        except (IndexError, KeyError, ValueError):
            # The constant is not the one this mapping was written against, so
            # this release cannot answer for that file after all. `unverifiable`
            # is the honest outcome; a half-substituted template is not.
            return None


def templates_from(from_checkout: str | Path | None) -> Templates:
    """Resolve ``--from``, refusing a path that is not a readable git checkout."""
    if from_checkout is None:
        return Templates()
    path = Path(from_checkout)
    if not path.is_dir():
        raise UpgradeError(
            f"--from {path}: not a directory. It names a checkout of "
            f"{install.HOST_REPO} whose tags this reads a release's templates at."
        )
    try:
        resolve(path, "HEAD")
    except GitUnavailable as exc:
        raise UpgradeError(
            f"--from {path}: not a readable git checkout ({one_line(str(exc))}). "
            f"A release's templates are read out of one with `git show "
            f"<ref>:<path>`; nothing here reaches a network."
        ) from exc
    return Templates(checkout=path)


def _require_ref(source: Templates, ref: str, why: str) -> None:
    """Refuse, in one sentence, when *source* cannot answer for *ref*."""
    if source.serves(ref):
        return
    raise UpgradeError(
        f"{source.describe()} cannot answer for {ref}, and that is the release "
        f"{why}. This CLI carries one release's templates — its own, "
        f"{install.default_ref()} — so pass `--from <a checkout of "
        f"{install.HOST_REPO}>` carrying that tag. Nothing here fetches one: "
        f"what a release shipped has to read the same way forever, and a "
        f"command that asked a server would answer differently on two days."
    )


def _ref_exists(source: Templates, ref: str) -> None:
    if source.checkout is None:
        return
    try:
        resolve(source.checkout, ref)
    except GitUnavailable as exc:
        raise UpgradeError(
            f"{source.checkout} carries no ref {one_line(ref)!r} "
            f"({one_line(str(exc))}). A release is a tag on {install.HOST_REPO}; "
            f"fetch its tags, or name one the checkout has."
        ) from exc


# =====================================================================
# Which side of the pair this checkout is
# =====================================================================


def side_of(root: Path) -> str:
    """``intent`` or ``product``, from the file that defines each. Never guessed.

    ``vellum.install.side_of``'s answer, in this module's error type. The rule
    is stated there, where the table of which stubs belong to which side lives;
    what matters here is that the side decides which stubs are re-stamped and
    which owned set is even legal.
    """
    try:
        return install.side_of(root)
    except install.InstallError as exc:
        # Re-raised rather than reimplemented. `init` and `doctor` ask the same
        # question now, and three commands reading one fact through two
        # implementations is how they come to disagree about a checkout that
        # carries both files — which is the case that decides whether an
        # upgrade rewrites an intent repo's stubs or a product repo's.
        raise UpgradeError(str(exc)) from exc


def _values(root: Path, side: str) -> dict[str, str]:
    """The installation's own values, for the templates that interpolate one.

    Read out of the checkout rather than remembered from provisioning, which is
    the rule that decides what is ownable at all (``vellum.owned``): a template
    whose values the checkout no longer carries is one an upgrade could not
    reproduce, so it is not Vellum's to rewrite.
    """
    if side != owned.PRODUCT:
        return {}
    try:
        declared = product.load(root)
    except Exception as exc:  # the reader raises its own; this is "cannot answer"
        raise UpgradeError(f"{product.product_path(root)}: {one_line(str(exc))}") from exc
    slug = ((declared.get("intent") or {}) if isinstance(declared, dict) else {}).get("repo")
    if not isinstance(slug, str) or not slug.strip():
        raise UpgradeError(
            f"{product.product_path(root)} declares no `intent.repo`, and the "
            f"product repo's memory map names it. Without it this cannot "
            f"reproduce that file's template and so cannot tell an edited one "
            f"from an unedited one."
        )
    return {"intent_slug": slug.strip()}


# =====================================================================
# The comparison, which happens before anything is written
# =====================================================================


@dataclass
class Change:
    """One owned path, and what this upgrade would do about it."""

    path: str
    action: str
    detail: str = ""
    #: The text to write, for the outcomes in :data:`WRITES`.
    text: str | None = None


@dataclass
class Upgrade:
    """One run of ``vellum upgrade``."""

    checkout: Path
    side: str
    source: Templates
    #: The release the manifest named on the way in.
    was: str
    #: The release being upgraded to.
    to: str
    changes: list[Change]
    shape: tuple = ()
    shape_note: str | None = None
    plan_only: bool = False
    restore: bool = False
    #: Set once a run has done its git half.
    branch: str | None = None
    base: str | None = None
    commit: str | None = None
    #: ``owner/name`` on the forge, read from ``origin``, or None when this
    #: checkout has no remote to read one from.
    slug: str | None = None
    #: Steps a transport did not take, as the exact commands to run.
    manual: list[str] = field(default_factory=list)
    pr_url: str | None = None
    pr_body_path: Path | None = None

    def by(self, *actions: str) -> list[Change]:
        return [c for c in self.changes if c.action in actions]

    @property
    def refused(self) -> list[Change]:
        """The owned files this installation has edited. Exit 1, nothing written."""
        return self.by(EDITED)

    @property
    def unanswerable(self) -> list[Change]:
        return self.by(UNVERIFIABLE)

    @property
    def unsafe(self) -> list[Change]:
        """Owned paths this will not write into. Exit 2, nothing written."""
        return self.by(UNSAFE)

    @property
    def stopped(self) -> bool:
        """True when this run computed a list and then wrote nothing."""
        return bool(self.unsafe or self.refused or self.unanswerable)

    def report(self) -> str:
        lines = [
            f"vellum upgrade — {self.was} → {self.to} in {self.checkout}",
            f"  side:      {self.side}",
            f"  templates: {self.source.describe()}",
            f"  manifest:  {manifest.MANIFEST_RELPATH.as_posix()} names "
            f"{len(self.changes)} owned path(s)",
            "",
        ]
        if self.plan_only:
            lines.append("Nothing below has happened, and --plan creates nothing.")
            lines.append("")
        lines.append(f"Owned files ({len(self.changes)})")
        for change in self.changes:
            lines.append(f"  {change.action:<12} {change.path}")
            if change.detail:
                lines.append(f"               {change.detail}")
        lines.append("")
        lines += changes.render(self.shape, self.shape_note, after=self.was, to=self.to)
        lines.append("")
        if self.unsafe:
            lines.append(
                f"BLOCKED: {len(self.unsafe)} owned path(s) this will not write "
                f"into — the tree redirects them somewhere Vellum does not own. "
                f"Nothing was written and no branch was created."
            )
            lines.append(
                "  This is about the SHAPE of the checkout, not about a file's "
                "contents: a symlink among a path's components, or a parent that "
                "is not a directory, makes `mkdir -p` and a write land somewhere "
                "the manifest never named. Fix the tree, or take the line out of "
                f"`{manifest.OWNED_KEY}:` in "
                f"{manifest.MANIFEST_RELPATH.as_posix()}."
            )
        elif self.refused:
            lines.append(
                f"BLOCKED: {len(self.refused)} owned file(s) differ from what "
                f"{self.was} shipped, so this installation has edited them. "
                f"Nothing was written and no branch was created."
            )
            lines.append(
                "  Two ways out, and they are the operator's to choose between: "
                "put the file back as the release shipped it and Vellum goes on "
                "owning it, or take its line out of `owned:` in "
                f"{manifest.MANIFEST_RELPATH.as_posix()} and it is yours for good. "
                "A three-way merge would decide that for you, in the middle of an "
                "upgrade (spec/decisions/2026-09-04-vellum-owned-files-and-"
                "upgrades.md)."
            )
        elif self.unanswerable:
            lines.append(
                f"COULD NOT ANSWER: {len(self.unanswerable)} owned file(s) exist "
                f"here but {self.was} shipped no template for them, so nothing "
                f"can say whether they are as Vellum left them."
            )
        elif self.plan_only:
            written = len(self.by(*WRITES))
            lines.append(
                f"Plan only. {written} file(s) would be written, "
                f"{len(self.by(UNCHANGED))} are already what {self.to} ships, and "
                f"nothing was created — no branch, no file, no pull request."
            )
        else:
            lines.append(
                f"Done. {len(self.by(*WRITES))} file(s) rewritten and the manifest "
                f"records {self.to}."
            )
            if self.branch:
                lines.append(f"  branch {self.branch}, off {self.base}, "
                             f"commit {(self.commit or '')[:12]}")
                lines.append(f"  {self.base} was not touched; this lands as a pull "
                             f"request or not at all.")
                # Said out loud, because it is the one thing about this command
                # an operator finds out later otherwise: the checkout they ran
                # it in is standing somewhere else now.
                lines.append(f"  this checkout is now ON {self.branch}; `git -C "
                             f"{self.checkout} checkout {self.base}` returns it.")
            if self.pr_url:
                lines.append(f"  pull request: {self.pr_url}")
            if self.pr_body_path:
                lines.append(f"  pull request body: {self.pr_body_path} (outside "
                             f"the working tree, so no run trips over it)")
        if self.manual:
            lines.append("")
            lines.append("Steps no transport took; run them as they are:")
            lines += [f"  {n:>2}. {command}" for n, command in enumerate(self.manual, 1)]
        return "\n".join(lines)


def _stub_text(row: owned.Owned, *, ref: str, host: str, branch: str, forge: str) -> str:
    return install.render(row.shipped, host=host, ref=ref, forge=forge, branch=branch)


def _template_text(
    row: owned.Owned, source: Templates, ref: str, values: dict[str, str]
) -> str | None:
    text = source.read(ref, row.source)
    if text is None:
        return None
    if not row.placeholders:
        return text
    missing = [name for name in row.placeholders if name not in values]
    if missing:
        raise UpgradeError(
            f"{row.path}: its template interpolates `{'`, `'.join(missing)}`, "
            f"which this checkout does not supply."
        )
    # `str.replace`, not `str.format`. A template is a file a release ships, and
    # the one thing `format` does that this must not is treat every other brace
    # in it as a field: a `{` an author wrote for its own sake — a JSON snippet
    # in a comment, a YAML flow mapping — would raise mid-upgrade, which is a
    # crash for a file that is otherwise fine. The placeholder set is a stated
    # tuple on the row, so substitution has nothing to discover.
    for name in row.placeholders:
        text = text.replace("{" + name + "}", values[name])
    return text


def compare(
    root: Path,
    listed,
    *,
    source: Templates,
    was: str,
    to: str,
    side: str,
    forge: str,
    restore: bool,
    base: str,
) -> list[Change]:
    """What this upgrade would do to every owned path. Writes nothing.

    The whole list is computed before a single byte is written, which is what
    makes "exit 1 and nothing is written" true rather than "exit 1 and some of
    it is written". Two runs of this over one checkout produce the same list.

    **Every file is read out of *base*, not out of the working tree.** The
    upgrade branch is cut from *base* and its pull request merges back into
    *base*, so *base* is the tree this rewrite lands on and the only one whose
    contents the safety property can be about. Reading the working tree instead
    got the question wrong in both directions the moment ``HEAD`` was anything
    else: an edit made on *base* and hidden by a feature branch checked out over
    it compared as unedited and was overwritten, and an edit made only on the
    feature branch was reported as one the installation had made to *base* and
    refused an upgrade nothing was wrong with. :func:`upgrade` also refuses to
    run unless ``HEAD`` *is* *base* — the two together, because either alone
    still leaves one of those two directions live.
    """
    table = owned.table(forge)
    values = _values(root, side)
    host, branch = install.installed_shape(root, forge)
    found: list[Change] = []
    for path in listed:
        row = table.get(path)
        if row is None:
            found.append(Change(path, RETIRED, (
                f"{to} ships no template for it, so Vellum has stopped shipping "
                f"this file (or never shipped it). Left exactly as it is; take "
                f"its line out of `{manifest.OWNED_KEY}:` and it is yours."
            )))
            continue
        if row.side != side:
            found.append(Change(path, RETIRED, (
                f"is a {row.side}-side file and this is a {side} checkout, so no "
                f"release of Vellum writes it here. Left alone."
            )))
            continue
        try:
            current = blob_at(root, f"refs/heads/{base}", path)
        except (UnicodeDecodeError, ValueError) as exc:
            found.append(Change(path, UNVERIFIABLE, (
                f"could not be read out of {base} ({one_line(str(exc))}), so it "
                f"cannot be compared against what {was} shipped."
            )))
            continue
        exists = current is not None
        if row.kind == owned.STUB:
            # Compared against a render at the ref THIS STUB pins, not at the
            # release the manifest names. The two come apart legitimately and
            # often: `vellum init --ref <new> --force` restamps the stubs on
            # their own and deliberately leaves the manifest's release line
            # where it is (`install.stamp_manifest`), so an installation whose
            # stubs are ahead of its manifest is an ordinary one — and rendering
            # at `was` reported all three of its stubs as edits it had made.
            stub_host, pinned, stub_branch = (
                install.stub_shape(current, forge) if exists else (None, None, None)
            )
            shape = {"host": stub_host or host, "branch": stub_branch or branch,
                     "forge": forge}
            before = _stub_text(row, ref=pinned or was, **shape)
            after = _stub_text(row, ref=to, **shape)
        else:
            before = _template_text(row, source, was, values)
            after = _template_text(row, source, to, values)
        if not exists:
            if after is None:
                found.append(Change(path, RETIRED, (
                    f"is not on {base} and {to} ships none either. Nothing to do."
                )))
            elif restore:
                found.append(Change(path, RESTORE, (
                    f"is not on {base} and --restore was given, so {to}'s copy "
                    f"is written."
                ), text=after))
            elif before is None:
                found.append(Change(path, NEW, (
                    f"{was} shipped none and {to} does, so this file is new in "
                    f"the range and is written."
                ), text=after))
            else:
                found.append(Change(path, MISSING, (
                    f"is owned but not on {base}. Skipped, not recreated: an "
                    f"installation that removed a file removed it on purpose, "
                    f"and an upgrade is not where that gets re-opened. "
                    f"`--restore` writes it back."
                )))
            continue
        if after is None:
            found.append(Change(path, RETIRED, (
                f"{to} ships no template for it. Left exactly as it is — Vellum "
                f"deletes nothing on upgrade — and it is yours once you take its "
                f"line out of `{manifest.OWNED_KEY}:`."
            )))
            continue
        if before is None:
            found.append(Change(path, UNVERIFIABLE, (
                f"is on {base} and owned, but {was} shipped no template for it, "
                f"so nothing can say whether it is as Vellum left it. Rewriting "
                f"it would overwrite whatever it actually is."
            )))
            continue
        if current != before:
            found.append(Change(path, EDITED, (
                f"differs from what {was} shipped. This installation has made it "
                f"its own."
            )))
        elif before == after:
            found.append(Change(path, UNCHANGED, (
                f"{was} and {to} ship the same file; nothing to write."
            )))
        else:
            found.append(Change(path, REWRITE, (
                f"is as {was} shipped it, so it is rewritten from {to}'s template."
            ), text=after))
    for change in found:
        if change.action not in WRITES:
            continue
        refusal = unsafe_write(root, change.path)
        if refusal is not None:
            change.action, change.detail, change.text = UNSAFE, refusal, None
    return found


def unsafe_write(root: Path, relative: str) -> str | None:
    """Why *relative* is not a path this may write into *root*, or None.

    ``vellum.manifest.check_owned_path`` holds the *lexical* half of this — no
    absolute path, no ``..``, nothing under ``.git/`` — and cannot hold any of
    the rest, because the rest is about a filesystem it never looks at. A
    manifest entry is a line in a repository that anybody who can land a pull
    request can write, and ``upgrade`` writes every path on that list after a
    ``mkdir(parents=True)``. Three ways that becomes a write somewhere else:

    * **a symlink among the components.** ``.github/workflows`` a symlink to
      ``../.git/hooks``, or the file itself a dangling symlink pointing there,
      and an owned path becomes a hook — one this command's own ``git commit``
      then executes, in the operator's shell, in the same run.
    * **a parent that is a regular file.** ``mkdir(parents=True)`` fails
      halfway, which is a traceback out of a half-written tree rather than a
      refusal before one exists (see :func:`_apply`).
    * **a parent that resolves outside the checkout.** The backstop for the
      first: whatever the components are, the directory written into has to be
      inside ``root``.

    A reason, never a boolean, because the report names the path and says which
    of the three it is: an operator has to be able to look at the tree and see
    the same thing this saw.
    """
    settled_root = root.resolve()
    parts = PurePosixPath(relative).parts
    walked = root
    for index, part in enumerate(parts):
        walked = walked / part
        if walked.is_symlink():
            return (
                f"{'/'.join(parts[:index + 1])} is a symlink, and this writes "
                f"through no symlink: an owned path whose components can be "
                f"redirected is a write wherever the link points — `.git/hooks/` "
                f"among the reachable places, where it would run during this "
                f"upgrade's own commit. Nothing was written. Replace the link "
                f"with the real path, or take the line out of "
                f"`{manifest.OWNED_KEY}:`."
            )
        if index < len(parts) - 1 and walked.exists() and not walked.is_dir():
            return (
                f"{'/'.join(parts[:index + 1])} is a file, and this path needs "
                f"it to be a directory. Writing would have to create a directory "
                f"where a file already is, which fails part way through a run "
                f"that has already written other files — so it is refused here, "
                f"before anything is written."
            )
    try:
        settled = (root / relative).parent.resolve()
    except OSError as exc:  # a symlink loop, or a component that cannot be read
        return (
            f"its directory could not be resolved ({one_line(str(exc))}), so "
            f"nothing can say that writing it writes inside this checkout."
        )
    if settled != settled_root and settled_root not in settled.parents:
        return (
            f"its directory resolves to {settled}, which is outside {settled_root}. "
            f"Vellum owns files in the installation, and an `{manifest.OWNED_KEY}:` "
            f"line cannot claim one anywhere else."
        )
    return None


# =====================================================================
# The command
# =====================================================================


def upgrade(
    checkout: str | Path,
    *,
    to: str,
    from_checkout: str | Path | None = None,
    plan_only: bool = False,
    restore: bool = False,
    yes: bool = False,
) -> Upgrade:
    """Plan, and unless ``plan_only``, carry out an upgrade."""
    root = Path(checkout)
    if not root.is_dir():
        raise UpgradeError(f"{root}: not a directory; is this an installation checkout?")
    # A RELEASE, not any ref git would take. "Upgrading is adopting a cut"
    # (spec/decisions/2026-09-04-vellum-owned-files-and-upgrades.md): an
    # installation pins releases and never `main`, and a manifest naming a
    # branch is a claim about files that changes under it without anybody
    # upgrading anything. `install.RELEASE_RE` is the same shape `doctor` reads
    # currency by, so the two agree on what a release is.
    if not install.RELEASE_RE.match(str(to)):
        raise UpgradeError(
            f"--to {to!r} is not a release. It must be `v` and a dotted version "
            f"— v0.3.0 — because that is what an installation pins: upgrading is "
            f"adopting a cut, and a branch or a sha is a pin that moves under "
            f"the manifest without anybody having upgraded anything "
            f"(spec/decisions/2026-09-04-vellum-owned-files-and-upgrades.md)."
        )
    side = side_of(root)
    try:
        installed = manifest.load(root)
    except manifest.ManifestError as exc:
        raise UpgradeError(str(exc)) from exc

    source = templates_from(from_checkout)
    _ref_exists(source, to)
    _require_ref(source, to, "being upgraded to")
    _ref_exists(source, installed.release)
    _require_ref(
        source, installed.release,
        f"the manifest names, and every owned file is compared against ITS "
        f"templates before anything is written",
    )
    forge = install.read_forge(root) if side == owned.INTENT else "github"
    base = _base(root, forge)
    # The manifest is the one file `_apply` writes that is not on the owned
    # list, so it gets the same walk the owned paths get.
    held = unsafe_write(root, manifest.MANIFEST_RELPATH.as_posix())
    if held is not None:
        raise UpgradeError(f"{manifest.MANIFEST_RELPATH.as_posix()}: {held}")

    found = compare(
        root, installed.owned, source=source, was=installed.release, to=to,
        side=side, forge=forge, restore=restore, base=base,
    )
    shape, note = _shape(source, to, installed.release)
    result = Upgrade(
        checkout=root, side=side, source=source, was=installed.release, to=to,
        changes=found, shape=shape, shape_note=note, plan_only=plan_only,
        restore=restore, base=base,
    )
    if result.stopped or plan_only:
        return result
    _apply(result, yes=yes)
    return result


def _base(root: Path, forge: str) -> str:
    """The branch this upgrade is computed from and cut from, checked to be HEAD.

    One ref does both jobs and that is the point. The branch is created off the
    default branch and the pull request merges back into it, so the default
    branch is what an owned file has to be compared against — and a checkout
    standing somewhere else is a run whose comparison and whose write are about
    two different trees. Rather than silently comparing one and writing the
    other, this refuses and names both branches: whichever way the divergence
    goes, the operator can see it in one line.
    """
    # `origin/HEAD` says which branch when there is a remote to say it; an
    # installation provisioned `--branch trunk` with no remote yet has only its
    # stubs to say so, and `on-spec-merge` watches exactly that branch.
    _, watched = install.installed_shape(root, forge)
    try:
        base = default_branch(root, watched)
    except ProvisionError as exc:
        raise UpgradeError(str(exc)) from exc
    head = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if head.returncode != 0:
        raise UpgradeError(
            f"{root} is not a readable git checkout "
            f"({one_line(head.stderr or head.stdout)}). An upgrade compares every "
            f"owned file against {base} and cuts its branch from it, so there has "
            f"to be a repository to read."
        )
    standing = head.stdout.strip()
    if standing != base:
        raise UpgradeError(
            f"{root} is on {standing!r}, and an upgrade runs on {base!r}. Every "
            f"owned file is compared against what {base} carries — that is the "
            f"branch this upgrade's pull request merges into — and the upgrade "
            f"branch is cut from {base} too. Run from anywhere else, the "
            f"comparison and the write are about two different trees: an edit "
            f"made on {base} and hidden by {standing!r} would be silently "
            f"overwritten, and an edit made only on {standing!r} would be "
            f"reported as one this installation had made to {base}. `git -C "
            f"{root} checkout {base}` first."
        )
    return base


def _shape(source: Templates, to: str, was: str):
    """The shape entries for ``(was, to]``, read from the release being adopted.

    From the *release's* changelog rather than this CLI's, when a checkout can
    give one: a release describes its own installation-shape changes, and a CLI
    older than the release would describe them from before they were written.
    """
    text = source.read(to, seeds.source_path(seeds.CHANGES))
    if text is None:
        return (), (
            f"{to} ships no {seeds.source_path(seeds.CHANGES)}, so this cannot "
            f"say what it changes about an installation's shape. Releases from "
            f"before the changelog existed are the ordinary case for that."
        )
    try:
        return changes.parse(text).between(was, to)
    except changes.ChangesError as exc:
        return (), (
            f"{to}'s {seeds.source_path(seeds.CHANGES)} could not be read "
            f"({one_line(str(exc))}), so no shape changes are printed. The files "
            f"below are unaffected: they are rewritten from that ref's templates "
            f"either way."
        )


# =====================================================================
# The git half: a branch off the default branch, and a pull request
# =====================================================================


def _clean(root: Path) -> None:
    """Refuse a dirty tree, before a branch exists.

    The same refusal an adoption makes (``vellum.provision``'s
    ``_check_adoption``) and for the same reason one level over: whatever is in
    the working tree would be swept into the upgrade commit and then into a pull
    request about something else.
    """
    dirty = git(root, "status", "--porcelain").stdout.strip()
    if dirty:
        raise UpgradeError(
            f"{root} has uncommitted changes:\n{dirty}\n"
            f"An upgrade commits the files it rewrites, so anything else in the "
            f"tree would land in the same pull request. Commit or stash first."
        )


def _apply(result: Upgrade, *, yes: bool) -> None:
    """Branch, write, commit, and open the pull request or print the commands.

    Everything from the branch onwards runs inside :func:`_wound_back`, so a
    failure part way through the writes leaves the checkout on the branch it
    started on with nothing of this run's in it. Without that, the first
    unwritable path left an operator standing on ``vellum/upgrade-<release>``
    with half a release's files in their tree and a command that had exited —
    which is the one state the "nothing is written" promise is supposed to make
    impossible, arrived at from the other side.
    """
    root = result.checkout
    base, branch = result.base, BRANCH_PREFIX + result.to
    # Before the branch, because a run that cannot name the repository is a run
    # whose printed `gh pr create` would be a placeholder — and finding that out
    # after the commit is finding it out too late.
    slug = _origin_slug(root, required=yes)
    try:
        _clean(root)
        if git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}",
               check=False).returncode != 0:
            raise UpgradeError(
                f"{root} has no {base!r} branch to open the upgrade against. The "
                f"change lands on a branch off the default branch and never on "
                f"the default branch itself, so there has to be one to branch off."
            )
        _branch_is_free(root, branch)
        start = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        # Qualified: a tag that happened to share the branch's name would win
        # git's disambiguation for a bare name.
        git(root, "checkout", "-q", "-b", branch, f"refs/heads/{base}")
    except ProvisionError as exc:
        raise UpgradeError(str(exc)) from exc

    result.branch, result.slug = branch, slug
    with _wound_back(root, start=start, branch=branch) as written:
        for change in result.by(*WRITES):
            path = root / change.path
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Recorded BEFORE the write, not after: a write that fails part
                # way through has still created the file, and a path the
                # wind-back does not know about is one it leaves behind.
                written.append(change.path)
                path.write_text(change.text or "", encoding="utf-8")
                # The same chmod the seed does, for the same reason:
                # `harness/run.py` carries a shebang and an operator will try to
                # execute it.
                if change.path.endswith("run.py"):
                    path.chmod(0o755)
            except OSError as exc:
                raise UpgradeError(f"{path}: cannot write it: {exc}") from exc
        manifest.write(root, result.to, [c.path for c in result.changes])
        written.append(manifest.MANIFEST_RELPATH.as_posix())
        try:
            git(root, "add", "-A")
            git(root, "commit", "-qm", _message(result))
            result.commit = git(root, "rev-parse", "HEAD").stdout.strip()
        except ProvisionError as exc:
            raise UpgradeError(str(exc)) from exc

    body = git_dir(root) / PR_BODY_UNDER_GIT
    try:
        body.parent.mkdir(parents=True, exist_ok=True)
        body.write_text(_body(result), encoding="utf-8")
    except OSError as exc:
        # After the commit, so nothing is wound back: the branch is real and
        # the operator can land it by hand. Said plainly, not as a traceback.
        raise UpgradeError(
            f"{body}: cannot write the pull request body ({exc}). The upgrade "
            f"commit {(result.commit or '')[:12]} exists on {branch}; push it and "
            f"open the pull request by hand."
        ) from exc
    result.pr_body_path = body
    _land(result, yes=yes)


def _branch_is_free(root: Path, branch: str) -> None:
    """Refuse an upgrade branch that already exists, locally or on the remote.

    The remote half is not fussiness. ``vellum/upgrade-<release>`` is one branch
    per release by design, so a second run for the same release is either a
    retry or a second operator — and if the first run pushed, the branch has a
    pull request on it that a force-push from here would rewrite under its
    reviewers. Only what the checkout **already knows** is consulted: no fetch,
    because a command that reached the network to decide whether to refuse would
    answer differently on a laptop with no signal, and `git fetch` before a
    refusal is a side effect on the way to doing nothing.
    """
    for ref, where in ((f"refs/heads/{branch}", "here"),
                       (f"refs/remotes/origin/{branch}", "on origin")):
        if git(root, "rev-parse", "--verify", "--quiet", ref, check=False).returncode != 0:
            continue
        raise UpgradeError(
            f"{root} already has a {branch!r} branch {where}. That is this "
            f"upgrade's branch and something is already on it; delete it or "
            f"merge it rather than having this write over somebody's review. "
            f"(Only refs this checkout already carries were consulted — nothing "
            f"here fetches, so an `origin` this checkout has not seen recently "
            f"may carry one it cannot know about.)"
        )


@contextmanager
def _wound_back(root: Path, *, start: str, branch: str):
    """Run the write-and-commit block, or leave the checkout as it was found.

    The contract is the one the refusals already make and this is the other half
    of it: a run either lands a commit on *branch*, or the checkout is back on
    *start* with no *branch* and nothing of this run's in the tree. Yields the
    list to record written paths on, because untracked ones are the half `git
    checkout -- .` cannot undo.
    """
    written: list[str] = []
    try:
        yield written
    except BaseException as exc:
        trouble = _wind_back(root, start=start, branch=branch, written=written)
        detail = (
            f" The checkout is back on {start!r} and {branch!r} was deleted; "
            f"nothing of this upgrade is left in the tree."
        )
        if trouble:
            detail = (
                f" Putting the checkout back did not fully succeed — "
                f"{'; '.join(trouble)} — so it may still be on {branch!r}: "
                f"`git -C {root} checkout -f {start}` finishes it."
            )
        if isinstance(exc, UpgradeError):
            raise UpgradeError(f"{exc}{detail}") from exc
        raise


def _wind_back(root: Path, *, start: str, branch: str, written: list[str]) -> list[str]:
    """Undo a half-written upgrade. Returns what it could not undo, in words."""
    trouble: list[str] = []
    # FIRST, before anything reads the index: a failure at `git commit` comes
    # after `git add -A`, so every written path is staged by then and
    # `ls-files` would call all of them tracked — leaving the whole upgrade
    # staged on the branch this returns to. `reset --hard` is safe here
    # because `_clean` proved the tree clean before the branch was cut: the
    # only thing it can discard is this run's own writes.
    done = git(root, "reset", "-q", "--hard", check=False)
    if done.returncode != 0:
        trouble.append(f"`git reset --hard` failed ({one_line(done.stderr or done.stdout)})")
    for relative in written:
        tracked = git(root, "ls-files", "--error-unmatch", "--", relative, check=False)
        if tracked.returncode == 0:
            continue  # `git checkout -- .` below puts it back
        try:
            (root / relative).unlink()
        except OSError as exc:
            trouble.append(f"{relative} could not be removed ({one_line(str(exc))})")
    for argv in (("checkout", "-q", "--", "."), ("checkout", "-q", start),
                 ("branch", "-qD", branch)):
        done = git(root, *argv, check=False)
        if done.returncode != 0:
            trouble.append(
                f"`git {' '.join(argv)}` failed "
                f"({one_line(done.stderr or done.stdout)})"
            )
    return trouble


def _origin_slug(root: Path, *, required: bool) -> str | None:
    """``owner/name`` from this checkout's ``origin``, or None when it has none.

    ``gh pr create`` resolves the repository from the directory it runs in, and
    this command's transport does not run it in one: :meth:`Gh.run` inherited
    the *process's* working directory, so ``--yes`` in an operator's shell could
    open the pull request against whatever repository they happened to be
    standing in. Both halves are fixed — the transport is given a cwd and the
    command is given ``--repo`` — because either alone leaves the printed
    fallback commands, which nobody runs from a controlled directory, naming no
    repository at all.

    *required* is ``--yes``: a run that is about to call ``gh`` and cannot name
    the repository refuses before it writes anything, while a run that is only
    going to *print* the commands says so in the line it prints. A checkout with
    no ``origin`` is an ordinary local installation, not a broken one.
    """
    found = git(root, "remote", "get-url", "origin", check=False)
    url = found.stdout.strip() if found.returncode == 0 else ""
    slug = slug_of(url)
    if slug is not None:
        return slug
    if not required:
        return None
    raise UpgradeError(
        f"{root} has no `origin` this can read as a forge repository"
        + (f" (`origin` is {one_line(url)!r})" if url else "")
        + f". --yes opens the pull request with `gh pr create --repo "
        f"<owner/name>`, and the repository is named explicitly rather than "
        f"inferred from wherever this process happens to be running. Set the "
        f"remote (`git -C {root} remote add origin <url>`), or drop --yes and "
        f"run the two commands this prints yourself with the repository you "
        f"mean."
    )


#: The two shapes a forge remote's URL takes: a scheme URL, and the scp-like
#: form ``git@host:owner/name``. Deliberately both anchored on a **host** —
#: a bare local path is a clone of a directory, and taking the last two path
#: components of one would hand `gh pr create --repo` a slug naming somebody
#: else's repository on the forge.
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://(?:[^/@]*@)?[^/]+/(?P<path>.+)$")
_SCP_RE = re.compile(r"^(?:[^/@]+@)?[^/:]+:(?P<path>.+)$")


def slug_of(url: str) -> str | None:
    """``owner/name`` from a remote URL, or None when it is not one."""
    text = url.strip()
    match = _URL_RE.match(text) or _SCP_RE.match(text)
    if match is None:
        return None
    path = match.group("path").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path if SLUG_RE.match(path) else None


def _message(result: Upgrade) -> str:
    written = result.by(*WRITES)
    return (
        f"vellum upgrade: {result.was} -> {result.to}\n"
        f"\n"
        f"Rewrites {len(written)} Vellum-owned file(s) from {result.to}'s "
        f"templates and records the release in "
        f"{manifest.MANIFEST_RELPATH.as_posix()}. Only files this "
        f"installation's manifest names as owned were touched; ownership is "
        f"data and is never inferred (spec/features/installation.md).\n"
        + "".join(f"\n  {c.action:<10} {c.path}" for c in result.changes)
        + "\n"
    )


def _body(result: Upgrade) -> str:
    lines = [
        f"# vellum upgrade: {result.was} → {result.to}",
        "",
        f"Opened by `vellum upgrade --to {result.to}`. Every file below is named "
        f"as Vellum's in `{manifest.MANIFEST_RELPATH.as_posix()}`; nothing else "
        f"in this repository was read or written.",
        "",
        "## Files",
        "",
        "| File | |",
        "|---|---|",
    ]
    lines += [f"| `{c.path}` | {c.action} |" for c in result.changes]
    shape = changes.render(result.shape, result.shape_note,
                           after=result.was, to=result.to)
    fence = _fence(shape)
    lines += ["", "## Installation-shape changes", "", fence]
    lines += shape
    lines += [fence, "",
              "Reverting is pinning back: this is a branch, and the release it "
              "adopts is a cut (`spec/features/certification-and-releases.md`)."]
    return "\n".join(lines) + "\n"


#: The shortest fence Markdown allows.
FENCE = "```"


def _fence(lines) -> str:
    """A code fence longer than any run of backticks in *lines*.

    The content is a release's own changelog prose, and prose about a tool says
    `like this`. A fixed three-backtick fence around it closes on the first line
    that carries three of its own, and everything after that line renders as
    markup in a pull request body — including, in the worst shape of it, a
    heading or a link a summary happened to contain. CommonMark's rule is that a
    fence is closed only by a run at least as long as the one that opened it, so
    the opener counts.
    """
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)),
                  default=0)
    return "`" * max(len(FENCE), longest + 1)


def _land(result: Upgrade, *, yes: bool) -> None:
    """Push and open the pull request, or print the exact commands.

    ``--yes`` is required for the forge half and not for the local half, which
    is the same line the installer draws: writing in a checkout is reversible
    with `git`, and opening a pull request in somebody's organization is a thing
    that happens to other people.
    """
    push = f"git -C {result.checkout} push -u origin {result.branch}"
    create = (
        f"gh pr create --repo {result.slug or '<owner/name>'} "
        f"--base {result.base} --head {result.branch} "
        f'--title "vellum upgrade: {result.was} -> {result.to}" '
        f"--body-file {result.pr_body_path}"
    )
    gh = detect_gh()
    if gh is None or not yes:
        result.manual = [push, create]
        if result.slug is None:
            result.manual.append(
                f"(`{result.checkout}` has no `origin` this could read a "
                f"repository from, so `--repo` above is yours to fill in — the "
                f"command names it explicitly rather than taking whichever "
                f"repository the shell running it happens to be standing in)"
            )
        if gh is not None and not yes:
            result.manual.append(
                "(`gh` is here and authenticated; --yes is what asks it to open "
                "the pull request for you)"
            )
        elif gh is None:
            result.manual.append(
                "(no authenticated `gh` was found, so the forge half is yours; "
                "everything a checkout can hold is done and committed)"
            )
        return
    _open_pr(gh, result)


def _open_pr(gh: Gh, result: Upgrade) -> None:
    try:
        gh.run(("git", "-C", str(result.checkout), "push", "-u", "origin",
                str(result.branch)))
        # `--repo` AND a cwd. `gh` resolves a repository from the directory it
        # runs in, and this process's directory is wherever the operator was
        # standing — so without both, `--yes` opened the pull request against
        # somebody else's repository or none at all.
        created = gh.run((
            "gh", "pr", "create",
            "--repo", str(result.slug),
            "--base", str(result.base),
            "--head", str(result.branch),
            "--title", f"vellum upgrade: {result.was} -> {result.to}",
            "--body-file", str(result.pr_body_path),
        ), cwd=result.checkout)
    except ProvisionError as exc:
        # The commit is made and the branch exists; only the forge half failed.
        # Reported with the commands that finish it rather than raised as a
        # failure of the whole run, which would leave an operator guessing which
        # half happened — the posture `provision._interrupted` takes.
        result.manual = [
            f"# the forge step failed: {one_line(str(exc))}",
            f"git -C {result.checkout} push -u origin {result.branch}",
            f"gh pr create --repo {result.slug or '<owner/name>'} "
            f"--base {result.base} --head {result.branch} "
            f'--title "vellum upgrade: {result.was} -> {result.to}" '
            f"--body-file {result.pr_body_path}",
        ]
        return
    result.pr_url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else None
    # The body existed to be handed to `gh`, and `gh` has taken it. Removed
    # rather than left behind: a file whose only reader has read it is one more
    # thing for the next run — or the next operator — to wonder about.
    if result.pr_body_path is not None:
        result.pr_body_path.unlink(missing_ok=True)
        result.pr_body_path = None


# =====================================================================
# The CLI entry point
# =====================================================================


def run_upgrade(
    checkout: str,
    to: str,
    from_checkout: str | None = None,
    plan_only: bool = False,
    restore: bool = False,
    yes: bool = False,
    out=None,
) -> int:
    """Exit 0 done or planned, 1 an edited owned file, 2 it could not answer."""
    stream = out if out is not None else sys.stdout
    result = upgrade(
        checkout, to=to, from_checkout=from_checkout, plan_only=plan_only,
        restore=restore, yes=yes,
    )
    print(result.report(), file=stream)
    if result.unsafe:
        print(
            f"vellum: upgrade — {len(result.unsafe)} owned path(s) this will not "
            f"write into: "
            f"{', '.join(c.path for c in result.unsafe)}. Nothing was written "
            f"(spec/features/installation.md)",
            file=sys.stderr,
        )
        # Exit 1, the same as an edited file: both are refusals about the
        # installation's tree that the operator has to act on, and the spec
        # names them together. Exit 2 is for a question this could not answer.
        return 1
    if result.refused:
        print(
            f"vellum: upgrade — {len(result.refused)} owned file(s) this "
            f"installation has edited: "
            f"{', '.join(c.path for c in result.refused)}. Nothing was written "
            f"(spec/features/installation.md)",
            file=sys.stderr,
        )
        return 1
    if result.unanswerable:
        print(
            f"vellum: upgrade — cannot verify "
            f"{', '.join(c.path for c in result.unanswerable)} against "
            f"{result.was}; nothing was written",
            file=sys.stderr,
        )
        return 2
    return 0


__all__ = [
    "BRANCH_PREFIX", "Change", "PR_BODY_RELPATH", "Templates", "Upgrade",
    "UpgradeError", "PR_BODY_UNDER_GIT", "compare", "run_upgrade", "side_of", "slug_of",
    "unsafe_write", "upgrade",
]
