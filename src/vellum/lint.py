"""``vellum lint`` — frontmatter schema, cross-references, and Gherkin parsing.

Every finding is one ``Finding``; any finding at all fails the run, which is
what ``spec/features/spec-pipeline.md`` requires of spec CI.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, asdict
from pathlib import Path

from vellum.gherkin_blocks import (
    ID_TAG_PREFIX,
    GherkinParseError,
    Scenario,
    parse_block,
)
from vellum.links import find_references, heading_anchors, resolve
from vellum.specfile import (
    DATE_RE,
    ID_RE,
    SINCE_RE,
    SpecFile,
    iter_spec_files,
    resolve_spec_root,
    schema_for,
)


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    code: str
    message: str

    def format(self) -> str:
        return f"{self.file}:{self.line}: {self.code} {self.message}"


def _is_iso_date(value) -> bool:
    """YAML turns an unquoted ``2026-08-27`` into a date; a quoted one stays text."""
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return True
    return isinstance(value, str) and bool(DATE_RE.match(value))


def _check_frontmatter(sf: SpecFile) -> list[Finding]:
    if sf.frontmatter_error is not None:
        return [Finding(sf.relpath, 1, "FM001", sf.frontmatter_error)]

    findings: list[Finding] = []
    schema = schema_for(sf.relpath)
    fm = sf.frontmatter or {}
    known = set(schema["required"]) | set(schema["optional"])

    for key in schema["required"]:
        if key not in fm:
            findings.append(
                Finding(sf.relpath, 1, "FM002", f"frontmatter is missing '{key}'")
            )
    for key in sorted(set(fm) - known):
        findings.append(
            Finding(
                sf.relpath,
                1,
                "FM003",
                f"unknown frontmatter key '{key}' "
                f"(allowed: {', '.join(sorted(known))})",
            )
        )

    if "id" in fm and not (isinstance(fm["id"], str) and ID_RE.match(fm["id"])):
        findings.append(
            Finding(sf.relpath, 1, "FM004", f"id {fm['id']!r} is not a lowercase slug")
        )
    if "title" in fm and not (isinstance(fm["title"], str) and fm["title"].strip()):
        findings.append(Finding(sf.relpath, 1, "FM004", "title must be a non-empty string"))
    if "since" in fm and not (
        isinstance(fm["since"], str) and SINCE_RE.match(fm["since"])
    ):
        findings.append(
            Finding(
                sf.relpath,
                1,
                "FM004",
                f"since {fm['since']!r} is not 'spec-v<integer>' (decision D6)",
            )
        )
    if "date" in fm and not _is_iso_date(fm["date"]):
        findings.append(
            Finding(sf.relpath, 1, "FM004", f"date {fm['date']!r} is not YYYY-MM-DD")
        )
    return findings


def _check_links(sf: SpecFile, root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for ref in find_references(sf):
        target = resolve(ref, sf, root)
        if target is None:
            findings.append(
                Finding(
                    sf.relpath,
                    ref.line,
                    "LN001",
                    f"reference '{ref.target}' does not resolve "
                    f"(tried alongside the file, the spec root, and its parent)",
                )
            )
            continue
        if not ref.fragment:
            continue
        if ref.fragment not in heading_anchors(target):
            findings.append(
                Finding(
                    sf.relpath,
                    ref.line,
                    "LN002",
                    f"'{ref.target}' has no heading '#{ref.fragment}'",
                )
            )
    return findings


def _check_gherkin(sf: SpecFile) -> tuple[list[Finding], list[Scenario]]:
    """Parse every gherkin block; return findings and the scenarios found.

    Id uniqueness is not checked here: ids are unique across the whole intent
    repo, not per file, so ``lint_tree`` checks them once it has the tree.
    """
    findings: list[Finding] = []
    scenarios: list[Scenario] = []
    for fence in sf.fences:
        if fence.info != "gherkin":
            continue
        try:
            block = parse_block(fence.body, fence.body_line)
        except GherkinParseError as exc:
            findings.append(Finding(sf.relpath, exc.line, "GH001", exc.message))
            continue
        found = block.scenarios
        # A fence holds exactly one Gherkin document, so a stock Cucumber
        # runner reads it unmodified and drops nothing. The extra Feature is
        # the defect; the scenarios under it are well-formed and still extract.
        for extra in block.features[1:]:
            findings.append(
                Finding(
                    sf.relpath,
                    extra.line,
                    "GH009",
                    f"feature '{extra.name}' shares a fence with "
                    f"'{block.features[0].name}'; each gherkin fence holds exactly "
                    f"one Gherkin document, so give each Feature its own fence",
                )
            )
        # A scenario is a contract unit that travels alone: briefings quote it,
        # the ledger references scenario:<id>, question issues attach one.
        # A Background puts part of its meaning outside it.
        for background in block.backgrounds:
            findings.append(
                Finding(
                    sf.relpath,
                    background.line,
                    "GH008",
                    f"feature '{background.feature}' declares a Background; "
                    f"scenarios are self-contained, so move shared setup into "
                    f"a harness compound step",
                )
            )
        if not found:
            findings.append(
                Finding(
                    sf.relpath,
                    fence.start_line,
                    "GH002",
                    "gherkin block declares no scenarios",
                )
            )
        for sc in found:
            findings.extend(_check_scenario_id(sf, sc))
            if not sc.steps and not sc.background_steps:
                findings.append(
                    Finding(
                        sf.relpath, sc.line, "GH004", f"scenario '{sc.name}' has no steps"
                    )
                )
            # An outline with no Examples parses cleanly and then never runs —
            # a suite gap that looks like coverage. spec-v5 defines the class by
            # construct, not by keyword, so this asks whether the node is an
            # outline: `Scenario Template` is the same construct under Gherkin's
            # other English keyword and fails for the same reason. The finding
            # names the keyword actually written.
            if sc.is_outline and not any(ex["rows"] for ex in sc.examples):
                findings.append(
                    Finding(
                        sf.relpath,
                        sc.line,
                        "GH007",
                        f"{sc.keyword.lower()} '{sc.name}' has no Examples rows, "
                        f"so it never runs",
                    )
                )
        scenarios.extend(found)
    return findings, scenarios


def _check_scenario_id(sf: SpecFile, sc: Scenario) -> list[Finding]:
    """Every scenario carries exactly one well-formed ``@id:`` tag."""
    if not sc.id_tags:
        return [
            Finding(
                sf.relpath,
                sc.line,
                "GH005",
                f"scenario '{sc.name}' has no {ID_TAG_PREFIX}<slug> tag",
            )
        ]
    if len(sc.id_tags) > 1:
        return [
            Finding(
                sf.relpath,
                sc.line,
                "GH006",
                f"scenario '{sc.name}' carries {len(sc.id_tags)} id tags "
                f"({', '.join(sorted(sc.id_tags))}); a scenario has exactly one",
            )
        ]
    if sc.id is None:
        return [
            Finding(
                sf.relpath,
                sc.line,
                "GH006",
                f"scenario id '{sc.id_tags[0]}' is not a lowercase slug",
            )
        ]
    return []


def _check_unique_ids(found: list[tuple[str, Scenario]]) -> list[Finding]:
    """Scenario ids are unique across the intent repo, not merely per file.

    Identity is the id and the file is only its current home, so two files
    claiming one id makes "introduced or last changed" unanswerable.
    """
    homes: dict[str, list[tuple[str, Scenario]]] = {}
    for relpath, sc in found:
        if sc.id is not None:
            homes.setdefault(sc.id, []).append((relpath, sc))

    findings: list[Finding] = []
    for scenario_id, claims in sorted(homes.items()):
        if len(claims) == 1:
            continue
        for relpath, sc in claims:
            others = [
                f"{other}:{o.line}" for other, o in claims if o is not sc
            ]
            findings.append(
                Finding(
                    relpath,
                    sc.line,
                    "GH003",
                    f"duplicate scenario id '{scenario_id}', also at "
                    f"{', '.join(sorted(others))}",
                )
            )
    return findings


def lint_tree(spec_dir: str | Path) -> list[Finding]:
    """Every finding in the tree at *spec_dir*, ordered by file then line."""
    root = resolve_spec_root(spec_dir)
    files = iter_spec_files(root)

    findings: list[Finding] = []
    scenarios: list[tuple[str, Scenario]] = []
    for sf in files:
        gh_findings, found = _check_gherkin(sf)
        findings.extend(gh_findings)
        scenarios.extend((sf.relpath, sc) for sc in found)
    findings.extend(_check_unique_ids(scenarios))
    for sf in files:
        findings.extend(_check_frontmatter(sf))
        findings.extend(_check_links(sf, root))
    return sorted(findings, key=lambda f: (f.file, f.line, f.code, f.message))


def run(spec_dir: str, as_json: bool = False, out=None) -> int:
    """Print findings and return the process exit code."""
    import sys

    stream = out or sys.stdout
    findings = lint_tree(spec_dir)
    if as_json:
        json.dump(
            {"ok": not findings, "findings": [asdict(f) for f in findings]},
            stream,
            indent=2,
        )
        stream.write("\n")
    else:
        for f in findings:
            print(f.format(), file=stream)
        if findings:
            print(
                f"{len(findings)} finding(s) in {spec_dir}", file=stream
            )
    return 1 if findings else 0
