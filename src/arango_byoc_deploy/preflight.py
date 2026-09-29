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
_ROOT_ABSOLUTE_ASSET = re.compile(r'(?:href|src)="/(?!/)[^"]*"')


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
            if required not in names:
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
            if SECRET_KEY_PATTERN.search(env_text):
                problems.append(
                    "an *_API_KEY is baked into .env — tarballs are uploaded and archived; "
                    "set API keys in the Container Manager instead"
                )
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
            elif _ROOT_ABSOLUTE_ASSET.search(index):
                problems.append(
                    f"{config.index_html} references root-absolute assets; they resolve against "
                    f"the cluster root under the mount prefix and the page loads blank"
                )

    return problems


def require(tarball: Path, config: AppConfig, db_name: str | None) -> None:
    """Raise with every problem at once, or return quietly."""
    problems = check(tarball, config, db_name)
    if problems:
        raise DeployError("pre-flight failed:\n  - " + "\n  - ".join(problems))
