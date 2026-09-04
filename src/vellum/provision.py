"""``vellum init --shape …`` — provisioning a repo pair (installation, part 2).

Part 1 (``vellum.install``) stamps caller stubs into an intent checkout whose
repos already exist. This is the other half: ``spec/features/installation.md``,
"Run where no installation exists, ``vellum init`` provisions a repo pair in one
of three shapes … **greenfield** … **brownfield** … **brownfield with docs**".

Part 1's behavior is untouched. ``vellum init <checkout>`` with no provisioning
argument is the stub-stamping command it has always been, byte for byte; this
module is reached only when the caller says something about provisioning. Which
of the two a run is, is a fact about the *command line* and never about the
directory — the spec is explicit that the shape "is chosen by the operator,
never inferred from a directory", and inferring the *mode* from one would be
the same mistake one step earlier. ``requested`` below is the whole rule.

The four things this module is built around
-------------------------------------------

**A conversation with a plan.** Every value is promptable and every prompt is
answerable by a flag, so an unattended run is the same command with no prompts
left. Everything is validated *before* the plan is rendered, so the plan is
either complete or never shown; the plan is shown before anything is created;
``--plan`` prints it and stops. That is what makes provisioning drivable in the
acceptance suite without a forge (decision: installer transport).

**The plan and the checklist are one list.** The forge steps are
``ForgeStep`` values built once, by :func:`forge_steps`. The plan prints them
because it must name "every step the transport cannot take"; the manual rung
prints the same list because it *is* the steps it could not take. Two renderings
of one list cannot disagree about what the installer would have done — and the
manual rung is the rung an operator actually follows, so a checklist that had
drifted from the plan would be wrong exactly where it is trusted most.

**Nothing here mints a credential, and no secret is ever an argv element.**
The cross-repo token pair is supplied by the operator — from the environment, or
a hidden prompt — and reaches ``gh`` on **stdin** via ``--body -``. An argv
element is world-readable on the machine (``/proc/<pid>/cmdline``, ``ps``) and
lands in shell history; a value on stdin is neither. :class:`ForgeStep` cannot
carry a value at all: its ``stdin`` field holds a *description* of what is piped
in, which is what the plan and the checklist print.

**The seed is checked before it is pushed.** ``spec/features/installation.md``:
"The seed lints clean and doctors green before it is pushed." Both run against
the built tree — ``vellum lint`` over the spec tree and ``vellum doctor`` over
the whole intent checkout, after the stubs are stamped — and a red seed is
refused rather than pushed. That ordering is why the stubs are stamped as part
of the local half rather than after the push: doctor cannot be green before they
exist, and a push that happened first could not be un-pushed.

Exit codes, matching the guards' contract (``vellum.cli``'s docstring):

* ``0`` — provisioned, or the plan was printed, or the manual rung did the half
  a checkout can hold.
* ``1`` — a finding: the seed this command just built does not lint, or does not
  doctor green. Nothing is pushed. It is the one code ``init`` may now return,
  and it is still doctor's sentence to pass — this command runs doctor and
  reports its verdict rather than inventing a second one.
* ``2`` — it could not answer: a value that will not validate, a prompt with no
  TTY to ask on, a checkout that is already an installation, a repository name
  the forge already has.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from vellum import install
from vellum import seeds
from vellum.lint import lint_tree
from vellum.specfile import ID_RE
from vellum.text import one_line
from vellum.workspace import WORKSPACE_RELPATH

#: The three shapes, in the order the prompt offers them. Each is a path
#: ``docs/design.md`` already names: greenfield is "Cold start", the two
#: brownfield shapes are "Adoption".
GREENFIELD = "greenfield"
BROWNFIELD = "brownfield"
BROWNFIELD_WITH_DOCS = "brownfield-with-docs"
SHAPES = (GREENFIELD, BROWNFIELD, BROWNFIELD_WITH_DOCS)

#: The shapes whose product repository already exists.
ADOPTING = (BROWNFIELD, BROWNFIELD_WITH_DOCS)

VISIBILITIES = ("public", "private")

#: The branch a brownfield installation's ``.vellum/`` arrives on.
#: ``spec/features/installation.md``: "its ``.vellum/`` arrives on a branch as a
#: pull request, never as a push to its default branch". Vellum is a guest in a
#: repository it did not create, and a guest does not write to `main`.
ADOPT_BRANCH = "vellum/adopt"

#: The cross-repo pair. Both names are read from the *existing* workflows rather
#: than chosen here: ``VELLUM_TOKEN`` is what every caller stub passes and what
#: the reusable workflows check in their first step (``vellum.install.SECRET``),
#: and ``SPEC_TOKEN`` is what a product repo's conformance job reads to fetch
#: the intent repo at the pin (``.github/workflows/ci.yml``). Inventing a third
#: name here would set a secret nothing reads.
INTENT_SECRET = install.SECRET
PRODUCT_SECRET = "SPEC_TOKEN"

#: A forge organization or user. GitHub's own shape: alphanumerics and single
#: hyphens, not starting or ending with one.
ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")

#: A repository name. Wider than a slug — real repositories carry dots and
#: underscores — but never a path segment that means something else to a shell
#: or a filesystem, because this value becomes a directory name.
REPO_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$")

#: A product name. Narrower than a repository name because it is *also* a
#: `.vellum/workspace.yaml` key and the default stem of both repository names.
PRODUCT_RE = ID_RE

#: An area name. This becomes a spec file's ``id:``, which lint holds to a
#: lowercase slug (``vellum.specfile.ID_RE``), so it is held to the same shape
#: here — a value refused at seed time by lint is one to refuse before the plan.
AREA_RE = ID_RE


class ProvisionError(Exception):
    """The command could not answer: a bad value, an unanswerable prompt."""


# =====================================================================
# Is this a provisioning run at all?
# =====================================================================

#: Every argument that means "provision", by ``argparse`` dest. Naming them
#: rather than testing ``--shape`` alone is what makes ``--plan`` and ``--yes``
#: and a bare ``--product`` enter the conversation instead of being ignored
#: beside a command that then stamps stubs. Part 1's own arguments — ``--ref``,
#: ``--force``, ``--from``, ``--forge``, ``--releases-from``, ``--branch`` — are
#: deliberately absent: every one of them is meaningful to stub-stamping, so any
#: of them switching modes would change part 1's behavior.
PROVISIONING_ARGS = (
    "shape", "product", "org", "intent_repo", "product_repo",
    "visibility", "intent_visibility", "product_visibility",
    "areas", "docs", "into", "plan", "yes",
)


def requested(args) -> bool:
    """True when the command line asks for provisioning rather than stamping."""
    return any(getattr(args, name, None) for name in PROVISIONING_ARGS)


# =====================================================================
# The conversation
# =====================================================================


@dataclass
class Console:
    """Where prompts are asked, and whether they can be asked at all.

    A record rather than three bare calls to ``input``: the tests drive the
    real command through it, and "prompt only on a TTY" is then one field
    instead of a scattered ``isatty`` check. ``ask_secret`` is separate from
    ``ask`` because a token must not echo — and because the two failure modes
    differ, a missing answer to one being a flag and to the other an
    environment variable.
    """

    tty: bool = False
    ask: Callable[[str], str] = input
    ask_secret: Callable[[str], str] = getpass.getpass

    @classmethod
    def detect(cls) -> "Console":
        try:
            tty = sys.stdin.isatty()
        except (AttributeError, ValueError):  # a closed or replaced stdin
            tty = False
        return cls(tty=tty)


def _prompt(console: Console, flag: str, question: str, *,
            default: str | None = None, choices: Sequence[str] | None = None) -> str:
    """Ask for one value, or refuse because there is nothing to ask on.

    ``spec/features/installation.md``: "every prompt is answerable by a flag, so
    an unattended run is the same command with no prompts left". The refusal
    therefore names the flag, not the question — an unattended run that stops
    needs to be told what to add to its command line.
    """
    if not console.tty:
        raise ProvisionError(
            f"{question} — and there is no terminal to ask on. Every prompt is "
            f"answerable by a flag: pass {flag}"
            + (f" ({'|'.join(choices)})" if choices else "")
            + (f", or accept the default with --yes ({default})" if default else "")
            + "."
        )
    suffix = f" [{'/'.join(choices)}]" if choices else ""
    suffix += f" ({default})" if default else ""
    while True:
        answer = console.ask(f"{question}{suffix}: ").strip()
        if not answer and default is not None:
            return default
        if not answer:
            continue
        if choices and answer not in choices:
            continue
        return answer


@dataclass
class Answers:
    """Every value the conversation settled, validated."""

    shape: str
    product: str
    org: str
    intent_repo: str
    product_repo: str
    intent_visibility: str
    product_visibility: str
    branch: str
    areas: tuple[str, ...]
    docs: tuple[str, ...]

    @property
    def adopting(self) -> bool:
        return self.shape in ADOPTING

    @property
    def intent_slug(self) -> str:
        return f"{self.org}/{self.intent_repo}"

    @property
    def product_slug(self) -> str:
        return f"{self.org}/{self.product_repo}"

    @property
    def title(self) -> str:
        """The product name as a heading: ``billing-api`` -> ``Billing Api``."""
        return " ".join(part.capitalize() for part in self.product.split("-"))


def _check(value: str, pattern: re.Pattern, flag: str, what: str) -> str:
    text = value.strip()
    if not pattern.match(text):
        raise ProvisionError(
            f"{flag} {one_line(value)!r} is not {what}. Every value is validated "
            f"before the plan is shown, so a run either has a complete plan or "
            f"has not started (spec/features/installation.md)."
        )
    return text


def resolve(args, console: Console) -> Answers:
    """The conversation, in order: ask, default, validate. Nothing is created.

    Every value is checked here rather than at the point it is used, because
    "exits 2 for any value it cannot validate before the plan" is a statement
    about *when*: a plan naming a repository the forge would refuse is a plan
    that lied, and an operator who confirmed it has confirmed nothing.
    """
    shape = args.shape or _prompt(
        console, "--shape", "Which shape is this installation", choices=SHAPES,
    )
    if shape not in SHAPES:
        raise ProvisionError(
            f"--shape {one_line(shape)!r} is not one of {', '.join(SHAPES)}. The "
            f"shape is chosen by the operator and never inferred from a "
            f"directory (spec/features/installation.md)."
        )

    product = _check(
        args.product or _prompt(console, "--product", "Product name (a slug)"),
        PRODUCT_RE, "--product", "a lowercase slug (letters, digits, single hyphens)",
    )
    org = _check(
        args.org or _prompt(console, "--org", "Forge organization or user"),
        ORG_RE, "--org", "a forge organization or user name",
    )
    intent_repo = _check(
        args.intent_repo or _default_or_prompt(
            console, "--intent-repo", "Intent repository name", f"{product}-intent",
            args.yes,
        ),
        REPO_RE, "--intent-repo", "a repository name",
    )
    product_question = (
        "Existing product repository name" if shape in ADOPTING
        else "Product repository name"
    )
    product_repo = _check(
        args.product_repo or _default_or_prompt(
            console, "--product-repo", product_question, product, args.yes,
        ),
        REPO_RE, "--product-repo", "a repository name",
    )
    if intent_repo == product_repo:
        raise ProvisionError(
            f"--intent-repo and --product-repo are both {intent_repo!r}. Every "
            f"installation splits intent from product (decision D3, "
            f"spec/features/repo-topology.md); they cannot be one repository."
        )

    visibility = args.visibility or (
        None if (args.intent_visibility and args.product_visibility) else
        _default_or_prompt(
            console, "--visibility", "Repository visibility", "private", args.yes,
            choices=VISIBILITIES,
        )
    )
    intent_visibility = _visibility(args.intent_visibility or visibility, "--intent-visibility")
    product_visibility = _visibility(args.product_visibility or visibility, "--product-visibility")

    branch = _check(
        args.branch or _default_or_prompt(
            console, "--branch", "Default branch", install.DEFAULT_BRANCH, args.yes,
        ),
        install.REF_RE, "--branch", "a branch name git would accept",
    )

    areas = tuple(
        _check(area, AREA_RE, "--area", "a lowercase slug (it becomes a spec file's `id:`)")
        for area in (args.areas or [_prompt(
            console, "--area",
            "First feature area (a slug)" if shape == GREENFIELD
            else "An area to survey (a slug)",
        )])
    )
    if len(set(areas)) != len(areas):
        raise ProvisionError(
            f"--area names {one_line(', '.join(areas))} with a repeat. Each area "
            f"is one spec file and one `id:`, and lint refuses two files claiming "
            f"one id."
        )

    docs = tuple(str(d) for d in (args.docs or ()))
    if docs and shape != BROWNFIELD_WITH_DOCS:
        raise ProvisionError(
            f"--docs is for --shape {BROWNFIELD_WITH_DOCS}; this run is "
            f"--shape {shape}, which stages no survey sources. Existing "
            f"documentation seeds the survey (docs/design.md, adoption), and a "
            f"shape that has no survey has nowhere to list it."
        )
    if shape == BROWNFIELD_WITH_DOCS and not docs:
        raise ProvisionError(
            f"--shape {BROWNFIELD_WITH_DOCS} stages existing documentation as the "
            f"survey's sources, and none was named. Pass --docs <path> (repeatable), "
            f"or use --shape {BROWNFIELD}."
        )
    for path in docs:
        if not Path(path).exists():
            raise ProvisionError(
                f"--docs {one_line(path)!r} does not exist. The survey sources are "
                f"listed in the seeded index for the surveyor to find, so a path "
                f"that is not there would be a promise the index cannot keep."
            )

    return Answers(
        shape=shape, product=product, org=org,
        intent_repo=intent_repo, product_repo=product_repo,
        intent_visibility=intent_visibility, product_visibility=product_visibility,
        branch=branch, areas=areas, docs=docs,
    )


def _default_or_prompt(console: Console, flag: str, question: str, default: str,
                       yes: bool, choices: Sequence[str] | None = None) -> str:
    """A defaulted value: taken silently under ``--yes``, asked for otherwise.

    ``--yes`` "accepts defaults", so it answers exactly the prompts that have
    one and none of the prompts that do not — which is why it is passed here and
    not consulted in :func:`_prompt`.
    """
    if yes or not console.tty:
        return default
    return _prompt(console, flag, question, default=default, choices=choices)


def _visibility(value: str | None, flag: str) -> str:
    if value is None:
        raise ProvisionError(
            f"no visibility for this repository; pass {flag} "
            f"({'|'.join(VISIBILITIES)}) or --visibility for both."
        )
    text = str(value).strip().lower()
    if text not in VISIBILITIES:
        raise ProvisionError(
            f"{flag} {one_line(str(value))!r} is not one of {', '.join(VISIBILITIES)}"
        )
    return text


# =====================================================================
# The seed
# =====================================================================

PRODUCT_MD = """---
id: product
title: {title}
since: spec-v1
---

# {title}

## Behavior

- Write what {title} is and who it is for, in the vocabulary every other spec
  file then borrows. This file is the one place a term is defined; an area file
  that re-defines one has found a gap here.

## Glossary

- **term** — the definition the areas use without restating it.
"""

INDEX_GREENFIELD = """---
id: index
title: Spec index
since: spec-v1
---

# Index

Reading order for a first pass: product.md, then the feature areas below, then
behaviors. The vocabulary is the glossary in product.md. Decisions are consulted
by date or by link, not read linearly.

## Features

| Area | File | Covers |
|---|---|---|
{areas}

## Status

No area is unsurveyed. A greenfield installation starts with a skeletal spec and
grows it the way code grows: thin vertical slices, each a small spec PR whose
wave ships something runnable.
"""

INDEX_BROWNFIELD = """---
id: index
title: Spec index
since: spec-v1
---

# Index

Reading order for a first pass: product.md, then the feature areas below. The
vocabulary is the glossary in product.md.

## Features

| Area | File | Covers |
|---|---|---|
{areas}

## Status

An existing product adopts Vellum by survey, not by rewrite. A surveyor drafts
each area *from the code*, one area per spec PR, with scenarios that must pass
against the current deployment before the PR merges: the spec starts true, or it
does not merge. Every area below is `unsurveyed` until it is covered, and the
spec-first guard applies only to surveyed areas, so normal work continues during
adoption (`docs/design.md`, "Adoption (brownfield)").

| Area | File | Status |
|---|---|---|
{statuses}

Adoption is complete when this table has no `unsurveyed` left.

### Survey sources

Existing documentation in `{product_slug}`, staged for the surveyor by
`vellum init --docs`. These are starting points, not the spec: a scenario drawn
from one still has to pass against the current deployment before it merges.

{sources}
"""

AREA_GREENFIELD = """---
id: {slug}
title: {title}
since: spec-v1
---

# {title}

## Behavior

- Write the behavior of {title} here, as statements about what the product does
  rather than how it does it. One bullet per rule; the scenarios below are the
  ones that have to hold.

## Acceptance

```gherkin
Feature: {title}
  @id:{slug}-placeholder
  Scenario: The installation runs its acceptance suite once, end to end
    Given a {product} installation seeded by vellum init
    When the acceptance suite runs
    Then this placeholder is replaced by the first scenario describing {title}
```
"""

AREA_BROWNFIELD = """---
id: {slug}
title: {title}
status: unsurveyed
since: spec-v1
---

# {title}

## Behavior

- **Unsurveyed.** The surveyor drafts this area from the existing code and the
  survey sources named in index.md, one area per spec PR. Until then this file
  is a placeholder and the spec-first guard does not apply to {title}.

## Acceptance

No scenarios yet. The first spec PR for this area brings scenarios that pass
against the *current* deployment: a survey that describes something the product
does not do is not a survey.
"""

HARNESS_README = """# harness/

The acceptance suite for **{title}**: the scenarios in the intent repo's spec
tree, executed against a deployment of the product.

`vellum init` seeded everything here. The machinery — `run.py`,
`support/runner.py`, `support/registry.py`, `support/report.py`,
`support/world.py` — is generic and is the same in every Vellum installation.
Two files are yours:

| File | What you write |
|---|---|
| `steps/` | One module per spec file that carries scenarios, imported by `steps/__init__.py`. Seeded empty. |
| `support/adapter.py` | How this harness reaches the product. Seeded with `no_deployment()`. |

## Running it

    python3 harness/run.py

Exit codes: 0 when nothing failed or errored, 1 when a scenario FAILED,
ERRORED or had an UNDEFINED step, 2 when the harness could not start.

**A fresh seed exits 1**, because no sentence in the seeded spec tree has a
step definition yet and an unexecutable suite is not a suite. It is not broken;
it is telling you what to do next. The two steps, in order:

1. Write step definitions until nothing reports UNDEFINED. With no deployment
   yet, a definition's body is `world.require("deployment")` — the scenario
   then reports CANNOT RUN YET naming what is missing, which is an honest
   answer rather than a skip or a fake pass.
2. Write a real deployment in `support/adapter.py` and declare what it
   provides. The scenarios waiting on `deployment` stop waiting.

## The report

`run.py` prints a conformance map: every scenario, its outcome, and — for the
ones that cannot run — the capability that is missing and the sentence naming
it. The report is deterministic by construction: no timestamps, no durations,
no absolute paths, and scenarios in the extracted suite's own order, so two
runs at one commit produce byte-identical output.

## The write boundary

This tree belongs to the harness engineer and nothing else does
(`.vellum/config.yaml`, `write_boundaries.harness-engineer`). `run.py` enforces
half of it itself: it compares the intent repo's working tree before and after
the run and exits 2 if the run left a trace.
"""


WORKSPACE_YAML = """# One intent repo governs one or more product repos; each product repo answers
# to exactly one intent repo (spec/features/repo-topology.md). Written by
# `vellum init`; `vellum init` and `vellum doctor` read it back.
intent: {intent_slug}

# The forge this installation's caller stubs are stamped for.
forge: github

products:
  {product}: {{repo: {product_slug}, trees: [src, .vellum/memory]}}
"""

CONFIG_YAML = """# Vellum installation config. One config governs this intent repo and every
# product repo under it, because the values in it are installation policy rather
# than product code.
#
# Written by `vellum init` with the installation defaults. Every key the CLI
# reads today is here with the command that reads it named beside it; the rest
# is the v1 shape, reserved so a later feature is implementation rather than
# migration. A key the CLI reads is an error when it is MISSING, never a
# silent default — a gate that turns itself off when its key is misspelled is
# not a gate.

# Decorative names for spec versions. A version is a commit; this is the prefix
# the `spec-v<N>` tag beside it carries.
version_prefix: spec-v

release:
  policy: batched            # continuous | batched | train
  channels: [production]     # canary and lines are reserved

budgets:
  # read by `vellum budget`
  per_item_usd: 10
  period_usd: 250
  period: monthly            # daily | weekly | monthly
  # read by `vellum backpressure`: unshipped spec versions before the gate
  # reports blocked. Start loose — early intent naturally outruns early product.
  divergence_cap: {divergence_cap}
  verifier_round_trips: 3
  concurrent_implementers: 2
  lease_minutes: 60          # a claim's lifetime before it lapses

questions:
  # read by `vellum tick`: how long a parked question waits before it escalates
  timebox_hours: 24
  delivery: github-native

flake:
  retries: 2
  quarantine_label: scenario-bug

labels:
  spec: [spec:feature, spec:fix, spec:clarify, spec:redefining]
  escalation: [needs-human, question]
  ideation: [backlog]

dependency_policy:
  # read by `vellum verify deps`: the registries a dependency may come from.
  # A new or changed dependency is a verifier red-flag item
  # (spec/behaviors/security.md); set this to the registries this product
  # actually uses before the first dependency lands.
  registries: [pypi.org]
  lockfile_required: true

write_boundaries:
  # read by `vellum verify boundaries <intent-checkout> --role <role>`: the
  # trees each of THIS repo's roles may write. The same block a product repo
  # carries in .vellum/product.yaml, here because the intent repo has no
  # product file (spec/behaviors/write-boundaries.md). Entries are
  # repo-relative path prefixes matched component-wise; "", ".", "/", an
  # absolute path and ".." are all refused as tree-widening.
  harness-engineer: [harness]
  librarian: [ledger, .vellum/memory]

executors:
  # Bind roles to executors; first available claims. Reserved until this
  # installation runs agents.
  {{}}

roles:
  {{}}
"""

RELEASES_YAML = """# The release ledger: what has been cut, and what each channel has conformed.
# `spec_conformed: null` is the honest starting state — this installation has
# certified nothing yet, so the armed/enforced partition arms every scenario and
# enforces none.
spec_head: null
channels:
  production:
    spec_conformed: null
cuts: []
stamps: {}
"""

PRODUCT_YAML = """# Backref from this product repo to the intent repo that governs it.
# One intent repo governs one or more product repos; each product repo answers
# to exactly one intent repo (spec/features/repo-topology.md).

intent:
  repo: {intent_slug}
  url: https://github.com/{intent_slug}

# The pin of record. This file IS the pin: `commit` is the spec version this
# code implements, and build and CI fetch the spec tree at it. No submodule or
# subtree is mounted; an installation may keep one as a developer convenience,
# but nothing treats a gitlink as authoritative.
#
# Seeded by `vellum init` at the first commit touching spec/ in
# {intent_slug} — the commit that made this installation's spec exist.
# `name` is decoration: nothing reads it to decide anything, so it may be
# absent, late or wrong without changing behavior.
pin:
  commit: {commit}
  name: null

# This repo's entry in the intent repo's .vellum/workspace.yaml.
product:
  name: {product}
  trees: [src, .vellum/memory]

# Trees an implementer may write in this repo
# (spec/behaviors/write-boundaries.md). Widen it as the repo grows; every entry
# is a repo-relative prefix matched component-wise.
write_boundaries:
  implementer: [src, tests, .vellum/memory]
"""

MEMORY_MAP = """# Map

Where things are in this repo, and the standing decisions behind them. Area
notes live in `.vellum/memory/areas/`; wave worklogs in `.vellum/memory/waves/`.

Seeded by `vellum init`. It is a skeleton on purpose: a map written ahead of the
repository it describes is a second place for the layout to drift. Fill it in as
the first wave lands, and keep every claim in it something a reader can grep for.

## Layout

| Path | What |
|---|---|
| `src/` | The product. |
| `.vellum/product.yaml` | Backref to `{intent_slug}`, and the pin of record — `pin.commit`. |
| `.vellum/memory/` | This map, area notes, and one worklog per wave. |

## Areas

None yet. An area note is the durable answer to "how does this part work and
why"; exit duty is what keeps them true — a work item is done only when the
implementer updated the area notes it touched.

## Waves

None yet.

## Technology choice, and why

Not chosen yet. Record it here when it is, with the reasoning, so a later wave
can re-open the decision knowingly rather than by accident.
"""


def _table_row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def intent_seed(answers: Answers) -> dict[str, str]:
    """``{repo-relative path: text}`` for the intent repo, ordered by path.

    One dict, built once: the plan lists its keys and the build writes its
    items, so "every file to be seeded (paths)" in the plan and the files that
    appear in the checkout are the same set by construction rather than by two
    lists being kept in step.
    """
    titles = {slug: " ".join(p.capitalize() for p in slug.split("-")) for slug in answers.areas}
    files: dict[str, str] = {
        "spec/product.md": PRODUCT_MD.format(title=answers.title),
    }
    if answers.adopting:
        files["spec/index.md"] = INDEX_BROWNFIELD.format(
            areas="\n".join(
                _table_row([titles[a], f"features/{a}.md", "drafted by the survey"])
                for a in answers.areas
            ),
            statuses="\n".join(
                _table_row([titles[a], f"features/{a}.md", "unsurveyed"])
                for a in answers.areas
            ),
            product_slug=answers.product_slug,
            # Backticked, and that is load-bearing rather than cosmetic: lint
            # resolves bare `.md` paths in prose as cross-references, and these
            # name files in the PRODUCT repo, which this tree cannot resolve.
            # Inline code is masked before references are found, so a quoted
            # path is data. It also renders as a path, which is what it is.
            sources="\n".join(f"- `{one_line(d)}`" for d in answers.docs) or "- (none named)",
        )
        for slug in answers.areas:
            files[f"spec/features/{slug}.md"] = AREA_BROWNFIELD.format(
                slug=slug, title=titles[slug]
            )
    else:
        files["spec/index.md"] = INDEX_GREENFIELD.format(
            areas="\n".join(
                _table_row([titles[a], f"features/{a}.md", "write what this area covers"])
                for a in answers.areas
            ),
        )
        for slug in answers.areas:
            files[f"spec/features/{slug}.md"] = AREA_GREENFIELD.format(
                slug=slug, title=titles[slug], product=answers.product
            )

    files[".vellum/workspace.yaml"] = WORKSPACE_YAML.format(
        intent_slug=answers.intent_slug,
        product=answers.product,
        product_slug=answers.product_slug,
    )
    files[".vellum/config.yaml"] = CONFIG_YAML.format(divergence_cap=3)
    files["ledger/releases.yaml"] = RELEASES_YAML
    files.update(seeds.harness_files())
    # Rendered here rather than shipped beside the modules it describes, on the
    # split `vellum.seeds` states: what is copied verbatim is package data, and
    # what interpolates the installation is a template. This one names the
    # product, so it is a template — which is also why it does not have to be
    # package data, and `vellum/seeds/harness/__init__.py` explains why that
    # matters.
    files["harness/README.md"] = HARNESS_README.format(title=answers.title)
    return dict(sorted(files.items()))


def product_seed(answers: Answers, commit: str) -> dict[str, str]:
    """``{repo-relative path: text}`` for the product repo, at the pin *commit*."""
    return {
        ".vellum/memory/map.md": MEMORY_MAP.format(intent_slug=answers.intent_slug),
        ".vellum/product.yaml": PRODUCT_YAML.format(
            intent_slug=answers.intent_slug,
            commit=commit,
            product=answers.product,
        ),
    }


#: Where the local half is built when ``--into`` names nowhere. A placeholder in
#: the plan and a real ``mkdtemp`` afterwards, so ``--plan`` creates nothing and
#: two plans of one command line are byte-identical.
STAGING = "<a staging directory, made when the plan is confirmed>"

#: The pin's placeholder in the plan. The sha is not knowable before the seed is
#: committed, and the plan is rendered before anything is created — so it says
#: which commit it will be rather than a number it would have to invent.
PIN_PLACEHOLDER = "<the seed's first commit touching spec/>"


# =====================================================================
# The forge steps — one list, printed twice
# =====================================================================


@dataclass(frozen=True)
class ForgeStep:
    """One thing the transport does on the forge, or tells the operator to do.

    ``stdin`` is a *description* of what is piped in, never a value: this object
    reaches the plan, the checklist and the report, and a secret that can be
    held cannot be printed by accident. The value travels beside the step, in a
    local variable, to :meth:`Gh.run`.
    """

    what: str
    argv: tuple[str, ...] = ()
    stdin: str | None = None
    #: True when nothing automates this: branch protection, a review, a setting
    #: on a repository this installation does not own.
    manual: bool = False
    #: True when the LOCAL half depends on this step. Only one does — cloning
    #: the existing product repository of a brownfield shape, without which the
    #: adoption branch would sit on no history and push to nothing — so it runs
    #: before the build rather than after it. It is still one entry in one list;
    #: this only says where in the run it happens.
    before: bool = False

    def command(self) -> str:
        if not self.argv:
            return ""
        rendered = " ".join(_quote(a) for a in self.argv)
        return f"printf %s \"$TOKEN\" | {rendered}" if self.stdin else rendered


_SAFE = re.compile(r"^[A-Za-z0-9@%_+=:,./-]+$")


def _quote(arg: str) -> str:
    """*arg* as it would be typed. Display only — nothing here runs a shell."""
    return arg if _SAFE.match(arg) else "'" + arg.replace("'", "'\\''") + "'"


def forge_steps(answers: Answers, *, host: str) -> list[ForgeStep]:
    """Every forge action this installation needs, in the order to take them.

    The single source for both renderings. The plan prints this list so it can
    "name every step the transport cannot take"; the manual rung prints the same
    list, with the exact commands and values, as the checklist an operator
    follows. Building it takes no transport and reaches no forge, which is why
    ``--plan`` can print it having created nothing.
    """
    steps: list[ForgeStep] = []
    intent, product = answers.intent_slug, answers.product_slug

    if answers.adopting:
        steps.append(ForgeStep(
            f"clone the existing product repository {product}, so the adoption "
            f"branch sits on its real history. Without a forge CLI: clone it "
            f"yourself, copy the two files from the local product checkout onto "
            f"a {ADOPT_BRANCH} branch of it, and commit",
            ("gh", "repo", "clone", product, "--", "<product checkout>"),
            before=True,
        ))
    # The intent repo first, in both shapes. It is the command surface
    # (spec/behaviors/security.md) and the repository the product's pin points
    # at, so a run that stops half way leaves the half that can stand alone.
    steps.append(ForgeStep(
        f"create the intent repository {intent} ({answers.intent_visibility}) "
        f"and push its seed",
        ("gh", "repo", "create", intent, f"--{answers.intent_visibility}",
         "--source", "<intent checkout>", "--remote", "origin", "--push",
         "--description", f"Intent repo for {answers.product}: spec, harness, "
                          f"ledger (vellum init)"),
    ))
    if not answers.adopting:
        steps.append(ForgeStep(
            f"create the product repository {product} ({answers.product_visibility}) "
            f"and push its seed",
            ("gh", "repo", "create", product, f"--{answers.product_visibility}",
             "--source", "<product checkout>", "--remote", "origin", "--push",
             "--description", f"{answers.product} — product repo (vellum init)"),
        ))
    if answers.adopting:
        steps.append(ForgeStep(
            f"push {ADOPT_BRANCH} to {product} — Vellum's `.vellum/` arrives on a "
            f"branch, never as a push to {answers.branch}",
            ("git", "-C", "<product checkout>", "push", "-u", "origin", ADOPT_BRANCH),
        ))
        steps.append(ForgeStep(
            f"open the adoption pull request on {product}",
            ("gh", "pr", "create", "--repo", product, "--base", answers.branch,
             "--head", ADOPT_BRANCH, "--title", "Adopt Vellum: the pin and the memory map",
             "--body-file", "<adopt PR body>"),
        ))

    steps.append(ForgeStep(
        f"set {INTENT_SECRET} on {intent} — the credential its caller stubs pass "
        f"to the reusable workflows, which read {product}",
        ("gh", "secret", "set", INTENT_SECRET, "--repo", intent, "--body", "-"),
        stdin=f"the {INTENT_SECRET} value, on stdin",
    ))
    steps.append(ForgeStep(
        f"set {PRODUCT_SECRET} on {product} — the credential its conformance job "
        f"reads {intent} with, to fetch the spec tree at the pin",
        ("gh", "secret", "set", PRODUCT_SECRET, "--repo", product, "--body", "-"),
        stdin=f"the {PRODUCT_SECRET} value, on stdin",
    ))
    if answers.product_visibility == "private":
        steps.append(ForgeStep(
            f"open {product}'s workflows to reuse from the organization; a private "
            f"repository's workflows resolve nowhere else, and a caller stub that "
            f"cannot resolve fails at `uses:` on every run",
            ("gh", "api", "-X", "PUT",
             f"repos/{product}/actions/permissions/access",
             "-f", "access_level=organization"),
        ))

    # ------------------------------------------------------------------
    # What no transport takes. `spec/features/installation.md` requires the
    # plan to name "every step the transport cannot take", and the honest list
    # is longer than the one gh could shorten: two of these are settings on a
    # repository this installation does not own.
    # ------------------------------------------------------------------
    if host != answers.product_slug:
        steps.append(ForgeStep(
            f"confirm {host} — the repo hosting the reusable workflows — allows "
            f"them to be reused by other repositories in the organization. It is "
            f"not one of this installation's repos, so nothing here can set it, "
            f"and nothing in either checkout can see it",
            manual=True,
        ))
    steps.append(ForgeStep(
        f"protect {answers.branch} on {intent}: owner-only merge rights and "
        f"required checks. The intent repo is the command surface "
        f"(spec/behaviors/security.md); branch protection stays the operator's "
        f"(spec/features/installation.md, out of scope). Required checks, once "
        f"the stubs have run once, are: "
        + ", ".join(f"`{s.name} / <job>`" for s in install.SHIPPED),
        manual=True,
    ))
    steps.append(ForgeStep(
        f"let the workflow token push to {answers.branch} on {intent}: "
        f"on-spec-merge commits the ledger record and pushes the version tag, so "
        f"branch protection that refuses the token fails that step",
        manual=True,
    ))
    if answers.adopting:
        steps.append(ForgeStep(
            f"review and merge the adoption pull request on {product}",
            manual=True,
        ))
    return steps


# =====================================================================
# The plan
# =====================================================================


@dataclass
class Plan:
    """What a run would do, rendered before it does any of it."""

    answers: Answers
    host: str
    ref: str
    transport: str
    intent_files: tuple[str, ...]
    product_files: tuple[str, ...]
    stubs: tuple[str, ...]
    steps: tuple[ForgeStep, ...]
    #: Where the local half is built: ``--into``, or a staging directory.
    intent_dir: Path
    product_dir: Path

    def render(self) -> str:
        a = self.answers
        lines = [
            f"vellum init — plan ({a.shape})",
            "",
            "Nothing below has happened yet.",
            "",
            "Repositories",
            f"  intent    {a.intent_slug:<40} {a.intent_visibility:<8} create",
            f"  product   {a.product_slug:<40} {a.product_visibility:<8} "
            + ("EXISTS — adopted, not created" if a.adopting else "create"),
            f"  default branch: {a.branch}",
            f"  local checkouts: {self.intent_dir}",
            f"                   {self.product_dir}",
            "",
            f"Seed — {a.intent_slug} ({len(self.intent_files)} files)",
        ]
        lines += [f"  {path}" for path in self.intent_files]
        lines += [
            "",
            f"Seed — {a.product_slug} ({len(self.product_files)} files)"
            + (f", on branch {ADOPT_BRANCH} as a pull request" if a.adopting else ""),
        ]
        lines += [f"  {path}" for path in self.product_files]
        lines += [
            f"  the pin is {PIN_PLACEHOLDER}",
            "",
            f"Caller stubs — {a.intent_slug}, from {self.host} at {self.ref}",
        ]
        lines += [f"  {path}" for path in self.stubs]
        lines += [
            "",
            "Secrets — supplied by you, never minted here, never an argv element",
            f"  {INTENT_SECRET:<14} on {a.intent_slug:<38} reads {a.product_slug}",
            f"  {PRODUCT_SECRET:<14} on {a.product_slug:<38} reads {a.intent_slug}",
            f"  values come from ${INTENT_SECRET} / ${PRODUCT_SECRET} in the "
            f"environment, or a hidden prompt",
            "",
            "Actions access",
        ]
        if a.product_visibility == "private":
            lines.append(
                f"  {a.product_slug}: access_level=organization, so this "
                f"repository's workflows can be reused inside {a.org}"
            )
        else:
            lines.append(
                f"  {a.product_slug} is public, so its workflows are already "
                f"reusable; no access change is needed"
            )
        lines += ["", f"Steps (transport: {self.transport})"]
        for number, step in enumerate(self.steps, start=1):
            lines.append(f"  {number:>2}. {step.what}")
            if step.argv:
                lines.append(f"      {step.command()}")
            if step.stdin:
                lines.append(f"      stdin: {step.stdin}")
            if step.manual:
                lines.append("      (no transport takes this one; it is yours)")
        lines += ["", *install.CANNOT_KNOW]
        return "\n".join(lines)


def build_plan(answers: Answers, *, host: str, ref: str, transport: str,
               intent_dir: Path, product_dir: Path) -> Plan:
    workflows = install.WORKFLOWS_DIR["github"]
    return Plan(
        answers=answers,
        host=host,
        ref=ref,
        transport=transport,
        intent_files=tuple(intent_seed(answers)),
        product_files=tuple(sorted(product_seed(answers, PIN_PLACEHOLDER))),
        stubs=tuple((workflows / s.filename).as_posix() for s in install.SHIPPED),
        steps=tuple(forge_steps(answers, host=host)),
        intent_dir=intent_dir,
        product_dir=product_dir,
    )


# =====================================================================
# git, and the local half
# =====================================================================


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """One git command. List arguments, no shell, ever.

    An identity is supplied only when the machine has none, so a real operator's
    commits carry their own name and a sandbox — a CI runner, this product's own
    test suite — still gets a commit rather than git's "please tell me who you
    are".
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *_identity(repo), *args],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise ProvisionError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{one_line(proc.stderr or proc.stdout)}"
        )
    return proc


def _identity(repo: Path) -> list[str]:
    args: list[str] = []
    for key, value in (("user.name", "vellum init"),
                       ("user.email", "vellum-init@localhost")):
        found = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", key],
            capture_output=True, text=True,
        )
        if found.returncode != 0 or not found.stdout.strip():
            args += ["-c", f"{key}={value}"]
    return args


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        # `harness/run.py` is documented as `python3 harness/run.py`, but it
        # carries a shebang and an operator will try to execute it. Seeding it
        # non-executable makes the first thing they try fail for a reason that
        # has nothing to do with their installation.
        if relative.endswith("run.py"):
            path.chmod(0o755)


def first_spec_commit(repo: Path) -> str:
    """The first commit in *repo* touching ``spec/`` — the pin of record.

    ``spec/features/installation.md``: "The product repo receives
    ``.vellum/product.yaml`` pinned at the seed's first spec commit." Derived
    from the repository rather than remembered from the write, so it stays true
    when the seed is committed differently — by hand, or in two commits — and so
    anyone can re-derive it with the same command.
    """
    found = _git(repo, "log", "--reverse", "--format=%H", "--", "spec").stdout.split()
    if not found:
        raise ProvisionError(
            f"{repo}: no commit touches spec/, so there is no spec version to pin "
            f"the product repo at. The seed was not committed."
        )
    return found[0]


def build_intent(directory: Path, answers: Answers, *, host: str, ref: str) -> tuple[str, list[str]]:
    """Build and commit the intent checkout. Returns ``(pin, stub paths)``.

    The order is the contract. The seed is committed first, because that commit
    *is* the spec version the product repo pins; the stubs are stamped and
    committed second, because they are not spec and must not date it; and both
    happen before anything is pushed, because the seed is checked before it
    leaves the machine.
    """
    directory.mkdir(parents=True, exist_ok=True)
    _git(directory, "init", "-q", "-b", answers.branch, ".")
    write_files(directory, intent_seed(answers))
    _git(directory, "add", "-A", "--", ".")
    _git(directory, "commit", "-q", "-m",
         f"seed the {answers.product} spec: product, index, "
         f"{len(answers.areas)} area(s), config, ledger, harness")
    pin = first_spec_commit(directory)

    stamped = install.init(directory, ref=ref, host=host, forge="github",
                           branch=answers.branch)
    _git(directory, "add", "-A", "--", ".")
    _git(directory, "commit", "-q", "-m",
         f"install the {ref} caller stubs (vellum init)")
    return pin, [str(stamp.path) for stamp in stamped.stamps]


def build_product(directory: Path, answers: Answers, pin: str) -> None:
    """Build and commit the product checkout.

    Greenfield creates it. The brownfield shapes put ``.vellum/`` on
    :data:`ADOPT_BRANCH` over whatever history the repository already has —
    ``spec/features/installation.md``: "its ``.vellum/`` arrives on a branch as a
    pull request, never as a push to its default branch". Vellum is a guest in a
    repository it did not create.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / ".git").is_dir():
        # No clone happened: either this is greenfield, or it is an adoption
        # with no transport to clone with (`--into`, or no `gh`). An adoption
        # still needs a base commit to branch from — a branch with no base is
        # not a pull request anyone could open — so it gets an empty one, and
        # the checklist tells the operator to move these two files onto a clone
        # of the real repository.
        _git(directory, "init", "-q", "-b", answers.branch, ".")
        if answers.adopting:
            _git(directory, "commit", "-q", "--allow-empty", "-m",
                 "the existing product repo, before Vellum")
    if answers.adopting:
        _git(directory, "checkout", "-q", "-B", ADOPT_BRANCH)
    write_files(directory, product_seed(answers, pin))
    _git(directory, "add", "-A", "--", ".")
    _git(directory, "commit", "-q", "-m", (
        f"adopt Vellum: pin {answers.product} at {pin[:12]} and seed the memory map"
        if answers.adopting else
        f"pin {answers.product} at {pin[:12]} and seed the memory map"
    ))


# =====================================================================
# Checking the seed before it is pushed
# =====================================================================


@dataclass
class SeedCheck:
    """``vellum lint`` and ``vellum doctor`` over the seed this run just built."""

    lint_findings: list
    doctor_findings: list

    @property
    def green(self) -> bool:
        return not self.lint_findings and not self.doctor_findings

    def report(self) -> str:
        lines = ["Seed checks (a red seed is not pushed)"]
        lines.append(
            f"  vellum lint    {'OK' if not self.lint_findings else 'FINDING'}"
            + (f" — {len(self.lint_findings)} finding(s)" if self.lint_findings else "")
        )
        for finding in self.lint_findings[:20]:
            lines.append(f"    {finding.format()}")
        lines.append(
            f"  vellum doctor  {'OK' if not self.doctor_findings else 'FINDING'}"
            + (f" — {len(self.doctor_findings)} finding(s)" if self.doctor_findings else "")
        )
        for finding in self.doctor_findings[:20]:
            lines.append(f"    {finding.file}: [{finding.code}] {one_line(finding.detail)}")
        return "\n".join(lines)


def check_seed(directory: Path, *, host: str) -> SeedCheck:
    """Run both guards over the built intent checkout. Nothing is written."""
    return SeedCheck(
        lint_findings=lint_tree(directory),
        doctor_findings=install.doctor(directory, host=host, forge="github").findings,
    )


# =====================================================================
# The transport
# =====================================================================


@dataclass
class Gh:
    """The operator's forge CLI, authenticated. The only thing that reaches a forge."""

    path: str

    def run(self, argv: Sequence[str], *, stdin: str | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        """One ``gh`` (or ``git``) invocation. List arguments, no shell.

        *stdin* is the one place a secret value appears, and it appears nowhere
        else: not in ``argv``, not in this object, not in any report. The
        subprocess reads it from a pipe, so it is never in ``/proc/<pid>/cmdline``
        and never in a shell history.
        """
        command = [self.path if argv[0] == "gh" else argv[0], *argv[1:]]
        proc = subprocess.run(command, input=stdin, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise ProvisionError(
                f"`{' '.join(_quote(a) for a in argv)}` exited {proc.returncode}: "
                f"{one_line(proc.stderr or proc.stdout)}"
            )
        return proc

    def repo_exists(self, slug: str) -> bool:
        return self.run(("gh", "repo", "view", slug, "--json", "name"),
                        check=False).returncode == 0


def detect_gh() -> Gh | None:
    """``gh`` on PATH and authenticated, or None for the manual rung.

    Both halves are required. ``gh`` present but logged out would fail at the
    first create, having already been announced in the plan as the transport —
    and the manual rung exists precisely so that an operator without a working
    forge CLI is not told to go and install one.
    """
    found = shutil.which("gh")
    if not found:
        return None
    status = subprocess.run([found, "auth", "status"], capture_output=True, text=True)
    return Gh(found) if status.returncode == 0 else None


def secret_for(name: str, console: Console) -> str | None:
    """The operator's value for one secret: the environment, or a hidden prompt.

    Never minted (decision: installer transport — "a tool that creates tokens on
    an operator's behalf holds more than the least-authority posture lets any
    single component hold"). None means "not supplied", which is not an error:
    the step moves to the list of things this run did not do, where the operator
    can see it. A secret quietly left unset would be a run that says it
    provisioned an installation whose every workflow fails in its first step.
    """
    value = os.environ.get(name)
    if value:
        return value
    if not console.tty:
        return None
    return console.ask_secret(
        f"{name} (hidden; leave empty to set it yourself later): "
    ).strip() or None


# =====================================================================
# The command
# =====================================================================


ADOPT_PR_BODY = """Adopt Vellum: the pin and the memory map.

`vellum init --shape {shape}` seeded `{intent_slug}` beside this repository and
opened this pull request rather than pushing to `{branch}`: Vellum claims one
namespaced directory in a repo it did not create and never writes outside it
(`spec/features/repo-topology.md`).

What this adds, and nothing else:

- `.vellum/product.yaml` — the backref to `{intent_slug}` and the pin of record,
  `pin.commit`, at the first commit touching `spec/` in the intent repo.
- `.vellum/memory/map.md` — the memory map skeleton.

Adoption is by survey, not by rewrite: every area in the intent repo's
`spec/index.md` is `unsurveyed`, and the spec-first guard applies only to
surveyed areas, so normal work continues while the survey proceeds.
"""


def run(
    checkout: str,
    *,
    shape: str | None,
    product: str | None,
    org: str | None,
    intent_repo: str | None,
    product_repo: str | None,
    visibility: str | None,
    intent_visibility: str | None,
    product_visibility: str | None,
    branch: str | None,
    areas: Sequence[str] = (),
    docs: Sequence[str] = (),
    into: str | None = None,
    plan_only: bool = False,
    yes: bool = False,
    host: str = install.HOST_REPO,
    ref: str | None = None,
    console: Console | None = None,
    out=None,
) -> int:
    """Provision a repo pair. See the module docstring for the exit codes."""
    stream = out if out is not None else sys.stdout
    console = console or Console.detect()
    pinned = ref if ref is not None else install.default_ref()

    # ------------------------------------------------------------------
    # The refusal that comes before everything, including the conversation.
    # `spec/features/installation.md`: "a checkout that already carries
    # `.vellum/workspace.yaml` is part 1's stub-stamping case, not a
    # provisioning". Asked of the checkout argument rather than of `--into`,
    # because the mistake being caught is running the provisioning command
    # inside an installation — and asking before any prompt means an operator
    # who made it is told so instead of being interviewed first.
    # ------------------------------------------------------------------
    existing = Path(checkout) / WORKSPACE_RELPATH
    if existing.is_file():
        raise ProvisionError(
            f"{existing} already exists: {checkout} is an installation, and "
            f"provisioning does not run over one. `vellum init {checkout}` with "
            f"no provisioning argument is this checkout's case — it stamps the "
            f"caller stubs and is idempotent (spec/features/installation.md). "
            f"To provision a NEW pair, run this from a directory that is not an "
            f"installation, or point it at one: `vellum init <dir> --shape …`. "
            f"`--into` does not lift this — where the new pair is BUILT is a "
            f"different question from where this command was RUN, and the "
            f"mistake being caught here is the second."
        )

    args = _Resolved(shape, product, org, intent_repo, product_repo, visibility,
                     intent_visibility, product_visibility, branch, list(areas),
                     list(docs), yes)
    answers = resolve(args, console)

    # ------------------------------------------------------------------
    # Where the local half is built, and which transport carries it.
    # `--into` is "no forge at all" (it is how the acceptance suite drives this
    # command), so it does not even look for `gh`: probing for a transport it
    # has been told not to use would make the run's behavior depend on the
    # machine it is on, which is the one thing a suite fixture must not do.
    # ------------------------------------------------------------------
    gh = None if into else detect_gh()
    if into:
        transport = "none (--into: local directories, no forge)"
    elif gh is not None:
        transport = f"gh ({gh.path})"
    else:
        transport = "none (no authenticated `gh`) — the forge steps are a checklist"

    # The staging directory is not created until the plan is confirmed, because
    # "--plan prints it and stops, creating nothing" has to mean *nothing* — a
    # command that leaves an empty directory behind has created something, and a
    # plan whose paths were a fresh mkdtemp each run would not be deterministic
    # either. Until then the plan names where the checkouts will be.
    root = Path(into) if into else None
    base = root if root is not None else Path(STAGING)
    intent_dir = base / answers.intent_repo
    product_dir = base / answers.product_repo

    plan = build_plan(answers, host=host, ref=pinned, transport=transport,
                      intent_dir=intent_dir, product_dir=product_dir)
    print(plan.render(), file=stream)
    if plan_only:
        print("\n--plan: nothing was created.", file=stream)
        return 0

    _confirm(console, yes)

    if root is None:
        root = Path(tempfile.mkdtemp(prefix="vellum-init-"))
        intent_dir = root / answers.intent_repo
        product_dir = root / answers.product_repo
        print(f"\nStaging the local half in {root}", file=stream)

    # ------------------------------------------------------------------
    # Refusals that need to look outward. A name the forge already has is not a
    # value this command can validate on its own, so it is checked here rather
    # than in `resolve` — after the plan, which is where the spec puts the
    # boundary ("exits 2 for any value it cannot validate BEFORE the plan").
    # ------------------------------------------------------------------
    if gh is not None:
        _check_forge_names(gh, answers)
    _check_directories(answers, intent_dir, product_dir)

    places = {"<intent checkout>": str(intent_dir), "<product checkout>": str(product_dir)}
    pin, stubs = build_intent(intent_dir, answers, host=host, ref=pinned)
    # The one forge step the local half depends on: the clone a brownfield
    # adoption branches from. Without a transport it stays on the checklist and
    # `build_product` makes a standalone repository instead, which is the half a
    # checkout can hold — the checklist step says what to do with it.
    early = [step for step in plan.steps if step.before]
    taken = _take(gh, early, places) if gh is not None else []
    build_product(product_dir, answers, pin)

    check = check_seed(intent_dir, host=host)
    print("", file=stream)
    print(check.report(), file=stream)
    if not check.green:
        print(
            f"\nvellum: the seed this run built is not green, so nothing was "
            f"pushed. The checkouts are at {intent_dir} and {product_dir}; fix "
            f"them there, or delete them and re-run.",
            file=sys.stderr,
        )
        return 1

    later, remaining = _perform(gh, plan, answers, console, places=places)
    taken += later
    remaining = [step for step in remaining if step not in taken]
    print("", file=stream)
    print(_report(plan, answers, pin, stubs, taken, remaining,
                  intent_dir=intent_dir, product_dir=product_dir), file=stream)
    return 0


@dataclass
class _Resolved:
    """The command-line values ``resolve`` reads, as one object.

    A record rather than the ``argparse`` namespace, so ``resolve`` — which the
    tests drive directly — does not depend on the parser's shape.
    """

    shape: str | None
    product: str | None
    org: str | None
    intent_repo: str | None
    product_repo: str | None
    visibility: str | None
    intent_visibility: str | None
    product_visibility: str | None
    branch: str | None
    areas: list
    docs: list
    yes: bool


def _confirm(console: Console, yes: bool) -> None:
    """The plan is confirmed before anything is created. ``--yes`` skips this."""
    if yes:
        return
    if not console.tty:
        raise ProvisionError(
            "the plan above is shown before anything is created and has to be "
            "confirmed, and there is no terminal to confirm on. Pass --yes to "
            "accept it, or --plan to print it and stop."
        )
    if console.ask("Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
        raise ProvisionError("not confirmed; nothing was created.")


def _check_forge_names(gh: Gh, answers: Answers) -> None:
    """A name the forge already has, refused — with the one exception."""
    if gh.repo_exists(answers.intent_slug):
        raise ProvisionError(
            f"{answers.intent_slug} already exists on the forge. Provisioning "
            f"creates the intent repository; it never adopts one, because a "
            f"repository that is already there may hold an installation whose "
            f"spec this would seed over. Name another with --intent-repo."
        )
    exists = gh.repo_exists(answers.product_slug)
    if answers.adopting and not exists:
        raise ProvisionError(
            f"{answers.product_slug} does not exist on the forge, and --shape "
            f"{answers.shape} adopts an EXISTING product repository. Name the "
            f"real one with --product-repo, or use --shape {GREENFIELD} to "
            f"create it."
        )
    if exists and not answers.adopting:
        raise ProvisionError(
            f"{answers.product_slug} already exists on the forge. Only the "
            f"product repository of a brownfield shape may exist: pass --shape "
            f"{BROWNFIELD} (or {BROWNFIELD_WITH_DOCS}) to adopt it, or name "
            f"another with --product-repo."
        )


def _check_directories(answers: Answers, intent_dir: Path, product_dir: Path) -> None:
    """The same refusal, one layer down: a directory that is already something.

    The local half has the same hazard as the forge half and the same one
    exception, so it makes the same call — otherwise ``--into`` over a populated
    directory would seed a spec tree on top of somebody's repository, which is
    the failure the forge check exists to prevent.
    """
    if intent_dir.exists() and any(intent_dir.iterdir()):
        raise ProvisionError(
            f"{intent_dir} already exists and is not empty. The intent checkout "
            f"is created here; provisioning does not write into a directory it "
            f"did not make."
        )
    if product_dir.exists() and any(product_dir.iterdir()) and not answers.adopting:
        raise ProvisionError(
            f"{product_dir} already exists and is not empty, and --shape "
            f"{answers.shape} creates the product repository. Use --shape "
            f"{BROWNFIELD} to adopt what is there."
        )


def _take(gh: Gh, steps: Sequence[ForgeStep], places: dict[str, str], *,
          values: dict[str, str | None] | None = None,
          body: str | None = None) -> list[ForgeStep]:
    """Run *steps* through the transport. Returns the ones it took.

    A step is left untaken — and so lands on the checklist — when nothing
    automates it, or when the secret it carries was not supplied. Neither is
    silently skipped: both come back in the report as something to do.
    """
    values = values or {}
    taken: list[ForgeStep] = []
    written: Path | None = None
    try:
        for step in steps:
            if step.manual or not step.argv:
                continue
            secret = next((n for n in values if step.stdin and n in step.argv), None)
            if secret is not None and not values[secret]:
                continue
            argv = [places.get(arg, arg) for arg in step.argv]
            if "<adopt PR body>" in argv:
                written = Path(tempfile.mkdtemp(prefix="vellum-init-pr-")) / "body.md"
                written.write_text(body or "", encoding="utf-8")
                argv = [str(written) if arg == "<adopt PR body>" else arg for arg in argv]
            gh.run(argv, stdin=values[secret] if secret else None)
            taken.append(step)
    finally:
        if written is not None:
            shutil.rmtree(written.parent, ignore_errors=True)
    return taken


def _perform(gh: Gh | None, plan: Plan, answers: Answers, console: Console, *,
             places: dict[str, str]) -> tuple[list[ForgeStep], list[ForgeStep]]:
    """Take the forge steps, or take none and hand every one of them back.

    Returns ``(taken, remaining)``. The manual rung is not a different code
    path: it is this function with ``gh`` None, so the checklist an operator
    follows is the list :func:`forge_steps` built and the plan printed.
    """
    if gh is None:
        return [], list(plan.steps)
    values = {
        INTENT_SECRET: secret_for(INTENT_SECRET, console),
        PRODUCT_SECRET: secret_for(PRODUCT_SECRET, console),
    }
    steps = [step for step in plan.steps if not step.before]
    taken = _take(gh, steps, places, values=values, body=ADOPT_PR_BODY.format(
        shape=answers.shape, intent_slug=answers.intent_slug, branch=answers.branch,
    ))
    return taken, [step for step in plan.steps if step not in taken]


def _report(plan: Plan, answers: Answers, pin: str, stubs: list[str],
            taken: list[ForgeStep], remaining: list[ForgeStep], *,
            intent_dir: Path, product_dir: Path) -> str:
    lines = [
        f"vellum init — provisioned ({answers.shape})",
        "",
        f"  intent repo   {answers.intent_slug:<40} {answers.intent_visibility}",
        f"  product repo  {answers.product_slug:<40} {answers.product_visibility}"
        + ("  (adopted)" if answers.adopting else ""),
        f"  spec pin      {pin}",
        f"                the first commit touching spec/ in {intent_dir.name}",
        "",
        "  local checkouts:",
        f"    {intent_dir}",
        f"    {product_dir}"
        + (f"  (on {ADOPT_BRANCH})" if answers.adopting else ""),
        "",
        f"  caller stubs stamped at {plan.ref}:",
    ]
    lines += [f"    {path}" for path in stubs]
    lines.append("")
    if taken:
        lines.append(f"Forge steps taken ({plan.transport}):")
        for number, step in enumerate(taken, start=1):
            lines.append(f"  {number:>2}. {step.what}")
    else:
        lines.append("Forge steps taken: none. Nothing was created on a forge.")
    lines.append("")
    if remaining:
        lines.append(
            f"Do these yourself, in order — {len(remaining)} step(s) this run did "
            f"not take:"
        )
        for number, step in enumerate(remaining, start=1):
            lines.append(f"  {number:>2}. {step.what}")
            if step.argv:
                lines.append(f"      {step.command()}")
            if step.stdin:
                lines.append(f"      stdin: {step.stdin}")
            if step.manual:
                lines.append("      (no transport takes this one; it is yours)")
        lines.append("")
        lines.append(
            "  Then `vellum doctor <intent checkout>` verifies the whole: what a "
            "checkout can see it checks, and what it cannot it says it cannot."
        )
    else:
        lines.append(
            "Nothing is left over. `vellum doctor <intent checkout>` verifies the "
            "installation from the checkout alone."
        )
    lines += ["", *install.CANNOT_KNOW]
    return "\n".join(lines)


def run_provision(args, out=None) -> int:
    """The CLI's entry point: unpack the namespace and run.

    Kept here rather than in ``vellum.cli`` so the argument names and their
    meanings live beside the module that gives them meaning — and so a test can
    drive the real command through :func:`run` without building a namespace.
    """
    return run(
        args.checkout,
        shape=args.shape,
        product=args.product,
        org=args.org,
        intent_repo=args.intent_repo,
        product_repo=args.product_repo,
        visibility=args.visibility,
        intent_visibility=args.intent_visibility,
        product_visibility=args.product_visibility,
        branch=args.branch,
        areas=args.areas,
        docs=args.docs,
        into=args.into,
        plan_only=args.plan,
        yes=args.yes,
        host=args.host,
        ref=args.ref,
        out=out,
    )


__all__ = [
    "ADOPT_BRANCH", "Answers", "Console", "ForgeStep", "Gh", "Plan",
    "PRODUCT_SECRET", "INTENT_SECRET", "ProvisionError", "SHAPES",
    "VISIBILITIES", "build_plan", "check_seed", "detect_gh", "first_spec_commit",
    "forge_steps", "intent_seed", "product_seed", "requested", "resolve", "run",
    "run_provision",
]
