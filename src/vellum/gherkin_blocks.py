"""Parsing the fenced ``gherkin`` blocks embedded in spec files.

Gherkin allows one ``Feature:`` per document, and since spec-v4 the spec
requires each fence to hold exactly one, so a stock Cucumber parser reads every
block unmodified (``spec/features/scenarios-and-harness.md``). A conforming
block is therefore handed to the official parser whole.

Older tags are not conforming — ``features/certification-and-releases.md`` held
two Features in one fence from spec-v1 to spec-v5 — and ``suite.py`` reads
those trees to date scenarios. So ``split_documents`` stays as the fallback for
a block the parser cannot read whole: it cuts at top-level ``Feature:`` lines
and parses each piece separately, which is what lets lint name the extra
Feature (GH009) instead of reporting the parser's raw error, and lets
extraction keep describing every scenario in the block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from gherkin.dialect import Dialect
from gherkin.errors import CompositeParserException, ParserException
from gherkin.parser import Parser
from gherkin.token_scanner import TokenScanner

#: Scenario identity is an explicit ``@id:<slug>`` tag, globally unique across
#: the intent repo (spec/decisions/2026-08-28-scenario-identity.md). The id is
#: the identity; the file is only its current home.
ID_TAG_PREFIX = "@id:"
SCENARIO_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: ``Feature:`` at column zero starts a new document when a block has to be
#: split. Localised keywords are out of scope for v0.1: the spec tree is English
#: and lint would silently pass a block it had failed to split.
#:
#: Column zero is the entire scope, deliberately. An indented second
#: ``Feature:`` is absorbed as description prose and so escapes GH009 — which
#: costs nothing, because a stock parser reads that block identically. There is
#: no runner divergence to catch there, only a line that reads oddly.
_FEATURE_RE = re.compile(r"^Feature\s*:", re.MULTILINE)
_TAG_LINE_RE = re.compile(r"^\s*@\S")

#: The keywords that declare an outline, read from the parser's own English
#: dialect rather than written out here. Gherkin accepts ``Scenario Template``
#: as a synonym of ``Scenario Outline``, and a parsed scenario node records only
#: which word was written — it carries no flag saying the node is an outline.
#: Asking the dialect keeps this module's idea of the construct identical to the
#: parser's, so a synonym cannot go unrecognised. English only, for the reason
#: given above.
_OUTLINE_KEYWORDS = frozenset(Dialect.for_name("en").scenario_outline_keywords)


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
class Feature:
    """A ``Feature:`` declaration. The second and later ones are GH009."""

    name: str
    #: 1-based line within the spec file.
    line: int


@dataclass
class Rule:
    """A ``Rule:`` block. Banned by the spec; lint reports it (GH010).

    ``scenarios`` counts what the Rule holds rather than collecting it. The
    scenarios under a Rule are not admitted (the construct is banned), and the
    count exists so the finding can say how much a stock runner would execute
    that the suite does not describe — which is the defect
    (waviisoft/vellum-intent#16), not the keyword.
    """

    feature: str
    name: str
    keyword: str
    #: 1-based line within the spec file.
    line: int
    scenarios: int = 0


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
    #: True when ``keyword`` declares an outline. An outline with no Examples
    #: section parses identically to a plain Scenario but for the keyword, so
    #: this is the only thing separating them — and it is what GH007 tests,
    #: the construct rather than one of its two spellings.
    is_outline: bool = False
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
    #: Every ``Feature:`` the block declares, in order. A conforming block has
    #: exactly one; lint faults the rest (GH009).
    features: list[Feature] = field(default_factory=list)
    #: Every ``Rule:`` the block declares. A conforming block has none; lint
    #: faults each (GH010).
    rules: list[Rule] = field(default_factory=list)


def split_documents(body: str) -> list[tuple[int, str]]:
    """Split a block into ``(line_offset, text)`` documents at ``Feature:`` lines.

    ``line_offset`` is 0-based from the first line of *body*. Tag lines
    immediately above a ``Feature:`` travel with it.

    The cut is textual, so it is the fallback and not the first move: see
    ``_documents``, which asks the parser to read the block whole first.
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


def _parse_document(doc: str, base: int) -> dict:
    """One Gherkin document through the official parser, errors relocated to *base*."""
    try:
        return Parser().parse(TokenScanner(doc))
    except (CompositeParserException, ParserException) as exc:
        raise GherkinParseError(_first_error(exc), _error_line(exc, base)) from exc


def _split_and_parse(body: str, block_body_line: int) -> list[tuple[int, dict]]:
    """Cut *body* at top-level ``Feature:`` lines and parse each piece.

    Raises ``GherkinParseError`` for the first piece that does not parse, which
    for an unsplittable body is the block's own error and lint's GH001.
    """
    return [
        (offset, _parse_document(doc, block_body_line + offset))
        for offset, doc in split_documents(body)
    ]


def _documents(body: str, block_body_line: int) -> list[tuple[int, dict]]:
    """Parse a fence, asking the stock parser to read it whole before splitting.

    A conforming fence holds one Gherkin document, so the parser reads it
    unmodified and nothing is cut — which is exactly the property
    ``spec/features/scenarios-and-harness.md`` asks for, tested here directly
    rather than approximated by counting ``Feature:`` lines.

    Reading it whole is necessary but not sufficient, because the parser does
    not always *refuse* a second ``Feature:``. It only refuses one it reaches
    as a declaration; reached where free text is legal — a Feature's
    description, a Scenario's description — it silently absorbs the line as
    prose. The block then parses, one Feature short, with the second Feature's
    scenarios re-parented onto the first and its name gone. So a whole parse is
    trusted only when the body holds at most one top-level ``Feature:`` line.

    When it holds more, splitting is what tells the two cases apart, and the
    parser is again the one deciding: real Feature declarations each parse as
    their own document, while a cut through a docstring — where ``Feature:`` at
    column zero is literal text and the block really does hold one Feature —
    does not parse at all, so the whole parse stands.
    """
    try:
        whole = _parse_document(body, block_body_line)
    except GherkinParseError:
        return _split_and_parse(body, block_body_line)

    if len(split_documents(body)) <= 1:
        return [(0, whole)]
    try:
        return _split_and_parse(body, block_body_line)
    except GherkinParseError:
        return [(0, whole)]


def parse_block(body: str, block_body_line: int) -> Block:
    """Everything one fenced block contains: its Features, scenarios and Backgrounds.

    *block_body_line* is the 1-based spec-file line of the block's first line, so
    returned nodes and raised errors carry spec-file line numbers.
    """
    block = Block()
    scenarios = block.scenarios
    for offset, parsed in _documents(body, block_body_line):
        base = block_body_line + offset
        feature = parsed.get("feature")
        if not feature:
            continue
        block.features.append(
            Feature(
                name=feature["name"],
                line=base + feature["location"]["line"] - 1,
            )
        )
        background: list[Step] = []
        for child in feature.get("children", []):
            if "rule" in child:
                # Not descended into, deliberately. A Rule is banned
                # (spec/decisions/2026-08-28-no-rules.md), so its scenarios are
                # not admitted and lint rejects the construct. The decision
                # records what changes if the ban is ever lifted: walk them as
                # first-class, ids required as anywhere, and fold the rule text
                # into every nested scenario's fingerprint. Reported from the
                # parsed node, as GH008 and GH009 are — `Rule` localises, and a
                # rule written in a docstring or a step's text is not one.
                node = child["rule"]
                block.rules.append(
                    Rule(
                        feature=feature["name"],
                        name=node["name"],
                        keyword=node["keyword"].strip(),
                        line=base + node["location"]["line"] - 1,
                        scenarios=sum(
                            1 for c in node.get("children", []) if c.get("scenario")
                        ),
                    )
                )
                continue
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
            keyword = node["keyword"].strip()
            scenarios.append(
                Scenario(
                    feature=feature["name"],
                    name=node["name"],
                    keyword=keyword,
                    line=base + node["location"]["line"] - 1,
                    tags=[t["name"] for t in node.get("tags", [])],
                    steps=_steps(node.get("steps", [])),
                    background_steps=list(background),
                    examples=_examples(node.get("examples")),
                    is_outline=keyword in _OUTLINE_KEYWORDS,
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
