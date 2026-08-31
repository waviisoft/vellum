"""``vellum ledger open|advance`` — the per-version traceability records.

One YAML record per spec version, keyed by the version's commit sha and written
only by automation (``spec/features/ledger.md``). Records advance state in
place; history is git, so the file is append-only in effect rather than by
construction.

The key is the sha and only the sha. A record may also carry a decorative
``name`` (``spec-vN``), which is written and displayed and never read to find,
match or order anything (``spec/decisions/2026-08-28-versions-are-commits.md``)
— so a record whose name is missing, late or wrong still resolves.

Records are emitted in block style with a fixed key order, so that advancing a
state produces a one-line diff and a read/write round-trip is byte-stable.

Two of a work item's fields are about a run rather than about the work
(``spec/features/ledger.md``), and they are read on opposite time-scales:

* ``certification`` is the recorded proof, **bound to a sha**. It is the only
  thing that authorizes an auto-merge, and it authorizes exactly one commit —
  see ``certification_authorizes()``, and ``vellum certify`` in
  ``src/vellum/certify.py``.
* ``lease`` is transient claim state, not history: written at claim, cleared at
  report, and *expired means absent* — see ``active_lease()``.

Both are **optional**. ``new_item()`` writes them as ``null``, the way ``line``
and ``locks`` are written on a record, so activating them is implementation
rather than migration; but ``dump()`` never inserts a key an item does not
already have. That split is the whole compatibility story: a record written
before this wave round-trips byte-for-byte, because the constructor sets
defaults and the serialiser only ever reorders what it was handed.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import yaml

#: A spec version is a commit. Abbreviations are accepted because a human
#: types them; git's own 7-character floor is the floor here too.
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

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
    "name",
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
#: Fixed emission order for a work item. ``certification`` and ``lease`` are
#: appended rather than slotted in beside ``pr``, so an item written before this
#: wave keeps every byte of its existing shape and gains the two at the end.
ITEM_KEYS = (
    "issue",
    "title",
    "repo",
    "satisfies",
    "pr",
    "state",
    "briefing",
    "cost",
    "certification",
    "lease",
)
COST_KEYS = ("attempts", "tokens", "usd", "executor")
#: ``certification: {sha, run, at, result}`` (``spec/features/ledger.md``).
CERTIFICATION_KEYS = ("sha", "run", "at", "result")
#: ``lease: {executor, taken, expires}`` (``spec/features/ledger.md``).
LEASE_KEYS = ("executor", "taken", "expires")

#: The two results a certification run can record. Only ``green`` authorizes.
CERTIFICATION_RESULTS = ("green", "red")
GREEN = "green"

#: A certification binds to one commit, so the sha it names is the whole forty
#: and never an abbreviation. ``SHA_RE`` accepts git's 7-character floor because
#: a *human types* a version to look a record up; nothing types a certified sha
#: — a runner reports it — and an authorization decided on a prefix is a
#: decision about a set of commits rather than about the one that was proved.
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class LedgerError(Exception):
    """A ledger operation could not be completed."""


def parse_version(value: str) -> str:
    """A spec version is a commit sha (``spec/decisions/2026-08-28-versions-are-commits.md``)."""
    sha = str(value).strip().lower()
    if not SHA_RE.match(sha):
        raise LedgerError(
            f"{value!r} is not a spec version (expected a commit sha). "
            f"Versions stopped being integers when they became commits."
        )
    return sha


def record_path(ledger_dir: str | Path, sha: str) -> Path:
    """Where a record for *sha* is written. The filename is the key."""
    return Path(ledger_dir) / f"{sha}.yaml"


def find_record(ledger_dir: str | Path, sha: str) -> Path | None:
    """The existing record for *sha*, whatever its filename, or None.

    The filename is where a record is *written*; what identifies one is the
    ``spec_version`` field, so a record renamed by hand — or written under a
    fuller or shorter sha than the caller has — is still found. Matching is by
    sha prefix in either direction, which is what makes ``vellum ledger advance
    --version 9c8b70a`` reach the record opened with the full forty.

    Raises ``LedgerError`` when the abbreviation reaches more than one record.
    An abbreviation is a convenience for a human typing, and the convenience
    ends where it stops naming one version: picking the first match in filename
    order would advance the state of *a* record, plausibly the wrong one, and
    say nothing. The caller is told which records it reached and types more of
    the sha. (An exact filename hit short-circuits above and is never
    ambiguous.)
    """
    sha = str(sha).strip().lower()
    direct = record_path(ledger_dir, sha)
    if direct.exists():
        return direct
    directory = Path(ledger_dir)
    if not directory.is_dir():
        return None
    matches: list[tuple[Path, str]] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            recorded = str(yaml.safe_load(path.read_text(encoding="utf-8"))["spec_version"])
        except (OSError, yaml.YAMLError, KeyError, TypeError):
            continue
        recorded = recorded.strip().lower()
        if not SHA_RE.match(recorded):
            continue
        if recorded.startswith(sha) or sha.startswith(recorded):
            matches.append((path, recorded))
    if len(matches) > 1:
        candidates = ", ".join(f"{recorded} ({path.name})" for path, recorded in matches)
        raise LedgerError(
            f"{sha!r} is ambiguous: it matches {len(matches)} ledger records "
            f"— {candidates}. Give more of the sha."
        )
    return matches[0][0] if matches else None


def now() -> str:
    """The moment this project writes into a record: ISO 8601, UTC, to the second.

    Public because ``release.py`` stamps a cut with it. One definition of how a
    moment is written is the same discipline that moved ``parse_time`` here from
    ``budget.py`` — the reader and the writer must not come to disagree."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value) -> datetime.datetime | None:
    """A record's ``approved``, as an aware UTC datetime, or None.

    PyYAML turns an unquoted timestamp into a ``datetime`` before this is
    reached, and ``vellum.ledger.dump`` writes a quoted string, so both arrive
    here. A naive datetime is read as UTC: every time this file writes is UTC
    (``ledger.now``), and guessing local would move a record across a period
    boundary depending on where the guard ran.
    """
    if isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, datetime.date):
        moment = datetime.datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc)


def _ordered(data: dict, keys: tuple[str, ...]) -> dict:
    """Reorder *data* by *keys*, keeping any unrecognised keys at the end."""
    out = {k: data[k] for k in keys if k in data}
    out.update({k: v for k, v in data.items() if k not in out})
    return out


def _ordered_present(item: dict, key: str, keys: tuple[str, ...]) -> None:
    """Order ``item[key]`` in place, if it is there and is a mapping.

    Deliberately not ``item.get(key) or {}``, which is how ``cost`` is handled:
    that turns an absent or null value into an empty mapping, and for these two
    fields absent, null and ``{}`` are three different claims — no certification
    was ever recorded, none is recorded now, and one was recorded with nothing
    in it. Only the first of those is a shape older records actually have, and
    materialising a key into them is what would cost a byte-identical
    round-trip. A non-mapping is left exactly as found, so a corrupt field
    reaches the reader that reports it rather than being reshaped on the way.
    """
    if key in item and isinstance(item[key], dict):
        item[key] = _ordered(dict(item[key]), keys)


def new_cost() -> dict:
    return {"attempts": 0, "tokens": 0, "usd": 0.0, "executor": None}


def new_record(
    sha: str,
    spec_pr: int | None = None,
    baseline: str | None = None,
    labels: list[str] | None = None,
    line: str = "main",
    approved: str | None = None,
    name: str | None = None,
) -> dict:
    return {
        "spec_version": sha,
        "name": name,
        "approved": approved or now(),
        "spec_pr": spec_pr,
        "line": line,
        "baseline": baseline,
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
        # Written as null the way a record writes `line` and `locks`: the shape
        # is reserved, so recording the first certification or lease is an
        # edit to a key that is already there. `dump` still never *inserts*
        # either into an item that arrived without them.
        "certification": None,
        "lease": None,
    }


def _ordered_item(item: dict) -> dict:
    out = {**item, "cost": _ordered(dict(item.get("cost") or {}), COST_KEYS)}
    _ordered_present(out, "certification", CERTIFICATION_KEYS)
    _ordered_present(out, "lease", LEASE_KEYS)
    return _ordered(out, ITEM_KEYS)


def dump(record: dict) -> str:
    record = _ordered(dict(record), RECORD_KEYS)
    record["work_items"] = [_ordered_item(item) for item in record.get("work_items", [])]
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
    sha: str,
    spec_pr: int | None = None,
    baseline: str | None = None,
    labels: list[str] | None = None,
    line: str = "main",
    approved: str | None = None,
    name: str | None = None,
) -> tuple[Path, bool]:
    """Create the record for *sha*. Idempotent: an existing one is left alone.

    Returns ``(path, created)``. Idempotence matters because the reconciler may
    replay an approval (decision D11): a second call must not rewrite a record
    whose wave has already advanced. It is also the whole replay guard the
    minting workflow needs now that there is no version to arithmetic and no
    tag to check — the record either exists for this commit or it does not.
    """
    existing = find_record(ledger_dir, sha)
    if existing is not None:
        return existing, False
    path = record_path(ledger_dir, sha)
    write(path, new_record(sha, spec_pr, baseline, labels, line, approved, name))
    return path, True


def find_item(record: dict, issue: int) -> dict | None:
    return next(
        (i for i in record.get("work_items", []) if i.get("issue") == issue), None
    )


def advance(
    ledger_dir: str | Path,
    sha: str,
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
    path = find_record(ledger_dir, sha)
    if path is None:
        raise LedgerError(
            f"{record_path(ledger_dir, sha)}: no ledger record for {sha}; open it first"
        )
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


def upsert_plan(record: dict, plan: list[dict]) -> None:
    """Merge a whole work plan into *record*, in place, keyed by issue number.

    The public seam over ``_upsert_planned``. ``advance()`` reads a record,
    merges and writes in one call, which is what a single ``vellum ledger
    advance --plan`` wants; the reconciler holds several records open across one
    tick and writes each once at the end, so it needs the merge without the
    read/write around it (``src/vellum/reconcile.py``). Idempotent for the same
    reason ``advance --plan`` is: an entry whose issue is already in the record
    updates that item rather than adding a second one.
    """
    for entry in plan:
        _upsert_planned(record, entry)


def load_plan(path: str | Path) -> list[dict]:
    """Read ``workplan.yaml``: a ``work_items:`` list, or a bare list."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    items = data.get("work_items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise LedgerError(f"{path}: expected a list of work items")
    return items


# ------------------------------------------------- certification and leases

def parse_certified_sha(value, what: str = "certified commit") -> str:
    """The full forty characters of a commit sha, or raise.

    ``parse_version`` accepts git's 7-character abbreviation because a human
    types a version to *look a record up*, and reaching the wrong record by an
    ambiguous prefix is caught by ``find_record`` and reported. This is the
    other kind of sha: the one an authorization is decided on. A prefix names a
    set of commits, so a certification stored or checked against one would
    authorize every commit in that set — including a commit nobody proved
    anything about. Nothing types this value; a runner reports it. So the
    convenience is not offered here and the comparison stays exact.
    """
    sha = str(value).strip().lower()
    if not FULL_SHA_RE.match(sha):
        raise LedgerError(
            f"{value!r} is not a full commit sha, and a {what} must be one. "
            f"Certification binds to exactly one commit, so an abbreviation — "
            f"which names a set of them — is refused rather than resolved."
        )
    return sha


def new_certification(sha: str, result: str, run: str | None = None, at: str | None = None) -> dict:
    """``certification: {sha, run, at, result}`` (``spec/features/ledger.md``)."""
    if result not in CERTIFICATION_RESULTS:
        raise LedgerError(
            f"{result!r} is not a certification result "
            f"({', '.join(CERTIFICATION_RESULTS)})"
        )
    return {
        "sha": parse_certified_sha(sha),
        "run": run,
        "at": at or now(),
        "result": result,
    }


def certify(
    ledger_dir: str | Path,
    sha: str,
    issue: int,
    certified_sha: str,
    result: str,
    run: str | None = None,
    at: str | None = None,
) -> Path:
    """Record a certification run against one work item. Returns the record path.

    The new certification *replaces* whatever was there. Certification binds to
    a sha, so a record of a run against some earlier commit is not evidence
    about this one and keeping it alongside would only invite a reader to
    resolve two claims. The superseded certification is not lost — the ledger's
    history is git.

    This does not check that *certified_sha* is the work item's PR head, and it
    cannot: the item records the PR's *number*, not its head commit, so nothing
    in the ledger knows what the head is. That comparison is the caller's, and
    it is exactly what ``certification_authorizes`` is given a head to make.
    """
    path = find_record(ledger_dir, sha)
    if path is None:
        raise LedgerError(
            f"{record_path(ledger_dir, sha)}: no ledger record for {sha}; open it first"
        )
    record = load(path)
    item = find_item(record, issue)
    if item is None:
        raise LedgerError(
            f"work item {issue} is not in {path.name}, so there is nothing to "
            f"certify. A certification is recorded against planned work."
        )
    item["certification"] = new_certification(certified_sha, result, run=run, at=at)
    write(path, record)
    return path


def certification_authorizes(item: dict, head: str) -> tuple[bool, str]:
    """Whether *item*'s certification authorizes a merge at *head*.

    Returns ``(authorized, reason)``; the reason is written to be printed
    whichever way it went. Only a recorded ``green`` at exactly *head*
    authorizes (``spec/features/ledger.md``), which makes every other shape —
    no certification, a red one, a green one against another commit, a corrupt
    field — the same answer with a different sentence.

    Every denial is an *answer*, not a failure to answer: "no green
    certification exists at this head" is true of a malformed certification
    block as surely as of an absent one, and the spec says so in as many words
    — a work item whose PR head is not the certified sha is uncertified,
    "whatever the record says it once was".
    """
    head = parse_certified_sha(head, what="head commit")
    certification = item.get("certification")
    if certification is None:
        return False, (
            f"no certification is recorded for work item {item.get('issue')}. "
            f"A merge is authorized by a recorded green certification run, never "
            f"by checks the examined party ran on itself."
        )
    if not isinstance(certification, dict):
        return False, (
            f"work item {item.get('issue')} has a certification field that is not "
            f"a mapping ({type(certification).__name__}); it records no run, so it "
            f"authorizes nothing."
        )
    certified = str(certification.get("sha") or "").strip().lower()
    result = str(certification.get("result") or "").strip().lower()
    if not certified:
        return False, (
            f"work item {item.get('issue')} has a certification naming no sha, so "
            f"there is no commit it is evidence about."
        )
    if certified != head:
        return False, (
            f"the certification on work item {item.get('issue')} is bound to "
            f"{certified[:12]}, and the head is {head[:12]}. Certification binds to "
            f"a sha: a commit pushed after the run was not covered by it."
        )
    if result != GREEN:
        return False, (
            f"the certification at {head[:12]} recorded {result or 'no result'!r}, "
            f"not {GREEN!r}."
        )
    return True, f"green certification recorded at {head[:12]}."


def new_lease(executor: str, expires: str, taken: str | None = None) -> dict:
    """``lease: {executor, taken, expires}`` (``spec/features/ledger.md``)."""
    if not str(executor or "").strip():
        raise LedgerError("a lease names the executor holding it; --executor was empty")
    if parse_time(expires) is None:
        raise LedgerError(
            f"{expires!r} is not a moment a lease can expire at "
            f"(e.g. 2026-08-31T14:00:00Z). A lease with no readable expiry is "
            f"read as no lease, so writing one would silently claim nothing."
        )
    return {"executor": str(executor).strip(), "taken": taken or now(), "expires": expires}


def active_lease(item: dict, now: datetime.datetime | None = None) -> dict | None:
    """*item*'s lease if it is held and unexpired, else None.

    "The reconciler ... treats an expired lease as no lease, returning the item
    to the queue" (``spec/features/ledger.md``), so expiry is resolved here and
    not left to each caller: an item is claimed exactly when this returns
    something. "Mid-run means holding an unexpired lease" is the same sentence
    read the other way, and ``@id:fire-and-collect`` is the scenario that turns
    on it.

    A lease whose ``expires`` cannot be read is **absent**, like an expired one.
    Both directions lose something and they are not symmetric: reading it as
    held strands the item forever behind a claim no clock can ever retire,
    which is the failure the expiry exists to prevent; reading it as free costs
    at most a second executor starting from the last pushed commit, which is
    what a lapsed lease already means here. Expiry is exclusive — a lease is
    held *until* it expires — so a lease expiring exactly now is not held.
    """
    lease = item.get("lease")
    if not isinstance(lease, dict):
        return None
    expires = parse_time(lease.get("expires"))
    if expires is None:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    return lease if expires > now else None


def _item_for(ledger_dir: str | Path, sha: str, issue: int) -> tuple[Path, dict, dict]:
    """``(path, record, item)`` for one work item, or raise ``LedgerError``."""
    path = find_record(ledger_dir, sha)
    if path is None:
        raise LedgerError(
            f"{record_path(ledger_dir, sha)}: no ledger record for {sha}; open it first"
        )
    record = load(path)
    item = find_item(record, issue)
    if item is None:
        raise LedgerError(f"work item {issue} is not in {path.name}")
    return path, record, item


def take_lease(
    ledger_dir: str | Path,
    sha: str,
    issue: int,
    executor: str,
    expires: str,
    taken: str | None = None,
) -> Path:
    """Claim a work item for *executor* until *expires*."""
    path, record, item = _item_for(ledger_dir, sha, issue)
    item["lease"] = new_lease(executor, expires, taken=taken)
    write(path, record)
    return path


def clear_lease(ledger_dir: str | Path, sha: str, issue: int) -> Path:
    """Release a work item's claim — what the reconciler does at report.

    Clearing writes ``null`` rather than deleting the key: the field is part of
    the item's shape once the item has one, and a released claim and a field
    that was never there are different things to a reader looking at a diff.
    """
    path, record, item = _item_for(ledger_dir, sha, issue)
    item["lease"] = None
    write(path, record)
    return path
