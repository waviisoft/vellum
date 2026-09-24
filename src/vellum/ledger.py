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

import contextlib
import datetime
import hashlib
import os
import re
import subprocess
import tempfile
import threading
import urllib.parse
from pathlib import Path

import yaml

from vellum.text import one_line

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
# `announcements:` — the append-only log of every addressed event a run wrote
# at its own boundary (`spec/features/continuous-engineering.md`) — is
# deliberately NOT in the tuple above, and the omission is a finding rather
# than an oversight. `ITEM_KEYS` is this module's reading of the fields
# `spec/features/ledger.md` names, which is what
# `test_work_item_carries_every_field_the_spec_names` grades it as; that slice
# names an issue, a title, a repo, satisfies, a PR, a state, a briefing, a
# cost, a certification and a lease, and no announcement. So the field rides
# where `ordered` already promises an installation's own keys will ride — at the
# end, kept rather than dropped — and is materialised only on an item that has
# actually announced something, which leaves every record of an item that has
# not byte for byte what it was. Whether the ledger slice should name the field
# is a spec question and is raised as one.
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


def ordered(data: dict, keys: tuple[str, ...]) -> dict:
    """Reorder *data* by *keys*, keeping any unrecognised keys at the end.

    Public because ``release.py`` orders ``releases.yaml`` and each cut inside
    it the same way, for the same reason ``RECORD_KEYS`` and ``ITEM_KEYS``
    exist: a state change is then a one-line diff and a read/write round-trip
    is byte-stable. Keeping unrecognised keys is half the contract — these are
    the intent repo's files and an installation may carry keys this version
    does not model.
    """
    out = {k: data[k] for k in keys if k in data}
    out.update({k: v for k, v in data.items() if k not in out})
    return out


#: The private spelling this module has always used internally.
_ordered = ordered


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


def _ordered_list_present(item: dict, key: str, keys: tuple[str, ...]) -> None:
    """Order every mapping inside ``item[key]`` in place, if it is there and is
    a list. Each entry of the ``announcements:`` log is ordered independently
    (``_ordered_present``'s reasoning applies per-entry, not to the list as a
    whole) and a non-mapping entry is left exactly as found."""
    if key in item and isinstance(item[key], list):
        item[key] = [
            _ordered(dict(entry), keys) if isinstance(entry, dict) else entry
            for entry in item[key]
        ]


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
    # Imported here rather than at module scope: `vellum.announce` reads the
    # installation's declaration to address an announcement, and it reads this
    # module to find the record — so a top-level import either way is a cycle.
    # The key order is the announcement module's to state, for `LEASE_KEYS`'s
    # reason, and this is the one line that needs it.
    from vellum.announce import ANNOUNCEMENT_KEYS

    _ordered_list_present(out, "announcements", ANNOUNCEMENT_KEYS)
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
    """Write *record*, atomically (K4): a reader never observes a half-written
    file. A temp file in the same directory (so the rename is on one
    filesystem) is written and fsynced, then ``os.replace`` swaps it in —
    ``os.replace`` is atomic on POSIX and on Windows alike, unlike
    ``Path.write_text``'s truncate-then-write.

    **S-7: published at the ordinary file mode, not ``mkstemp``'s ``0600``.**
    A temp file `tempfile.mkstemp` creates is private to its own owner by
    construction, which is right for a file nobody else should ever see under
    its temporary name — but wrong for what it becomes after ``os.replace``:
    an ordinary tracked ledger record, which a checkout shared between
    accounts (or simply read by a different service user than the one that
    last advanced it) expects at the same ``0666 & ~umask`` a plain
    ``open(..., "w")`` would have given it. ``os.fchmod`` sets that explicitly
    before the rename publishes the file under its real name, so the
    permissive mode is in place from the first moment anything can see it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = dump(record)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        umask = os.umask(0)
        os.umask(umask)
        os.fchmod(fd, 0o666 & ~umask)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


#: The lock file's name, wherever it lands (R2/S8): never inside a tracked
#: ledger tree, so a workflow that commits ``ledger/`` never commits it, and
#: `vellum verify boundaries` never counts it as a crossing.
LOCK_NAME = "vellum-announce.lock"

#: Lock paths this thread currently holds, for ``locked()``'s reentrancy.
_lock_state = threading.local()


def _git_dir(ledger_dir: Path) -> Path | None:
    """The ``.git`` directory of the work tree containing *ledger_dir*, or
    None when there is none (not a git checkout, or git is unavailable).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(ledger_dir), "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    found = Path(raw)
    return found if found.is_absolute() else Path(ledger_dir) / found


def git_toplevel(path: str | Path) -> str | None:
    """The git work tree containing *path*, or None (no guessed fallback).

    The default checkout ``ledger advance`` addresses against (corrected
    ruling, superseding the first cut of S4): neither the process's current
    directory nor ``ledger_dir``'s textual parent, which is not obliged to be
    a checkout at all — the work tree git itself resolves *is*.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    found = proc.stdout.strip()
    return found or None


def _user_lock_dir() -> Path:
    """A private, per-user directory for the ledger lock's tempdir fallback
    (S-8).

    ``tempfile.gettempdir()`` is shared and world-writable; a lock file
    placed directly inside it sits at a name any other account on the same
    machine can pre-create, replace, or symlink before this process ever gets
    there — an ordinary shared-tempdir footgun this project's own exclusion
    lock must not carry. Scoped to this user (``uid`` in the name, mode
    ``0700``) and refused outright if something already occupies that name
    under a different owner, rather than silently reused.
    """
    base = Path(tempfile.gettempdir()) / f"vellum-locks-{os.getuid()}"
    try:
        base.mkdir(mode=0o700, exist_ok=True)
        owner = base.stat().st_uid
    except OSError as exc:
        raise LedgerError(f"{base}: cannot prepare the ledger lock directory: {exc}") from exc
    if owner != os.getuid():
        raise LedgerError(
            f"{base}: owned by another account, not this one; refusing to place "
            f"a ledger lock inside a directory this process does not control"
        )
    os.chmod(base, 0o700)
    return base


def _tempdir_lock_path(ledger_dir: str | Path) -> Path:
    key = hashlib.sha256(str(Path(ledger_dir).resolve()).encode()).hexdigest()[:24]
    return _user_lock_dir() / f"{LOCK_NAME}.{key}"


def _lock_path(ledger_dir: str | Path) -> Path:
    """Where the cross-process ledger lock lives (R2), for the ordinary case.

    Inside git's own directory when the ledger sits in a work tree — content
    nothing ever stages, so a workflow that commits ``ledger/`` never commits
    the lock and a boundary guard diffing a commit never sees it. The
    per-user tempdir path otherwise, for a ledger this cannot place in a git
    checkout at all. This is only the *candidate*: ``locked()`` is what
    actually falls back to the tempdir path (S-8), and it does so on any
    failure to open this one, not only when there is no git directory to
    begin with.
    """
    git_dir = _git_dir(Path(ledger_dir))
    if git_dir is not None:
        return git_dir / LOCK_NAME
    return _tempdir_lock_path(ledger_dir)


@contextlib.contextmanager
def locked(ledger_dir: str | Path):
    """Hold an exclusive lock over a whole read-modify-write cycle on
    *ledger_dir*'s records (K4).

    One lock per ledger directory, shared by every writer — ``ledger
    open``/``advance``, ``announce``'s record and deliver paths — so two
    concurrent writers touching different work items in the same record (or
    different records under the same ledger) serialize rather than one
    clobbering the other's read. Opened with ``O_NOFOLLOW`` (S8): the lock
    file is never written through a symlink either.

    Reentrant within one thread: ``record_handoff`` holds this while it also
    calls ``record_announcement``, which holds it too, and ``flock`` treats
    two file descriptors on the same file — even from one process — as
    independent, so a naive second acquisition here would deadlock against
    the first. A thread-local set of currently-held lock paths makes the
    second (and any further nested) acquisition a no-op; a genuinely
    different thread or process still blocks on the real ``flock``.

    **S-8: the tempdir fallback triggers on failing to open the git-directory
    path, not only on there being no git directory at all.** A ``.git`` this
    process cannot write into — read-only, wrong permissions, an unusual
    submodule layout — is exactly as unusable as no ``.git`` being there, and
    the old rule only caught the second. Falling back is silent (the lock
    still does its job from the per-user tempdir instead); a failure to open
    *either* path is not, and becomes ``LedgerError`` rather than a raw
    ``OSError`` a caller elsewhere in this project is not written to expect.
    """
    import fcntl  # lazy: `fcntl` is POSIX-only, and every other symbol in
    # this module is used on every platform vellum otherwise runs on.

    held = getattr(_lock_state, "paths", None)
    if held is None:
        held = _lock_state.paths = set()

    primary = _lock_path(ledger_dir)
    if str(primary) in held:
        yield
        return

    fd = None
    used_path = primary
    try:
        primary.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(primary, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
    except OSError as primary_exc:
        fallback = _tempdir_lock_path(ledger_dir)
        if str(fallback) in held:
            yield
            return
        try:
            fallback.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(fallback, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
            used_path = fallback
        except OSError as fallback_exc:
            raise LedgerError(
                f"{ledger_dir}: cannot open a ledger lock at {primary} "
                f"({primary_exc}) or its fallback {fallback} ({fallback_exc}); "
                f"no exclusive lock could be taken"
            ) from fallback_exc

    key = str(used_path)
    held.add(key)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        held.discard(key)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    with locked(ledger_dir):
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
    *,
    checkout: str | Path | None = None,
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
    announce: bool = True,
    notes: list[str] | None = None,
) -> Path:
    """Advance a record's state, commit a work plan, or update one work item.

    Cost is *accumulated*: ``spec/behaviors/budgets-and-costs.md`` records every
    agent invocation into the item's entry, so ``--attempts/--tokens/--usd`` add
    to what is there rather than replacing it. ``--executor`` names the most
    recent one.

    **A pull request reaching a work item announces the run's end.**
    ``spec/features/continuous-engineering.md``: "a run's last act is to say
    so", and this is the act. What it writes is an ``announced:`` block naming
    the role the news is for, left undelivered (``dispatched: false``) here —
    ``vellum tick`` or ``vellum announce deliver`` performs the actual
    dispatch and reports it, and this command reports one itself only when
    called with ``--json`` (K3): marking dispatched without emitting a dispatch
    anywhere loses the event, since nothing ever reads it back out.

    *checkout* overrides the addressee's source. Left unnamed, it defaults to
    the git work tree containing *ledger_dir* (corrected S4 ruling,
    superseding the first cut, which was ``ledger_dir``'s textual parent alone
    with no git-toplevel attempt first — never the process's current
    directory) — falling back to that same textual parent only when
    *ledger_dir* is not inside a git work tree at all. When no addressee can
    be found even so — no ``write_boundaries``, no unique holder — the item's
    own state (its PR, its cost) is still recorded, the announcement is
    recorded too but with an empty ``to`` so it dispatches nobody, a warning
    lands in *notes*, and
    this still returns normally: an implicit announcement inside ordinary
    ledger bookkeeping must never fail a state change over an address it could
    not compute. ``vellum announce finished`` — the explicit command — keeps
    refusing outright in the same situation; only this implicit path softens.
    ``announce=False`` turns the whole of it off for a caller repairing a
    record rather than reporting a run.
    """
    with locked(ledger_dir):
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
                if announce:
                    # K3: based on the announcement's own state, not on
                    # whether the number changed — a retry after an
                    # unaddressed attempt must still get a chance to address
                    # it, and `set_announcement`'s own comparison (sans
                    # `dispatched`) is what actually decides whether anything
                    # changed.
                    _announce_finish(checkout, ledger_dir, item, issue, pr, notes)
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


def _announce_finish(checkout, ledger_dir, item: dict, issue: int, pr: int,
                     notes: list[str] | None) -> None:
    """Append a ``finished`` announcement to *item*'s log. Never raises, never
    marks ``dispatched`` (K3, and the corrected S4 ruling): recording a run's
    end is unconditional, and only ``deliver``/``tick`` (or this command's own
    ``--json``, in the CLI layer) ever flips that bit.
    """
    from vellum.announce import (
        AnnounceError,
        addressee_for_ledger,
        append_announcement,
        finished_announcement_id,
        new_announcement,
        retry_unaddressed,
    )

    # Note 2/4's rule: the git work tree containing `--ledger-dir`, falling
    # back to its textual parent *only* when the ledger is not in a git work
    # tree at all. `ledger_dir.parent` alone, with no git-toplevel attempt
    # first, is exactly the guess the blind review flagged (S4); this is the
    # fallback for the one case that guess did get right, not the whole rule.
    resolved = checkout
    if resolved is None:
        resolved = git_toplevel(ledger_dir) or str(Path(ledger_dir).parent)
    # Rule 4: a prior call may have left an earlier entry on this same item
    # unaddressed (no declared holder found at the time); retried here so an
    # installation that adds `write_boundaries` later does not have to wait
    # for the next tick to see it resolved.
    retried = retry_unaddressed(resolved, ledger_dir, item)
    to = ""
    try:
        to = addressee_for_ledger(resolved, ledger_dir)
    except AnnounceError as exc:
        if notes is not None:
            notes.append(
                f"Work item {issue} reported pull request {pr} and nothing "
                f"was addressed: {exc} The announcement is recorded as "
                f"undelivered; `vellum tick` and `announce list` will "
                f"surface it."
            )
    changed = append_announcement(item, new_announcement(
        finished_announcement_id(pr), "finished", to,
        f"work item {issue} has finished and reported pull request {pr}; "
        f"the wave's next part begins",
    )) or retried
    if notes is not None and to:
        notes.append(
            f"Work item {issue}'s run announced its end to {to}; `deliver` or "
            f"`tick` will dispatch it."
            if changed else
            f"Work item {issue} already announced its end to {to}; not re-announced."
        )


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


def clean_run_reference(run) -> tuple[str | None, tuple[str, ...]]:
    """``(the value to store, what was removed from it)``.

    ``--run`` is a *reference* to where the certification run is recorded, and
    it is published twice over: written into a ledger record that is committed
    to the intent repo, and printed by every ``certify check`` that reads it —
    which in CI means a job log and, piped to a step summary, a page. So the
    two places a token rides in a URL go: ``https://user:tok@host/run/7?token=y``
    is stored and printed as ``https://host/run/7``.

    A credential is stripped rather than refused, and that is deliberate: a
    ``certify record`` that failed would leave the ledger unable to say a run
    happened at all, which is the same reason recording a *red* result exits 0.
    The path and any fragment survive, because ``#step:3:1`` is how a forge
    addresses a line of a run log and losing it costs a reader the thing the
    reference exists for.

    The second half of the pair exists so the command can say *what* it
    dropped rather than diffing two strings and guessing. Userinfo and a query
    string are not the same news — one means a credential is now in a shell
    history and wants rotating, the other means a `?check_suite_focus=true`
    went with the rule — and a report that called both a credential would
    train a reader to ignore the one that is.

    This is a backstop, not a laundering service. ``--run`` must be
    credential-free at the point it is typed: by the time a value reaches here
    it has already been through a shell history, a process table and whatever
    workflow expression composed it, and none of those are undone by a
    substring being dropped on the way to disk.
    """
    if run is None:
        return None, ()
    text = str(run).strip()
    parts = urllib.parse.urlsplit(text)
    if not parts.netloc:
        # Not URL-shaped to urlsplit — a bare run id, a forge's own
        # `owner/repo#7`, or a URL typed without its scheme. There is no
        # userinfo to find without a scheme (a `:` or `@` in a bare id is
        # ordinary), but a `?…` tail is a query string whatever the shape, and
        # `ci.example/run/7?token=x` is URL-shaped to the human who typed it.
        # Drop the tail; leave the rest exactly as typed.
        head, sep, _ = text.partition("?")
        return head, (("query string",) if sep else ())
    host = parts.netloc.rpartition("@")[2]
    removed = tuple(
        what
        for what, present in (("userinfo", host != parts.netloc), ("query string", bool(parts.query)))
        if present
    )
    cleaned = urllib.parse.urlunsplit(
        (parts.scheme, host, parts.path, "", parts.fragment)
    )
    return cleaned, removed


def credential_free_run(run):
    """The value ``clean_run_reference`` would store, for callers printing one."""
    return clean_run_reference(run)[0]


def new_certification(sha: str, result: str, run: str | None = None, at: str | None = None) -> dict:
    """``certification: {sha, run, at, result}`` (``spec/features/ledger.md``)."""
    if result not in CERTIFICATION_RESULTS:
        raise LedgerError(
            f"{result!r} is not a certification result "
            f"({', '.join(CERTIFICATION_RESULTS)})"
        )
    return {
        "sha": parse_certified_sha(sha),
        "run": credential_free_run(run),
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
    certified = certification.get("sha")
    result = certification.get("result")
    if not certified:
        return False, (
            f"work item {item.get('issue')} has a certification naming no sha, so "
            f"there is no commit it is evidence about."
        )
    # Read exactly what a write would have produced, and nothing else. The
    # comparison used to normalise both fields on the way in — `.strip()` and
    # `.lower()` — while `new_certification` and `parse_certified_sha` accept
    # only the strict forms. That asymmetry is the whole defect: a hand-written
    # `Green`, or a sha with a stray space, authorized a merge although no run
    # this CLI performed could ever have written one. A record it could not
    # have written is a record it cannot vouch for, and the denial branch is
    # already the right home for that — every non-green shape is one answer
    # with a different sentence.
    if not isinstance(certified, str) or not FULL_SHA_RE.match(certified):
        return False, (
            f"work item {item.get('issue')} has a certification whose sha is "
            f"{one_line(certified)!r}, which is not the full lowercase forty a "
            f"recorded run writes. Nothing this CLI wrote looks like that, so it "
            f"is evidence about no commit in particular."
        )
    if certified != head:
        return False, (
            f"the certification on work item {item.get('issue')} is bound to "
            f"{certified[:12]}, and the head is {head[:12]}. Certification binds to "
            f"a sha: a commit pushed after the run was not covered by it."
        )
    if result != GREEN:
        recorded = "no result" if result in (None, "") else one_line(result)
        return False, (
            f"the certification at {head[:12]} recorded {recorded!r}, "
            f"not {GREEN!r}. The result is matched exactly as recorded: "
            f"`certify record` writes only {' or '.join(CERTIFICATION_RESULTS)}, "
            f"so a value that has to be trimmed or lowercased to read as "
            f"{GREEN!r} was not written by a certification run."
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
