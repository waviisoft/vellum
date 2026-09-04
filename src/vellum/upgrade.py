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

A missing owned file is skipped, not recreated
----------------------------------------------
An installation that deleted an owned file deleted it on purpose — the intent
repo this product's own installation pairs with carries no ``harness-ci.yml``
stub by design. Recreating it would make an upgrade undo a decision nobody
re-opened, in a pull request about something else. So a missing owned file is
reported and skipped, and ``--restore`` is how an operator asks for it back.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from vellum import changes, install, manifest, owned, product, seeds
from vellum.gitver import GitUnavailable, resolve, show
from vellum.provision import Gh, ProvisionError, default_branch, detect_gh, git
from vellum.text import one_line
from vellum.workspace import WORKSPACE_RELPATH

#: The branch an upgrade lands on. One per release, so two upgrades in flight
#: are two branches and neither is ``main``: "the upgrade pull request is
#: reviewable and revertible by pinning back"
#: (``spec/decisions/2026-09-04-vellum-owned-files-and-upgrades.md``).
BRANCH_PREFIX = "vellum/upgrade-"

#: Where the pull request's body is written inside the checkout, and
#: deliberately **not** committed — the same shape ``provision.ADOPT_PR_RELPATH``
#: takes, and for the same reason: the transport passes it to ``gh pr create
#: --body-file`` and the printed commands name the same path, so an operator
#: following the printed rung sends the body the transport would have sent
#: rather than a placeholder.
PR_BODY_RELPATH = ".vellum/UPGRADE_PR.md"

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

#: The outcomes that mean a file gets written.
WRITES = (REWRITE, RESTORE, NEW)


class UpgradeError(Exception):
    """The command could not answer: no manifest, no templates, a tree it will not touch."""


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
            return show(self.checkout, ref, path)
        try:
            return seeds.read_source(path)
        except seeds.SeedsMissing:
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

    An intent checkout carries ``.vellum/workspace.yaml`` — the repo map ``init``
    and ``doctor`` both start from — and a product checkout carries
    ``.vellum/product.yaml``, the pin. A checkout with neither is not an
    installation, and one with both is a repository that has been made into two
    things; either way this refuses rather than picking, because the side
    decides whether the stubs are re-stamped and which owned set is even legal.
    """
    has_intent = (root / WORKSPACE_RELPATH).is_file()
    has_product = (root / product.PRODUCT_RELPATH).is_file()
    if has_intent and not has_product:
        return owned.INTENT
    if has_product and not has_intent:
        return owned.PRODUCT
    if has_intent and has_product:
        raise UpgradeError(
            f"{root} carries both {WORKSPACE_RELPATH.as_posix()} and "
            f"{product.PRODUCT_RELPATH.as_posix()}, so this cannot tell which "
            f"side of the pair it is. An intent repo governs product repos and a "
            f"product repo answers to one intent repo "
            f"(spec/features/repo-topology.md); one checkout is one of the two."
        )
    raise UpgradeError(
        f"{root} carries neither {WORKSPACE_RELPATH.as_posix()} nor "
        f"{product.PRODUCT_RELPATH.as_posix()}, so it is not an installation to "
        f"upgrade. Run this in an intent checkout or a product checkout."
    )


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
        if self.refused:
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
            if self.pr_url:
                lines.append(f"  pull request: {self.pr_url}")
            if self.pr_body_path:
                lines.append(f"  pull request body: {self.pr_body_path} (not committed)")
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
    return text.format(**{name: values[name] for name in row.placeholders})


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
) -> list[Change]:
    """What this upgrade would do to every owned path. Writes nothing.

    The whole list is computed before a single byte is written, which is what
    makes "exit 1 and nothing is written" true rather than "exit 1 and some of
    it is written". Two runs of this over one checkout produce the same list.
    """
    table = owned.table(forge)
    values = _values(root, side)
    host, branch = install.installed_shape(root, forge)
    found: list[Change] = []
    for path in listed:
        row = table.get(path)
        target = root / path
        exists = target.is_file()
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
        if row.kind == owned.STUB:
            before = _stub_text(row, ref=was, host=host, branch=branch, forge=forge)
            after = _stub_text(row, ref=to, host=host, branch=branch, forge=forge)
        else:
            before = _template_text(row, source, was, values)
            after = _template_text(row, source, to, values)
        if not exists:
            if after is None:
                found.append(Change(path, RETIRED, (
                    f"is not here and {to} ships none either. Nothing to do."
                )))
            elif restore:
                found.append(Change(path, RESTORE, (
                    f"is not here and --restore was given, so {to}'s copy is "
                    f"written."
                ), text=after))
            elif before is None:
                found.append(Change(path, NEW, (
                    f"{was} shipped none and {to} does, so this file is new in "
                    f"the range and is written."
                ), text=after))
            else:
                found.append(Change(path, MISSING, (
                    f"is owned but not here. Skipped, not recreated: an "
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
                f"is here and owned, but {was} shipped no template for it, so "
                f"nothing can say whether it is as Vellum left it. Rewriting it "
                f"would overwrite whatever it actually is."
            )))
            continue
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            found.append(Change(path, UNVERIFIABLE, (
                f"could not be read ({one_line(str(exc))}), so it cannot be "
                f"compared against what {was} shipped."
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
    return found


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
    if not install.REF_RE.match(str(to)):
        raise UpgradeError(
            f"--to {to!r} is not a usable release. It is handed to git as a ref "
            f"and stamped into the caller stubs' `uses:` lines, so it must be a "
            f"plain tag that `git check-ref-format` would accept."
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

    found = compare(
        root, installed.owned, source=source, was=installed.release, to=to,
        side=side, forge=forge, restore=restore,
    )
    shape, note = _shape(source, to, installed.release)
    result = Upgrade(
        checkout=root, side=side, source=source, was=installed.release, to=to,
        changes=found, shape=shape, shape_note=note, plan_only=plan_only,
        restore=restore,
    )
    if result.refused or result.unanswerable or plan_only:
        return result
    _apply(result, yes=yes)
    return result


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
    """Branch, write, commit, and open the pull request or print the commands."""
    root = result.checkout
    try:
        _clean(root)
        base = default_branch(root, install.DEFAULT_BRANCH)
        if git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}",
               check=False).returncode != 0:
            raise UpgradeError(
                f"{root} has no {base!r} branch to open the upgrade against. The "
                f"change lands on a branch off the default branch and never on "
                f"the default branch itself, so there has to be one to branch off."
            )
        branch = BRANCH_PREFIX + result.to
        if git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
               check=False).returncode == 0:
            raise UpgradeError(
                f"{root} already has a {branch!r} branch. That is this upgrade's "
                f"branch and something is already on it; delete it or merge it "
                f"rather than having this write over somebody's review."
            )
        git(root, "checkout", "-q", "-b", branch, base)
    except ProvisionError as exc:
        raise UpgradeError(str(exc)) from exc

    result.base, result.branch = base, branch
    for change in result.by(*WRITES):
        path = root / change.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(change.text or "", encoding="utf-8")
            # The same chmod the seed does, for the same reason: `harness/run.py`
            # carries a shebang and an operator will try to execute it.
            if change.path.endswith("run.py"):
                path.chmod(0o755)
        except OSError as exc:
            raise UpgradeError(f"{path}: cannot write it: {exc}") from exc
    manifest.write(root, result.to, [c.path for c in result.changes])

    try:
        git(root, "add", "-A")
        git(root, "commit", "-qm", _message(result))
        result.commit = git(root, "rev-parse", "HEAD").stdout.strip()
    except ProvisionError as exc:
        raise UpgradeError(str(exc)) from exc

    body = root / PR_BODY_RELPATH
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text(_body(result), encoding="utf-8")
    result.pr_body_path = body
    _land(result, yes=yes)


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
    lines += ["", "## Installation-shape changes", "", "```"]
    lines += changes.render(result.shape, result.shape_note,
                            after=result.was, to=result.to)
    lines += ["```", "",
              "Reverting is pinning back: this is a branch, and the release it "
              "adopts is a cut (`spec/features/certification-and-releases.md`)."]
    return "\n".join(lines) + "\n"


def _land(result: Upgrade, *, yes: bool) -> None:
    """Push and open the pull request, or print the exact commands.

    ``--yes`` is required for the forge half and not for the local half, which
    is the same line the installer draws: writing in a checkout is reversible
    with `git`, and opening a pull request in somebody's organization is a thing
    that happens to other people.
    """
    push = f"git -C {result.checkout} push -u origin {result.branch}"
    create = (
        f"gh pr create --repo <this repo> --base {result.base} "
        f"--head {result.branch} "
        f'--title "vellum upgrade: {result.was} -> {result.to}" '
        f"--body-file {result.pr_body_path}"
    )
    gh = detect_gh()
    if gh is None or not yes:
        result.manual = [push, create]
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
        created = gh.run((
            "gh", "pr", "create",
            "--base", str(result.base),
            "--head", str(result.branch),
            "--title", f"vellum upgrade: {result.was} -> {result.to}",
            "--body-file", str(result.pr_body_path),
        ))
    except ProvisionError as exc:
        # The commit is made and the branch exists; only the forge half failed.
        # Reported with the commands that finish it rather than raised as a
        # failure of the whole run, which would leave an operator guessing which
        # half happened — the posture `provision._interrupted` takes.
        result.manual = [
            f"# the forge step failed: {one_line(str(exc))}",
            f"git -C {result.checkout} push -u origin {result.branch}",
            f"gh pr create --base {result.base} --head {result.branch} "
            f'--title "vellum upgrade: {result.was} -> {result.to}" '
            f"--body-file {result.pr_body_path}",
        ]
        return
    result.pr_url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else None


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
    "BRANCH_PREFIX", "Change", "Templates", "Upgrade", "UpgradeError", "compare",
    "run_upgrade", "side_of", "upgrade",
]
