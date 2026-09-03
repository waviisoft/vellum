"""``vellum init`` and ``vellum doctor`` — the adapters install thin.

``spec/features/installation.md``: "The forge adapters ship once and install
thin. The Vellum product repo hosts the real logic of each adapter workflow
(``spec-ci``, ``on-spec-merge``, ``harness-ci``) as a reusable workflow ... and
an intent repo carries one **caller stub** per workflow: a few lines naming the
reusable workflow at a pinned Vellum ref and passing the secrets it needs. A
stub holds no logic, so it has nothing to drift."

The shape, and why it is a generator
------------------------------------
The stubs are *rendered* from the table below rather than copied out of files on
disk, because an installed CLI is a wheel: ``adapters/github/`` and
``.github/workflows/`` are repository paths, not package data, and a command
that read them would work from a development checkout and fail from a pip
install. ``adapters/github/`` still holds a committed rendering of every stub —
that is what a reviewer reads and what an operator copies by hand when they have
no CLI — and ``tests/test_install.py`` asserts the two are byte-identical, so
the reviewable artifact cannot drift from the one that gets stamped. Which is
the same argument the stubs themselves make one level down.

What lives in a stub, and what does not
---------------------------------------
A stub carries the *caller's* half and nothing else: the triggers, the
``permissions`` grant, the ``concurrency`` group, one ``uses:`` job, and the
secrets by name.

* **Triggers** are the caller's because they are statements about the caller's
  repository — which paths, which branch — and a reusable workflow has no
  trigger but ``workflow_call``.
* **``permissions``** is the caller's because a called workflow's token can only
  be *narrowed* by the callee, never widened. The grant has to be made where the
  run starts or the callee's jobs are refused it.
* **``concurrency``** is the caller's because a group serialises the runs of one
  repository, and two installations sharing a group would serialise unrelated
  repositories against each other.
* **``secrets:`` by name, never ``secrets: inherit``** — ``spec/features/
  installation.md``: "a stub passes each secret by name and never inherits the
  caller's whole secret set, so a reusable workflow holds exactly the credential
  its job names and nothing else in the installation". ``doctor`` treats
  ``inherit`` as a finding rather than a style note for that reason.

None of those can drift into a wrong *answer* — a trigger that is wrong does not
run, a permission that is wrong is refused — but every one of them fails
**silently**, which is the same defect wearing a different coat: a trigger
narrowed to `paths:` is a required check that never reports and a PR that waits
forever. So ``doctor`` compares all three against what ships (``CALLER_HALF``),
parsed rather than as text, so a stub somebody annotated is not a finding. What
used to drift was a copied `run:` body; there is none here, and the blocks that
remain are checked.

The ref, and the two places it appears
--------------------------------------
``uses: ...@<ref>`` pins the workflow file; ``with: vellum-ref: <ref>`` pins the
CLI the workflow installs. They are stamped equal and ``doctor`` reports when
they have come apart, which is the honest shape: the ``@<ref>`` alone does not
pin the CLI, because the checkout of ``waviisoft/vellum`` inside the body needs
a ref it can be handed. Deriving the second from ``github.job_workflow_sha``
would remove the second line at the cost of making the CLI version invisible in
the installed file, and an installation's version has to be readable in the
repository that runs it.

What a checkout cannot know
---------------------------
Two things, said rather than passed over (``spec/features/installation.md``):
whether the ``VELLUM_TOKEN`` secret is actually set, which is forge state; and
whether the forge permits reuse of a private repository's workflows within the
organization, which is an Actions setting on ``waviisoft/vellum``. ``doctor``
prints both every time. Ref currency is the third thing a bare checkout cannot
answer — it needs the *release* side — and it is reported, never failed on,
mirroring the divergence posture (``spec/features/repo-topology.md``).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vellum import __version__
from vellum.gitver import GitUnavailable, tags
from vellum.text import one_line
from vellum.workspace import SLUG_RE, WorkspaceError, forge as workspace_forge
from vellum.workspace import load as workspace_load
from vellum.workspace import intent as workspace_intent
from vellum.workspace import products as workspace_products
from vellum.workspace import workspace_path

#: The repo that hosts the reusable workflows. A constant rather than a lookup
#: in ``.vellum/workspace.yaml``: the workspace maps an installation's *own*
#: product repos, and the workflows come from the vendor's, which for every
#: installation is this one. ``--from`` exists for a fork or an internal mirror
#: — and a fork has one more line to change than the stub does: the shipped
#: workflows check the CLI out of ``waviisoft/vellum`` by name, so a fork that
#: wants its own CLI edits its own copy of them. That is the fork's file to
#: edit, and nothing here could edit it for them.
HOST_REPO = "waviisoft/vellum"

#: Where a forge keeps its workflows inside a repository, per forge.
WORKFLOWS_DIR = {"github": Path(".github") / "workflows"}

#: The forges ``init`` has stubs for. ``spec/features/installation.md`` names
#: GitLab's ``include:`` as the same core's other emission; until that emission
#: exists, a workspace naming it is "I cannot answer", not "GitHub will do".
FORGES = tuple(sorted(WORKFLOWS_DIR))

#: A pinnable ref: a tag, a branch, or a sha. Narrow because it is pasted into
#: a ``uses:`` line the forge then resolves — a value carrying whitespace, a
#: quote or a newline is a value that reshapes the file it is written into.
REF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")

#: A release of this product: ``v`` and a dotted integer version. Read for
#: *reporting* currency and nothing else, so a tag that does not match is
#: ignored rather than refused — the same posture ``gitver.TAG_RE`` takes to
#: the decorative ``spec-v<N>`` names.
RELEASE_RE = re.compile(r"^v(\d+(?:\.\d+)*)$")

#: The secret every shipped workflow declares.
SECRET = "VELLUM_TOKEN"

#: The input every shipped workflow declares.
REF_INPUT = "vellum-ref"


class InstallError(Exception):
    """The command could not answer: no workspace, an unknown forge, a bad ref."""


def default_ref() -> str:
    """The ref ``init`` pins when the caller names none: this CLI's own version.

    ``spec/features/installation.md``: "pinning the ref it is given or the CLI's
    own version by default". Whether ``waviisoft/vellum`` actually carries that
    tag is not knowable from an intent checkout, so the report says so rather
    than the command guessing a ref that does exist.
    """
    return f"v{__version__}"


# =====================================================================
# What ships, and the caller half of each.
# =====================================================================


@dataclass(frozen=True)
class Shipped:
    """One reusable workflow and the caller stub that invokes it."""

    name: str
    #: Prose for the top of the stub, after the generated banner.
    about: str
    #: The stub's ``on:`` block, verbatim.
    triggers: str
    #: The stub's ``permissions:`` block, verbatim, with the note that explains it.
    permissions: str
    #: The stub's ``concurrency:`` block, verbatim.
    concurrency: str

    @property
    def filename(self) -> str:
        return f"{self.name}.yml"


SPEC_CI = Shipped(
    name="spec-ci",
    about="""# Lint, suite extraction and the divergence report on every spec PR
# (spec/features/spec-pipeline.md). The three agent reviews are stubs in v0.1
# and pass vacuously; the backpressure check reports and does not block.""",
    triggers="""on:
  pull_request:
    paths:
      - 'spec/**'
      - '.github/workflows/spec-ci.yml'
      # The backpressure job reads these, so a PR that changes them has to
      # re-run it: `divergence_cap` lives in the config, and the window is
      # counted from the ledger. Without these a PR could raise the cap, or
      # add unshipped versions, without the gate that reads them ever running.
      - '.vellum/config.yaml'
      - 'ledger/**'""",
    permissions="""# Granted here because a called workflow's token can only be narrowed by the
# callee, never widened: `pull-requests: write` is what the agent-review job
# asks for, and the callee's own `permissions:` can only take away from this.
permissions:
  contents: read
  pull-requests: write""",
    concurrency="""concurrency:
  group: spec-ci-${{ github.event.pull_request.number }}
  cancel-in-progress: true""",
)

ON_SPEC_MERGE = Shipped(
    name="on-spec-merge",
    about="""# The bookkeeping a spec merge leaves behind: `vellum mint` opens the ledger
# record for the merge commit, the workflow tags the decorative name, extracts
# the suite, files work items and pushes (spec/features/spec-pipeline.md).""",
    triggers="""on:
  push:
    branches: [main]
    paths:
      - 'spec/**'
  workflow_dispatch:
    inputs:
      reason:
        description: 'Why this is being run by hand'
        required: false""",
    permissions="""# Granted here because a called workflow's token can only be narrowed by the
# callee, never widened. `contents: write` pushes the tag and the ledger
# commit; `issues: write` files work items. Branch protection on `main` must
# let the workflow token push, or the ledger commit step fails.
permissions:
  contents: write
  issues: write""",
    concurrency="""# The reconciler is idempotent (decision D11) and so is every step the called
# workflow runs, but two concurrent runs writing the same ledger record would
# still race on the push. The group is this repository's alone.
concurrency:
  group: on-spec-merge
  cancel-in-progress: false""",
)

HARNESS_CI = Shipped(
    name="harness-ci",
    about="""# The write boundary on harness PRs, and the acceptance suite
# (spec/behaviors/write-boundaries.md, spec/features/scenarios-and-harness.md).
#
# NO `paths:` FILTER, DELIBERATELY. A path-filtered required check never
# reports on the PRs it filters out and GitHub leaves those waiting forever, so
# a job that must be required has to run on every PR. It is also the reason
# these checks could not live in spec-ci: the breach they guard — a harness
# session also editing `.vellum/memory/` — is a diff touching none of
# spec-ci's paths.
#
# Do not install this ahead of a `write_boundaries` block in
# `.vellum/config.yaml`: with nothing to check against, the boundary job exits
# 2 ("I could not answer") on any PR that writes `harness/`.""",
    triggers="""on:
  pull_request:""",
    permissions="""# Granted here because a called workflow's token can only be narrowed by the
# callee, never widened.
permissions:
  contents: read""",
    concurrency="""concurrency:
  group: harness-ci-${{ github.event.pull_request.number }}
  cancel-in-progress: true""",
)

#: Everything this repo ships, in the order a report lists it.
SHIPPED: tuple[Shipped, ...] = (SPEC_CI, ON_SPEC_MERGE, HARNESS_CI)

BANNER = """# {name} — the caller stub. Stamped by `vellum init`; edit the ref, not the body.
#
# The logic is a reusable workflow in {host}, pinned below. A stub holds
# no logic, so it has nothing to drift: upgrading this installation is bumping
# the ref on the two lines that carry it — `vellum init --ref <new> --force` —
# and `vellum doctor` checks that what is installed is what ships. A `run:` or a
# second job here is a finding, not a customisation
# (spec/features/installation.md).
#
{about}

"""

BODY = """name: {name}

{triggers}

{permissions}

{concurrency}

jobs:
  {name}:
    uses: {host}/{workflows_dir}/{filename}@{ref}
    with:
      # The ref the CLI itself is checked out and installed from, kept equal to
      # the ref above so one installation runs one version of both.
      {ref_input}: {ref}
    # By name, never `secrets: inherit`: the reusable workflow holds exactly the
    # credential its jobs name and nothing else in this installation
    # (spec/features/installation.md).
    secrets:
      {secret}: ${{{{ secrets.{secret} }}}}
"""


def render(shipped: Shipped, *, host: str = HOST_REPO, ref: str, forge: str = "github") -> str:
    """One caller stub, as the text ``init`` writes and ``adapters/`` holds.

    Both interpolated values are validated, and for one reason: they are pasted
    into a `uses:` line in a workflow file a forge then executes. A `--from`
    carrying a newline and two spaces of indent does not name a fork — it closes
    the `uses:` line and opens a second job, in a file this command stamps with
    `pull-requests: write`. Neither value is more trusted than the other because
    both arrive on the command line.
    """
    if not SLUG_RE.match(host):
        raise InstallError(
            f"--from {host!r} is not an `owner/name` repo slug. It is pasted into "
            f"a `uses:` line the forge resolves, so a value carrying whitespace or "
            f"a newline would reshape the workflow file this writes."
        )
    if not REF_RE.match(ref):
        raise InstallError(
            f"--ref {ref!r} is not a usable ref. It is pasted into a `uses:` line "
            f"the forge resolves, so it must be a plain tag, branch or sha."
        )
    return BANNER.format(name=shipped.name, host=host, about=shipped.about) + BODY.format(
        name=shipped.name,
        host=host,
        workflows_dir=WORKFLOWS_DIR[forge].as_posix(),
        filename=shipped.filename,
        ref=ref,
        ref_input=REF_INPUT,
        secret=SECRET,
        triggers=shipped.triggers,
        permissions=shipped.permissions,
        concurrency=shipped.concurrency,
    )


def check_forge(forge: str) -> str:
    if forge not in WORKFLOWS_DIR:
        raise InstallError(
            f"forge {forge!r} is not one this CLI has stubs for; it has "
            f"{', '.join(FORGES)}. spec/features/installation.md names GitLab's "
            f"`include:` as the same core's other emission — until it exists, "
            f"stamping GitHub stubs into a {forge} installation would be a guess."
        )
    return forge


def read_forge(checkout: str | Path, override: str | None = None) -> str:
    """The forge to stamp for: ``--forge`` if given, else the workspace's."""
    if override is not None:
        return check_forge(override.strip().lower())
    try:
        return check_forge(workspace_forge(checkout))
    except WorkspaceError as exc:
        raise InstallError(str(exc)) from exc


# =====================================================================
# Release currency. Reported, never failed on.
# =====================================================================


def releases(checkout: str | Path) -> list[str]:
    """Every ``v*`` release tag in a ``waviisoft/vellum`` checkout, oldest first.

    Sorted as version tuples, not lexically: ``v0.10.0`` is newer than
    ``v0.9.0`` and a `sort` that says otherwise names the wrong "newest
    release" — the same lexical hazard the old `spec-v<N>` minting had.
    """
    found: list[tuple[tuple[int, ...], str]] = []
    for tag in tags(Path(checkout), "v*"):
        match = RELEASE_RE.match(tag)
        if match:
            found.append((tuple(int(p) for p in match.group(1).split(".")), tag))
    return [tag for _, tag in sorted(found)]


@dataclass
class Currency:
    """What is known about the pinned refs against the newest release."""

    #: The checkout releases were read from, or None when none was supplied.
    source: Path | None = None
    #: Why currency could not be established, when it could not.
    unknown: str | None = None
    known: list[str] = field(default_factory=list)

    @property
    def newest(self) -> str | None:
        return self.known[-1] if self.known else None

    def about(self, ref: str) -> str:
        """One line about *ref*, always reporting and never judging."""
        if self.newest is None:
            return f"currency not checked ({self.unknown})"
        if ref == self.newest:
            return f"current ({self.newest} is the newest release)"
        if ref in self.known:
            behind = len(self.known) - 1 - self.known.index(ref)
            return f"behind by {behind} release(s); newest is {self.newest}"
        if RELEASE_RE.match(ref):
            # A release tag this checkout does not carry. Ahead of what was
            # read, not "not a release tag" — which is what an operator sees on
            # a pre-release install, or against a checkout whose tags are stale,
            # and saying the wrong one of those out loud is worse than saying
            # nothing.
            return (
                f"pinned to {ref}, a release tag not among the ones read here "
                f"(newest read: {self.newest}); this checkout's tags may be behind"
            )
        return (
            f"pinned to {one_line(ref)!r}, which is not a release tag, so it "
            f"cannot be compared; newest release is {self.newest}"
        )


def currency(releases_from: str | Path | None) -> Currency:
    """Read the release tags, or record why they could not be read.

    Every failure here is a *report*, never an exception. ``spec/features/
    installation.md``: ref currency is "reported, never failed on" — so a
    `--releases-from` naming something that is not a checkout must not turn
    doctor red, any more than a pin behind spec-head fails conformance CI.
    """
    if releases_from is None:
        return Currency(unknown=(
            "no waviisoft/vellum checkout was supplied; pass --releases-from "
            "<checkout> to compare the pinned ref against the newest release"
        ))
    source = Path(releases_from)
    try:
        known = releases(source)
    except GitUnavailable as exc:
        return Currency(source=source, unknown=f"{source} is not a readable git checkout: {exc}")
    if not known:
        return Currency(source=source, unknown=(
            f"{source} carries no v* release tags. This product has cut no release "
            f"yet, so there is no newest release to be behind."
        ))
    return Currency(source=source, known=known)


# =====================================================================
# `vellum init`
# =====================================================================

#: What happened to one stub. ``left`` is a stub that exists and differs, which
#: init reports and does not touch: writing is init's job and judging is
#: doctor's, and silently rewriting an operator's file is neither.
WROTE, INSTALLED, LEFT = "wrote", "installed", "left alone"


@dataclass
class Stamp:
    """One stub, and what init did about it.

    The path names the stub; there is no ``shipped`` field beside it, because
    nothing reads one. This module is otherwise careful not to write surface
    ahead of a reader (``vellum.workspace``'s docstring makes the argument).
    """

    path: Path
    outcome: str


@dataclass
class Init:
    """One run of ``vellum init``."""

    checkout: Path
    forge: str
    intent: str
    products: dict[str, str]
    host: str
    ref: str
    currency: Currency
    stamps: list[Stamp]

    def report(self) -> str:
        lines = [
            f"vellum init — {self.forge} caller stubs in {self.checkout}",
            f"  intent repo:  {self.intent}",
            f"  workflows:    {self.host} at {self.ref}",
            "  products:     " + ", ".join(
                # Read out of `.vellum/workspace.yaml`, which anyone who can
                # land a merge in the intent repo writes, and printed into a
                # report a caller may pipe into a forge step summary.
                f"{one_line(name)} ({one_line(repo) or 'no repo declared'})"
                for name, repo in sorted(self.products.items())
            ),
            "",
        ]
        for stamp in self.stamps:
            lines.append(f"  {stamp.outcome:<11} {stamp.path}")
        lines.append("")
        counts = {outcome: 0 for outcome in (WROTE, INSTALLED, LEFT)}
        for stamp in self.stamps:
            counts[stamp.outcome] += 1
        if counts[WROTE]:
            lines.append(
                f"{counts[WROTE]} stub(s) written, {counts[INSTALLED]} already "
                f"installed, {counts[LEFT]} left alone."
            )
        else:
            lines.append(
                f"Nothing to do: {counts[INSTALLED]} stub(s) already match what "
                f"ships and {counts[LEFT]} differ and were left alone."
            )
        if counts[LEFT]:
            lines.append(
                "A stub that differs is not rewritten — writing is this command's "
                "job and judging is `vellum doctor`'s. Run doctor for what it "
                "finds, or `vellum init --force` to restamp. Note the two ask "
                "different questions: this command compared the whole file byte "
                "for byte, and doctor checks the delegation, the pinned ref, the "
                "secret and the caller half — so a comment somebody added is a "
                "difference here and no finding there."
            )
        lines.append("")
        lines.append(f"Pinned ref {self.ref}: {self.currency.about(self.ref)}.")
        if self.ref == default_ref() and self.currency.newest is None:
            lines.append(
                f"That is this CLI's own version ({__version__}), pinned because no "
                f"--ref was given. Nothing in an intent checkout can confirm that "
                f"{self.host} carries the tag {self.ref}; if it does not, the "
                f"stubs resolve to nothing and every run fails at `uses:`."
            )
        lines.append("")
        lines += CANNOT_KNOW
        return "\n".join(lines)


#: Printed by both commands, every time. ``spec/features/installation.md``:
#: "What a checkout cannot know ... doctor says it cannot check, rather than
#: passing over." Saying it on a green run is the whole point — a report that
#: only lists its blind spots when something else went wrong is one nobody
#: reads at the moment they matter.
CANNOT_KNOW = [
    "What a checkout cannot tell you, and this command therefore did not check:",
    f"  * whether the {SECRET} secret is set on the intent repo. That is forge",
    "    state, not repository state. Each shipped workflow asserts it in its",
    "    first step and fails with a named error when it is empty.",
    f"  * whether {HOST_REPO} allows its workflows to be reused by other",
    "    repositories in the organization. It is a private repo, so reuse needs",
    "    Actions > General > 'Accessible from repositories in the organization'",
    "    on it. Without that the caller's run fails at `uses:` with a resolution",
    "    error, and nothing in either checkout can see the setting.",
]


def init(
    checkout: str | Path,
    ref: str | None = None,
    host: str = HOST_REPO,
    forge: str | None = None,
    force: bool = False,
    releases_from: str | Path | None = None,
) -> Init:
    """Stamp the caller stubs into an intent checkout. Idempotent."""
    root = Path(checkout)
    if not root.is_dir():
        raise InstallError(f"{root}: not a directory; is this an intent checkout?")
    chosen = read_forge(root, forge)
    try:
        intent_slug = workspace_intent(root)
        declared = workspace_products(root)
    except WorkspaceError as exc:
        raise InstallError(str(exc)) from exc
    pinned = ref if ref is not None else default_ref()

    directory = root / WORKFLOWS_DIR[chosen]
    stamps: list[Stamp] = []
    for shipped in SHIPPED:
        path = directory / shipped.filename
        text = render(shipped, host=host, ref=pinned, forge=chosen)
        existing: str | None = None
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # There is a file here and this could not read it — a permission
                # the operator has set, or bytes that are not UTF-8 text. NOT
                # rewritten: "init does not destroy a stub it did not write"
                # cannot have an exception for the files it understands least.
                # (`UnicodeDecodeError` is a ValueError, not an OSError, which
                # is why it is named: uncaught it left both commands exiting 1
                # with a traceback, and 1 is the code that must mean a finding.)
                stamps.append(Stamp(path, LEFT))
                continue
        if existing == text:
            stamps.append(Stamp(path, INSTALLED))
            continue
        if existing is not None and not force:
            stamps.append(Stamp(path, LEFT))
            continue
        # A write that cannot happen is "I could not answer", not a traceback.
        # This command's whole contract is its exit code, and a read-only tree
        # or a `.github/workflows` that is a file are both things an operator
        # can be told about in one line.
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise InstallError(f"{path}: cannot write the stub: {exc}") from exc
        stamps.append(Stamp(path, WROTE))
    return Init(
        checkout=root,
        forge=chosen,
        intent=intent_slug,
        products=declared,
        host=host,
        ref=pinned,
        currency=currency(releases_from),
        stamps=stamps,
    )


def run_init(
    checkout: str,
    ref: str | None = None,
    host: str = HOST_REPO,
    forge: str | None = None,
    force: bool = False,
    releases_from: str | None = None,
    out=None,
) -> int:
    """Report what was stamped. Exit 0: it wrote, or there was nothing to do."""
    stream = out if out is not None else sys.stdout
    print(init(checkout, ref=ref, host=host, forge=forge, force=force,
               releases_from=releases_from).report(), file=stream)
    return 0


# =====================================================================
# `vellum doctor`
# =====================================================================


@dataclass(frozen=True)
class Finding:
    """One way an installed stub is not what ships."""

    file: str
    code: str
    detail: str


def _walk(node):
    """Every mapping in a parsed workflow, the stub's own included."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _jobs(data: dict):
    """The ``jobs:`` mapping, or None when there is not one."""
    jobs = data.get("jobs")
    return jobs if isinstance(jobs, dict) else None


#: What a stub carries on the caller's behalf, and what `doctor` compares.
#: These three are the whole of the caller half (see the module docstring), and
#: none of them mentions the ref or the host — so a stub pinned to an older
#: release is compared against the current shape without being restamped, which
#: is what keeps currency reported and drift failed.
CALLER_HALF = ("on", "permissions", "concurrency")


def _caller_half(data: dict) -> dict:
    """``on:``, ``permissions:`` and ``concurrency:``, parsed, by name.

    YAML 1.1 reads a bare ``on`` as the boolean ``True``, which is why this is a
    function and not three ``.get()`` calls at the call site: getting it wrong
    once would make every stub's triggers compare as absent-and-equal, and the
    check would pass on everything.
    """
    return {
        "on": data[True] if True in data else data.get("on"),
        "permissions": data.get("permissions"),
        "concurrency": data.get("concurrency"),
    }


def inspect(
    path: Path, shipped: Shipped, *, host: str, forge: str, relative: str
) -> tuple[list[Finding], str | None]:
    """Read one installed stub. Returns its findings and the ref it pins."""
    found: list[Finding] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # A ValueError, not an OSError. Uncaught it left the command exiting 1
        # with a traceback, which is the one code that must mean "a finding".
        return [Finding(relative, "unparseable", f"is not UTF-8 text: {one_line(exc)}")], None
    except OSError:
        return [Finding(relative, "missing", (
            f"no stub for the shipped workflow {shipped.name!r}. `vellum init` "
            f"stamps one; without it that half of the pipeline never runs."
        ))], None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [Finding(relative, "unparseable", f"not valid YAML: {one_line(exc)}")], None
    if not isinstance(data, dict):
        return [Finding(relative, "unparseable", "not a YAML mapping, so not a workflow")], None

    jobs = _jobs(data)
    if jobs is None:
        return [Finding(relative, "unparseable", "declares no `jobs:` mapping")], None

    # ------------------------------------------------------------------
    # The caller half, compared against what ships.
    #
    # A stub carries three blocks besides its one `uses:` job, and each of them
    # is load-bearing in a way that fails SILENTLY when it is wrong: a trigger
    # narrowed to `paths:` makes a required check that never reports and leaves
    # every PR waiting forever; a `permissions` block narrowed below what a job
    # asks for makes a run refused at the point of use; a `concurrency` group
    # renamed stops serialising the thing it exists to serialise. None of those
    # reddens on its own, which is exactly the class of drift this whole wave
    # is about — so "installed matches shipped" has to mean these too, or the
    # claim is narrower than the sentence.
    #
    # Compared PARSED, not as text: a stub whose comments an operator has added
    # to has not drifted, and a check that said otherwise would be one people
    # learn to ignore. And compared against a render at THIS stub's own pinned
    # ref, so an installation behind the newest release is not reported as
    # drifted — currency is reported and never failed on, and the two must not
    # bleed into each other.
    # ------------------------------------------------------------------
    shipped_half = _caller_half(yaml.safe_load(render(shipped, host=host, forge=forge,
                                                      ref=default_ref())))
    installed_half = _caller_half(data)
    for block in CALLER_HALF:
        if installed_half[block] != shipped_half[block]:
            found.append(Finding(relative, "drifted", (
                f"its `{block}:` is not what ships. Installed "
                f"{one_line(str(installed_half[block]))}; ships "
                f"{one_line(str(shipped_half[block]))}. A stub carries this block "
                f"on the caller's behalf and a wrong one fails silently — a "
                f"narrowed trigger is a required check that never reports, a "
                f"narrowed permission is a job refused at the point of use. "
                f"`vellum init --force` restamps it."
            )))

    # A stub that has been edited in place to carry logic. Both halves of the
    # test matter: a second job is logic beside the delegation, and a `run:`
    # anywhere is logic inside it. `spec/features/installation.md`: "a stub that
    # has been edited in place to carry logic is a finding, named by file."
    if len(jobs) != 1:
        found.append(Finding(relative, "carries-logic", (
            f"declares {len(jobs)} jobs ({one_line(', '.join(map(str, jobs)))}); a caller stub "
            f"is one job that delegates. A job of its own is logic that can drift "
            f"from what ships (spec/features/installation.md)."
        )))
    runs = [node for node in _walk(data) if "run" in node]
    if runs:
        found.append(Finding(relative, "carries-logic", (
            f"carries {len(runs)} `run:` body/bodies. A stub holds no logic — that "
            f"is what gives it nothing to drift; move the body into the reusable "
            f"workflow in {host} (spec/features/installation.md)."
        )))

    want = f"{host}/{WORKFLOWS_DIR[forge].as_posix()}/{shipped.filename}"
    ref: str | None = None
    caller = None
    for name, job in jobs.items():
        uses = job.get("uses") if isinstance(job, dict) else None
        if isinstance(uses, str) and uses.split("@", 1)[0] == want:
            caller, ref = job, (uses.split("@", 1)[1] if "@" in uses else None)
            break
    if caller is None:
        named = sorted(
            str(job.get("uses")) for job in jobs.values()
            if isinstance(job, dict) and job.get("uses")
        )
        found.append(Finding(relative, "wrong-workflow", (
            f"no job delegates to {want}; it uses "
            f"{one_line(', '.join(named)) or '(nothing)'}. "
            f"A stub names the shipped workflow it stands for."
        )))
        return found, None

    if not ref:
        found.append(Finding(relative, "unpinned", (
            f"`uses: {want}` names no ref. An installation pins the Vellum ref it "
            f"runs, and upgrading is bumping it (spec/features/installation.md)."
        )))

    secrets = caller.get("secrets")
    if secrets == "inherit":
        found.append(Finding(relative, "secrets-inherit", (
            "passes `secrets: inherit`. A stub passes each secret by name, so the "
            "reusable workflow holds exactly the credential its job names and "
            "nothing else in this installation (spec/features/installation.md)."
        )))
    elif not isinstance(secrets, dict) or SECRET not in secrets:
        found.append(Finding(relative, "no-secret", (
            f"does not pass {SECRET} by name. The shipped workflow declares it and "
            f"checks it out {host} with it; without it every run fails in its first "
            f"step."
        )))

    inputs = caller.get("with")
    passed = inputs.get(REF_INPUT) if isinstance(inputs, dict) else None
    if passed is None:
        found.append(Finding(relative, "no-cli-ref", (
            f"passes no `{REF_INPUT}`. The shipped workflow checks the CLI out of "
            f"{host} at that ref and declares it required."
        )))
    elif ref and str(passed) != ref:
        found.append(Finding(relative, "ref-mismatch", (
            f"pins the workflow at {one_line(ref)!r} and the CLI at "
            f"{one_line(passed)!r}. One "
            f"installation runs one version of both; `vellum init --ref <ref> "
            f"--force` restamps them together."
        )))
    return found, ref


@dataclass
class Doctor:
    """One run of ``vellum doctor``."""

    checkout: Path
    forge: str
    host: str
    currency: Currency
    #: ``(relative path, findings, pinned ref)`` per shipped workflow, in the
    #: order they ship. No ``Shipped`` beside the path: nothing reads one.
    stubs: list[tuple[str, list[Finding], str | None]]

    @property
    def findings(self) -> list[Finding]:
        return [f for _, found, _ in self.stubs for f in found]

    def report(self) -> str:
        lines = [
            f"vellum doctor — {self.forge} caller stubs in {self.checkout}",
            f"  shipped from: {self.host}",
            "",
        ]
        for relative, found, ref in self.stubs:
            mark = "FINDING" if found else "ok"
            lines.append(f"  {mark:<8} {relative}"
                         + (f"  @{ref}" if ref else ""))
            for finding in found:
                lines.append(f"           - [{finding.code}] {finding.detail}")
        lines.append("")
        if self.findings:
            lines.append(
                f"BLOCKED: {len(self.findings)} finding(s) across "
                f"{len({f.file for f in self.findings})} file(s). What is installed "
                f"is not what ships (spec/features/installation.md)."
            )
        else:
            lines.append(
                "OK: every shipped workflow has a stub that names it and pins a ref."
            )
        lines.append("")

        # ------------------------------------------------------------------
        # Reported, never failed on. `spec/features/installation.md`: "Ref
        # currency ... is **reported, never failed on**, mirroring the
        # divergence posture: an installation behind is divergence to
        # summarise, not a broken install." So this section is printed after
        # the verdict above and contributes nothing to it — the same shape
        # `.github/workflows/ci.yml` gives "Report divergence from spec-head".
        # ------------------------------------------------------------------
        lines.append("Ref currency (reported, never failed on):")
        if self.currency.source is not None:
            lines.append(f"  releases read from {self.currency.source}")
        if self.currency.newest is None:
            # One line, not one per stub: the reason is a fact about this run,
            # not about each file, and repeating it three times buries the
            # findings above it.
            lines.append(f"  not checked: {self.currency.unknown}")
        else:
            for relative, _, ref in self.stubs:
                if ref is None:
                    continue
                lines.append(f"  {relative}: {self.currency.about(ref)}")
        lines.append(
            "  An installation behind the newest release is divergence to "
            "summarise, not a broken install; upgrading is `vellum init --ref "
            "<newer> --force`."
        )
        lines.append("")
        lines += CANNOT_KNOW
        return "\n".join(lines)


def doctor(
    checkout: str | Path,
    host: str = HOST_REPO,
    forge: str | None = None,
    releases_from: str | Path | None = None,
) -> Doctor:
    """Check installed-matches-shipped from the checkout alone."""
    root = Path(checkout)
    if not root.is_dir():
        raise InstallError(f"{root}: not a directory; is this an intent checkout?")
    chosen = read_forge(root, forge)
    # Read unconditionally, even when `--forge` made the forge knowable without
    # it. `read_forge` short-circuits on the override, and without this a
    # `doctor --forge github` pointed at any directory at all reported three
    # missing stubs and exited 1 — "a finding" for what is plainly "I could not
    # answer", plus a stderr line naming a workspace file that does not exist.
    # A checkout with no workspace is not an installation to have findings about.
    try:
        workspace_load(root)
    except WorkspaceError as exc:
        raise InstallError(str(exc)) from exc
    directory = WORKFLOWS_DIR[chosen]
    stubs = []
    for shipped in SHIPPED:
        relative = (directory / shipped.filename).as_posix()
        found, ref = inspect(
            root / directory / shipped.filename, shipped,
            host=host, forge=chosen, relative=relative,
        )
        stubs.append((relative, found, ref))
    return Doctor(
        checkout=root, forge=chosen, host=host,
        currency=currency(releases_from), stubs=stubs,
    )


def run_doctor(
    checkout: str,
    host: str = HOST_REPO,
    forge: str | None = None,
    releases_from: str | None = None,
    out=None,
) -> int:
    """Report the installation. Exit 1 on a finding, 0 when every stub matches."""
    stream = out if out is not None else sys.stdout
    result = doctor(checkout, host=host, forge=forge, releases_from=releases_from)
    print(result.report(), file=stream)
    if result.findings:
        print(
            f"vellum: doctor — {len(result.findings)} finding(s); what is installed "
            f"is not what {result.host} ships "
            f"(see {workspace_path(result.checkout)} for this installation)",
            file=sys.stderr,
        )
        return 1
    return 0
