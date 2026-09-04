"""``src/vellum/seeds/CHANGES.yaml`` — the installation-shape changelog, read.

``spec/features/installation.md``: "``--plan`` prints the files it would rewrite
and the installation-shape changes the release range carries (configuration keys
added, always with a default; files added; files retired), and creates nothing."

The file is *data* shipped with the CLI rather than prose in a release note, for
the same reason the manifest is data rather than a heuristic: something has to
be able to answer "what does crossing v0.2.0 → v0.4.0 do to my installation"
mechanically, and a human-written changelog answers it only for a human who
reads all of it. ``vellum.seeds`` reads this install's copy; ``vellum.upgrade``
reads a *release's* copy with ``git show <ref>:src/vellum/seeds/CHANGES.yaml``,
so a plan built from a ``--from`` checkout describes the releases as those
releases described themselves.

A default is not optional
-------------------------
``config_keys_added`` entries carry ``default:`` and :func:`load` refuses one
that does not. The decision is explicit — configuration keys added, "always with
a default; never required without one" — and the reason is what an upgrade *is*:
a pull request that rewrites files and is then merged, with nobody in the loop
to answer a question. A release that adds a key it cannot default has added a
prompt to a command that has no prompt, and the installation that merges the
upgrade finds out when a gate exits 2. Refusing here makes that a failing test
in this repository, at the moment the entry is written, instead.

Ordering, and the two ends of a range
-------------------------------------
Entries order as version tuples (``vellum.install.RELEASE_RE``), never lexically
— ``v0.10.0`` is newer than ``v0.9.0``, the hazard ``install.releases`` already
names. A manifest may legitimately name something that is not a release tag: an
installation stamped ``--ref main`` before any release was cut is a real one, and
refusing to plan its upgrade would strand it. So an unorderable *lower* bound
opens the range at the bottom and says so; an unorderable *upper* bound closes
it to nothing and says so, because "everything up to a release I cannot place"
is not a range anybody can review.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field

import yaml

from vellum.install import RELEASE_RE
from vellum.text import one_line

#: The top-level keys. ``template`` is the shape a release copies when it is cut
#: and is deliberately NOT a release: nothing reads it into a range and
#: ``--plan`` never prints it, so the file can carry a worked example without
#: that example becoming a release nobody tagged.
SCHEMA_KEY, RELEASES_KEY, TEMPLATE_KEY = "schema", "releases", "template"

#: The schema this reader understands. A file declaring another one is refused
#: rather than read optimistically: this is the input to a command that writes.
SCHEMA = 1

#: The four sections, in the order ``--plan`` prints them, and the heading each
#: gets. Ordered by how much of an installation they touch: the config is a file
#: every gate reads, files come and go, and the stub inputs are the forge's half.
SECTIONS = (
    ("config_keys_added", "configuration keys added"),
    ("files_added", "files added"),
    ("files_retired", "files retired (reported; upgrade deletes nothing)"),
    ("stub_inputs", "caller stub inputs"),
)

#: How far a row is flattened before it is wrapped. ``one_line`` exists to stop
#: an untrusted value opening a line of its own in a report, and its default
#: truncation is right for a scenario id somebody wrote in a spec file. This
#: file is not that: it ships with the CLI, and a plan that truncated the
#: sentence explaining a configuration key would be worse than no plan. So the
#: newlines are collapsed at a limit nothing real reaches, and :func:`_wrap`
#: gives the text back its lines at a width a terminal can hold.
FLAT = 4000
WIDTH = 78


def _wrap(text: str, *, indent: str, bullet: str = "") -> list[str]:
    """One row, wrapped to :data:`WIDTH` with its continuations hanging."""
    return textwrap.wrap(
        text, width=WIDTH, initial_indent=indent + bullet,
        subsequent_indent=indent + " " * len(bullet),
    ) or [indent + bullet]


#: Keys a ``config_keys_added`` entry must carry. ``default`` is the load-bearing
#: one — see the module docstring — and ``key`` is what it is the default for.
CONFIG_KEY_REQUIRED = ("key", "default")


class ChangesError(Exception):
    """The changelog could not be read, or an entry is not a legal one."""


@dataclass(frozen=True)
class Entry:
    """One release's installation-shape changes."""

    release: str
    summary: str = ""
    sections: dict[str, list] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def version(self) -> tuple[int, ...] | None:
        return version_of(self.release)

    @property
    def empty(self) -> bool:
        """True when this release changed nothing about an installation's shape."""
        return not any(self.sections.get(name) for name, _ in SECTIONS) and not self.notes

    def render(self) -> list[str]:
        lines = _wrap(
            self.release + (f" — {self.summary}" if self.summary else ""), indent="  "
        )
        for name, heading in SECTIONS:
            rows = self.sections.get(name) or []
            if not rows:
                continue
            lines.append(f"    {heading}:")
            for row in rows:
                lines += _wrap(row, indent="      ", bullet="- ")
        for note in self.notes:
            lines += _wrap(note, indent="    ", bullet="note: ")
        if self.empty:
            lines.append("    nothing about an installation's shape changed.")
        return lines


def version_of(release: str) -> tuple[int, ...] | None:
    """*release* as a version tuple, or None when it is not a release tag."""
    match = RELEASE_RE.match(str(release))
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _text(value) -> str:
    """One row of a section, as the single line a plan prints.

    A row may be a string or a mapping — ``files_added`` carries ``path`` and
    ``what``, ``config_keys_added`` carries ``key``, ``default`` and often
    ``read_by`` — and both render here rather than at the call site, so the plan,
    the run's report and a caller reading this as a library agree.
    """
    if isinstance(value, dict):
        if "key" in value:
            rendered = f"{value['key']} (default: {value.get('default')!r})"
            if value.get("read_by"):
                rendered += f", read by {value['read_by']}"
            if value.get("what"):
                rendered += f" — {value['what']}"
            return one_line(rendered, FLAT)
        if "path" in value:
            what = value.get("what")
            return one_line(f"{value['path']}" + (f" — {what}" if what else ""), FLAT)
        return one_line(str(value), FLAT)
    return one_line(str(value), FLAT)


def _entry(raw, where: str) -> Entry:
    if not isinstance(raw, dict):
        raise ChangesError(f"{where}: is not a mapping, so it names no release")
    release = raw.get("release")
    if not isinstance(release, str) or not release.strip():
        raise ChangesError(
            f"{where}: declares no `release:`. Every entry is one release's "
            f"changes, and a range is selected by that name."
        )
    sections: dict[str, list] = {}
    for name, _ in SECTIONS:
        rows = raw.get(name) or []
        if not isinstance(rows, list):
            raise ChangesError(
                f"{where} ({release}): `{name}:` is "
                f"{one_line(str(rows))!r}, which is not a list."
            )
        if name == "config_keys_added":
            for row in rows:
                if not isinstance(row, dict):
                    raise ChangesError(
                        f"{where} ({release}): `{name}:` carries "
                        f"{one_line(str(row))!r}, which is not a mapping. A key "
                        f"added by a release is named with its default."
                    )
                missing = [k for k in CONFIG_KEY_REQUIRED if k not in row]
                if missing:
                    raise ChangesError(
                        f"{where} ({release}): the configuration key "
                        f"{one_line(str(row.get('key', row)))!r} declares no "
                        f"`{'`, `'.join(missing)}`. A release never adds a "
                        f"required key without a default "
                        f"(spec/decisions/2026-09-04-vellum-owned-files-and-"
                        f"upgrades.md): an upgrade is a pull request, not a "
                        f"conversation, and nobody is there to be asked."
                    )
        sections[name] = [_text(row) for row in rows]
    notes = raw.get("notes") or []
    if not isinstance(notes, list):
        raise ChangesError(f"{where} ({release}): `notes:` is not a list.")
    return Entry(
        release=release,
        summary=str(raw.get("summary") or "").strip(),
        sections=sections,
        notes=tuple(one_line(str(note), FLAT) for note in notes),
    )


@dataclass(frozen=True)
class Changes:
    """One installation-shape changelog."""

    entries: tuple[Entry, ...]
    template: Entry | None = None

    def by_release(self, release: str) -> Entry | None:
        return next((e for e in self.entries if e.release == release), None)

    def between(self, after: str, to: str) -> tuple[tuple[Entry, ...], str | None]:
        """Entries in ``(after, to]``, and a line about the range when it is odd.

        Returns the entries and, when either bound could not be placed, the
        sentence a plan prints instead of pretending it was placed.
        """
        upper = version_of(to)
        if upper is None:
            return (), (
                f"{one_line(to)!r} is not a release tag, so this cannot place it "
                f"among the ones the changelog knows and prints no shape changes. "
                f"The upgrade itself does not depend on this: the files are "
                f"rewritten from that ref's templates either way."
            )
        lower = version_of(after)
        note = None
        if lower is None:
            note = (
                f"the manifest names {one_line(after)!r}, which is not a release "
                f"tag, so the range has no lower bound this can place: every "
                f"entry up to and including {to} is printed."
            )
        found = tuple(
            entry for entry in self.entries
            if entry.version is not None
            and entry.version <= upper
            and (lower is None or entry.version > lower)
        )
        if not found and note is None:
            note = (
                f"the changelog records no shape entry in ({after}, {to}]. Either "
                f"those releases changed nothing about an installation's shape, "
                f"or the entries were never written — a release is cut with its "
                f"entry, and this file is where it goes."
            )
        return found, note


def parse(text: str) -> Changes:
    """Read a changelog. Raises :class:`ChangesError` on anything malformed."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ChangesError(f"is not valid YAML: {one_line(str(exc))}") from exc
    if not isinstance(data, dict):
        raise ChangesError("is not a YAML mapping, so it declares no releases")
    schema = data.get(SCHEMA_KEY)
    if schema != SCHEMA:
        raise ChangesError(
            f"declares `{SCHEMA_KEY}: {one_line(str(schema))}`; this CLI reads "
            f"schema {SCHEMA}. A shape changelog is the input to a command that "
            f"writes, so a shape it does not understand is refused rather than "
            f"read for whatever happens to parse."
        )
    raw = data.get(RELEASES_KEY)
    if not isinstance(raw, list):
        raise ChangesError(
            f"declares `{RELEASES_KEY}:` as {one_line(str(raw))!r}, which is not "
            f"a list of entries."
        )
    entries = [_entry(item, f"{RELEASES_KEY}[{n}]") for n, item in enumerate(raw)]
    # Sorted as VERSION TUPLES, never lexically: `v0.10.0` is newer than
    # `v0.9.0`, and a plan that printed them the other way round would describe
    # the range backwards. An entry whose release is not a release tag sorts
    # last and is never selected into a range (`between` places by version).
    entries.sort(key=lambda e: (e.version is None, e.version or (), e.release))
    template = data.get(TEMPLATE_KEY)
    return Changes(
        entries=tuple(entries),
        template=_entry(template, TEMPLATE_KEY) if template is not None else None,
    )


def load() -> Changes:
    """This install's changelog, out of its own package data."""
    from vellum import seeds

    try:
        return parse(seeds.changes_text())
    except ChangesError as exc:
        raise ChangesError(f"{seeds.source_path(seeds.CHANGES)}: {exc}") from exc


def render(entries, note: str | None, *, after: str, to: str) -> list[str]:
    """The plan's shape-changes section: the heading, the entries, the caveat."""
    lines = [f"Installation-shape changes in ({after}, {to}]"]
    if note:
        lines += _wrap(note, indent="  ")
    for entry in entries:
        lines += entry.render()
    return lines
