"""``vellum verify deps`` — the dependency-registry guard.

``spec/behaviors/security.md``: "A new or changed dependency is always a
verifier red-flag item, checked against the product's dependency policy
(allowlist, registry pinning, lockfile rules) in config."
``@id:unlisted-registry-fails`` states the failing shape: a PR adding a
dependency from a registry not in the policy fails with a supply-chain finding.

The policy is ``dependency_policy.registries`` in the installation's
``.vellum/config.yaml``, which lives in the *intent* repo
(``vellum.config``) — it is installation policy, not product code. So this
command reads two checkouts: the product, for its manifests, and the intent
repo, for the allowlist. The intent checkout is named the same way ``vellum pin
advance`` names it (``--intent`` or ``$VELLUM_INTENT_REPO``), because an
installation should have one answer to "where is the intent repo checked out".

What "resolves to a registry" means
-----------------------------------
Every requirement this reads is attributed to exactly one host, and a
requirement with no host is attributed to the default index:

* ``sandbox-widget @ https://packages.example.invalid/x.tar.gz`` -> that host;
* ``pkg @ git+https://github.com/o/r`` -> ``github.com``;
* ``PyYAML>=6.0,<7`` -> whatever index is in force, which is ``pypi.org`` unless
  a requirements file changed it with ``--index-url``;
* ``--extra-index-url``/``--find-links`` -> that host, counted as in use because
  every plain requirement in the file may be served from it;
* ``-e .``, a bare path, a ``file:`` URL -> no registry, and no finding.

Hosts are compared exactly, after ``urlsplit`` has extracted them. Neither
``pypi.org.evil.invalid`` nor ``https://pypi.org@evil.invalid/simple`` is
``pypi.org``, and both of those are the shapes an allowlist gets past with a
substring test.

Reading pyproject.toml below Python 3.11
----------------------------------------
``tomllib`` arrived in 3.11 and this package's floor is 3.10, and the one thing
this guard must never do is quietly find fewer dependencies than the file
declares. ``_scan_toml_arrays`` is therefore not a TOML parser with gaps: it
reads the arrays it understands and **raises** the moment it meets anything
inside the tables it cares about that it does not, so the failure is "no answer"
(exit 2) rather than a shorter answer that reads like a pass. On 3.11 and up the
real parser is used and the fallback is exercised against it in the tests.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from vellum.config import ConfigError, config_path, load as load_config

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 only
    tomllib = None  # type: ignore[assignment]

#: The index a plain requirement resolves to when nothing overrides it.
DEFAULT_INDEX = "pypi.org"

#: Manifests read when the caller names none, in the order they are looked for.
#: Globs, because an installation may split its requirements by purpose and a
#: guard that read only ``requirements.txt`` would miss ``requirements-dev.txt``
#: — a dev dependency is executed on a machine holding credentials just as a
#: runtime one is.
DEFAULT_MANIFESTS = ("pyproject.toml", "requirements*.txt")

#: ``name @ url`` (PEP 508 direct reference).
_DIRECT_RE = re.compile(r"^\s*[A-Za-z0-9._-]+\s*(\[[^\]]*\])?\s*@\s*(?P<url>\S+)")
#: A requirement that IS a URL, with no ``name @`` in front of it.
_BARE_URL_RE = re.compile(r"^\s*(?P<url>[A-Za-z][A-Za-z0-9+.-]*://\S+)")
#: pip options in a requirements file that name where packages come from.
_INDEX_OPTS = {"--index-url": "index", "-i": "index",
               "--extra-index-url": "extra", "--find-links": "extra", "-f": "extra"}


class DependencyError(Exception):
    """The dependency policy could not be checked."""


@dataclass
class Requirement:
    """One declared dependency and the host it resolves to."""

    source: str
    text: str
    #: None when the requirement names no registry at all (a local path).
    registry: str | None
    #: How the registry was decided, for the report.
    why: str


@dataclass
class Policy:
    """One dependency-policy check."""

    product: Path
    config: Path
    allowed: list[str]
    manifests: list[str]
    requirements: list[Requirement]
    offending: list[Requirement] = field(default_factory=list)

    @property
    def violated(self) -> bool:
        return bool(self.offending)

    def report(self) -> str:
        lines = [
            f"Dependency policy for {self.product}",
            f"  allowed registries: {', '.join(self.allowed)}   ({self.config})",
            f"  manifests read: {', '.join(self.manifests) or '(none found)'}",
            "",
            f"{len(self.requirements)} requirement(s):",
        ]
        for req in self.requirements:
            mark = "UNLISTED" if req in self.offending else "  ok    "
            lines.append(f"  {mark}  {req.registry or '(no registry)':<28}  {req.text}")
            lines.append(f"            {req.source}: {req.why}")
        if not self.requirements:
            lines.append("  (nothing declared)")
        lines.append("")
        if self.violated:
            hosts = sorted({r.registry for r in self.offending if r.registry})
            lines.append(
                f"BLOCKED: {len(self.offending)} requirement(s) resolve to "
                f"{', '.join(hosts)}, which dependency_policy.registries does not "
                f"list. A dependency from an unlisted registry is a supply-chain "
                f"finding (spec/behaviors/security.md)."
            )
        else:
            lines.append(
                "OK: every requirement resolves to a listed registry, or to no "
                "registry at all."
            )
        return "\n".join(lines)


# ------------------------------------------------------------------ hosts

def host_of(url: str) -> str | None:
    """The host a URL names, lowercased, or None when it names none.

    ``urlsplit().hostname`` is what does the work, and it is used rather than a
    regex on purpose: it strips userinfo and port, so
    ``https://pypi.org@evil.invalid/simple`` reports ``evil.invalid`` — the
    reading a credential-shaped prefix is written to defeat.
    """
    text = url.strip()
    # A VCS requirement is `git+https://host/...`; the transport after the `+`
    # is the part that carries the host.
    if "+" in text.split("://", 1)[0]:
        text = text.split("+", 1)[1]
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme in ("", "file"):
        return None
    try:
        host = parts.hostname
    except ValueError:  # a malformed authority, e.g. a bad IPv6 literal
        return None
    return host.lower() if host else None


def _allowed_hosts(values, path: Path) -> list[str]:
    """``dependency_policy.registries``, as bare lowercased hosts.

    Entries may be written as hosts (``pypi.org``) or as index URLs
    (``https://pypi.org/simple``); both are the same policy and are normalised
    to the same host. An entry naming no host at all is refused rather than
    dropped: silently discarding one shortens the allowlist, and a shorter
    allowlist fails *closed*, which would send a reviewer hunting a supply-chain
    finding that is really a typo in policy.
    """
    hosts = []
    for entry in values:
        if not isinstance(entry, str) or not entry.strip():
            raise DependencyError(
                f"{path}: dependency_policy.registries contains {entry!r}; "
                f"every entry must be a host or an index URL"
            )
        text = entry.strip()
        host = host_of(text) if "://" in text else text.split("/", 1)[0].lower()
        if not host:
            raise DependencyError(
                f"{path}: dependency_policy.registries entry {entry!r} names no host"
            )
        hosts.append(host)
    return hosts


def registries(intent: str | Path) -> list[str]:
    """The installation's allowed registries.

    Missing is an error rather than "everything is allowed", the same call
    ``config.divergence_cap`` makes about its own key: a policy that disappears
    when its key is misspelled is not a policy.
    """
    path = config_path(intent)
    try:
        data = load_config(intent)
    except ConfigError as exc:
        raise DependencyError(str(exc)) from exc
    policy = data.get("dependency_policy")
    if not isinstance(policy, dict) or "registries" not in policy:
        raise DependencyError(
            f"{path}: no dependency_policy.registries. There is no allowlist to "
            f"check dependencies against; declare one "
            f"(spec/behaviors/security.md)."
        )
    values = policy["registries"]
    if not isinstance(values, list) or not values:
        raise DependencyError(
            f"{path}: dependency_policy.registries is {values!r}; expected a "
            f"non-empty list of registries"
        )
    return _allowed_hosts(values, path)


# ------------------------------------------------------- reading manifests

def _classify(
    text: str, source: str, index: str, extras: list[str], index_why: str | None = None
) -> Requirement | None:
    """One requirement line, attributed to the host it resolves to."""
    stripped = text.strip()
    if not stripped:
        return None
    direct = _DIRECT_RE.match(stripped) or _BARE_URL_RE.match(stripped)
    if direct:
        url = direct.group("url")
        host = host_of(url)
        if host is None:
            return Requirement(source, stripped, None, "a local path or file: URL")
        return Requirement(source, stripped, host, f"direct reference to {url}")
    if stripped.startswith(".") or stripped.startswith("/"):
        return Requirement(source, stripped, None, "a local path")
    why = index_why or f"the index in force for {source}"
    if extras:
        why += f" (extra indexes also declared: {', '.join(extras)})"
    return Requirement(source, stripped, index, why)


def _contained(root: Path, target: Path, *, source: str) -> Path:
    """*target*, refused unless it lies inside *root*.

    A requirements file may include another with ``-r``, and the path it gives
    is text from the repository. Following ``-r ../../../etc/shadow`` would make
    a guard a file-read primitive pointed by whoever writes the manifest, so the
    resolved path is required to stay inside the checkout.
    """
    resolved = (root / target).resolve() if not target.is_absolute() else target.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise DependencyError(
            f"{source}: refuses to follow {target} — it leaves the checkout"
        ) from None
    return resolved


def read_requirements_txt(
    root: Path, path: Path, seen: set[Path] | None = None
) -> list[Requirement]:
    """Requirements declared by a pip requirements file, following ``-r``."""
    seen = seen if seen is not None else set()
    resolved = path.resolve()
    if resolved in seen:
        return []
    seen.add(resolved)
    source = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DependencyError(f"{source}: cannot read the manifest: {exc}") from exc

    index, extras = DEFAULT_INDEX, []
    lines, buffer = [], ""
    for raw in text.splitlines():
        # A backslash continues a requirement onto the next line.
        line = buffer + raw.rstrip()
        if line.endswith("\\"):
            buffer = line[:-1]
            continue
        buffer = ""
        # `#` starts a comment only at the start of a line or after whitespace;
        # inside a URL fragment (`#egg=name`) it is part of the requirement.
        lines.append(re.sub(r"(?:^|\s)#.*$", "", line).strip())

    found: list[Requirement] = []
    for line in lines:
        if not line:
            continue
        if line.startswith("-"):
            option, _, value = line.partition(" ")
            option, value = option.strip(), value.strip().strip("=").strip()
            if "=" in option and not value:
                option, _, value = line.partition("=")
                option, value = option.strip(), value.strip()
            if option in ("-r", "--requirement"):
                nested = _contained(root, Path(value), source=source)
                found += read_requirements_txt(root, nested, seen)
                continue
            kind = _INDEX_OPTS.get(option)
            if kind is None:
                # `-e .`, `--no-binary`, `--hash`: nothing about a registry.
                if option in ("-e", "--editable"):
                    req = _classify(value, source, index, extras)
                    if req is not None:
                        found.append(req)
                continue
            host = host_of(value)
            if host is None:
                continue
            if kind == "index":
                index = host
            else:
                extras.append(host)
            found.append(
                Requirement(source, line, host, f"{option} names this registry")
            )
            continue
        req = _classify(line, source, index, extras)
        if req is not None:
            found.append(req)
    # An `--extra-index-url` declared after a requirement still serves it, so the
    # attribution is redone once the whole file's options are known.
    return [
        r if r.text.startswith("-") else _classify(r.text, r.source, index, extras)
        for r in found
    ]


_TABLE_RE = re.compile(r"^\s*\[\s*(?P<name>[^\]]+?)\s*\]\s*(#.*)?$")
_ARRAY_RE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_.\"'-]+)\s*=\s*\[(?P<rest>.*)$")
_STRING_RE = re.compile(r"""^\s*(?P<q>"|')(?P<value>(?:[^\\]|\\.)*?)(?P=q)\s*(?P<tail>,?)\s*""")

#: Tables whose string arrays are dependency declarations.
_DEP_TABLES = {
    "project": ("dependencies",),
    "project.optional-dependencies": None,   # every key is a dependency list
    "dependency-groups": None,               # PEP 735
    "build-system": ("requires",),
}


def _scan_toml_arrays(text: str, source: str) -> dict[str, list[str]]:
    """Arrays of strings in the dependency tables, without a TOML parser.

    The fallback for Python 3.10, where ``tomllib`` does not exist. It is
    strict by construction: inside a table it cares about, a key whose value it
    cannot read *exactly* raises rather than being skipped, because the only
    dangerous outcome for this guard is a manifest that appears to declare fewer
    dependencies than it does. Outside those tables nothing is read at all.
    """
    out: dict[str, list[str]] = {}
    table = ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        header = _TABLE_RE.match(line)
        if header:
            table = header.group("name").strip()
            i += 1
            continue
        if table not in _DEP_TABLES:
            i += 1
            continue
        wanted = _DEP_TABLES[table]
        match = _ARRAY_RE.match(line)
        if match is None:
            i += 1
            continue
        key = match.group("key").strip().strip("\"'")
        if wanted is not None and key not in wanted:
            i += 1
            continue
        values, rest, depth = [], match.group("rest"), 1
        while True:
            rest = rest.strip()
            if rest.startswith("#") or rest == "":
                i += 1
                if i >= len(lines):
                    raise DependencyError(
                        f"{source}: [{table}] {key} is an array that never closes; "
                        f"this reader will not guess at how much of it it missed"
                    )
                rest = lines[i]
                continue
            if rest.startswith("]"):
                depth -= 1
                break
            item = _STRING_RE.match(rest)
            if item is None:
                raise DependencyError(
                    f"{source}: [{table}] {key} contains {rest.split(',')[0]!r}, "
                    f"which this reader cannot read as a string. Run this guard on "
                    f"Python 3.11 or later, where the real TOML parser is used."
                )
            values.append(item.group("value").encode().decode("unicode_escape"))
            rest = rest[item.end():]
        out[f"{table}.{key}"] = values
        i += 1
    return out


def read_pyproject(root: Path, path: Path) -> list[Requirement]:
    """Requirements declared by a ``pyproject.toml``."""
    source = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DependencyError(f"{source}: cannot read the manifest: {exc}") from exc
    if tomllib is not None:
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise DependencyError(f"{source}: not valid TOML: {exc}") from exc
        arrays = _from_parsed(data)
    else:  # pragma: no cover - 3.10 only
        arrays = _scan_toml_arrays(raw.decode("utf-8", "replace"), source)
    found: list[Requirement] = []
    for where, values in sorted(arrays.items()):
        for value in values:
            req = _classify(
                value, f"{source} [{where}]", DEFAULT_INDEX, [],
                index_why="the default index; pyproject.toml names no index",
            )
            if req is not None:
                found.append(req)
    return found


def _from_parsed(data: dict) -> dict[str, list[str]]:
    """The same arrays ``_scan_toml_arrays`` finds, out of a parsed document."""
    out: dict[str, list[str]] = {}
    for table, wanted in _DEP_TABLES.items():
        node = data
        for part in table.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if wanted is not None and key not in wanted:
                continue
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                out[f"{table}.{key}"] = list(value)
    return out


def manifests(product: Path, patterns: tuple[str, ...] | list[str] | None = None) -> list[Path]:
    """The manifest files to read, in a stable order."""
    found: list[Path] = []
    for pattern in patterns or DEFAULT_MANIFESTS:
        found += sorted(p for p in product.glob(pattern) if p.is_file())
    # Deduplicated because an explicit `--manifest` may name what a default glob
    # already matched, and a requirement counted twice reads as two findings.
    seen, ordered = set(), []
    for path in found:
        if path.resolve() not in seen:
            seen.add(path.resolve())
            ordered.append(path)
    return ordered


def check(
    product_checkout: str | Path,
    intent: str | Path,
    patterns: tuple[str, ...] | list[str] | None = None,
) -> Policy:
    """Every declared dependency, and which of them the policy does not admit."""
    product = Path(product_checkout)
    if not product.is_dir():
        raise DependencyError(f"{product}: not a directory; is this a product checkout?")
    allowed = registries(intent)
    paths = manifests(product, patterns)
    requirements: list[Requirement] = []
    for path in paths:
        if path.name == "pyproject.toml":
            requirements += read_pyproject(product, path)
        else:
            requirements += read_requirements_txt(product, path, set())
    offending = [
        r for r in requirements if r.registry is not None and r.registry not in allowed
    ]
    return Policy(
        product=product,
        config=config_path(intent),
        allowed=allowed,
        manifests=[str(p.relative_to(product)) for p in paths],
        requirements=requirements,
        offending=offending,
    )


def run(
    product_checkout: str,
    intent: str,
    patterns: tuple[str, ...] | list[str] | None = None,
    out=None,
) -> int:
    """Report the policy check and exit 1 on an unlisted registry."""
    stream = out if out is not None else sys.stdout
    result = check(product_checkout, intent, patterns)
    print(result.report(), file=stream)
    if result.violated:
        first = result.offending[0]
        print(
            f"vellum: supply chain — {first.text} resolves to {first.registry}, "
            f"which {result.config} does not list "
            f"({len(result.offending)} finding(s) in total)",
            file=sys.stderr,
        )
        return 1
    return 0
