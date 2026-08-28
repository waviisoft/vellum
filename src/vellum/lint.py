"""``vellum lint`` — frontmatter schema, cross-references, and Gherkin parsing.

Every finding is one ``Finding``; any finding at all fails the run, which is
what ``spec/features/spec-pipeline.md`` requires of spec CI.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, asdict
from pathlib import Path

from vellum.gherkin_blocks import GherkinParseError, parse_block
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


def _check_links(
    sf: SpecFile, root: Path, scenario_anchors: dict[str, set[str]]
) -> list[Finding]:
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
        try:
            rel = target.relative_to(root).as_posix()
        except ValueError:
            rel = None
        known = heading_anchors(target) | scenario_anchors.get(rel or "", set())
        if ref.fragment not in known:
            findings.append(
                Finding(
                    sf.relpath,
                    ref.line,
                    "LN002",
                    f"'{ref.target}' has no heading or scenario anchor "
                    f"'#{ref.fragment}'",
                )
            )
    return findings


def _check_gherkin(sf: SpecFile) -> tuple[list[Finding], set[str]]:
    findings: list[Finding] = []
    anchors: set[str] = set()
    for fence in sf.fences:
        if fence.info != "gherkin":
            continue
        try:
            scenarios = parse_block(fence.body, fence.body_line)
        except GherkinParseError as exc:
            findings.append(Finding(sf.relpath, exc.line, "GH001", exc.message))
            continue
        if not scenarios:
            findings.append(
                Finding(
                    sf.relpath,
                    fence.start_line,
                    "GH002",
                    "gherkin block declares no scenarios",
                )
            )
        for sc in scenarios:
            if sc.anchor in anchors or sc.anchor[-2:-1] == "-" and sc.anchor[-1].isdigit():
                findings.append(
                    Finding(
                        sf.relpath,
                        sc.line,
                        "GH003",
                        f"duplicate scenario anchor '{sc.anchor.rsplit('-', 1)[0]}'"
                        f" — give the scenario a distinct name",
                    )
                )
            anchors.add(sc.anchor)
            if not sc.steps and not sc.background_steps:
                findings.append(
                    Finding(
                        sf.relpath, sc.line, "GH004", f"scenario '{sc.name}' has no steps"
                    )
                )
    return findings, anchors


def lint_tree(spec_dir: str | Path) -> list[Finding]:
    """Every finding in the tree at *spec_dir*, ordered by file then line."""
    root = resolve_spec_root(spec_dir)
    files = iter_spec_files(root)

    findings: list[Finding] = []
    scenario_anchors: dict[str, set[str]] = {}
    for sf in files:
        gh_findings, anchors = _check_gherkin(sf)
        findings.extend(gh_findings)
        scenario_anchors[sf.relpath] = anchors
    for sf in files:
        findings.extend(_check_frontmatter(sf))
        findings.extend(_check_links(sf, root, scenario_anchors))
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
