"""Parsing the fenced ``gherkin`` blocks embedded in spec files.

Gherkin allows one ``Feature:`` per document, but the spec tree puts more than
one in a single fence (``features/certification-and-releases.md`` at spec-v1).
``split_documents`` therefore cuts a block at top-level ``Feature:`` lines and
hands each piece to the official Cucumber parser separately, so a block "parses"
when every document inside it does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from gherkin.errors import CompositeParserException, ParserException
from gherkin.parser import Parser
from gherkin.token_scanner import TokenScanner

#: Scenario identity is an explicit ``@id:<slug>`` tag, globally unique across
#: the intent repo (spec/decisions/2026-08-28-scenario-identity.md). The id is
#: the identity; the file is only its current home.
ID_TAG_PREFIX = "@id:"
SCENARIO_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: ``Feature:`` at column zero starts a new document. Localised keywords are out
#: of scope for v0.1: the spec tree is English and lint would silently pass a
#: block it had failed to split.
_FEATURE_RE = re.compile(r"^Feature\s*:", re.MULTILINE)
_TAG_LINE_RE = re.compile(r"^\s*@\S")


class GherkinParseError(Exception):
    """A gherkin block did not parse. ``line`` is 1-based within the spec file."""

    def __init__(self, message: str, line: int):
        super().__init__(message)
        self.message = message
        self.line = line


@dataclass
class Step:
    keyword: str
    text: str
    #: Context / Action / Outcome, with And and But resolved to the type of the
    #: step above them. Fingerprints use this rather than the written keyword,
    #: so rewriting "And" as "Given" is not a behavioral change.
    keyword_type: str = ""


@dataclass
class Background:
    """A ``Background:`` block. Banned by the spec; lint reports it (GH008)."""

    feature: str
    #: 1-based line within the spec file.
    line: int
    steps: list[Step] = field(default_factory=list)


@dataclass
class Scenario:
    feature: str
    name: str
    keyword: str
    #: 1-based line within the spec file.
    line: int
    tags: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    background_steps: list[Step] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)
    #: The ``@id:`` slug, or None when the scenario declares none. Lint reports
    #: a missing id (GH005); extraction falls back to the fingerprint, which is
    #: how scenarios written before ids existed keep their version.
    id: str | None = None
    #: Every ``@id:`` tag seen, so lint can report a scenario carrying two.
    id_tags: list[str] = field(default_factory=list)


@dataclass
class Block:
    """What one fenced ``gherkin`` block contains."""

    scenarios: list[Scenario] = field(default_factory=list)
    backgrounds: list[Background] = field(default_factory=list)


def split_documents(body: str) -> list[tuple[int, str]]:
    """Split a block into ``(line_offset, text)`` documents at ``Feature:`` lines.

    ``line_offset`` is 0-based from the first line of *body*. Tag lines
    immediately above a ``Feature:`` travel with it.
    """
    lines = body.split("\n")
    starts = [i for i, ln in enumerate(lines) if _FEATURE_RE.match(ln)]
    if len(starts) <= 1:
        return [(0, body)]
    # Walk each Feature line back over its contiguous tag lines.
    cuts = []
    for s in starts:
        j = s
        while j > 0 and _TAG_LINE_RE.match(lines[j - 1]):
            j -= 1
        cuts.append(j)
    cuts[0] = 0
    docs = []
    for idx, start in enumerate(cuts):
        end = cuts[idx + 1] if idx + 1 < len(cuts) else len(lines)
        docs.append((start, "\n".join(lines[start:end])))
    return docs


def _steps(raw: list[dict]) -> list[Step]:
    steps: list[Step] = []
    previous = "Context"
    for item in raw:
        keyword_type = item.get("keywordType") or ""
        if keyword_type == "Conjunction":
            keyword_type = previous
        elif keyword_type:
            previous = keyword_type
        steps.append(
            Step(
                keyword=item["keyword"].strip(),
                text=item["text"],
                keyword_type=keyword_type,
            )
        )
    return steps


def _examples(raw: list[dict]) -> list[dict]:
    out = []
    for ex in raw or []:
        header = ex.get("tableHeader")
        out.append(
            {
                "name": ex.get("name", ""),
                "header": [c["value"] for c in header["cells"]] if header else [],
                "rows": [
                    [c["value"] for c in row["cells"]]
                    for row in ex.get("tableBody", []) or []
                ],
            }
        )
    return out


def parse_block(body: str, block_body_line: int) -> Block:
    """Everything one fenced block contains: its scenarios and any Backgrounds.

    *block_body_line* is the 1-based spec-file line of the block's first line, so
    returned nodes and raised errors carry spec-file line numbers.
    """
    block = Block()
    scenarios = block.scenarios
    for offset, doc in split_documents(body):
        base = block_body_line + offset
        try:
            parsed = Parser().parse(TokenScanner(doc))
        except (CompositeParserException, ParserException) as exc:
            raise GherkinParseError(_first_error(exc), _error_line(exc, base)) from exc
        feature = parsed.get("feature")
        if not feature:
            continue
        background: list[Step] = []
        for child in feature.get("children", []):
            if "background" in child:
                node = child["background"]
                background = _steps(node.get("steps", []))
                block.backgrounds.append(
                    Background(
                        feature=feature["name"],
                        line=base + node["location"]["line"] - 1,
                        steps=list(background),
                    )
                )
                continue
            node = child.get("scenario")
            if not node:
                continue
            scenarios.append(
                Scenario(
                    feature=feature["name"],
                    name=node["name"],
                    keyword=node["keyword"].strip(),
                    line=base + node["location"]["line"] - 1,
                    tags=[t["name"] for t in node.get("tags", [])],
                    steps=_steps(node.get("steps", [])),
                    background_steps=list(background),
                    examples=_examples(node.get("examples")),
                )
            )
    block.scenarios = [_attach_id(sc) for sc in scenarios]
    return block


def _attach_id(sc: Scenario) -> Scenario:
    """Read the scenario's ``@id:`` tag(s) onto it. Validation is lint's job."""
    sc.id_tags = [
        t[len(ID_TAG_PREFIX) :] for t in sc.tags if t.startswith(ID_TAG_PREFIX)
    ]
    if len(sc.id_tags) == 1 and SCENARIO_ID_RE.match(sc.id_tags[0]):
        sc.id = sc.id_tags[0]
    return sc


def scenario_ref(scenario_id: str) -> str:
    """The form ledger ``satisfies:`` entries use for an acceptance criterion."""
    return f"scenario:{scenario_id}"


def _first_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None) or [exc]
    return " ".join(str(errors[0]).split())


def _error_line(exc: Exception, base: int) -> int:
    err = (getattr(exc, "errors", None) or [exc])[0]
    location = getattr(err, "location", None)
    if isinstance(location, dict) and "line" in location:
        return base + location["line"] - 1
    m = re.search(r"\((\d+):\d+\)", str(err))
    return base + int(m.group(1)) - 1 if m else base
