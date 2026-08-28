"""``vellum ledger open|advance`` — the per-version traceability records.

One YAML record per spec version, written only by automation
(``spec/features/ledger.md``). Records advance state in place; history is git,
so the file is append-only in effect rather than by construction.

Records are emitted in block style with a fixed key order, so that advancing a
state produces a one-line diff and a read/write round-trip is byte-stable.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import yaml

VERSION_RE = re.compile(r"^(?:spec-v)?(\d+)$")

RECORD_STATES = (
    "approved",
    "planning",
    "implementing",
    "verified",
    "shipped",
    "superseded",
)
ITEM_STATES = ("planned", "implementing", "merged", "superseded")

#: Fixed emission order. Keys the spec reserves but v0.1 never sets (``line``
#: for maintenance lines, ``locks`` for area-locked parallel waves) are written
#: with their defaults so activating them later is implementation, not migration.
RECORD_KEYS = (
    "spec_version",
    "approved",
    "spec_pr",
    "line",
    "baseline",
    "labels",
    "state",
    "locks",
    "work_items",
    "release",
)
ITEM_KEYS = ("issue", "title", "repo", "satisfies", "pr", "state", "briefing", "cost")
COST_KEYS = ("attempts", "tokens", "usd", "executor")


class LedgerError(Exception):
    """A ledger operation could not be completed."""


def parse_version(value: str | int) -> int:
    m = VERSION_RE.match(str(value).strip())
    if not m:
        raise LedgerError(f"{value!r} is not a spec version (expected N or spec-vN)")
    return int(m.group(1))


def version_tag(number: int) -> str:
    return f"spec-v{number}"


def record_path(ledger_dir: str | Path, number: int) -> Path:
    return Path(ledger_dir) / f"{version_tag(number)}.yaml"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ordered(data: dict, keys: tuple[str, ...]) -> dict:
    """Reorder *data* by *keys*, keeping any unrecognised keys at the end."""
    out = {k: data[k] for k in keys if k in data}
    out.update({k: v for k, v in data.items() if k not in out})
    return out


def new_cost() -> dict:
    return {"attempts": 0, "tokens": 0, "usd": 0.0, "executor": None}


def new_record(
    number: int,
    spec_pr: int | None = None,
    baseline: int | None = None,
    labels: list[str] | None = None,
    line: str = "main",
    approved: str | None = None,
) -> dict:
    return {
        "spec_version": version_tag(number),
        "approved": approved or _now(),
        "spec_pr": spec_pr,
        "line": line,
        "baseline": version_tag(baseline) if baseline is not None else None,
        "labels": list(labels or []),
        "state": "approved",
        "locks": [],
        "work_items": [],
        "release": None,
    }


def new_item(
    issue: int,
    title: str,
    repo: str,
    satisfies: list[str] | None = None,
    state: str = "planned",
    briefing: str | None = None,
) -> dict:
    return {
        "issue": issue,
        "title": title,
        "repo": repo,
        "satisfies": list(satisfies or []),
        "pr": None,
        "state": state,
        "briefing": briefing,
        "cost": new_cost(),
    }


def dump(record: dict) -> str:
    record = _ordered(dict(record), RECORD_KEYS)
    record["work_items"] = [
        _ordered({**item, "cost": _ordered(dict(item.get("cost") or {}), COST_KEYS)}, ITEM_KEYS)
        for item in record.get("work_items", [])
    ]
    return yaml.safe_dump(record, sort_keys=False, default_flow_style=False, width=100)


def load(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LedgerError(f"{path}: cannot read ledger record: {exc}") from exc
    if not isinstance(data, dict):
        raise LedgerError(f"{path}: ledger record is not a YAML mapping")
    return data


def write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(record), encoding="utf-8")


def open_record(
    ledger_dir: str | Path,
    number: int,
    spec_pr: int | None = None,
    baseline: int | None = None,
    labels: list[str] | None = None,
    line: str = "main",
    approved: str | None = None,
) -> tuple[Path, bool]:
    """Create the record for *number*. Idempotent: an existing one is left alone.

    Returns ``(path, created)``. Idempotence matters because the reconciler may
    replay an approval (decision D11): a second call must not rewrite a record
    whose wave has already advanced.
    """
    path = record_path(ledger_dir, number)
    if path.exists():
        return path, False
    write(path, new_record(number, spec_pr, baseline, labels, line, approved))
    return path, True


def find_item(record: dict, issue: int) -> dict | None:
    return next(
        (i for i in record.get("work_items", []) if i.get("issue") == issue), None
    )


def advance(
    ledger_dir: str | Path,
    number: int,
    state: str | None = None,
    release: str | None = None,
    plan: list[dict] | None = None,
    issue: int | None = None,
    title: str | None = None,
    repo: str | None = None,
    satisfies: list[str] | None = None,
    item_state: str | None = None,
    pr: int | None = None,
    briefing: str | None = None,
    attempts: int = 0,
    tokens: int = 0,
    usd: float = 0.0,
    executor: str | None = None,
) -> Path:
    """Advance a record's state, commit a work plan, or update one work item.

    Cost is *accumulated*: ``spec/behaviors/budgets-and-costs.md`` records every
    agent invocation into the item's entry, so ``--attempts/--tokens/--usd`` add
    to what is there rather than replacing it. ``--executor`` names the most
    recent one.
    """
    path = record_path(ledger_dir, number)
    if not path.exists():
        raise LedgerError(f"{path}: no ledger record for {version_tag(number)}; open it first")
    record = load(path)

    if state is not None:
        if state not in RECORD_STATES:
            raise LedgerError(
                f"{state!r} is not a record state ({', '.join(RECORD_STATES)})"
            )
        record["state"] = state
    if release is not None:
        record["release"] = release

    if plan is not None:
        for entry in plan:
            _upsert_planned(record, entry)

    if issue is not None:
        item = find_item(record, issue)
        if item is None:
            if title is None or repo is None:
                raise LedgerError(
                    f"work item {issue} is not in {path.name}; "
                    f"--title and --repo are required to add it"
                )
            item = new_item(issue, title, repo, satisfies, briefing=briefing)
            record.setdefault("work_items", []).append(item)
        else:
            if title is not None:
                item["title"] = title
            if repo is not None:
                item["repo"] = repo
            if satisfies:
                item["satisfies"] = list(satisfies)
            if briefing is not None:
                item["briefing"] = briefing
        if item_state is not None:
            if item_state not in ITEM_STATES:
                raise LedgerError(
                    f"{item_state!r} is not a work-item state ({', '.join(ITEM_STATES)})"
                )
            item["state"] = item_state
        if pr is not None:
            item["pr"] = pr
        cost = item.setdefault("cost", new_cost())
        cost["attempts"] = (cost.get("attempts") or 0) + attempts
        cost["tokens"] = (cost.get("tokens") or 0) + tokens
        cost["usd"] = round((cost.get("usd") or 0.0) + usd, 6)
        if executor is not None:
            cost["executor"] = executor
    elif any((title, repo, satisfies, item_state, pr, briefing, attempts, tokens, usd, executor)):
        raise LedgerError("work-item options require --item <issue>")

    write(path, record)
    return path


def _upsert_planned(record: dict, entry: dict) -> None:
    """Merge one work plan entry into the record, keyed by issue number."""
    issue = entry.get("issue")
    if issue is None:
        raise LedgerError(f"work plan entry has no 'issue': {entry!r}")
    existing = find_item(record, issue)
    fields = {
        "title": entry.get("title", ""),
        "repo": entry.get("repo", ""),
        "satisfies": list(entry.get("satisfies") or []),
    }
    if existing is None:
        record.setdefault("work_items", []).append(new_item(issue, **fields))
    else:
        existing.update({k: v for k, v in fields.items() if v})


def load_plan(path: str | Path) -> list[dict]:
    """Read ``workplan.yaml``: a ``work_items:`` list, or a bare list."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    items = data.get("work_items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise LedgerError(f"{path}: expected a list of work items")
    return items
