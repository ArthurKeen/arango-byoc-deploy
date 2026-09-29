"""Credential resolution from a repository's ``.env``.

The estate has not agreed on spellings — some repos use ``ARANGO_ENDPOINT`` and
``ARANGO_USERNAME``, others ``ARANGO_URL`` and ``ARANGO_USER`` — so every
accepted spelling is tried rather than forcing a rename on six repositories.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ENDPOINT_KEYS = ("ARANGO_ENDPOINT", "ARANGO_URL")
USER_KEYS = ("ARANGO_USERNAME", "ARANGO_USER")
PASSWORD_KEYS = ("ARANGO_PASSWORD", "ARANGO_PASS")
DATABASE_KEYS = ("ARANGO_DB", "ARANGO_DATABASE")

#: Matches a baked API key line. A bundle carrying one is refused: tarballs are
#: uploaded, archived and shared, and an LLM key has no business in one.
SECRET_KEY_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Z0-9_]*API_KEY)\s*=\s*\S", re.MULTILINE)

LOOPBACK_PATTERN = re.compile(r"localhost|127\.0\.0\.1|\[::1\]", re.IGNORECASE)


class CredentialError(ValueError):
    """Required platform credentials are absent."""


def load_env(path: Path) -> dict[str, str]:
    """Parse a dotenv file into a plain dict.

    Surrounding quotes are stripped: the platform and ``docker --env-file``
    preserve them where python-dotenv does not, and a quoted password surfaces
    as a 401 that points nowhere near its cause.
    """
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def first(env: dict[str, str], keys: tuple[str, ...]) -> str | None:
    """The first non-empty value among ``keys``."""
    for key in keys:
        value = env.get(key)
        if value:
            return value
    return None


@dataclass(frozen=True)
class Credentials:
    endpoint: str
    user: str
    password: str
    database: str | None


def resolve(env: dict[str, str], *, endpoint_override: str | None = None) -> Credentials:
    """Platform credentials from ``env``, accepting every estate spelling."""
    endpoint = endpoint_override or first(env, ENDPOINT_KEYS)
    user = first(env, USER_KEYS)
    password = first(env, PASSWORD_KEYS)
    missing = [
        label
        for label, value in (
            ("/".join(ENDPOINT_KEYS), endpoint),
            ("/".join(USER_KEYS), user),
            ("/".join(PASSWORD_KEYS), password),
        )
        if not value
    ]
    if missing:
        raise CredentialError(f"missing in .env: {', '.join(missing)}")
    return Credentials(
        endpoint=str(endpoint).rstrip("/"),
        user=str(user),
        password=str(password),
        database=first(env, DATABASE_KEYS),
    )
