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
a hidden prompt — and reaches ``gh secret set`` on **stdin**, which is what that
command reads when ``--body`` is *absent*. It has to be absent: ``gh`` takes
``--body``'s value whenever it is non-empty and only falls back to stdin when the
flag was not given, so ``--body -`` does not mean "read stdin" — it sets the
secret to the literal string ``-``. The pipe is enough on its own:
``subprocess.run(input=…)`` hands ``gh`` a pipe rather than a terminal, and so
does ``printf … |`` in the checklist. An argv element is world-readable on the
machine (``/proc/<pid>/cmdline``, ``ps``) and lands in shell history; a value on
stdin is neither. :class:`ForgeStep` cannot carry a value at all: its ``stdin``
field holds a *description* of what is piped in and its ``secret`` field the
*name* of the variable holding it, which is what the plan and the checklist
print.

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
  the forge already has, a product checkout this adoption would not be a guest
  in, a plan the operator declined, or a forge step that failed mid-run (the
  report says what was taken before it did).
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

#: Where the adoption pull request's body is written, inside the product
#: checkout and deliberately **not** committed: the transport passes it to
#: ``gh pr create --body-file`` and the manual rung's checklist names the same
#: path, so an operator following the checklist has the body the transport would
#: have used rather than a placeholder it never filled in.
ADOPT_PR_RELPATH = ".vellum/ADOPT_PR.md"

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

#: The two ids the seed claims for itself. ``spec/product.md`` is ``id: product``
#: and ``spec/index.md`` is ``id: index``, so an area of either name seeds a
#: SECOND file claiming an id that is already taken. Lint's ``GH003`` would not
#: catch it — that check is about scenario ids — so the seed would go green and
#: the collision would surface later as two files answering to one name.
RESERVED_AREAS = ("product", "index")

#: YAML 1.1 spells these as booleans and nulls, and a product or area name is
#: written into a seeded file as a bare scalar — ``.vellum/workspace.yaml``'s
#: product key, ``.vellum/product.yaml``'s ``product.name``, a spec file's
#: ``id:``. A product called ``no`` would be read back as ``False`` by the same
#: parser that wrote it. Refused here rather than quoted in five templates,
#: because a template someone adds later would not know to quote.
YAML_KEYWORDS = frozenset(
    "y|yes|n|no|true|false|on|off|null|none|~".split("|")
)


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
        try:
            answer = console.ask(f"{question}{suffix}: ").strip()
        except EOFError:
            # A terminal that closed mid-conversation — a piped heredoc that ran
            # out, a Ctrl-D. It is the same situation as having no terminal at
            # all, so it gets the same answer and the same advice, rather than a
            # traceback out of `input()`.
            raise ProvisionError(
                f"{question} — and the terminal closed before it was answered. "
                f"Every prompt is answerable by a flag: pass {flag}"
                + (f" ({'|'.join(choices)})" if choices else "")
                + (f", or accept the default with --yes ({default})" if default else "")
                + "."
            ) from None
        if not answer and default is not None:
            return default
        # A re-ask says why. A prompt that silently reprints itself looks like
        # the terminal ate the answer, and the operator retypes the same thing.
        if not answer:
            print(f"  {flag} has no default; an answer is needed.", file=sys.stderr)
            continue
        if choices and answer not in choices:
            print(f"  {one_line(answer)!r} is not one of {', '.join(choices)}.",
                  file=sys.stderr)
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


def _refuse_yaml_keyword(value: str, flag: str) -> None:
    """A name YAML 1.1 reads as a boolean or a null, refused before the plan.

    :data:`YAML_KEYWORDS` has the argument: this value is written into seeded
    files as a bare scalar and read back by the same parser that wrote it, so a
    product called ``no`` would come back as ``False``.
    """
    if value.lower() in YAML_KEYWORDS:
        raise ProvisionError(
            f"{flag} {value!r} is a YAML 1.1 keyword. It is written into the seed "
            f"as a bare scalar — a workspace key, a `product.name`, a spec file's "
            f"`id:` — and the parser that reads the seed back would answer with a "
            f"boolean or a null rather than this name. Choose another."
        )


#: Characters a survey source path may not carry. The backtick because the
#: seeded index wraps each path in inline code and lint's masking depends on
#: that quoting holding (`links.find_references`); the control characters
#: because the index is prose a human reads and a newline inside one entry
#: makes a second entry nobody wrote.
_DOCS_FORBIDDEN = re.compile(r"[`\x00-\x1f\x7f]")


def _docs_base(into, product_repo: str) -> Path:
    """The checkout ``--docs`` paths are relative to.

    Survey sources are documentation that lives in the PRODUCT repository, and
    the seeded index names them for a surveyor who will be reading that
    repository — so they are recorded repo-relative. The base is the product
    checkout when ``--into`` already names one (adoption over a checkout the
    operator has), and otherwise the directory the command was run from, which
    is where an operator adopting their own product repo is standing.
    """
    if into:
        candidate = Path(into).resolve() / product_repo
        if candidate.is_dir():
            return candidate
    return Path.cwd().resolve()


def _resolve_docs(args, shape: str, product_repo: str) -> tuple[str, ...]:
    """``--docs`` as product-repo-relative paths, each one checked.

    Three refusals, and each is a promise the seeded index would otherwise not
    keep. A path that does not exist is a source the surveyor cannot open. A
    path OUTSIDE the product checkout is a file that is not in the repository
    the index describes — an absolute path on this operator's laptop, most
    likely, which means nothing to anyone else reading `spec/index.md`. And a
    path carrying a backtick or a control character would break the inline-code
    quoting the seed's own lint depends on.
    """
    base = _docs_base(args.into, product_repo)
    resolved: list[str] = []
    for raw in (str(d) for d in (args.docs or ())):
        if _DOCS_FORBIDDEN.search(raw):
            raise ProvisionError(
                f"--docs {one_line(raw)!r} carries a backtick or a control "
                f"character. Each source is written into the seeded index as "
                f"inline code, and lint's masking — which is what keeps these "
                f"paths from being read as cross-references — depends on that "
                f"quoting holding."
            )
        full = (base / raw).resolve()
        try:
            relative = full.relative_to(base)
        except ValueError:
            raise ProvisionError(
                f"--docs {raw!r} resolves to {full}, which is outside "
                f"{base}. Survey sources are documentation in the PRODUCT "
                f"repository and are listed repo-relative, so a path outside it "
                f"would put a location only this machine has into "
                f"spec/index.md."
            ) from None
        if not full.exists():
            raise ProvisionError(
                f"--docs {raw!r} does not exist ({full}). The survey sources are "
                f"listed in the seeded index for the surveyor to find, so a path "
                f"that is not there would be a promise the index cannot keep."
            )
        resolved.append(relative.as_posix())
    return tuple(resolved)


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
    _refuse_yaml_keyword(product, "--product")
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
    for area in areas:
        if area in RESERVED_AREAS:
            raise ProvisionError(
                f"--area {area!r} is the id the seed's own spec/{area}.md claims, "
                f"so an area of that name would seed a second file answering to "
                f"one id. Lint would not say so — its duplicate-id check is about "
                f"SCENARIO ids — so the seed would go green and the collision "
                f"would surface later. Name it something else "
                f"({', '.join(RESERVED_AREAS)} are the seed's)."
            )
        _refuse_yaml_keyword(area, "--area")

    docs = _resolve_docs(args, shape, product_repo)
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
# `commit` is quoted because a sha is a string and only accidentally not a
# number: a 40-character sha that happened to be all digits would be read back
# as an integer, and nothing downstream would match it against a ref.
#
# Seeded by `vellum init` at the first commit touching spec/ in
# {intent_slug} — the commit that made this installation's spec exist.
# `name` is decoration: nothing reads it to decide anything, so it may be
# absent, late or wrong without changing behavior.
pin:
  commit: "{commit}"
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
            # Rendered raw, NOT through `one_line`: these are paths a surveyor
            # opens, and a path truncated at 120 characters is a promise the
            # index cannot keep. What `one_line` would have guarded against —
            # a newline, a backtick — is refused outright in `_resolve_docs`
            # instead, which is the only place that can refuse rather than
            # mangle.
            sources="\n".join(f"- `{d}`" for d in answers.docs) or "- (none named)",
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
    held cannot be printed by accident. ``secret`` is the *name* of the
    environment variable the value comes from, which is printable for the same
    reason and is what the checklist's ``printf`` line has to name. The value
    itself travels beside the step, in a local dict, to :meth:`Gh.run`.
    """

    what: str
    argv: tuple[str, ...] = ()
    stdin: str | None = None
    #: The secret this step's value is looked up under — an environment variable
    #: name, never a value. Set exactly on the steps that pipe something in.
    secret: str | None = None
    #: True when nothing automates this: branch protection, a review, a setting
    #: on a repository this installation does not own.
    manual: bool = False
    #: True when the LOCAL half depends on this step. Only one does — cloning
    #: the existing product repository of a brownfield shape, without which the
    #: adoption branch would sit on no history and push to nothing — so it runs
    #: before the build rather than after it. It is still one entry in one list;
    #: this only says where in the run it happens.
    before: bool = False

    def command(self, places: dict[str, str] | None = None) -> str:
        """The step as it would be typed, with *places* filled in.

        The checklist has to carry "the exact values"
        (``spec/features/installation.md``), and two of them — where each
        checkout is — are not known when :func:`forge_steps` builds the list.
        They travel as placeholders (:attr:`Plan.places` names all of them) and
        are substituted here and in :func:`_take`, from one mapping, so what an
        operator is told to run is what the transport would have run.
        """
        if not self.argv:
            return ""
        rendered = " ".join(_quote((places or {}).get(a, a)) for a in self.argv)
        if not self.secret:
            return rendered
        # The variable this step's own value comes from, not a stand-in: an
        # operator pastes this line, and `$TOKEN` is a variable nothing in the
        # plan ever told them to set. `printf %s` and no `--body`, because
        # `gh secret set` reads stdin only when the flag is absent — see the
        # module docstring.
        return f'printf %s "${self.secret}" | {rendered}'


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
            ("gh", "repo", "clone", product, "--", "<product clone>"),
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
            f"branch, never as a push to its default branch",
            ("git", "-C", "<product clone>", "push", "-u", "origin", ADOPT_BRANCH),
        ))
        steps.append(ForgeStep(
            f"open the adoption pull request on {product}",
            ("gh", "pr", "create", "--repo", product, "--base", "<adopt base>",
             "--head", ADOPT_BRANCH, "--title", "Adopt Vellum: the pin and the memory map",
             "--body-file", "<adopt PR body>"),
        ))

    steps.append(ForgeStep(
        f"set {INTENT_SECRET} on {intent} — the credential its caller stubs pass "
        f"to the reusable workflows, which read {product}",
        ("gh", "secret", "set", INTENT_SECRET, "--repo", intent),
        stdin=f"the {INTENT_SECRET} value, on stdin",
        secret=INTENT_SECRET,
    ))
    steps.append(ForgeStep(
        f"set {PRODUCT_SECRET} on {product} — the credential its conformance job "
        f"reads {intent} with, to fetch the spec tree at the pin",
        ("gh", "secret", "set", PRODUCT_SECRET, "--repo", product),
        stdin=f"the {PRODUCT_SECRET} value, on stdin",
        secret=PRODUCT_SECRET,
    ))
    # Nothing here sets `actions/permissions/access` on the PRODUCT repo, and
    # the omission is the point. That setting governs whether a repository's own
    # workflows may be REUSED by others; the workflows this installation's caller
    # stubs resolve against live in the host repo (`--from`), which no
    # installation owns. Setting it on the product repo would ask the forge for a
    # permission nothing needs — and on a user-owned account, where organization
    # access does not exist, the call fails outright and takes a successful
    # provisioning down with it. The setting that DOES matter is the host's, and
    # it is named below as a step no transport takes.
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
    #: True when a transport will clone the existing product repository, which
    #: decides where the adoption's clone lives — see :attr:`places`.
    cloned: bool = False
    #: The branch the adoption pull request is opened against. ``--branch`` until
    #: a clone says otherwise; the clone's own ``origin/HEAD`` after one, because
    #: the repository being adopted already has a default branch and it is not
    #: this installation's business to assume it is the one ``--branch`` named.
    adopt_base: str | None = None

    @property
    def places(self) -> dict[str, str]:
        """Every placeholder a step's argv can carry, from this plan's paths.

        ``<product clone>`` is the one that is not simply a path. With a
        transport, ``gh repo clone`` clones the existing product repository
        straight into the product checkout and everything afterwards runs there.
        Without one, that checkout is the STAND-IN this run built — it already
        holds the two seeded files and a root commit — so telling an operator to
        clone into it is telling them to run a command that fails. The checklist
        therefore names a sibling, and the push and the pull request name it too,
        so the three steps are one story an operator can follow top to bottom.
        """
        product = str(self.product_dir)
        return {
            "<intent checkout>": str(self.intent_dir),
            "<product checkout>": product,
            "<product clone>": product if self.cloned else f"{product}-clone",
            "<adopt PR body>": str(self.product_dir / ADOPT_PR_RELPATH),
            "<adopt base>": self.adopt_base or self.answers.branch,
        }

    def at(self, intent_dir: Path, product_dir: Path) -> None:
        """Move the plan to the checkouts a confirmed run actually made.

        Without ``--into`` the staging directory does not exist when the plan is
        printed — it must not, or ``--plan`` would create something — so the plan
        names a placeholder and the *report* names the real thing. One mutation,
        in one place, rather than a second list of steps carrying real paths.
        """
        self.intent_dir, self.product_dir = intent_dir, product_dir

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
            f"  nothing is changed on {a.intent_slug} or {a.product_slug}. The "
            f"workflows this installation's",
            f"  caller stubs resolve against live in {self.host}, which this "
            f"installation does not own,",
            "  so the setting that has to be right is that repo's and no "
            "transport here can set it.",
            "  It is in the steps below.",
            "",
            f"Steps (transport: {self.transport})"]
        lines += _step_lines(self.steps, self.places)
        lines += ["", *install.CANNOT_KNOW]
        return "\n".join(lines)


def _step_lines(steps: Sequence[ForgeStep], places: dict[str, str]) -> list[str]:
    """One step list, rendered.

    The plan, the checklist and the interrupted report all print the same shape,
    and printing it in one place is the same argument :func:`forge_steps` makes
    one level up: three renderings of one list cannot disagree about what the
    installer would have done.
    """
    lines: list[str] = []
    for number, step in enumerate(steps, start=1):
        lines.append(f"  {number:>2}. {step.what}")
        if step.argv:
            lines.append(f"      {step.command(places)}")
        if step.stdin:
            lines.append(f"      stdin: {step.stdin}")
        if step.manual:
            lines.append("      (no transport takes this one; it is yours)")
    return lines


def build_plan(answers: Answers, *, host: str, ref: str, transport: str,
               intent_dir: Path, product_dir: Path, cloned: bool = False) -> Plan:
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
        cloned=cloned,
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


def _default_branch(repo: Path, fallback: str) -> str:
    """The branch an adoption is a guest of: ``origin/HEAD``, or *fallback*.

    A cloned repository already has a default branch and it is the repository's
    fact, not this installation's — ``--branch`` is the branch the INTENT repo
    is created with, and assuming the two agree is how an adoption of a
    ``master``-based repo opens a pull request against a ``main`` that is not
    there. Read from the clone when there is one; ``--branch`` is the answer
    only when nothing else can say.
    """
    found = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD",
                 check=False)
    ref = found.stdout.strip()
    if found.returncode == 0 and ref.startswith("origin/"):
        return ref[len("origin/"):]
    return fallback


def _check_adoption(directory: Path, answers: Answers) -> str:
    """Refuse an adoption that would not be a guest. Returns the base branch.

    Vellum "claims one namespaced directory in a repo it did not create and
    never writes outside it" — and an adoption running over a checkout the
    operator is *working in* breaks that promise in three ways, none of them
    visible until the commit is already made:

    * a checkout that already carries ``.vellum/product.yaml`` is an
      installation, and seeding over it replaces its pin. It is the product-side
      twin of the refusal :func:`run` opens with, and it gets the same answer.
    * a dirty tree gets swept into the adoption commit. That is somebody's
      work-in-progress, and — since the sweep is indiscriminate — an untracked
      ``.env`` beside it, pushed to a branch and opened as a pull request.
    * a ``vellum/adopt`` branch that already exists is somebody's, and this run
      would move it.

    All three are exit 2: the command cannot answer, because answering means
    destroying something it did not make.
    """
    product_file = directory / ".vellum" / "product.yaml"
    if product_file.is_file():
        raise ProvisionError(
            f"{product_file} already exists: {directory} is already a Vellum "
            f"product repo, and adoption does not run over one. Seeding it again "
            f"would replace the pin this repository already answers to. If the "
            f"installation it belongs to is the one you meant, this is not the "
            f"command — `vellum init <intent-checkout>` stamps its stubs and is "
            f"idempotent."
        )
    dirty = _git(directory, "status", "--porcelain").stdout.strip().splitlines()
    if dirty:
        listed = ", ".join(one_line(line.strip()) for line in dirty[:10])
        more = f" (and {len(dirty) - 10} more)" if len(dirty) > 10 else ""
        raise ProvisionError(
            f"{directory} has uncommitted changes and adoption commits into it: "
            f"{listed}{more}. Everything above would land in the adoption commit "
            f"and then in a pull request — an untracked file included. Commit or "
            f"stash it first, or point --into at a clean checkout."
        )
    if _git(directory, "rev-parse", "--verify", "--quiet",
            f"refs/heads/{ADOPT_BRANCH}", check=False).returncode == 0:
        raise ProvisionError(
            f"{directory} already has a {ADOPT_BRANCH} branch. This run would "
            f"move it, and whatever is on it is somebody's — an adoption already "
            f"in review, most likely. Delete it, or rename it, and re-run."
        )
    base = _default_branch(directory, answers.branch)
    if _git(directory, "rev-parse", "--verify", "--quiet",
            f"refs/heads/{base}", check=False).returncode != 0:
        raise ProvisionError(
            f"{directory} has no {base!r} branch to open the adoption against, "
            f"and nothing in it names a default. Pass --branch <name> with the "
            f"branch this repository actually uses."
        )
    return base


def build_product(directory: Path, answers: Answers, pin: str) -> str | None:
    """Build and commit the product checkout. Returns the adoption's base.

    Greenfield creates it. The brownfield shapes put ``.vellum/`` on
    :data:`ADOPT_BRANCH` over whatever history the repository already has —
    ``spec/features/installation.md``: "its ``.vellum/`` arrives on a branch as a
    pull request, never as a push to its default branch". Vellum is a guest in a
    repository it did not create, and the three things a guest does not do are
    :func:`_check_adoption`'s.
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
    base = None
    if answers.adopting:
        base = _check_adoption(directory, answers)
        # `-b` off the named base, never `-B` off HEAD. `-B` resets a branch
        # that already exists, and HEAD is whatever the operator last checked
        # out — so the pair would silently adopt onto somebody else's feature
        # branch and open a pull request carrying its commits.
        _git(directory, "checkout", "-q", "-b", ADOPT_BRANCH, base)
    seed = product_seed(answers, pin)
    write_files(directory, seed)
    # Exactly the seeded paths, never `add -A`. This directory may be the
    # operator's own checkout, and `-A` sweeps whatever is in it — which is the
    # difference between "Vellum added two files" and "Vellum committed your
    # working tree". `_check_adoption` has already refused a dirty tree; this is
    # the second half of the same promise, and it holds even if that check is
    # ever loosened.
    _git(directory, "add", "--", *sorted(seed))
    _git(directory, "commit", "-q", "-m", (
        f"adopt Vellum: pin {answers.product} at {pin[:12]} and seed the memory map"
        if answers.adopting else
        f"pin {answers.product} at {pin[:12]} and seed the memory map"
    ))
    return base


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


def _write_adopt_body(directory: Path, answers: Answers, base: str) -> Path:
    """The adoption pull request's body, in the product checkout, uncommitted.

    In the checkout rather than in a temporary directory that is deleted on the
    way out, because the manual rung's checklist names this path: an operator
    reaching the ``gh pr create`` line needs the file to still be there. It is
    written after the adoption commit and never added, so the branch carries the
    two seeded files and nothing else.
    """
    path = directory / ADOPT_PR_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ADOPT_PR_BODY.format(shape=answers.shape, intent_slug=answers.intent_slug,
                             branch=base),
        encoding="utf-8",
    )
    return path


def _interrupted(plan: Plan, taken: list[ForgeStep]) -> str:
    """The report a forge failure leaves behind: what is real, and what is left.

    A transport failure is not a rollback. ``gh`` has already created whatever it
    created, and an operator who sees only the exception has to work out for
    themselves which of eight steps happened — so this says it, and hands the
    rest back in the same shape the checklist uses, because from here they ARE
    the checklist.
    """
    remaining = [step for step in plan.steps if step not in taken]
    lines = [
        f"vellum init — interrupted ({plan.answers.shape})",
        "",
        "A forge step failed and the run stopped there. Nothing below is rolled "
        "back:",
        "what was taken is real, and re-running this command over it would "
        "refuse at",
        "the name the forge now has.",
        "",
    ]
    if taken:
        lines.append(f"Forge steps taken before the failure ({plan.transport}):")
        lines += [f"  {number:>2}. {step.what}"
                  for number, step in enumerate(taken, start=1)]
    else:
        lines.append("Forge steps taken: none. Nothing was created on a forge.")
    lines.append("")
    lines.append(
        f"Left to you — {len(remaining)} step(s), starting with the one that "
        f"failed:"
    )
    lines += _step_lines(remaining, plan.places)
    lines += [
        "",
        f"  The local checkouts are at {plan.intent_dir} and {plan.product_dir}; "
        f"the commands",
        f"  above name them. `vellum doctor {plan.intent_dir}` verifies what a "
        f"checkout can see.",
    ]
    return "\n".join(lines)


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
                     list(docs), yes, into)
    answers = resolve(args, console)

    # ------------------------------------------------------------------
    # Where the local half is built, and which transport carries it.
    # `--into` is "no forge at all" (it is how the acceptance suite drives this
    # command), so it does not even look for `gh`: probing for a transport it
    # has been told not to use would make the run's behavior depend on the
    # machine it is on, which is the one thing a suite fixture must not do.
    # ------------------------------------------------------------------
    # `--plan` reaches no forge either, and that is stricter than it looks:
    # `detect_gh` runs `gh auth status`, which is a call to the forge — so the
    # one command whose whole promise is "creating nothing" would have contacted
    # one before printing a word. It asks PATH and nothing else, and labels what
    # it found hedged, because whether that `gh` is authenticated is exactly the
    # question it declined to ask.
    if into:
        gh, has_gh = None, False
        transport = "none (--into: local directories, no forge)"
    elif plan_only:
        gh, has_gh = None, shutil.which("gh") is not None
        transport = ("gh (if authenticated)" if has_gh else
                     "none (no `gh` on PATH) — the forge steps are a checklist")
    else:
        gh = detect_gh()
        has_gh = gh is not None
        transport = (f"gh ({gh.path})" if gh is not None else
                     "none (no authenticated `gh`) — the forge steps are a checklist")

    # The staging directory is not created until the plan is confirmed, because
    # "--plan prints it and stops, creating nothing" has to mean *nothing* — a
    # command that leaves an empty directory behind has created something, and a
    # plan whose paths were a fresh mkdtemp each run would not be deterministic
    # either. Until then the plan names where the checkouts will be.
    # Resolved, so every path this run prints and every path it hands `git -C`
    # is the same absolute one. A relative `--into` printed in a checklist an
    # operator reads in another directory is a checklist that does not work.
    root = Path(into).resolve() if into else None
    base = root if root is not None else Path(STAGING)
    intent_dir = base / answers.intent_repo
    product_dir = base / answers.product_repo

    plan = build_plan(answers, host=host, ref=pinned, transport=transport,
                      intent_dir=intent_dir, product_dir=product_dir, cloned=has_gh)
    print(plan.render(), file=stream)
    if plan_only:
        print("\n--plan: nothing was created.", file=stream)
        return 0

    # ------------------------------------------------------------------
    # Refusals that need to look outward, and they come BEFORE the confirmation
    # and before any directory is made. A name the forge already has is not a
    # value this command can validate on its own, so it is not in `resolve` —
    # but asking it after the operator has said yes means the run refuses
    # something they already agreed to, having made a staging directory it then
    # leaves behind. The spec's boundary is about the PLAN ("exits 2 for any
    # value it cannot validate before the plan"), and the plan has been printed.
    #
    # The directory check runs only when `--into` named where to build. Without
    # it the checkouts go inside a `mkdtemp` that does not exist yet and cannot
    # collide with anything — and it must not exist yet, or `--plan` further up
    # would have created something.
    # ------------------------------------------------------------------
    if gh is not None:
        _check_forge_names(gh, answers)
    if root is not None:
        _check_directories(answers, intent_dir, product_dir)

    _confirm(console, yes)

    #: The staging directory, while this run is still the only thing that would
    #: miss it. Set to None the moment the checkouts become something the
    #: operator is told to go and use — a seed that failed its checks, or a
    #: green one whose forge steps have started — because from then on deleting
    #: them would destroy the run's own report.
    staged: Path | None = None
    if root is None:
        root = Path(tempfile.mkdtemp(prefix="vellum-init-"))
        staged = root
        intent_dir = root / answers.intent_repo
        product_dir = root / answers.product_repo
        plan.at(intent_dir, product_dir)
        print(f"\nStaging the local half in {root}", file=stream)

    taken: list[ForgeStep] = []
    try:
        places = plan.places
        pin, stubs = build_intent(intent_dir, answers, host=host, ref=pinned)
        try:
            # The one forge step the local half depends on: the clone a
            # brownfield adoption branches from. Without a transport it stays on
            # the checklist and `build_product` makes a standalone repository
            # instead, which is the half a checkout can hold — the checklist step
            # says what to do with it.
            if gh is not None:
                _take(gh, [s for s in plan.steps if s.before], places, taken=taken)
            adopt_base = build_product(product_dir, answers, pin)
            if adopt_base is not None:
                # Now that the clone is here, the pull request's base is the
                # repository's own default branch rather than `--branch`, and
                # the body it is opened with is a file in the checkout.
                plan.adopt_base = adopt_base
                _write_adopt_body(product_dir, answers, adopt_base)
                places = plan.places

            check = check_seed(intent_dir, host=host)
            print("", file=stream)
            print(check.report(), file=stream)
            if not check.green:
                staged = None
                print(
                    f"\nvellum: the seed this run built is not green, so nothing "
                    f"was pushed. The checkouts are at {intent_dir} and "
                    f"{product_dir}; fix them there, or delete them and re-run.",
                    file=sys.stderr,
                )
                return 1

            # The seed passed both guards, so these checkouts are the operator's
            # now: every command below names them, and so does the report on the
            # way out of a failure.
            staged = None
            _perform(gh, plan, console, places=places, taken=taken)
        except ProvisionError:
            # A transport failure part way through. What was taken is real and
            # nothing rolls it back, so the report is the whole value left: it
            # says what exists on the forge and hands back every step from the
            # failure onward as the operator's to finish.
            if gh is not None:
                print("", file=stream)
                print(_interrupted(plan, taken), file=stream)
            raise
    finally:
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)

    remaining = [step for step in plan.steps if step not in taken]
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
    #: ``--into``. Read only by :func:`_docs_base`, which needs to know whether
    #: the product checkout the survey sources belong to is one this run was
    #: pointed at. Last and defaulted so the positional construction in
    #: :func:`run` stays the conversation's own order.
    into: str | None = None


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
    try:
        answer = console.ask("Proceed? [y/N]: ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        # Exit 2, not 0. Declining is "this command did not do the thing it was
        # asked to do", and a script that reads 0 as "the installation is there"
        # would be wrong — which is the same reason the no-TTY case above is a 2.
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


def _occupied(path: Path) -> bool:
    """True when *path* is already something: a file, or a non-empty directory.

    ``iterdir()`` on a path that is a *file* raises ``NotADirectoryError``, which
    reaches the operator as a traceback rather than as the refusal this question
    exists to make — and a file where a checkout should go is exactly the
    situation the refusal is about.
    """
    if not path.exists():
        return False
    return not path.is_dir() or any(path.iterdir())


def _check_directories(answers: Answers, intent_dir: Path, product_dir: Path) -> None:
    """The same refusal, one layer down: a directory that is already something.

    The local half has the same hazard as the forge half and the same one
    exception, so it makes the same call — otherwise ``--into`` over a populated
    directory would seed a spec tree on top of somebody's repository, which is
    the failure the forge check exists to prevent. The one exception, an
    adoption pointed at a checkout that is already there, is not waved through:
    it goes to :func:`_check_adoption`, which asks what a guest has to ask.
    """
    if _occupied(intent_dir):
        raise ProvisionError(
            f"{intent_dir} already exists and is not empty. The intent checkout "
            f"is created here; provisioning does not write into a directory it "
            f"did not make."
        )
    if _occupied(product_dir) and not answers.adopting:
        raise ProvisionError(
            f"{product_dir} already exists and is not empty, and --shape "
            f"{answers.shape} creates the product repository. Use --shape "
            f"{BROWNFIELD} to adopt what is there."
        )
    if answers.adopting and (product_dir / ".git").is_dir():
        # Asked here as well as in `build_product`, and deliberately: this is
        # the `--into`-over-a-real-checkout case, and asking now means an
        # operator whose tree is dirty is told so BEFORE the plan is confirmed
        # and before anything is built, rather than after an intent checkout
        # already exists.
        _check_adoption(product_dir, answers)


def _take(gh: Gh, steps: Sequence[ForgeStep], places: dict[str, str], *,
          taken: list[ForgeStep],
          values: dict[str, str | None] | None = None) -> None:
    """Run *steps* through the transport, appending each one taken to *taken*.

    A step is left untaken — and so lands on the checklist — when nothing
    automates it, or when the secret it carries was not supplied. Neither is
    silently skipped: both come back in the report as something to do.

    *taken* is a list the caller owns rather than a return value, and that is
    the whole point: a step that raises takes the run down with it, and a
    caller that learned what had been done from a ``return`` would learn
    nothing. What was created on the forge before the failure is real and
    cannot be rolled back, so the caller has to be able to say so.
    """
    values = values or {}
    for step in steps:
        if step.manual or not step.argv:
            continue
        if step.secret and not values.get(step.secret):
            continue
        argv = [places.get(arg, arg) for arg in step.argv]
        gh.run(argv, stdin=values.get(step.secret) if step.secret else None)
        taken.append(step)


def _perform(gh: Gh | None, plan: Plan, console: Console, *,
             places: dict[str, str], taken: list[ForgeStep]) -> None:
    """Take the forge steps, or take none and leave every one of them.

    The manual rung is not a different code path: it is this function with
    ``gh`` None, so the checklist an operator follows is the list
    :func:`forge_steps` built and the plan printed. What is left over is always
    ``plan.steps`` minus *taken*, computed by the caller from one rule.
    """
    if gh is None:
        return
    values = {
        INTENT_SECRET: secret_for(INTENT_SECRET, console),
        PRODUCT_SECRET: secret_for(PRODUCT_SECRET, console),
    }
    _take(gh, [step for step in plan.steps if not step.before], places,
          values=values, taken=taken)


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
        lines += _step_lines(remaining, plan.places)
        lines.append("")
        lines.append(
            f"  Then `vellum doctor {intent_dir}` verifies the whole: what a "
            f"checkout can see it checks, and what it cannot it says it cannot."
        )
    else:
        lines.append(
            f"Nothing is left over. `vellum doctor {intent_dir}` verifies the "
            f"installation from the checkout alone."
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
    "ADOPT_BRANCH", "ADOPT_PR_RELPATH", "RESERVED_AREAS",
    "Answers", "Console", "ForgeStep", "Gh", "Plan",
    "PRODUCT_SECRET", "INTENT_SECRET", "ProvisionError", "SHAPES",
    "VISIBILITIES", "build_plan", "check_seed", "detect_gh", "first_spec_commit",
    "forge_steps", "intent_seed", "product_seed", "requested", "resolve", "run",
    "run_provision",
]
