"""The conformance map: what the suite proves about the product right now.

The report is deterministic by construction — no timestamps, no durations, no
absolute paths, and scenarios in the extracted suite's own order — so two runs
at one commit produce byte-identical output and a diff between two commits is
the change in conformance.
"""

from __future__ import annotations

import json
from collections import Counter

from support.adapter import CAPABILITIES
from support.runner import CANNOT_RUN, ERROR, FAIL, OUTCOMES, PASS, UNDEFINED, ScenarioResult

_HEADLINE = {
    PASS: "Passes against the current product",
    FAIL: "Fails honestly (the behavior is specified and not built)",
    CANNOT_RUN: "Cannot run yet (names the missing infrastructure)",
    ERROR: "Errored (a defect in harness/)",
    UNDEFINED: "Undefined (no step definition — the suite is not executable)",
}


def counts(results: list[ScenarioResult]) -> Counter:
    return Counter(r.outcome for r in results)


def to_dict(suite: dict, deployment_name: str, results: list[ScenarioResult]) -> dict:
    tally = counts(results)
    return {
        "schema": 1,
        "deployment": deployment_name,
        "spec_version": suite["spec_version"],
        "spec_version_name": suite.get("spec_version_name"),
        "spec_head": suite["spec_head"],
        "shallow": suite["shallow"],
        "scenario_count": suite["scenario_count"],
        "counts": {outcome: tally.get(outcome, 0) for outcome in OUTCOMES},
        "scenarios": [
            {
                "id": r.id,
                "file": r.file,
                "line": r.line,
                "feature": r.feature,
                "name": r.name,
                "version": r.version,
                "outcome": r.outcome,
                "blocked_on": r.blocked_on,
                "example": r.example,
                "progress": r.progress,
                "steps": [
                    {
                        "keyword": s.keyword,
                        "text": s.text,
                        "status": s.status,
                        "detail": s.detail,
                        "step_definition": s.where,
                    }
                    for s in r.steps
                ],
            }
            for r in results
        ],
    }


def _table(rows: list[list[str]], header: list[str]) -> list[str]:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return out


def to_markdown(suite: dict, deployment_name: str, results: list[ScenarioResult]) -> str:
    tally = counts(results)
    lines: list[str] = ["# Acceptance suite — conformance map", ""]

    version = suite["spec_version"] or "unknown"
    lines += [
        f"- Deployment under test: `{deployment_name}`",
        f"- Spec version extracted at: `{version[:12]}`"
        + (f" ({suite['spec_version_name']})" if suite.get("spec_version_name") else ""),
        f"- Scenarios in the suite: {suite['scenario_count']}",
    ]
    if suite["shallow"]:
        lines.append(
            "- **The checkout is shallow**, so `vellum suite extract` re-dated "
            "scenarios below the graft. Scenario *versions* in this report are "
            "unreliable; outcomes are not affected."
        )
    lines.append("")

    lines += ["## Summary", ""]
    lines += _table(
        [[outcome, str(tally.get(outcome, 0)), _HEADLINE[outcome]]
         for outcome in OUTCOMES if tally.get(outcome, 0)],
        ["Outcome", "Scenarios", "Meaning"],
    )
    lines.append("")

    lines += ["## The map", ""]
    lines += _table(
        [[f"`{r.key}`", r.outcome, r.file,
          ", ".join(f"`{c}`" for c in r.blocked_on) or "—"]
         for r in results],
        ["Scenario", "Outcome", "Spec file", "Blocked on"],
    )
    lines.append("")

    for outcome in OUTCOMES:
        chosen = [r for r in results if r.outcome == outcome]
        if not chosen:
            continue
        lines += [f"## {outcome} — {_HEADLINE[outcome].lower()}", ""]
        for r in chosen:
            lines.append(f"### `{r.key}` — {r.name}")
            lines.append("")
            lines.append(f"`{r.file}:{r.line}` · Feature: {r.feature}")
            lines.append("")
            for capability in r.blocked_on:
                kind, description = CAPABILITIES[capability]
                lines += [f"Blocked on **`{capability}`** ({kind}): {description}.", ""]
            for step in r.steps:
                marker = {
                    "passed": "ok", "failed": "FAILED", "blocked": "blocked",
                    "error": "ERROR", "undefined": "UNDEFINED", "not run": "not run",
                }[step.status]
                lines.append(f"- `{marker}` **{step.keyword}** {step.text}")
                if step.detail:
                    detail = " ".join(step.detail.split())
                    lines.append(f"  - {detail}")
            if r.progress:
                lines += ["", "Notes from the run:"]
                lines += [f"  - {note}" for note in r.progress]
            lines.append("")

    blocked = sorted({c for r in results for c in r.blocked_on})
    if blocked:
        lines += ["## What has to exist", ""]
        rows = []
        for capability in blocked:
            kind, description = CAPABILITIES[capability]
            waiting = sorted(r.key for r in results if capability in r.blocked_on)
            rows.append([f"`{capability}`", kind, description,
                         ", ".join(f"`{key}`" for key in waiting)])
        lines += _table(rows, ["Capability", "Kind", "What it would be", "Scenarios waiting"])
        lines.append("")

    return "\n".join(lines) + "\n"


def to_json(suite: dict, deployment_name: str, results: list[ScenarioResult]) -> str:
    return json.dumps(to_dict(suite, deployment_name, results), indent=2, sort_keys=False) + "\n"
