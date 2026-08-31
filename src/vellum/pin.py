"""``vellum pin advance`` — moving a product repo's pin of record.

``.vellum/product.yaml`` **is** the pin (``spec/decisions/2026-08-28-pin-file.md``):
``pin.commit`` is the spec version this code implements, and CI fetches the
spec tree at it. Advancing it is the third step of a paired landing — merge the
spec PR, advance the pin, land the implementation
(``spec/features/spec-pipeline.md``) — and it is the step most worth having a
command for, because doing it by hand is how a pin ends up naming a commit that
is not a version.

Validation
----------
A sha is a real spec version if the intent repo says so, two ways, either
sufficient:

* a ledger record exists for it — automation opened one, so a version exists; or
* it is a spec-touching commit in the checkout's first-parent ancestry —
  ancestry is what defines a version, and the record may simply not have been
  written yet.

The second is not redundant with the first. Under paired landing the pin
advances to a commit whose ledger record may still be a minute away, and a
command that refused that would refuse the exact case it exists to serve. The
first is not redundant either: a shallow or stale intent checkout cannot see a
commit whose record is sitting right there in it.

Both need an intent checkout, so one is required. There is no ``--force``. A
pin that names a non-version is the failure this command exists to prevent, and
an escape hatch is how a guard stops being one — the file is three lines of
YAML and can still be edited by hand where a human genuinely means to.

Why the file is edited a line at a time
---------------------------------------
``.vellum/product.yaml`` is mostly comments, and they are load-bearing
documentation — why the submodule is gone, why ``name`` is decoration, what the
write boundaries mean. A ``yaml.safe_load`` / ``safe_dump`` round-trip would
delete every one of them, re-quote what it kept and reflow the lists, turning a
one-value change into a whole-file diff that no reviewer can read. So the value
is replaced in place and nothing else in the file is touched — which also
satisfies "preserve the file's other fields exactly" by never rewriting them.

The edit is verified rather than trusted: the result is re-parsed and compared
field by field against the original, and a mismatch anywhere outside ``pin``
raises with the file left alone. If the ``pin:`` block cannot be located
unambiguously the command refuses instead of falling back to a lossy dump — an
unreadable pin file is a thing to look at, not to rewrite.

``pin.name``
------------
Decoration, and updated with the commit rather than left behind. A ``name``
that still reads ``spec-v16`` beside a ``commit`` that is a different version
is decoration that has become a lie, and the next person to read it is exactly
the person the decoration was for. It is set from the ledger record's name when
there is one and to ``null`` when there is not — "no name yet" being the
documented shape for a commit whose tag has not been pushed. Nothing reads it
to decide anything either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from vellum.gitver import (
    TAG_RE,
    GitUnavailable,
    is_ancestor,
    is_shallow,
    prefix_of,
    repo_root,
    resolve,
    spec_commits,
)
from vellum.ledger import LedgerError, find_record, parse_version
from vellum.specfile import SpecTreeError, resolve_spec_root

#: The pin of record, relative to a product checkout.
PRODUCT_RELPATH = Path(".vellum") / "product.yaml"

#: A top-level `pin:` key: column zero, no leading space.
_PIN_BLOCK_RE = re.compile(r"^pin:\s*$")
#: `  commit: <value>` / `  name: <value>` under the pin block. Matched only at
#: the block's *own* indent — see `_rewrite`. The indent is captured so the
#: rewritten line keeps the file's own, and any trailing `# comment` is
#: captured so the rewrite keeps that too: this file's comments are the
#: documentation, and dropping one while replacing the value beside it is
#: exactly the loss the line-level edit exists to avoid.
#:
#: The comment group requires whitespace before the `#`, because a `#` with no
#: space in front of it is part of the scalar (`name: v1#2` is the five
#: characters `v1#2`), not a comment. `value` is `.*?` rather than `[^#]*?` for
#: the same reason — excluding `#` from the value made such a line match
#: nothing at all, which left `pin.name` stale and silent.
_FIELD_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?P<key>commit|name):"
    r"(?P<value>.*?)"
    r"(?P<comment>[ \t]+#.*)?$"
)

#: A blank line, or one holding only a comment. Neither sets the block's indent.
_SKIP_RE = re.compile(r"^\s*(#.*)?$")
#: Any other key at column zero ends the pin block.
_TOP_LEVEL_RE = re.compile(r"^[A-Za-z_][\w-]*:")


class PinError(Exception):
    """The pin could not be advanced."""


@dataclass
class Advance:
    """What one ``vellum pin advance`` run did."""

    path: Path
    was: str
    now: str
    name: str | None
    was_name: str | None
    #: How the sha was recognised: ``ledger-record`` or ``spec-ancestor``.
    evidence: str
    changed: bool
    #: False when the file's pin block carries no ``name:`` line. Nothing is
    #: inserted, so the report must not claim a name was written.
    has_name: bool = True

    def report(self) -> str:
        lines = [f"{self.path}", f"  pin.commit  {self.was} -> {self.now}"]
        if self.has_name:
            lines.append(f"  pin.name    {self.was_name or 'null'} -> {self.name or 'null'}")
        else:
            lines.append("  pin.name    (no name: line in the pin block; none added)")
        lines.append(f"  evidence    {self.evidence}")
        if not self.changed:
            lines.append("  (already at this pin; the file was not rewritten)")
        return "\n".join(lines)


def product_path(checkout: str | Path) -> Path:
    return Path(checkout) / PRODUCT_RELPATH


def _intent_repo(intent: str | Path) -> tuple[Path, str]:
    """The intent checkout's repo root and spec prefix."""
    try:
        spec_root = resolve_spec_root(intent)
    except SpecTreeError as exc:
        # Re-raised as the same type, for the same exit code, with the reason
        # the caller needs: it is not obvious why a *pin* command wants a spec
        # tree until you are told that only the intent repo defines a version.
        raise SpecTreeError(
            f"{intent}: not an intent checkout ({exc}). Advancing a pin means "
            f"checking the sha is a real spec version, which only the intent repo "
            f"can answer."
        ) from exc
    try:
        repo = repo_root(spec_root)
        return repo, prefix_of(repo, spec_root)
    except (GitUnavailable, ValueError) as exc:
        raise PinError(f"{intent}: cannot read the intent repo's history: {exc}") from exc


def verify_version(intent: str | Path, sha: str) -> tuple[str, str | None, str]:
    """Check *sha* names a spec version. Returns ``(full sha, name, evidence)``.

    Raises ``PinError`` when the intent checkout does not recognise it.
    """
    repo, prefix = _intent_repo(intent)
    ledger = repo / "ledger"

    try:
        full = resolve(repo, sha)
    except GitUnavailable:
        full = None

    # The ledger first: a record is automation's own statement that a version
    # exists, and it holds the decorative name the pin should carry.
    try:
        record = find_record(ledger, full or sha)
    except LedgerError as exc:
        raise PinError(str(exc)) from exc
    if record is not None:
        # `find_record` short-circuits on an exact filename hit without parsing,
        # so an unreadable `ledger/<sha>.yaml` arrives here unparsed. Letting
        # `yaml` raise out of this would be a traceback rather than one of the
        # two exit codes this CLI promises.
        try:
            data = yaml.safe_load(record.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise PinError(f"{record}: cannot read the ledger record: {exc}") from exc
        if not isinstance(data, dict):
            raise PinError(f"{record}: ledger record is not a YAML mapping")
        recorded = str(data.get("spec_version") or "").strip().lower()
        return (full or recorded), _decorative_name(data.get("name")), f"ledger record {record.name}"

    if full is None:
        raise PinError(
            f"{sha} is not a commit in {repo} and has no ledger record there. "
            f"Is the intent checkout up to date?"
        )

    # Ancestry: the definition itself. Under paired landing the record may not
    # be written yet, and the commit is a version regardless.
    if is_shallow(repo):
        raise PinError(
            f"{repo} is a shallow clone, so its first-parent history cannot say "
            f"whether {full[:12]} is a spec version, and no ledger record vouches "
            f"for it. Fetch the full history (fetch-depth: 0)."
        )
    try:
        head = resolve(repo, "HEAD")
        if not is_ancestor(repo, full, head):
            raise PinError(
                f"{full[:12]} is not an ancestor of {repo}'s HEAD and has no ledger "
                f"record. A pin names a version on the line the checkout is on."
            )
        versions = set(spec_commits(repo, head, prefix))
    except GitUnavailable as exc:
        raise PinError(f"{repo}: cannot read the spec history: {exc}") from exc
    if full not in versions:
        raise PinError(
            f"{full[:12]} is a commit but not a spec version: it does not change "
            f"{prefix or 'the spec tree'} in {repo}'s first-parent history, and no "
            f"ledger record vouches for it. Pinning it would name a version that "
            f"does not exist (spec/decisions/2026-08-28-versions-are-commits.md)."
        )
    return full, None, "spec-touching commit in the intent checkout's ancestry"


def _decorative_name(value) -> str | None:
    """A ledger record's ``name``, if it is one this will write into the pin.

    Validated against `spec-v<N>` rather than trusted, because it is written
    into a YAML file by string interpolation and it arrives from the intent
    repo's `ledger/`, which anyone who can land a merge there can write. A name
    is decoration and nothing reads it to decide anything, so a malformed one is
    dropped rather than raised on — the pin still advances, it just carries no
    name, which is the documented shape for a version whose tag has not been
    pushed.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text if TAG_RE.match(text) else None


def _rewrite(text: str, commit: str, name: str | None) -> str:
    """Replace ``pin.commit`` and ``pin.name`` in *text*, touching nothing else."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if _PIN_BLOCK_RE.match(line)]
    if len(starts) != 1:
        raise PinError(
            f"expected exactly one top-level `pin:` block, found {len(starts)}; "
            f"refusing to guess which one is the pin of record"
        )
    start = starts[0]

    # Only the block's own keys are rewritten, never anything nested inside
    # them. YAML requires a block scalar's body to be indented deeper than its
    # key, so an indent test is what separates `pin.name` from the word `name:`
    # appearing inside a `note: |` two lines below it. Without this the scan
    # rewrote a line in the middle of someone's prose and every check below
    # passed: `drifted` skips `pin` entirely, and comparing key sets cannot see
    # a changed value.
    block_indent: str | None = None
    seen: dict[str, int] = {}
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if _TOP_LEVEL_RE.match(line):
            break
        if _SKIP_RE.match(line):
            continue
        indent = line[: len(line) - len(line.lstrip())]
        if block_indent is None:
            block_indent = indent
        if indent != block_indent:
            continue
        field = _FIELD_RE.match(line)
        if not field:
            continue
        key = field.group("key")
        if key in seen:
            raise PinError(f"`pin.{key}` appears twice; refusing to guess which is the pin")
        seen[key] = i
        value = commit if key == "commit" else (name or "null")
        # A value spanning lines would write a second key into the block, and
        # neither check in `advance()` would notice: the key set grows by one
        # the comparison does not look for. `name` reaches here from a ledger
        # record, which anyone who can land a merge on the intent repo writes,
        # so this is a boundary and not a formality.
        if "\n" in value or "\r" in value:
            raise PinError(f"refusing to write a `pin.{key}` that spans lines: {value!r}")
        lines[i] = f"{field.group('indent')}{key}: {value}{field.group('comment') or ''}"

    if "commit" not in seen:
        raise PinError("no `pin.commit` under the `pin:` block; this is not a pin file")
    return "\n".join(lines)


def advance(product_checkout: str | Path, to: str, intent: str | Path) -> Advance:
    """Move *product_checkout*'s pin to *to*, having checked it is a version."""
    path = product_path(product_checkout)
    # Absent, unreadable, or not YAML: all "there is no pin file here", which
    # is the same answer as "this is not a pin file" below and takes the same
    # code. A caller should not have to tell those apart by reading the text.
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecTreeError(f"{path}: cannot read the pin of record: {exc}") from exc
    try:
        before = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecTreeError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(before, dict) or not isinstance(before.get("pin"), dict):
        raise SpecTreeError(f"{path}: no `pin:` mapping; this is not a pin file")
    if "commit" not in before["pin"]:
        raise SpecTreeError(f"{path}: no `pin.commit`; this is not a pin file")

    # Unwrapped: `LedgerError` already means "what you typed is not a spec
    # version", and it already exits 2. Wrapping it would report a bad command
    # line as a bad repository.
    parse_version(to)

    full, name, evidence = verify_version(intent, to)
    was = str(before["pin"]["commit"])
    was_name = before["pin"].get("name")
    was_name = str(was_name) if was_name else None

    updated = _rewrite(text, full, name)

    # The edit re-read rather than trusted. A line-level rewrite is the right
    # tool for a commented file and the wrong one to take on faith: this is
    # what makes "preserve the file's other fields exactly" a checked property.
    try:
        after = yaml.safe_load(updated)
    except yaml.YAMLError as exc:
        raise PinError(f"{path}: the rewrite did not parse ({exc}); the file is unchanged") from exc
    if not isinstance(after, dict):
        raise PinError(f"{path}: the rewrite did not parse as a mapping; the file is unchanged")
    drifted = sorted(
        k for k in set(before) | set(after) if k != "pin" and before.get(k) != after.get(k)
    )
    if drifted:
        raise PinError(
            f"{path}: rewriting the pin would have changed {drifted}; the file is unchanged"
        )
    if after.get("pin", {}).get("commit") != full:
        raise PinError(f"{path}: the rewrite did not take; the file is unchanged")
    after_pin = after.get("pin") or {}
    if set(before["pin"]) != set(after_pin):
        raise PinError(f"{path}: the rewrite changed the pin's keys; the file is unchanged")
    # Values too, not only keys. `commit` and `name` are what this command is
    # for; anything else under `pin:` must come back identical, and comparing
    # key sets alone let a rewritten line inside a nested block scalar through.
    pin_drift = sorted(
        k for k in before["pin"]
        if k not in ("commit", "name") and before["pin"][k] != after_pin.get(k)
    )
    if pin_drift:
        raise PinError(
            f"{path}: rewriting the pin would have changed pin.{pin_drift}; "
            f"the file is unchanged"
        )

    changed = updated != text
    if changed:
        path.write_text(updated, encoding="utf-8")
    return Advance(
        path=path,
        was=was,
        now=full,
        name=name,
        was_name=was_name,
        evidence=evidence,
        changed=changed,
        has_name="name" in before["pin"],
    )
