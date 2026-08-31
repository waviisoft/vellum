"""``vellum budget`` — the spend guard.

``spec/behaviors/budgets-and-costs.md``: "Exceeding a per-item cap parks the
item as needs-human; hitting the global cap parks the queue with a spend report.
The system degrades to asking for help, never to unbounded spend."
``@id:global-cap-parks-queue`` states the shape: a period cap of $100 with $99
recorded, and a next work item whose certification would exceed the cap, parks
the queue and files a spend report.

Two caps, two parks
-------------------
They are separate questions and both are answered on every run:

* ``budgets.per_item_usd`` against each work item's own accumulated
  ``cost.usd`` — a lifetime cap on one item, so it is not windowed;
* ``budgets.period_usd`` against everything spent inside the current period,
  plus whatever the caller says the next item will cost.

Exit 1 if either parks. Nothing is written: this reports a state, and the marker
it emits (``needs-human``, the escalation label the installation config already
declares) is for the caller that can label an issue or park a queue. Filing the
spend-report *issue* is forge work, the same division ``vellum mint`` keeps by
computing a tag and never applying one.

What this cannot see
--------------------
The scenario turns on "the next work item's **certification** would exceed the
cap", and certification does not exist yet — the intent repo's harness names it
a product gap. A projected cost is therefore an input, not something this can
compute: ``--projected`` takes it from a caller that knows, exactly as ``vellum
backpressure --pending`` takes the forge half of its window. Left unset it is
zero, and the report says the cap was measured against recorded spend alone
rather than implying the whole question was answered.

Where a period boundary comes from
----------------------------------
A cost entry carries no timestamp — ``vellum.ledger.COST_KEYS`` is attempts,
tokens, usd, executor — so spend is attributed to the period containing its
record's ``approved`` time, which is the only clock the ledger has. A record
whose ``approved`` cannot be read is counted **inside** the window rather than
outside it: a cost this cannot prove belongs to an earlier period is one the cap
must not let through, and the report names every record it had to treat that way.
"""

from __future__ import annotations

import datetime
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vellum.backpressure import NOT_A_RECORD, ledger_dir_for
from vellum.config import ConfigError, config_path, load as load_config
# `parse_time` reads the ledger's own timestamps, and the ledger's lease
# expiry reads them too. It is defined once, next to the `_now()` that
# writes them, and re-exported here: two definitions of how this project
# reads a recorded moment is how a spend window and a lease come to
# disagree about when something happened.
from vellum.ledger import SHA_RE, parse_time

#: The escalation label an over-cap item is parked under
#: (``.vellum/config.yaml`` ``labels.escalation``).
PARK_MARKER = "needs-human"

#: Periods a cap can be declared over. ``monthly`` is what the installation uses;
#: the other two exist because a cap is a policy dial and an installation running
#: hotter wants a shorter one. An unrecognised period is refused rather than
#: defaulted — a spend cap measured over the wrong window is not a cap.
PERIODS = ("daily", "weekly", "monthly")


class BudgetError(Exception):
    """The spend could not be measured against a cap."""


@dataclass
class Item:
    """One work item's recorded spend."""

    record: str
    sha: str
    issue: object
    title: str
    usd: float
    attempts: int
    tokens: int
    executor: str | None
    in_window: bool
    #: Set when the record's ``approved`` could not be read, so the window
    #: membership above was assumed rather than established.
    undated: bool = False


@dataclass
class Spend:
    """One budget measurement."""

    ledger: Path
    config: Path
    period: str
    window_start: datetime.datetime
    window_end: datetime.datetime
    period_cap: float
    item_cap: float | None
    projected: float
    items: list[Item]
    undated: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    @property
    def windowed(self) -> list[Item]:
        return [i for i in self.items if i.in_window]

    @property
    def spent(self) -> float:
        return round(sum(i.usd for i in self.windowed), 6)

    @property
    def committed(self) -> float:
        """Recorded spend plus what the caller says the next item will cost."""
        return round(self.spent + self.projected, 6)

    @property
    def over_items(self) -> list[Item]:
        if self.item_cap is None:
            return []
        return [i for i in self.items if i.usd > self.item_cap]

    @property
    def queue_parked(self) -> bool:
        """Hitting the cap parks the queue, so this is ``>=``, not ``>``.

        The question is "may the next work item run", the same question
        ``backpressure`` asks about the next spec merge — and a period whose
        spend has reached its cap exactly has, in the spec's word, *hit* it.
        """
        return self.committed >= self.period_cap

    @property
    def parked(self) -> bool:
        return self.queue_parked or bool(self.over_items)

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "window_start": self.window_start.isoformat().replace("+00:00", "Z"),
            "window_end": self.window_end.isoformat().replace("+00:00", "Z"),
            "period_cap_usd": self.period_cap,
            "item_cap_usd": self.item_cap,
            "spent_usd": self.spent,
            "projected_usd": self.projected,
            "committed_usd": self.committed,
            "parked": self.parked,
            "state": "parked" if self.parked else "ok",
            "marker": PARK_MARKER if self.parked else None,
            "queue_parked": self.queue_parked,
            "parked_items": [
                {"record": i.record, "issue": i.issue, "title": i.title, "usd": i.usd,
                 "marker": PARK_MARKER}
                for i in self.over_items
            ],
            "undated_records": list(self.undated),
            "unreadable": list(self.unreadable),
        }

    def report(self) -> str:
        start = self.window_start.date().isoformat()
        end = self.window_end.date().isoformat()
        lines = [
            f"Spend window: {start} .. {end} ({self.period})",
            f"  recorded  ${self.spent:.2f}",
            f"  projected ${self.projected:.2f}",
            f"  committed ${self.committed:.2f} of ${self.period_cap:.2f}",
            "",
            f"{len(self.windowed)} work item(s) in the window:",
        ]
        for item in self.windowed:
            flag = "OVER" if item in self.over_items else "    "
            lines.append(
                f"  {flag}  ${item.usd:>9.2f}  {item.record[:12]}  item {item.issue}"
                f"  {item.title}"
            )
        if not self.windowed:
            lines.append("  (nothing recorded in this window)")
        lines.append("")
        if self.item_cap is None:
            lines.append(
                f"No budgets.per_item_usd in {self.config}, so no per-item cap was "
                f"checked. Only the period cap was measured; set one, or pass "
                f"--item-cap."
            )
            lines.append("")
        if self.undated:
            lines.append(
                f"{len(self.undated)} record(s) carry no readable `approved` time and "
                f"were counted inside the window rather than outside it: "
                f"{', '.join(self.undated)}"
            )
            lines.append("")
        if self.unreadable:
            lines.append(
                f"{len(self.unreadable)} file(s) in the ledger could not be read as "
                f"records: {', '.join(self.unreadable)}"
            )
            lines.append("")
        for item in self.over_items:
            lines.append(
                f"PARKED [{PARK_MARKER}]: {item.record[:12]} item {item.issue} "
                f"({item.title}) has spent ${item.usd:.2f} against a per-item cap of "
                f"${self.item_cap:.2f}."
            )
        if self.queue_parked:
            lines.append(
                f"PARKED [queue]: ${self.committed:.2f} committed against a "
                f"${self.period_cap:.2f} {self.period} cap. The queue parks and a "
                f"spend report is due (spec/behaviors/budgets-and-costs.md)."
            )
        if not self.parked:
            lines.append(
                f"OK: ${self.period_cap - self.committed:.2f} left in this "
                f"{self.period} period."
            )
        if not self.projected:
            lines.append(
                "Measured against recorded spend alone. The next work item's "
                "certification cost is not something a checkout can know — pass "
                "--projected to ask whether it would exceed the cap."
            )
        return "\n".join(lines)


# ------------------------------------------------------------------ window

def window_for(period: str, as_of: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime]:
    """The half-open period containing *as_of*, in UTC."""
    moment = as_of.astimezone(datetime.timezone.utc)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return midnight, midnight + datetime.timedelta(days=1)
    if period == "weekly":
        start = midnight - datetime.timedelta(days=midnight.weekday())
        return start, start + datetime.timedelta(days=7)
    if period == "monthly":
        start = midnight.replace(day=1)
        end = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        return start, end
    raise BudgetError(
        f"{period!r} is not a period this can measure over ({', '.join(PERIODS)})"
    )


# ------------------------------------------------------------------- caps

def _cap(config: dict, path: Path, key: str, required: bool) -> float | None:
    if key not in config:
        if not required:
            return None
        raise BudgetError(
            f"{path}: no budgets.{key}. There is no cap to measure spend against; "
            f"set one (spec/behaviors/budgets-and-costs.md)."
        )
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise BudgetError(
            f"{path}: budgets.{key} is {value!r}; expected a non-negative number"
        )
    return float(value)


def _budgets(checkout: str | Path) -> tuple[dict, Path]:
    path = config_path(checkout)
    try:
        data = load_config(checkout)
    except ConfigError as exc:
        raise BudgetError(str(exc)) from exc
    budgets = data.get("budgets")
    if not isinstance(budgets, dict):
        raise BudgetError(
            f"{path}: no budgets mapping. There are no caps here to measure against."
        )
    return budgets, path


# ---------------------------------------------------------------- measuring

def measure(
    checkout: str | Path,
    ledger_dir: str | Path | None = None,
    period_cap: float | None = None,
    item_cap: float | None = None,
    period: str | None = None,
    projected: float = 0.0,
    as_of: datetime.datetime | None = None,
) -> Spend:
    """Recorded spend against the installation's caps."""
    if projected < 0:
        raise BudgetError(f"--projected must not be negative (got {projected})")
    budgets, path = _budgets(checkout)
    if period_cap is None:
        period_cap = _cap(budgets, path, "period_usd", required=True)
    elif period_cap < 0:
        raise BudgetError(f"--period-cap must not be negative (got {period_cap})")
    if item_cap is None:
        item_cap = _cap(budgets, path, "per_item_usd", required=False)
    elif item_cap < 0:
        raise BudgetError(f"--item-cap must not be negative (got {item_cap})")
    if period is None:
        period = budgets.get("period")
        if not isinstance(period, str) or not period.strip():
            raise BudgetError(
                f"{path}: no budgets.period. A spend cap with no period is not a "
                f"period cap; set one of {', '.join(PERIODS)}."
            )
        period = period.strip().lower()

    now = as_of or datetime.datetime.now(datetime.timezone.utc)
    start, end = window_for(period, now)

    ledger = Path(ledger_dir) if ledger_dir is not None else ledger_dir_for(checkout)
    if not ledger.is_dir():
        raise BudgetError(
            f"{ledger}: no ledger directory. Spend is recorded in ledger records, so "
            f"there is nothing here to measure; is {checkout} an intent checkout?"
        )

    items: list[Item] = []
    undated: list[str] = []
    unreadable: list[str] = []
    for record_path in sorted(ledger.glob("*.yaml")):
        if record_path.name in NOT_A_RECORD:
            continue
        try:
            data = yaml.safe_load(record_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            unreadable.append(record_path.name)
            continue
        if not isinstance(data, dict):
            unreadable.append(record_path.name)
            continue
        sha = str(data.get("spec_version") or "").strip().lower()
        if not SHA_RE.match(sha):
            unreadable.append(record_path.name)
            continue
        approved = parse_time(data.get("approved"))
        is_undated = approved is None
        if is_undated:
            undated.append(record_path.name)
        in_window = True if is_undated else (start <= approved < end)
        for entry in data.get("work_items") or []:
            if not isinstance(entry, dict):
                continue
            cost = entry.get("cost") if isinstance(entry.get("cost"), dict) else {}
            items.append(
                Item(
                    record=record_path.name,
                    sha=sha,
                    issue=entry.get("issue"),
                    title=str(entry.get("title") or "untitled"),
                    usd=_number(cost.get("usd")),
                    attempts=int(_number(cost.get("attempts"))),
                    tokens=int(_number(cost.get("tokens"))),
                    executor=entry.get("executor") or cost.get("executor"),
                    in_window=in_window,
                    undated=is_undated,
                )
            )
    return Spend(
        ledger=ledger,
        config=path,
        period=period,
        window_start=start,
        window_end=end,
        period_cap=period_cap,
        item_cap=item_cap,
        projected=float(projected),
        items=items,
        undated=undated,
        unreadable=unreadable,
    )


def _number(value) -> float:
    """A cost field as a number. Anything unreadable is zero, not a crash.

    A ledger record is written by automation but lives in a repo people can
    edit, and a junk cost field must not take the whole measurement down —
    the other items' spend still has to be summed against the cap. Zero is the
    conservative direction here only because the item is still *listed*: the
    report shows it, so a cost that reads $0.00 next to 40 attempts is visible
    rather than absorbed.

    ``.nan`` and ``.inf`` are unreadable in exactly that sense, and they reach
    here as floats rather than as text, so the check above passes them through
    untouched unless this says otherwise. NaN is the dangerous one: it poisons
    the window's sum, and ``committed >= cap`` is *false* for NaN, so a single
    ``cost.usd: .nan`` in a ledger record — which the threat model treats as
    attacker-influenceable — turned the period cap off entirely while the
    report still read OK. Both go to 0.0 rather than only NaN: a cost of
    infinity is not a measurement either, ``int(float('nan'))`` raises where
    ``attempts`` and ``tokens`` are read, and ``json.dumps`` spells both as
    tokens (``NaN``, ``Infinity``) that are not valid JSON for the caller
    reading ``--json``. Real spend is unaffected and still parks the queue; a
    poisoned field lists at $0.00, which is what the paragraph above promises
    for every other unreadable value.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            number = float(str(value))
        except (TypeError, ValueError):
            return 0.0
    else:
        number = float(value)
    return number if math.isfinite(number) else 0.0


def run(
    checkout: str,
    ledger_dir: str | None = None,
    period_cap: float | None = None,
    item_cap: float | None = None,
    period: str | None = None,
    projected: float = 0.0,
    as_of: datetime.datetime | None = None,
    as_json: bool = False,
    out=None,
) -> int:
    """Report the spend window and exit 1 when a cap parks something."""
    stream = out if out is not None else sys.stdout
    spend = measure(
        checkout,
        ledger_dir=ledger_dir,
        period_cap=period_cap,
        item_cap=item_cap,
        period=period,
        projected=projected,
        as_of=as_of,
    )
    if as_json:
        print(json.dumps(spend.as_dict(), indent=2, sort_keys=True), file=stream)
    else:
        print(spend.report(), file=stream)
    if spend.parked:
        what = "the queue" if spend.queue_parked else f"{len(spend.over_items)} item(s)"
        print(
            f"vellum: budget — {what} parked [{PARK_MARKER}]: "
            f"${spend.committed:.2f} committed against a ${spend.period_cap:.2f} "
            f"{spend.period} cap; see {spend.config}",
            file=sys.stderr,
        )
        return 1
    return 0
