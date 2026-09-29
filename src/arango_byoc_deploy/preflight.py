"""Refuse to upload a bundle the platform will reject, or that cannot serve.

Each check here exists because a deploy somewhere in the estate failed without
it. The copies had diverged: some checked the baked mount prefix, some did not;
only one refused a bundle carrying an API key. This is the union.

``check`` returns problems rather than raising on the first, so one pre-flight
run reports everything wrong with a bundle instead of costing a rebuild per
defect.
"""

from __future__ import annotations

import re
import tarfile
from pathlib import Path

from .config import AppConfig
from .env import ENDPOINT_KEYS, LOOPBACK_PATTERN, SECRET_KEY_PATTERN, first
from .platform import DeployError, mount_path

#: A root-absolute asset URL in the SPA shell. Under a mount prefix these resolve
#: against the cluster root instead of the service, so the page loads blank
#: behind a green health check.
_ROOT_ABSOLUTE_REF = re.compile(r'(?:href|src)="(/(?!/)[^"]*)"')


def member_name(name: str) -> str:
    """Normalise a tar member (``./entrypoint`` -> ``entrypoint``)."""
    return name[2:] if name.startswith("./") else name


def _parse_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


_TRUTHY = {"true", "1", "yes"}


def baked_secret_keys(env_text: str) -> list[str]:
    """Names (never values) of every non-empty ``*_API_KEY`` in a baked .env."""
    return sorted(set(SECRET_KEY_PATTERN.findall(env_text)))


def secret_warnings(tarball: Path, config: AppConfig) -> list[str]:
    """One warning per allowed secret actually baked in *tarball*."""
    if not config.allow_baked_secrets or not tarball.is_file():
        return []
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            names = {member_name(n): n for n in archive.getnames()}
            raw = names.get(".env")
            handle = archive.extractfile(raw) if raw else None
            env_text = handle.read().decode("utf-8", "replace") if handle else ""
    except (tarfile.TarError, OSError):
        return []
    return [
        f"WARNING: {key} is baked into {tarball.name} (allowed by allow-baked-secrets) — "
        "the bundle is a credential: do not commit, attach or copy it"
        for key in baked_secret_keys(env_text)
        if key in config.allow_baked_secrets
    ]


def _env_rule_problems(env: dict[str, str], config: AppConfig) -> list[str]:
    """Problems from the app's own ``env-rules`` (values are never echoed)."""
    problems: list[str] = []
    for rule in config.env_rules:
        if rule.unless and env.get(rule.unless, "").strip().lower() in _TRUTHY:
            continue
        value = env.get(rule.key, "")
        why = f" — {rule.reason}" if rule.reason else ""
        if rule.equals is not None and value.strip() != rule.equals:
            problems.append(f"{rule.key} in the baked .env must be {rule.equals!r}{why}")
        if rule.forbid and value.strip().lower() in {v.lower() for v in rule.forbid}:
            problems.append(f"{rule.key}={value.strip()} in the baked .env is not allowed{why}")
        if rule.min_length is not None and len(value) < rule.min_length:
            problems.append(
                f"{rule.key} in the baked .env is missing or under {rule.min_length} characters{why}"
            )
        if rule.reject_prefixes and value.lower().startswith(tuple(p.lower() for p in rule.reject_prefixes)):
            problems.append(f"{rule.key} in the baked .env is a placeholder{why}")
    return problems


def check(tarball: Path, config: AppConfig, db_name: str | None) -> list[str]:
    """Every problem with ``tarball``; an empty list means it may be uploaded."""
    if not tarball.is_file():
        return [f"no tarball at {tarball}"]

    problems: list[str] = []
    try:
        archive = tarfile.open(tarball, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        return [f"{tarball} is not a readable gzip tarball: {exc}"]

    with archive:
        names = {member_name(n): n for n in archive.getnames()}

        def read(member: str) -> str:
            raw = names.get(member)
            if raw is None:
                return ""
            handle = archive.extractfile(raw)
            return handle.read().decode("utf-8", "replace") if handle else ""

        # Layout. A nested archive (myservice/entrypoint) fails on the platform
        # with a bare "No entrypoint found", so the root is checked explicitly.
        for required in config.required_members:
            if required.endswith("/"):
                # A directory: present when anything lives under it
                # (worldview must ship node_modules/ — boot cannot reach npm).
                if not any(n.startswith(required) for n in names):
                    problems.append(f"{required} missing (or empty) at the archive root")
            elif required not in names:
                problems.append(f"{required} missing from the archive root")

        # Entry-script detection: the platform runs
        # `python /project/<first whitespace-separated word of the file>`. A
        # shebang, docstring or comment on line 1 breaks it.
        entry = read("entrypoint")
        if "entrypoint" in names:
            first_line = entry.splitlines()[0] if entry else ""
            token = first_line.split()[0] if first_line.split() else ""
            if token != "entrypoint":
                problems.append(
                    f"entrypoint line 1 must begin with the token 'entrypoint' "
                    f"(found {token!r}); the platform would run python /project/{token}"
                )

        # Baked environment. The deploy call cannot carry application env — the
        # platform drops it — so anything the service needs at boot must be here.
        env_text = read(".env")
        if not env_text:
            if config.require_baked_env:
                problems.append(".env is not baked, and the platform injects nothing into the container")
        else:
            env = _parse_env(env_text)
            for key in config.required_env_keys:
                if not env.get(key):
                    problems.append(f"{key} missing from the baked .env")
            endpoint = first(env, ENDPOINT_KEYS) or ""
            if LOOPBACK_PATTERN.search(endpoint):
                problems.append(
                    "the baked Arango endpoint points at loopback — unreachable from the platform"
                )
            unlisted = [k for k in baked_secret_keys(env_text) if k not in config.allow_baked_secrets]
            if unlisted:
                problems.append(
                    f"{', '.join(unlisted)} baked into .env — tarballs are uploaded and archived; "
                    "set API keys in the Container Manager, or list a key you must bake in "
                    "allow-baked-secrets"
                )
            problems.extend(_env_rule_problems(env, config))
            if config.prefix_env_var:
                baked = env.get(config.prefix_env_var, "").rstrip("/")
                expected = mount_path(config.instance, db_name)
                if baked != expected:
                    problems.append(
                        f"{config.prefix_env_var}={baked or '(unset)'!r} but the deploy will mount at "
                        f"{expected!r} — the app will emit URLs for the wrong prefix. Rebuild with "
                        f"the prefix matching --instance/--db"
                    )

        if config.prefix_env_var and not env_text:
            problems.append(
                f"{config.prefix_env_var} is required but no .env is baked — without the mount "
                f"prefix the app emits URLs relative to the cluster root"
            )

        # Root-absolute assets break under a mount prefix.
        if config.index_html:
            index = read(config.index_html)
            if not index:
                problems.append(f"{config.index_html} missing or empty")
            else:
                # A prefix-baked absolute URL (Next.js basePath) is fine; one
                # outside the mount resolves against the cluster root.
                prefix = mount_path(config.instance, db_name) + "/"
                outside = [r for r in _ROOT_ABSOLUTE_REF.findall(index) if not r.startswith(prefix)]
                if outside:
                    problems.append(
                        f"{config.index_html} references {outside[0]!r} (+{len(outside) - 1} more) "
                        f"outside the mount prefix {prefix!r}; they resolve against the cluster root "
                        f"and the page loads blank"
                    )

    return problems


def require(tarball: Path, config: AppConfig, db_name: str | None) -> None:
    """Raise with every problem at once, or return quietly."""
    problems = check(tarball, config, db_name)
    if problems:
        raise DeployError("pre-flight failed:\n  - " + "\n  - ".join(problems))
