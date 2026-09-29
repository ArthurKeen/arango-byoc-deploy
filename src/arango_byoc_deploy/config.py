"""Per-application configuration — the half of a BYOC deploy that differs per repo.

Seven estate repos hand-copied the same deploy script, each recording in its
docstring that "the platform-shaped half is identical for every project. What
differs here: the defaults, the pre-flight checks and the verifier." This
module is that "what differs", made declarative so the platform half can live
in one place.

Configuration is read from, in order:

1. ``arango-byoc.toml`` at the repository root, or
2. the ``[tool.arango-byoc]`` table of ``pyproject.toml``.

A standalone file exists because several consumers are not Python-packaged and
have no ``pyproject.toml``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STANDALONE_FILE = "arango-byoc.toml"
PYPROJECT_TABLE = "arango-byoc"

#: py13base is the house standard but does not exist on every cluster —
#: prod.demo.pilot.arango.ai offers node22base, py12base, py12cugraph,
#: py12torch and test. py12base is therefore the portable default.
DEFAULT_BASE_IMAGE = "py12base"


class ConfigError(ValueError):
    """The repository's BYOC configuration is missing or malformed."""


@dataclass(frozen=True)
class Probe:
    """One URL the verifier must see succeed after a deploy.

    ``path`` is relative to the mount root. ``expect_json_key`` names one key,
    or several checked against the same response (one request, not one per
    key — some probes are slow graph queries). A key is dotted (``info.version``)
    and ``[]`` flattens a list (``windows[].id`` is every window's id).

    For each key: it must be present; ``min_items`` requires a list or dict to
    have at least that many entries, or a number to be at least that value;
    ``must_contain`` requires a list value to include that element. These are
    the checks that catch "healthy service, wrong or empty database".
    """

    path: str
    label: str = ""
    expect_json_key: str | tuple[str, ...] | None = None
    min_items: int | None = None
    timeout: float = 60.0
    must_contain: str | None = None

    @property
    def keys(self) -> tuple[str, ...]:
        if self.expect_json_key is None:
            return ()
        if isinstance(self.expect_json_key, str):
            return (self.expect_json_key,)
        return tuple(self.expect_json_key)


@dataclass(frozen=True)
class EnvRule:
    """An app-specific check on one key of the baked ``.env``.

    ``forbid`` rejects listed values (case-insensitive). ``min_length`` and
    ``reject_prefixes`` require a real secret rather than a placeholder.
    ``unless`` skips the rule when that key is truthy (``true``/``1``/``yes``).
    """

    key: str
    forbid: tuple[str, ...] = ()
    min_length: int | None = None
    reject_prefixes: tuple[str, ...] = ()
    unless: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class VersionSource:
    """Where the release number is read from.

    A JSON file and key (``package.json`` / ``version``), or any text file and
    a regex whose first group is the version (``__version__ = "(.+)"``).
    """

    file: str
    json_key: str | None = None
    regex: str | None = None


@dataclass(frozen=True)
class VersionProbe:
    """Where the *live* service reports its version: a path plus a dotted
    JSON key (``/openapi.json`` + ``info.version``, ``/healthz`` + ``version``)."""

    path: str
    json_key: str = "version"


@dataclass(frozen=True)
class AppConfig:
    """Everything one repository needs to tell the shared deployer."""

    app_name: str
    instance: str
    display_name: str = ""
    description: str = ""
    base_image: str = DEFAULT_BASE_IMAGE
    has_ui: bool = True
    #: Database to mount under. ``None`` defers to ``ARANGO_DB`` in ``.env``;
    #: the empty string forces a ``_global`` mount.
    database: str | None = None
    #: Path to the bundle, or a glob whose newest match is used.
    tarball: str = ""
    #: Archive members that must exist, relative to the archive root.
    required_members: tuple[str, ...] = ("entrypoint",)
    #: The SPA's HTML entry, checked for root-absolute asset URLs.
    index_html: str | None = None
    #: The env var carrying the baked mount prefix, e.g. ``ROOT_PATH`` or
    #: ``SERVICE_URL_PATH_PREFIX``. ``None`` disables the prefix check.
    prefix_env_var: str | None = None
    #: Whether the bundle must carry a baked ``.env``. Some services take
    #: credentials interactively and need none.
    require_baked_env: bool = False
    #: Keys that must be present in a baked ``.env`` when one is required.
    required_env_keys: tuple[str, ...] = ()
    probes: tuple[Probe, ...] = field(default_factory=tuple)
    #: Path polled until the service answers after a deploy. ``None`` means
    #: ``/`` for a UI service and ``/health`` for a headless one — a bare API
    #: registers no root route, so polling ``/`` would spin to timeout and
    #: report a healthy service as failed.
    ready_path: str | None = None
    env_rules: tuple[EnvRule, ...] = ()
    #: Page whose assets are verified, relative to the mount; default the root.
    asset_page: str | None = None
    #: Package language on the file manager: ``python`` or ``nodejs``.
    language: str = "python"
    #: Release number source; ``None`` reads ``[project].version`` from pyproject.toml.
    version_source: VersionSource | None = None
    #: Live version check. After ``release`` the reported version MUST equal the
    #: release; without one, a 200 cannot tell the new build from the old one.
    version_probe: VersionProbe | None = None

    @property
    def effective_ready_path(self) -> str:
        if self.ready_path:
            return self.ready_path
        return "/" if self.has_ui else "/health"


def _as_tuple(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise ConfigError(f"{key} must be a string or a list of strings, got {value!r}")


def _json_keys(raw: Any, index: int) -> str | tuple[str, ...] | None:
    if raw is None or isinstance(raw, str):
        return raw
    if isinstance(raw, list) and raw and all(isinstance(k, str) for k in raw):
        return tuple(raw)
    raise ConfigError(f"probes[{index}].expect-json-key must be a string or a non-empty list of strings")


def _parse_probes(raw: Any) -> tuple[Probe, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"probes must be a list of tables, got {type(raw).__name__}")
    probes = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            probes.append(Probe(path=item))
            continue
        if not isinstance(item, dict) or "path" not in item:
            raise ConfigError(f"probes[{i}] needs at least a 'path'")
        probes.append(
            Probe(
                path=str(item["path"]),
                label=str(item.get("label", "")),
                expect_json_key=_json_keys(item.get("expect-json-key"), i),
                min_items=item.get("min-items"),
                timeout=float(item.get("timeout", 60.0)),
                must_contain=item.get("must-contain"),
            )
        )
    return tuple(probes)


def _parse_env_rules(raw: Any) -> tuple[EnvRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("env-rules must be a list of tables")
    rules = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not item.get("key"):
            raise ConfigError(f"env-rules[{i}] needs a 'key'")
        rule = EnvRule(
            key=str(item["key"]),
            forbid=_as_tuple(item.get("forbid"), f"env-rules[{i}].forbid"),
            min_length=item.get("min-length"),
            reject_prefixes=_as_tuple(item.get("reject-prefixes"), f"env-rules[{i}].reject-prefixes"),
            unless=item.get("unless"),
            reason=str(item.get("reason", "")),
        )
        if not (rule.forbid or rule.min_length or rule.reject_prefixes):
            raise ConfigError(
                f"env-rules[{i}] ({rule.key}) checks nothing: set forbid, min-length or reject-prefixes"
            )
        rules.append(rule)
    return tuple(rules)


def _parse_version_source(raw: Any) -> VersionSource | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw.get("file"):
        raise ConfigError("version-source needs a 'file'")
    json_key, regex = raw.get("json-key"), raw.get("regex")
    if bool(json_key) == bool(regex):
        raise ConfigError("version-source needs exactly one of 'json-key' or 'regex'")
    return VersionSource(file=str(raw["file"]), json_key=json_key, regex=regex)


def _parse_version_probe(raw: Any) -> VersionProbe | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw.get("path"):
        raise ConfigError("version-probe needs a 'path'")
    return VersionProbe(path=str(raw["path"]), json_key=str(raw.get("json-key", "version")))


def from_mapping(raw: dict[str, Any]) -> AppConfig:
    """Build an :class:`AppConfig` from a parsed TOML table (kebab-case keys)."""
    for key in ("app-name", "instance"):
        if not raw.get(key):
            raise ConfigError(f"'{key}' is required")
    database = raw.get("database")
    return AppConfig(
        app_name=str(raw["app-name"]),
        instance=str(raw["instance"]),
        display_name=str(raw.get("display-name", "")),
        description=str(raw.get("description", "")),
        base_image=str(raw.get("base-image", DEFAULT_BASE_IMAGE)),
        has_ui=bool(raw.get("has-ui", True)),
        database=None if database is None else str(database),
        tarball=str(raw.get("tarball", "")),
        required_members=_as_tuple(raw.get("required-members", ["entrypoint"]), "required-members"),
        index_html=raw.get("index-html"),
        prefix_env_var=raw.get("prefix-env-var"),
        require_baked_env=bool(raw.get("require-baked-env", False)),
        required_env_keys=_as_tuple(raw.get("required-env-keys"), "required-env-keys"),
        probes=_parse_probes(raw.get("probes")),
        ready_path=raw.get("ready-path"),
        asset_page=raw.get("asset-page"),
        env_rules=_parse_env_rules(raw.get("env-rules")),
        language=str(raw.get("language", "python")),
        version_source=_parse_version_source(raw.get("version-source")),
        version_probe=_parse_version_probe(raw.get("version-probe")),
    )


def load(repo_root: Path) -> AppConfig:
    """Load configuration for the repository at ``repo_root``."""
    standalone = repo_root / STANDALONE_FILE
    if standalone.is_file():
        return from_mapping(tomllib.loads(standalone.read_text(encoding="utf-8")))

    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        table = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get(PYPROJECT_TABLE)
        if table:
            return from_mapping(table)

    raise ConfigError(
        f"no BYOC configuration: add {STANDALONE_FILE} or a [tool.{PYPROJECT_TABLE}] "
        f"table to pyproject.toml in {repo_root}"
    )
